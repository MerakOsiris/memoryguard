"""Durable GUI/background task coordination on top of RuntimeStore.

RuntimeStore is the source of truth for task state.  This coordinator owns only
process-local execution resources (threads/cancel tokens/cleanup callbacks), so
GUI reloads and process restarts recover status from SQLite instead of an
in-memory job dictionary.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import threading
import time
from typing import Any, Callable, Mapping

from ..governance_lock import GovernanceLockError, WorkspaceGovernanceLock
from .working_memory import MutationContext, RuntimeScope, RuntimeStore, RuntimeV2Error, TaskRun


class TaskCancelled(RuntimeError):
    """Worker observed its cancellation token and stopped cooperatively."""


class TaskCoordinatorError(RuntimeError):
    """Task scheduling, cancellation, or durable state transition failed."""

    def __init__(self, code: str) -> None:
        self.code = str(code or "task_coordinator_failed")
        super().__init__(self.code)


def _stable_run_id(operation: str, idempotency_key: str, scope: RuntimeScope) -> str:
    raw = json.dumps(
        {
            "operation": str(operation),
            "idempotency_key": str(idempotency_key),
            "scope": scope.as_tuple(),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "gui-task-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _request_fingerprint(request: Any) -> str:
    """Hash scheduling intent without retaining user/source payloads."""
    if request is None:
        return ""
    try:
        raw = json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    except (TypeError, ValueError) as exc:
        raise TaskCoordinatorError("task_request_invalid") from exc
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _safe_error(exc: BaseException) -> dict[str, Any]:
    # Never persist arbitrary exception text: it can contain source bodies,
    # credentials, absolute paths, or model output.  Error class/code are
    # enough for durable recovery; detailed diagnostics stay in bounded logs.
    code = getattr(exc, "code", None)
    return {
        "code": str(code or type(exc).__name__),
        "retryable": not isinstance(exc, (ValueError, PermissionError)),
    }


@dataclass
class TaskExecution:
    coordinator: "TaskCoordinator"
    run_id: str
    scope: RuntimeScope
    cancel_event: threading.Event
    _progress_seq: int = 0
    _cleanups: list[Callable[[], Any]] = field(default_factory=list)

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def check_cancelled(self) -> None:
        if self.cancelled:
            raise TaskCancelled(self.run_id)

    def progress(
        self,
        percent: int,
        stage: str,
        *,
        cancellable: bool = True,
        item_count: int | None = None,
    ) -> None:
        """Persist bounded real progress; never persist source/model bodies."""
        self.check_cancelled()
        value = max(0, min(int(percent), 100))
        self._progress_seq += 1
        state: dict[str, Any] = {
            "percent": value,
            "stage": str(stage or "running")[:128],
            "cancellable": bool(cancellable),
            "sequence": self._progress_seq,
        }
        if item_count is not None:
            state["item_count"] = max(0, int(item_count))
        self.coordinator._checkpoint(self.run_id, self.scope, state, sequence=self._progress_seq)

    def own_cleanup(self, callback: Callable[[], Any]) -> None:
        if not callable(callback):
            raise TypeError("cleanup callback must be callable")
        self._cleanups.append(callback)

    def close_owned(self) -> None:
        failures: list[str] = []
        for callback in reversed(self._cleanups):
            try:
                callback()
            except Exception as exc:  # resource cleanup must continue
                failures.append(type(exc).__name__)
        self._cleanups.clear()
        if failures:
            raise TaskCoordinatorError("owned resource cleanup failed: " + ",".join(sorted(set(failures))))


@dataclass
class _WorkerHandle:
    run_id: str
    thread: threading.Thread
    cancel_event: threading.Event
    done_event: threading.Event


class TaskCoordinator:
    """Process-local worker ownership with durable RuntimeStore state."""

    def __init__(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self._write_store: RuntimeStore | None = None
        self._lock = threading.RLock()
        self._scheduling_lock = WorkspaceGovernanceLock(self.workspace)
        self._workers: dict[str, _WorkerHandle] = {}
        self._closed = False

    @contextmanager
    def _scheduling_guard(self):
        """Serialize durable run creation and worker ownership across hosts."""
        try:
            with self._scheduling_lock:
                yield
        except GovernanceLockError as exc:
            raise TaskCoordinatorError("task_scheduling_lock_unavailable") from exc

    def _writer(self) -> RuntimeStore:
        with self._lock:
            if self._write_store is None:
                self._write_store = RuntimeStore(self.workspace, readonly=False)
            return self._write_store

    def _reader(self) -> RuntimeStore:
        return RuntimeStore(self.workspace, readonly=True)

    @staticmethod
    def scope_from_context(workspace: str | Path, context: Mapping[str, Any]) -> RuntimeScope:
        return RuntimeScope(
            workspace_id=str(Path(workspace).expanduser().resolve()),
            agent_instance_id=str(context.get("agent_instance_id") or ""),
            project_ref=str(context.get("project_ref") or ""),
            share_group_id=str(context.get("share_group_id") or ""),
            provider=str(context.get("provider") or ""),
            runtime_scope=str(context.get("runtime_scope") or context.get("runtime_role") or "gui"),
        )

    @staticmethod
    def _mutation(scope: RuntimeScope, key: str, *, actor: str = "gui") -> MutationContext:
        return MutationContext(scope=scope, idempotency_key=str(key), actor=str(actor or "gui"))

    def _transition(
        self,
        run_id: str,
        scope: RuntimeScope,
        state: str,
        *,
        key: str,
        result_ref: Mapping[str, Any] | None = None,
        error: Mapping[str, Any] | None = None,
    ) -> None:
        self._writer().transition(
            run_id,
            state,
            mutation=self._mutation(scope, key),
            result_ref=result_ref or {},
            error=error or {},
        )

    def _checkpoint(self, run_id: str, scope: RuntimeScope, state: Mapping[str, Any], *, sequence: int) -> None:
        self._writer().checkpoint(
            run_id,
            state,
            mutation=self._mutation(scope, f"{run_id}:progress:{sequence}"),
            checkpoint_key="progress",
        )

    def _record_owner(self, run_id: str, scope: RuntimeScope) -> None:
        """Persist process identity before worker launch for restart recovery."""
        from ..runtime_lease import _process_started_at_for_pid

        started = _process_started_at_for_pid(os.getpid())
        state = {
            "pid": os.getpid(),
            "process_started_at": started.isoformat() if started is not None else "",
        }
        self._writer().checkpoint(
            run_id,
            state,
            mutation=self._mutation(scope, f"{run_id}:owner:{os.getpid()}"),
            checkpoint_key="owner",
        )

    def _owner_state(self, run_id: str, scope: RuntimeScope) -> dict[str, Any]:
        checkpoint = self._reader().latest_checkpoint(
            run_id, scope, checkpoint_key="owner",
        )
        return dict(checkpoint.state) if checkpoint is not None else {}

    def _owner_is_live(self, run_id: str, scope: RuntimeScope) -> bool:
        from ..runtime_lease import _lease_is_live, _pid_alive

        owner = self._owner_state(run_id, scope)
        if not owner:
            return False
        # Platforms unable to expose process creation time fail closed while
        # the PID exists; Windows/Linux additionally reject PID reuse.
        if not owner.get("process_started_at"):
            return _pid_alive(owner.get("pid"))
        return _lease_is_live(owner)

    def start(
        self,
        *,
        operation: str,
        idempotency_key: str,
        scope: RuntimeScope,
        worker: Callable[[TaskExecution], Mapping[str, Any] | None],
        goal: str | None = None,
        importance: int = 0,
        request: Any = None,
    ) -> dict[str, Any]:
        if self._closed:
            raise TaskCoordinatorError("task coordinator is closed")
        if not callable(worker):
            raise TypeError("task worker must be callable")
        operation_text = str(operation or "").strip()
        key = str(idempotency_key or "").strip()
        if not operation_text or not key:
            raise TaskCoordinatorError("operation and idempotency_key are required")
        run_id = _stable_run_id(operation_text, key, scope)
        fingerprint = _request_fingerprint(request)
        with self._scheduling_guard(), self._lock:
            existing = self._reader().get_run(run_id, scope)
            if existing is not None:
                self._assert_request_replay(run_id, scope, fingerprint)
                return self.status(run_id, scope)
            self._schedule(
                operation=operation_text,
                key=key,
                scope=scope,
                worker=worker,
                goal=goal,
                importance=importance,
                request_fingerprint=fingerprint,
            )
        return self.status(run_id, scope)

    def start_scope_exclusive(
        self,
        *,
        operation: str,
        scope: RuntimeScope,
        worker: Callable[[TaskExecution], Mapping[str, Any] | None],
        goal: str | None = None,
        importance: int = 0,
        key: str | None = None,
        request: Any = None,
    ) -> dict[str, Any]:
        """Start one worker per (operation, scope).

        If a ``queued``/``running`` run of the same operation already exists in
        the exact trusted scope, focus it (``started=False``,
        ``code="operation_already_active"``) instead of creating duplicate work.
        The active-run check and the durable create are performed under one lock,
        so two racing starts cannot both win.
        """
        if self._closed:
            raise TaskCoordinatorError("task coordinator is closed")
        if not callable(worker):
            raise TypeError("task worker must be callable")
        operation_text = str(operation or "").strip()
        if not operation_text:
            raise TaskCoordinatorError("operation is required")
        if key is None:
            key = f"{operation_text}:{time.time_ns()}"
        key = str(key)
        run_id = _stable_run_id(operation_text, key, scope)
        fingerprint = _request_fingerprint(request)
        with self._scheduling_guard(), self._lock:
            if self._closed:
                raise TaskCoordinatorError("task coordinator closed during scheduling")
            existing = self._reader().get_run(run_id, scope)
            if existing is not None:
                self._assert_request_replay(run_id, scope, fingerprint)
                replay = self.status(run_id, scope)
                replay.update({
                    "started": False,
                    "reused": True,
                    "accepted": True,
                    "code": "idempotent_replay",
                    "job_id": run_id,
                })
                return replay
            active = self._reader().list_runs(
                scope,
                states=("queued", "running"),
                task_types=(operation_text,),
                limit=100,
            )
            for item in active:
                if item.run_id in self._workers or self._owner_is_live(item.run_id, scope):
                    focused = self.status(item.run_id, scope)
                    focused.update({
                        "started": False,
                        "focused": True,
                        "accepted": False,
                        "code": "operation_busy",
                        "job_id": item.run_id,
                    })
                    return focused
                # Process died after persisting queued/running state.  Close
                # that orphan before creating a replacement; never focus a
                # task that has no live owner.
                self._transition(
                    item.run_id,
                    scope,
                    "cancelled",
                    key=f"{item.run_id}:orphan-recovered",
                    error={"code": "task_owner_exited", "retryable": True},
                )
            self._schedule(
                operation=operation_text,
                key=key,
                scope=scope,
                worker=worker,
                goal=goal,
                importance=importance,
                request_fingerprint=fingerprint,
            )
        result = self.status(run_id, scope)
        result["started"] = True
        result["accepted"] = True
        return result

    def _assert_request_replay(
        self, run_id: str, scope: RuntimeScope, fingerprint: str,
    ) -> None:
        if not fingerprint:
            return
        persisted = self._reader().request_fingerprint(run_id, scope)
        if not persisted or persisted != fingerprint:
            raise TaskCoordinatorError("idempotency_key_reused")

    def active_runs(self, scope: RuntimeScope, *, operation: str | None = None, limit: int = 100) -> list[str]:
        """Run ids of ``queued``/``running`` runs in the exact trusted scope.

        Optionally filtered to a single operation (task_type).
        """
        if self._closed:
            return []
        runs = self._reader().list_runs(
            scope,
            states=("queued", "running"),
            task_types=(str(operation),) if operation else (),
            limit=limit,
        )
        return [run.run_id for run in runs]

    def _schedule(
        self,
        *,
        operation: str,
        key: str,
        scope: RuntimeScope,
        worker: Callable[[TaskExecution], Mapping[str, Any] | None],
        goal: str | None = None,
        importance: int = 0,
        request_fingerprint: str = "",
    ) -> None:
        """Create the durable run and start its worker thread.

        Caller must hold ``_lock`` (or be in a single-threaded context) so the
        active-run check and the create cannot race another start.
        """
        run_id = _stable_run_id(operation, key, scope)
        self._writer().create_run(
            run_id,
            task_type=operation,
            # Goal is body-filtered runtime metadata; do not copy a canonical
            # operation such as ``history_backfill`` into it because control
            # tokens are intentionally forbidden in free-form goal text.
            goal=str(goal or "background_task"),
            importance=int(importance),
            mutation=self._mutation(scope, f"{run_id}:create"),
            requested_by="gui",
            request_fingerprint=request_fingerprint,
        )
        self._record_owner(run_id, scope)
        cancel_event = threading.Event()
        done_event = threading.Event()

        def runner() -> None:
            execution = TaskExecution(self, run_id, scope, cancel_event)
            cleanup_error: BaseException | None = None
            try:
                if cancel_event.is_set():
                    self._transition(run_id, scope, "cancelled", key=f"{run_id}:cancel-before-start")
                    return
                self._transition(run_id, scope, "running", key=f"{run_id}:running")
                execution.progress(0, "running")
                result = worker(execution) or {}
                execution.check_cancelled()
                execution.progress(100, "complete", cancellable=False)
                self._transition(
                    run_id,
                    scope,
                    "succeeded",
                    key=f"{run_id}:succeeded",
                    result_ref=dict(result),
                )
            except TaskCancelled:
                current = self._reader().get_run(run_id, scope)
                if current is not None and current.status in {"queued", "running"}:
                    self._transition(run_id, scope, "cancelled", key=f"{run_id}:cancelled")
            except BaseException as exc:
                current = self._reader().get_run(run_id, scope)
                if current is not None and current.status in {"queued", "running"}:
                    self._transition(
                        run_id,
                        scope,
                        "failed",
                        key=f"{run_id}:failed:{type(exc).__name__}",
                        error=_safe_error(exc),
                    )
            finally:
                try:
                    execution.close_owned()
                except BaseException as exc:
                    cleanup_error = exc
                if cleanup_error is not None:
                    current = self._reader().get_run(run_id, scope)
                    if current is not None and current.status == "running":
                        self._transition(
                            run_id,
                            scope,
                            "failed",
                            key=f"{run_id}:cleanup-failed",
                            error=_safe_error(cleanup_error),
                        )
                done_event.set()
                with self._lock:
                    self._workers.pop(run_id, None)

        thread = threading.Thread(target=runner, name=f"MemoryGuardTask:{operation}:{run_id[-8:]}", daemon=False)
        self._workers[run_id] = _WorkerHandle(run_id, thread, cancel_event, done_event)
        thread.start()

    def status(self, run_id: str, scope: RuntimeScope) -> dict[str, Any]:
        run = self._reader().get_run(run_id, scope)
        if run is None:
            return {
                "ok": False,
                "status": "failed",
                "error": {"code": "task_not_found", "message": "Task was not found in the trusted scope", "details": {}},
            }
        reader = self._reader()
        checkpoint = reader.latest_checkpoint(run_id, scope, checkpoint_key="progress")
        result_ref = reader.run_result_ref(run_id, scope)
        progress = dict(checkpoint.state) if checkpoint is not None else {
            "percent": 0,
            "stage": "queued" if run.status == "queued" else run.status,
            "cancellable": run.status in {"queued", "running"},
            "sequence": 0,
        }
        with self._lock:
            owned = run_id in self._workers
        return {
            "ok": True,
            "status": run.status,
            "operation": run.task_type,
            "task": {
                "run_id": run.run_id,
                "state": run.status,
                "progress": int(progress.get("percent") or 0),
                "stage": str(progress.get("stage") or run.status),
                "cancellable": bool(progress.get("cancellable", run.status in {"queued", "running"})),
                "owned": owned,
                "created_at": run.created_at,
                "started_at": run.started_at,
                "finished_at": run.finished_at,
            },
            "result_ref": result_ref,
            "error": dict(run.error) if run.error else {},
        }

    def list_pending(self, scope: RuntimeScope, *, limit: int = 100) -> dict[str, Any]:
        runs = self._reader().list_runs(scope, states=("queued", "running"), limit=limit)
        return {
            "ok": True,
            "status": "succeeded",
            "operation": "task_list",
            "data": {"tasks": [self.status(run.run_id, scope)["task"] for run in runs]},
        }

    def cancel(self, run_id: str, scope: RuntimeScope, *, timeout: float = 5.0) -> dict[str, Any]:
        run = self._reader().get_run(run_id, scope)
        if run is None:
            return {
                "ok": False,
                "status": "failed",
                "error": {"code": "task_not_found", "message": "Task was not found in the trusted scope", "details": {}},
            }
        if run.status in {"succeeded", "failed", "cancelled"}:
            return self.status(run_id, scope)
        with self._lock:
            handle = self._workers.get(run_id)
        if handle is None:
            if self._owner_is_live(run_id, scope):
                return {
                    "ok": False,
                    "status": "failed",
                    "operation": "task_cancel",
                    "task": self.status(run_id, scope).get("task", {}),
                    "error": {
                        "code": "task_owned_by_other_process",
                        "message": "Task is owned by another live process",
                        "details": {},
                    },
                }
            # Process restart terminated the old owner.  Cancellation now
            # safely makes the orphan durable run terminal.
            self._transition(run_id, scope, "cancelled", key=f"{run_id}:cancel-recovered")
            return self.status(run_id, scope)

        # Persist cancellation intent without claiming terminal success.
        self._checkpoint(
            run_id,
            scope,
            {"percent": self.status(run_id, scope)["task"]["progress"], "stage": "cancelling", "cancellable": False, "sequence": 999999},
            sequence=999999,
        )
        handle.cancel_event.set()
        handle.thread.join(max(0.0, float(timeout)))
        if handle.thread.is_alive():
            return {
                "ok": False,
                "status": "failed",
                "operation": "task_cancel",
                "task": self.status(run_id, scope).get("task", {}),
                "error": {
                    "code": "task_cancel_timeout",
                    "message": "Owned worker did not terminate within the cancellation timeout",
                    "details": {"timeout_seconds": float(timeout)},
                },
            }
        return self.status(run_id, scope)

    def shutdown(self, *, timeout: float = 5.0) -> dict[str, Any]:
        """Cancel and join every worker owned by this GUI process."""
        with self._lock:
            self._closed = True
            handles = tuple(self._workers.values())
        for handle in handles:
            handle.cancel_event.set()
        deadline = time.monotonic() + max(0.0, float(timeout))
        for handle in handles:
            remaining = max(0.0, deadline - time.monotonic())
            handle.thread.join(remaining)
        with self._lock:
            alive = sorted(run_id for run_id, handle in self._workers.items() if handle.thread.is_alive())
        return {
            "ok": not alive,
            "status": "succeeded" if not alive else "failed",
            "owned_workers": len(handles),
            "alive_workers": alive,
        }

    def owned_worker_count(self) -> int:
        with self._lock:
            return sum(1 for item in self._workers.values() if item.thread.is_alive())


__all__ = [
    "TaskCancelled", "TaskCoordinator", "TaskCoordinatorError", "TaskExecution",
]
