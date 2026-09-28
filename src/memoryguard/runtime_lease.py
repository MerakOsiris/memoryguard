"""Multi-MCP runtime lease + split-brain detection (Req10).

Several MCP processes may legitimately share one control workspace (multi-MCP
is a feature).  A *runtime lease* records which live process holds the
workspace and its database files.  When a live process already holds the same
database set but was built from a different ``memoryguard_version`` or
``code_fingerprint``, that is a runtime split-brain: the newly arriving
process **must fail closed** (never write) and the operator is told to restart
it.  We never kill other processes — this module only probes liveness and
refuses to grant a write lease.

Lease file layout (JSON array, one entry per live process)::

    workspace/.memoryguard/runtime_leases.json
      [ {"pid": 1234, "process_started_at": "...", "memoryguard_version": "0.5.2",
         "code_fingerprint": "<sha256>", "control_workspace": "C:/...",
         "database_paths": ["C:/.../.memoryguard/memory/memory.db", ...]}, ... ]

``check_runtime_lease`` is the single entry point: no conflicting live lease →
it upserts this process's lease and returns ``granted=True``; a conflicting
live lease (same DB set, different version *or* fingerprint) → returns
``granted=False, split_brain=True, restart_required=True`` and lists the
conflicting entries without touching those processes.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .governance_lock import WorkspaceGovernanceLock
from .storage.layout import WorkspaceV2Layout

LEASE_FILE_NAME = "runtime_leases.json"
_LEASE_VERSION = 1
# Fallback used only when the OS cannot expose the process creation time.
_PROCESS_STARTED_AT = datetime.now(timezone.utc).isoformat()
_PROCESS_START_TOLERANCE_SECONDS = 5.0


# ---------------------------------------------------------------------------
# identity helpers
# ---------------------------------------------------------------------------


def process_started_at() -> str:
    """ISO-8601 OS start time of the current process when available."""
    observed = _process_started_at_for_pid(os.getpid())
    return observed.isoformat() if observed is not None else _PROCESS_STARTED_AT


def memoryguard_version() -> str:
    """Best-effort installed version: importlib.metadata → package __version__.

    ``importlib.metadata.version("memoryguard")`` is the required source; the
    distribution is normally named ``agent-memguard`` while the import package
    is ``memoryguard``, so the package ``__version__`` is the reliable
    fallback and ``"unknown"`` is the final resort.
    """
    # The published distribution is ``agent-memguard``.  An old unrelated
    # ``memoryguard`` distribution can coexist in site-packages, so consulting
    # that name first reports a false runtime version and triggers bogus lease
    # split-brain diagnostics.
    for dist in ("agent-memguard",):
        try:
            import importlib.metadata
            return importlib.metadata.version(dist)
        except Exception:  # noqa: BLE001 - metadata lookup must never raise here
            continue
    try:
        from . import __version__
        return str(__version__ or "")
    except Exception:  # noqa: BLE001
        return "unknown"


_INSTALL_KINDS = frozenset({"editable", "local_source", "installed", "unknown"})
_INSTALL_REASONS = frozenset({
    "direct_url_editable",
    "direct_url_local_path",
    "source_tree_on_sys_path",
    "distribution_installed",
    "metadata_unavailable",
})


def _public_install_origin(
    *,
    install_kind: str,
    reason: str,
    editable: bool,
) -> dict[str, Any]:
    """Machine-readable install origin. Never includes paths or URLs."""
    kind = str(install_kind or "unknown")
    if kind not in _INSTALL_KINDS:
        kind = "unknown"
    marker = str(reason or "metadata_unavailable")
    if marker not in _INSTALL_REASONS:
        marker = "metadata_unavailable"
    drift = kind in {"editable", "local_source"}
    return {
        "install_kind": kind,
        "install_reason": marker,
        "editable": bool(editable),
        "source_drift_risk": drift,
    }


def _parse_direct_url(direct_url: str | Mapping[str, Any] | None) -> dict[str, Any] | None:
    if isinstance(direct_url, Mapping):
        return dict(direct_url)
    text = str(direct_url or "").strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except Exception:  # noqa: BLE001 - diagnostics never fail closed on metadata
        return None
    return payload if isinstance(payload, Mapping) else None


def _looks_like_source_checkout(package_file: str | Path | None) -> bool:
    if package_file is None or not str(package_file).strip():
        return False
    try:
        path = Path(package_file).expanduser().resolve()
    except OSError:
        return False
    current = path
    while True:
        if current.name == "src" and (current / "memoryguard").exists():
            return (current.parent / "pyproject.toml").is_file()
        if current.parent == current:
            return False
        current = current.parent


def _looks_like_copied_install(package_file: str | Path | None) -> bool:
    """True when the live import is a copied install, not the source tree."""
    if package_file is None or not str(package_file).strip():
        return False
    try:
        path = Path(package_file).expanduser().resolve()
    except OSError:
        return False
    return any(
        part.casefold() in {"site-packages", "dist-packages"}
        for part in path.parts
    )


def _live_direct_url_text(dist_name: str = "agent-memguard") -> str:
    try:
        import importlib.metadata
        text = importlib.metadata.distribution(dist_name).read_text("direct_url.json")
    except Exception:  # noqa: BLE001
        return ""
    return str(text or "")


def _live_package_file() -> Path | None:
    try:
        from . import __file__ as package_file
        return Path(package_file)
    except Exception:  # noqa: BLE001
        return None


def inspect_distribution_origin(
    workspace: str | Path | None = None,
    *,
    direct_url: str | Mapping[str, Any] | None = None,
    package_file: str | Path | None = None,
    dist_name: str = "agent-memguard",
) -> dict[str, Any]:
    """Read-only PEP 610 / import origin. Never mutates the user install.

    Mutability follows the live import path and editable flag, not a file://
    provenance URL. A non-editable copy in site-packages is installed.
    ``workspace`` is accepted so diagnostics providers can be called with the
    control root; origin inspection does not read or write that path.
    """
    del workspace
    injected = direct_url is not None or package_file is not None
    payload = _parse_direct_url(direct_url)
    if not injected:
        payload = _parse_direct_url(_live_direct_url_text(dist_name))
        package_file = _live_package_file()
    dir_info = payload.get("dir_info") if isinstance(payload, Mapping) else None
    archive_info = payload.get("archive_info") if isinstance(payload, Mapping) else None
    url = str((payload or {}).get("url") or "") if isinstance(payload, Mapping) else ""
    editable = False
    if isinstance(dir_info, Mapping):
        editable = dir_info.get("editable") is True
    if editable:
        return _public_install_origin(
            install_kind="editable",
            reason="direct_url_editable",
            editable=True,
        )
    if isinstance(archive_info, Mapping) or url.startswith(("https://", "http://")):
        return _public_install_origin(
            install_kind="installed",
            reason="distribution_installed",
            editable=False,
        )
    # PEP 610 file:// names the source directory pip copied from. A live
    # import under site-packages/dist-packages is an immutable copy.
    if _looks_like_copied_install(package_file):
        return _public_install_origin(
            install_kind="installed",
            reason="distribution_installed",
            editable=False,
        )
    if url.startswith("file:") and isinstance(dir_info, Mapping):
        return _public_install_origin(
            install_kind="local_source",
            reason="direct_url_local_path",
            editable=False,
        )
    if _looks_like_source_checkout(package_file):
        return _public_install_origin(
            install_kind="local_source",
            reason="source_tree_on_sys_path",
            editable=False,
        )
    if payload is None and injected and package_file is None:
        return _public_install_origin(
            install_kind="unknown",
            reason="metadata_unavailable",
            editable=False,
        )
    return _public_install_origin(
        install_kind="installed",
        reason="distribution_installed",
        editable=False,
    )


_FINGERPRINT_CACHE: dict[tuple[Any, ...], str] = {}


def compute_code_fingerprint(package_dir: str | Path | None = None) -> str:
    """Stable sha256 over the package source ``.py`` files (read-only).

    The full package tree is hashed once per change; a stat signature
    (path + size + mtime) is cached so repeated calls only re-hash when a
    source file actually changed.  Nothing is written anywhere.
    """
    package_dir = Path(package_dir or Path(__file__).resolve().parent).resolve()
    try:
        files = sorted(
            p for p in package_dir.rglob("*.py")
            if p.is_file() and "__pycache__" not in p.parts
        )
    except OSError:
        files = []
    signature: list[tuple[Any, ...]] = []
    for p in files:
        try:
            st = p.stat()
        except OSError:
            continue
        signature.append((str(p.relative_to(package_dir)), st.st_size, st.st_mtime_ns))
    sig_tuple = tuple(signature)
    cached = _FINGERPRINT_CACHE.get((package_dir, sig_tuple))
    if cached is not None:
        return cached
    digest = hashlib.sha256()
    for p in files:
        try:
            rel = str(p.relative_to(package_dir)).replace("\\", "/")
            data = p.read_bytes()
        except OSError:
            continue
        digest.update(rel.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(data)
    fingerprint = digest.hexdigest()
    _FINGERPRINT_CACHE[(package_dir, sig_tuple)] = fingerprint
    return fingerprint


def default_database_paths(control_workspace: str | Path) -> list[str]:
    """Return every formal V2 database path controlled by a workspace.

    The layout is path-only and does not create directories or databases.
    Keeping the lease set aligned with ``WorkspaceV2Layout`` prevents a
    V2 process from silently leasing only the retired V1 database paths.
    """
    workspace = Path(control_workspace).resolve()
    return sorted(str(path.resolve()) for path in WorkspaceV2Layout(workspace).all_db_paths)


# ---------------------------------------------------------------------------
# liveness + path helpers
# ---------------------------------------------------------------------------


def _win_process_ids() -> set[int] | None:
    """Return the Windows process table, or ``None`` when enumeration fails."""
    import ctypes
    from ctypes import wintypes

    try:
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        psapi.EnumProcesses.argtypes = [
            ctypes.POINTER(wintypes.DWORD), wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        psapi.EnumProcesses.restype = wintypes.BOOL
        capacity = 1024
        while capacity <= 1 << 20:
            process_ids = (wintypes.DWORD * capacity)()
            returned = wintypes.DWORD()
            if not psapi.EnumProcesses(
                process_ids, ctypes.sizeof(process_ids), ctypes.byref(returned),
            ):
                return None
            count = returned.value // ctypes.sizeof(wintypes.DWORD)
            if count < capacity:
                return {int(process_ids[index]) for index in range(count)}
            capacity *= 2
    except (AttributeError, OSError, TypeError, ValueError):
        return None
    return None


def _win_pid_alive(pid: int) -> bool:
    """Read-only Windows process existence probe.

    Python's ``os.kill(pid, 0)`` is *not* a signal-0 probe on Windows; any
    non-ctrl signal calls ``TerminateProcess`` and would kill the peer we are
    checking.  OpenProcess + GetExitCodeProcess only reads process state.
    Process-table enumeration is authoritative for absence.  OpenProcess is
    only a fallback when enumeration itself is unavailable; this avoids
    treating an access-denied response for a vanished PID as a live process.
    """
    process_ids = _win_process_ids()
    if process_ids is not None:
        return pid in process_ids

    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [
        wintypes.DWORD, wintypes.BOOL, wintypes.DWORD,
    ]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION, False, pid,
    )
    if not handle:
        error = ctypes.get_last_error()
        # ERROR_INVALID_PARAMETER means no such pid; access denied is unknown.
        return error not in (0, 87)
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return True  # fail closed: do not prune a peer we cannot inspect
        return exit_code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _win_process_started_at(pid: int) -> datetime | None:
    """Return a live Windows process creation time as UTC."""
    if not _win_pid_alive(pid):
        return None

    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    WINDOWS_EPOCH_TICKS = 116_444_736_000_000_000
    TICKS_PER_SECOND = 10_000_000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [
        wintypes.DWORD, wintypes.BOOL, wintypes.DWORD,
    ]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
    ]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION, False, pid,
    )
    if not handle:
        return None
    try:
        created = wintypes.FILETIME()
        exited = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(created), ctypes.byref(exited),
            ctypes.byref(kernel), ctypes.byref(user),
        ):
            return None
        ticks = (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)
        if ticks <= WINDOWS_EPOCH_TICKS:
            return None
        timestamp = (ticks - WINDOWS_EPOCH_TICKS) / TICKS_PER_SECOND
        return datetime.fromtimestamp(timestamp, tz=timezone.utc)
    finally:
        kernel32.CloseHandle(handle)


def _linux_process_started_at(pid: int) -> datetime | None:
    """Return a Linux process start time from procfs as UTC."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        _, separator, tail = stat.rpartition(")")
        if not separator:
            return None
        fields = tail.strip().split()
        start_ticks = int(fields[19])  # field 22; tail starts at field 3
        ticks_per_second = int(os.sysconf("SC_CLK_TCK"))
        boot_time = None
        for line in Path("/proc/stat").read_text(encoding="utf-8").splitlines():
            if line.startswith("btime "):
                boot_time = int(line.split()[1])
                break
        if boot_time is None or ticks_per_second <= 0:
            return None
        return datetime.fromtimestamp(
            boot_time + (start_ticks / ticks_per_second), tz=timezone.utc,
        )
    except (IndexError, OSError, TypeError, ValueError):
        return None


def _process_started_at_for_pid(pid: Any) -> datetime | None:
    """Best-effort OS creation time for one currently live process."""
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return None
    if pid_int <= 0:
        return None
    if os.name == "nt":
        return _win_process_started_at(pid_int)
    if sys.platform.startswith("linux"):
        return _linux_process_started_at(pid_int)
    return None


def _parse_process_started_at(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _lease_is_live(lease: dict[str, Any]) -> bool:
    """True only when the PID still names the recorded process instance."""
    pid = lease.get("pid")
    if not _pid_alive(pid):
        return False
    recorded_start = _parse_process_started_at(lease.get("process_started_at"))
    if recorded_start is None:
        return False
    observed_start = _process_started_at_for_pid(pid)
    if observed_start is None:
        # The PID is present but this platform cannot expose its creation time.
        # Preserve fail-closed behavior rather than pruning a possibly live peer.
        return True
    return abs((observed_start - recorded_start).total_seconds()) <= (
        _PROCESS_START_TOLERANCE_SECONDS
    )


def _pid_alive(pid: Any) -> bool:
    """True when ``pid`` names a live process.  Never kills on any platform."""
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return False
    if pid_int <= 0:
        return False
    if os.name == "nt":
        return _win_pid_alive(pid_int)
    try:
        os.kill(pid_int, 0)
    except PermissionError:
        return True  # exists, but this process may not signal it
    except Exception:  # noqa: BLE001 - ProcessLookupError etc. → dead
        return False
    return True


def _norm_path(path: Any) -> str:
    if not path:
        return ""
    normalized = os.path.realpath(
        os.path.abspath(os.path.expanduser(str(path))),
    )
    return os.path.normcase(os.path.normpath(normalized))


def _lease_identity(lease: dict[str, Any]) -> tuple[Any, ...]:
    return (str(lease.get("pid", "")), _norm_path(lease.get("control_workspace", "")))


def _db_conflict(
    lease: dict[str, Any],
    control_workspace: str | Path,
    our_paths: set[str],
) -> bool:
    """A lease holds the same DB set when it controls the same workspace or
    its recorded database files overlap ours."""
    if _norm_path(lease.get("control_workspace", "")) and _norm_path(
        lease.get("control_workspace", "")
    ) == _norm_path(str(control_workspace)):
        return True
    lease_paths = {_norm_path(p) for p in (lease.get("database_paths") or [])}
    return bool(our_paths and lease_paths and (our_paths & lease_paths))


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------


class RuntimeLeaseStore:
    """JSON-persisted runtime leases for one control workspace.

    Mutations run under the workspace governance lock so two MCP processes
    acquiring concurrently never lose each other's lease (read-modify-write is
    atomic across processes).
    """

    def __init__(
        self,
        control_workspace: str | Path,
        *,
        leases_path: str | Path | None = None,
    ) -> None:
        self.control_workspace = Path(control_workspace).resolve()
        self.leases_path = (
            Path(leases_path).resolve()
            if leases_path is not None
            else self.control_workspace / ".memoryguard" / LEASE_FILE_NAME
        )

    # ------------------------------------------------------------- io

    def load(self) -> list[dict[str, Any]]:
        """Read the lease array; a missing/corrupt file yields ``[]``.

        Accepts both the historical plain-array layout and the wrapped
        ``{"version": N, "leases": [...]}`` layout this store writes.
        """
        if not self.leases_path.exists():
            return []
        try:
            data = json.loads(self.leases_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        if isinstance(data, dict):
            data = data.get("leases", [])
        if not isinstance(data, list):
            return []
        return [entry for entry in data if isinstance(entry, dict)]

    def save(self, leases: list[dict[str, Any]]) -> None:
        """Atomic write: temp file + ``os.replace``, so a reader never sees a
        half-written lease file."""
        self.leases_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.leases_path.with_suffix(self.leases_path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(
                {"version": _LEASE_VERSION, "leases": leases},
                ensure_ascii=False, indent=2, sort_keys=True,
            ),
            encoding="utf-8",
        )
        os.replace(tmp, self.leases_path)

    def upsert(self, lease: dict[str, Any]) -> None:
        leases = self.load()
        identity = _lease_identity(lease)
        leases = [l for l in leases if _lease_identity(l) != identity]
        leases.append(lease)
        self.save(leases)

    def remove(self, pid: Any, *, control_workspace: str | Path | None = None) -> int:
        leases = self.load()
        ws = _norm_path(control_workspace) if control_workspace is not None else ""
        kept: list[dict[str, Any]] = []
        removed = 0
        for lease in leases:
            if str(lease.get("pid", "")) == str(pid) and (
                not ws or _norm_path(lease.get("control_workspace", "")) == ws
            ):
                removed += 1
                continue
            kept.append(lease)
        if removed:
            self.save(kept)
        return removed

    def prune_stale(self) -> int:
        """Drop leases whose recorded process instance is no longer alive."""
        leases = self.load()
        live = [lease for lease in leases if _lease_is_live(lease)]
        pruned = len(leases) - len(live)
        if pruned:
            self.save(live)
        return pruned

    def list_live(self) -> list[dict[str, Any]]:
        return [lease for lease in self.load() if _lease_is_live(lease)]


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------


def check_runtime_lease(
    control_workspace: str | Path,
    database_paths: list[str] | None = None,
    *,
    version: str | None = None,
    code_fingerprint: str | None = None,
    pid: int | None = None,
    process_started_at: str | None = None,
    leases_path: str | Path | None = None,
) -> dict[str, Any]:
    """Check the workspace's runtime leases and (if safe) take one for the
    current process.

    Returns::

        {"granted": bool, "split_brain": bool, "restart_required": bool,
         "conflicting": [lease, ...], "pruned": int, "leases_path": str}

    * no conflicting live lease            → upsert this process's lease,
                                             ``granted=True``
    * live lease, same version+fingerprint → reuse, ``granted=True``
    * live lease, different version OR fingerprint on the same DB set
        → ``granted=False, split_brain=True, restart_required=True``,
        conflicting lists the offending leases.  No process is ever killed.
    """
    workspace = Path(control_workspace).resolve()
    store = RuntimeLeaseStore(workspace, leases_path=leases_path)
    our_paths = {_norm_path(p) for p in (database_paths or default_database_paths(workspace))}
    ver = str(version or "") or memoryguard_version()
    fp = str(code_fingerprint or "") if code_fingerprint else compute_code_fingerprint()
    my_pid = int(pid or os.getpid())
    observed_start = _process_started_at_for_pid(my_pid)
    started = process_started_at or (
        observed_start.isoformat() if observed_start is not None else _PROCESS_STARTED_AT
    )

    with WorkspaceGovernanceLock(workspace):
        pruned = store.prune_stale()
        other = [
            lease for lease in store.load()
            if str(lease.get("pid", "")) != str(my_pid)
        ]
        conflicts = [
            lease for lease in other
            if _db_conflict(lease, workspace, our_paths)
            and (
                str(lease.get("memoryguard_version", "") or "") != ver
                or str(lease.get("code_fingerprint", "") or "") != fp
            )
        ]
        if conflicts:
            return {
                "granted": False,
                "split_brain": True,
                "restart_required": True,
                "conflicting": conflicts,
                "pruned": pruned,
                "leases_path": str(store.leases_path),
            }
        store.upsert({
            "pid": my_pid,
            "process_started_at": started,
            "memoryguard_version": ver,
            "code_fingerprint": fp,
            "control_workspace": str(workspace),
            "database_paths": sorted(our_paths),
        })
    return {
        "granted": True,
        "split_brain": False,
        "restart_required": False,
        "conflicting": [],
        "pruned": pruned,
        "leases_path": str(store.leases_path),
    }


def release_runtime_lease(
    control_workspace: str | Path,
    pid: int | None = None,
    *,
    leases_path: str | Path | None = None,
) -> bool:
    """Remove this process's lease entry.  Idempotent: ``False`` when nothing
    was removed (already released / never held)."""
    store = RuntimeLeaseStore(control_workspace, leases_path=leases_path)
    with WorkspaceGovernanceLock(store.control_workspace):
        return store.remove(pid or os.getpid(), control_workspace=store.control_workspace) > 0


def runtime_lease_status(
    control_workspace: str | Path,
    *,
    version: str | None = None,
    code_fingerprint: str | None = None,
    leases_path: str | Path | None = None,
) -> dict[str, Any]:
    """Read-only split-brain status for diagnostics.  Never writes a lease;
    stale entries are reported but not pruned (``stale`` count)."""
    workspace = Path(control_workspace).resolve()
    store = RuntimeLeaseStore(workspace, leases_path=leases_path)
    leases = store.load()
    lease_states = [(lease, _lease_is_live(lease)) for lease in leases]
    stale = [lease for lease, is_live in lease_states if not is_live]
    live = [lease for lease, is_live in lease_states if is_live]
    ver = str(version or "") or memoryguard_version()
    fp = str(code_fingerprint or "") if code_fingerprint else compute_code_fingerprint()
    our_paths = {_norm_path(p) for p in default_database_paths(workspace)}
    conflicts = [
        l for l in live
        if _db_conflict(l, workspace, our_paths)
        and (
            str(l.get("memoryguard_version", "") or "") != ver
            or str(l.get("code_fingerprint", "") or "") != fp
        )
    ]
    return {
        "split_brain": bool(conflicts),
        "restart_required": bool(conflicts),
        "conflicting": conflicts,
        "stale": stale,
        "live": live,
        "leases_path": str(store.leases_path),
    }


def describe_split_brain(
    status: dict[str, Any],
    *,
    control_workspace: str | Path | None = None,
) -> dict[str, Any]:
    """Render a lease result into operator-facing text.

    Accepts the output of :func:`runtime_lease_status` or
    :func:`check_runtime_lease`.  Every surface that has to explain a
    split brain (hook denial, doctor, upgrade epilogue, reap) formats it
    through here so the wording and the suggested command stay identical.
    """
    conflicts = list(status.get("conflicting") or [])
    if not conflicts:
        return {
            "split_brain": False,
            "pids": [],
            "summary": "",
            "detail_lines": [],
            "remedy": "",
        }

    pids: list[int] = []
    detail_lines: list[str] = []
    for lease in conflicts:
        try:
            pid = int(lease.get("pid"))
        except (TypeError, ValueError):
            continue
        pids.append(pid)
        detail_lines.append(
            "  pid={pid} version={ver} started={started}".format(
                pid=pid,
                ver=str(lease.get("memoryguard_version", "") or "?"),
                started=str(lease.get("process_started_at", "") or "?"),
            )
        )

    ws = str(Path(control_workspace).resolve()) if control_workspace else ""
    remedy = "memoryguard runtime reap" + (f' -w "{ws}"' if ws else "")
    summary = (
        "runtime_split_brain: {n} 个仍在运行的进程以不同构建持有同一个 workspace，"
        "为保护数据一致性已拒绝写入。冲突 PID: {pids}".format(
            n=len(pids), pids=", ".join(str(p) for p in pids) or "?"
        )
    )
    return {
        "split_brain": True,
        "pids": pids,
        "summary": summary,
        "detail_lines": detail_lines,
        "remedy": remedy,
    }


def split_brain_hint(
    control_workspace: str | Path,
    *,
    leases_path: str | Path | None = None,
) -> dict[str, Any]:
    """Best-effort read-only split-brain probe.

    Never raises and never writes: callers use it on failure paths where an
    extra exception would replace a useful message with silence.
    """
    try:
        status = runtime_lease_status(control_workspace, leases_path=leases_path)
    except Exception:
        return {"split_brain": False, "pids": [], "summary": "", "detail_lines": [], "remedy": ""}
    return describe_split_brain(status, control_workspace=control_workspace)


if __name__ == "__main__":  # pragma: no cover - manual smoke
    result = check_runtime_lease(sys.argv[1] if len(sys.argv) > 1 else ".")
    print(json.dumps(result, ensure_ascii=False, indent=2))
