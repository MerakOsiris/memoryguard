from __future__ import annotations

import threading
import time
import os
import pytest

from memoryguard.runtime_v2.task_coordinator import TaskCoordinator, TaskCoordinatorError
from memoryguard.runtime_v2.working_memory import RuntimeScope


def _scope(tmp_path):
    return RuntimeScope(
        workspace_id=str(tmp_path.resolve()),
        agent_instance_id="agent-a",
        share_group_id="group-a",
        runtime_scope="gui",
    )


def _wait_terminal(coordinator: TaskCoordinator, run_id: str, scope: RuntimeScope, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = coordinator.status(run_id, scope)
        if status.get("status") in {"succeeded", "failed", "cancelled"}:
            return status
        time.sleep(0.01)
    raise AssertionError("task did not become terminal")


def test_task_coordinator_persists_real_progress_and_success(tmp_path):
    coordinator = TaskCoordinator(tmp_path)
    scope = _scope(tmp_path)

    def worker(execution):
        execution.progress(20, "scan", item_count=2)
        execution.progress(80, "project", item_count=4)
        return {"result_id": "projection-1"}

    accepted = coordinator.start(
        operation="projection_build",
        idempotency_key="req-1",
        scope=scope,
        worker=worker,
    )
    run_id = accepted["task"]["run_id"]
    final = _wait_terminal(coordinator, run_id, scope)
    assert final["status"] == "succeeded"
    assert final["task"]["progress"] == 100
    assert final["task"]["owned"] is False
    assert coordinator.owned_worker_count() == 0

    restarted = TaskCoordinator(tmp_path)
    recovered = restarted.status(run_id, scope)
    assert recovered["status"] == "succeeded"
    assert recovered["task"]["progress"] == 100
    assert recovered["task"]["owned"] is False


def test_task_coordinator_cancel_waits_for_owned_worker_exit(tmp_path):
    coordinator = TaskCoordinator(tmp_path)
    scope = _scope(tmp_path)
    started = threading.Event()

    def worker(execution):
        started.set()
        for index in range(1000):
            execution.check_cancelled()
            if index % 10 == 0:
                execution.progress(min(index // 10, 90), "scan")
            time.sleep(0.002)
        return {"result_id": "unexpected"}

    accepted = coordinator.start(
        operation="knowledge_reingest",
        idempotency_key="req-cancel",
        scope=scope,
        worker=worker,
    )
    run_id = accepted["task"]["run_id"]
    assert started.wait(1.0)
    cancelled = coordinator.cancel(run_id, scope, timeout=5.0)
    assert cancelled["ok"] is True
    assert cancelled["status"] == "cancelled"
    assert coordinator.owned_worker_count() == 0


def test_task_coordinator_shutdown_leaves_no_owned_worker(tmp_path):
    coordinator = TaskCoordinator(tmp_path)
    scope = _scope(tmp_path)
    started = threading.Event()

    def worker(execution):
        started.set()
        while True:
            execution.check_cancelled()
            time.sleep(0.002)

    accepted = coordinator.start(
        operation="history_backfill",
        idempotency_key="req-shutdown",
        scope=scope,
        worker=worker,
    )
    assert started.wait(1.0)
    result = coordinator.shutdown(timeout=5.0)
    assert result["ok"] is True
    assert result["alive_workers"] == []
    assert coordinator.owned_worker_count() == 0
    final = coordinator.status(accepted["task"]["run_id"], scope)
    assert final["status"] == "cancelled"


def test_task_coordinator_idempotent_start_reuses_durable_run(tmp_path):
    coordinator = TaskCoordinator(tmp_path)
    scope = _scope(tmp_path)
    calls = 0

    def worker(execution):
        nonlocal calls
        calls += 1
        return {"result_id": "one"}

    first = coordinator.start(
        operation="import_create",
        idempotency_key="same-request",
        scope=scope,
        worker=worker,
    )
    _wait_terminal(coordinator, first["task"]["run_id"], scope)
    second = coordinator.start(
        operation="import_create",
        idempotency_key="same-request",
        scope=scope,
        worker=worker,
    )
    assert second["task"]["run_id"] == first["task"]["run_id"]
    assert calls == 1


def test_scope_exclusive_start_returns_run_id_before_worker_completes(tmp_path):
    coordinator = TaskCoordinator(tmp_path)
    scope = _scope(tmp_path)
    started = threading.Event()

    def worker(execution):
        started.set()
        execution.check_cancelled()
        return {"result_id": "blocked"}

    accepted = coordinator.start_scope_exclusive(
        operation="projection_build",
        scope=scope,
        worker=worker,
    )
    assert accepted.get("started") is True
    run_id = accepted["task"]["run_id"]
    assert run_id
    # run id returned promptly, before the (blocked) worker can finish
    assert started.wait(1.0)
    final = _wait_terminal(coordinator, run_id, scope)
    assert final["status"] == "succeeded"


def test_scope_exclusive_focuses_existing_active_run_and_no_duplicate_worker(tmp_path):
    coordinator = TaskCoordinator(tmp_path)
    scope = _scope(tmp_path)
    calls = 0
    release = threading.Event()

    def worker(execution):
        nonlocal calls
        calls += 1
        while not release.is_set():
            execution.check_cancelled()
            time.sleep(0.01)
        return {"result_id": "one"}

    first = coordinator.start_scope_exclusive(
        operation="projection_build",
        scope=scope,
        worker=worker,
    )
    assert first.get("started") is True
    second = coordinator.start_scope_exclusive(
        operation="projection_build",
        scope=scope,
        worker=worker,
    )
    # 同一 (operation, scope) 的第二次启动聚焦已有任务，绝不创建第二个 worker
    assert second.get("started") is False
    assert second.get("focused") is True
    assert second.get("accepted") is False
    assert second.get("code") == "operation_busy"
    assert second["task"]["run_id"] == first["task"]["run_id"]
    release.set()
    _wait_terminal(coordinator, first["task"]["run_id"], scope)
    assert calls == 1


def test_scope_exclusive_replays_only_matching_request_and_rejects_key_reuse(tmp_path):
    coordinator = TaskCoordinator(tmp_path)
    scope = _scope(tmp_path)
    release = threading.Event()

    def worker(execution):
        while not release.is_set():
            execution.check_cancelled()
            time.sleep(0.01)
        return {"result_id": "source-a"}

    first = coordinator.start_scope_exclusive(
        operation="codegraph_build_bound",
        key="request-a",
        request={"source_id": "source-a"},
        scope=scope,
        worker=worker,
    )
    replay = coordinator.start_scope_exclusive(
        operation="codegraph_build_bound",
        key="request-a",
        request={"source_id": "source-a"},
        scope=scope,
        worker=worker,
    )
    assert replay["accepted"] is True
    assert replay["reused"] is True
    assert replay["code"] == "idempotent_replay"
    assert replay["job_id"] == first["task"]["run_id"]

    with pytest.raises(TaskCoordinatorError, match="idempotency_key_reused"):
        coordinator.start_scope_exclusive(
            operation="codegraph_build_bound",
            key="request-a",
            request={"source_id": "source-b"},
            scope=scope,
            worker=worker,
        )

    busy = coordinator.start_scope_exclusive(
        operation="codegraph_build_bound",
        key="request-b",
        request={"source_id": "source-b"},
        scope=scope,
        worker=worker,
    )
    assert busy["accepted"] is False
    assert busy["code"] == "operation_busy"
    assert busy["job_id"] == first["task"]["run_id"]
    release.set()
    assert _wait_terminal(coordinator, first["task"]["run_id"], scope)["status"] == "succeeded"


def test_two_coordinators_start_same_request_only_once(tmp_path):
    first = TaskCoordinator(tmp_path)
    second = TaskCoordinator(tmp_path)
    scope = _scope(tmp_path)
    gate = threading.Barrier(2)
    release = threading.Event()
    worker_started = threading.Event()
    calls = 0
    calls_lock = threading.Lock()
    results = []
    errors = []

    def worker(execution):
        nonlocal calls
        with calls_lock:
            calls += 1
        worker_started.set()
        while not release.is_set():
            execution.check_cancelled()
            time.sleep(0.01)
        return {"result_id": "one"}

    def start(coordinator):
        try:
            gate.wait(timeout=2.0)
            results.append(coordinator.start_scope_exclusive(
                operation="codegraph_build_bound",
                key="shared-request",
                request={"source_id": "source-a"},
                scope=scope,
                worker=worker,
            ))
        except BaseException as exc:  # pragma: no cover - assertion guard
            errors.append(exc)

    threads = [threading.Thread(target=start, args=(coordinator,)) for coordinator in (first, second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(3.0)
    assert not errors
    assert len(results) == 2
    assert {result["task"]["run_id"] for result in results} == {results[0]["task"]["run_id"]}
    assert sum(bool(result.get("started")) for result in results) == 1
    assert sum(bool(result.get("reused")) for result in results) == 1
    assert worker_started.wait(1.0)
    assert calls == 1
    release.set()
    assert _wait_terminal(first, results[0]["task"]["run_id"], scope)["status"] == "succeeded"


def test_two_coordinators_start_same_key_only_once(tmp_path):
    first = TaskCoordinator(tmp_path)
    second = TaskCoordinator(tmp_path)
    scope = _scope(tmp_path)
    gate = threading.Barrier(2)
    release = threading.Event()
    worker_started = threading.Event()
    calls = 0
    calls_lock = threading.Lock()
    results = []
    errors = []

    def worker(execution):
        nonlocal calls
        with calls_lock:
            calls += 1
        worker_started.set()
        while not release.is_set():
            execution.check_cancelled()
            time.sleep(0.01)
        return {"result_id": "one"}

    def start(coordinator):
        try:
            gate.wait(timeout=2.0)
            results.append(coordinator.start(
                operation="knowledge_reingest",
                idempotency_key="shared-request",
                request={"book_id": "book-a"},
                scope=scope,
                worker=worker,
            ))
        except BaseException as exc:  # pragma: no cover - assertion guard
            errors.append(exc)

    threads = [threading.Thread(target=start, args=(coordinator,)) for coordinator in (first, second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(3.0)
    assert not errors
    assert len(results) == 2
    assert {result["task"]["run_id"] for result in results} == {results[0]["task"]["run_id"]}
    assert worker_started.wait(1.0)
    assert calls == 1
    release.set()
    assert _wait_terminal(first, results[0]["task"]["run_id"], scope)["status"] == "succeeded"


def test_active_runs_filters_by_operation(tmp_path):
    coordinator = TaskCoordinator(tmp_path)
    scope = _scope(tmp_path)
    release = threading.Event()

    def blocker(execution):
        while not release.is_set():
            execution.check_cancelled()
            time.sleep(0.01)
        return {}

    build = coordinator.start_scope_exclusive(
        operation="projection_build", scope=scope, worker=blocker,
    )
    other = coordinator.start_scope_exclusive(
        operation="import_create", scope=scope, worker=blocker,
    )
    try:
        build_ids = coordinator.active_runs(scope, operation="projection_build")
        assert build_ids == [build["task"]["run_id"]]
        all_ids = set(coordinator.active_runs(scope))
        assert all_ids == {build["task"]["run_id"], other["task"]["run_id"]}
    finally:
        release.set()
        _wait_terminal(coordinator, build["task"]["run_id"], scope)
        _wait_terminal(coordinator, other["task"]["run_id"], scope)


def _seed_external_running(coordinator, scope, *, run_id, pid, started_at):
    store = coordinator._writer()
    store.create_run(
        run_id,
        task_type="projection_build",
        goal="background_task",
        importance=0,
        mutation=coordinator._mutation(scope, f"{run_id}:create"),
        requested_by="gui",
    )
    store.checkpoint(
        run_id,
        {"pid": pid, "process_started_at": started_at},
        mutation=coordinator._mutation(scope, f"{run_id}:owner:{pid}"),
        checkpoint_key="owner",
    )
    coordinator._transition(run_id, scope, "running", key=f"{run_id}:running")


def test_scope_exclusive_recovers_dead_process_run_before_restart(tmp_path):
    seed = TaskCoordinator(tmp_path)
    scope = _scope(tmp_path)
    stale_id = "stale-projection-build"
    _seed_external_running(
        seed, scope, run_id=stale_id, pid=2_000_000_000,
        started_at="2020-01-01T00:00:00+00:00",
    )
    calls = 0

    def worker(execution):
        nonlocal calls
        calls += 1
        return {"result_id": "replacement"}

    restarted = TaskCoordinator(tmp_path)
    accepted = restarted.start_scope_exclusive(
        operation="projection_build", scope=scope, worker=worker,
    )
    assert accepted["started"] is True
    assert accepted["task"]["run_id"] != stale_id
    assert restarted.status(stale_id, scope)["status"] == "cancelled"
    assert _wait_terminal(restarted, accepted["task"]["run_id"], scope)["status"] == "succeeded"
    assert calls == 1


def test_scope_exclusive_focuses_live_external_owner_and_cancel_fails_closed(tmp_path):
    from memoryguard.runtime_lease import _process_started_at_for_pid

    seed = TaskCoordinator(tmp_path)
    scope = _scope(tmp_path)
    run_id = "live-external-projection-build"
    started = _process_started_at_for_pid(os.getpid())
    _seed_external_running(
        seed, scope, run_id=run_id, pid=os.getpid(),
        started_at=started.isoformat() if started is not None else "",
    )
    restarted = TaskCoordinator(tmp_path)
    focused = restarted.start_scope_exclusive(
        operation="projection_build", scope=scope, worker=lambda execution: {},
    )
    assert focused["focused"] is True
    assert focused["task"]["run_id"] == run_id
    cancelled = restarted.cancel(run_id, scope)
    assert cancelled["ok"] is False
    assert cancelled["error"]["code"] == "task_owned_by_other_process"
