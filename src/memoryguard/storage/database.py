"""SQLite connection policy for V2 storage.

There is intentionally no schema bootstrap in :func:`connect_database`.
Opening a database is a transport concern; callers choose when to migrate and
read-only connections are therefore guaranteed not to create tables.
"""

from __future__ import annotations

import atexit
from contextlib import contextmanager
from pathlib import Path
import shutil
import sqlite3
import tempfile
import threading
from typing import Iterator
from urllib.parse import quote


class DatabaseError(RuntimeError):
    """A V2 database could not be opened or configured safely."""


def _readonly_uri(path: Path, *, immutable: bool = False) -> str:
    # Keep the drive colon and path separators unescaped.  ``quote`` handles
    # spaces and non-ASCII workspace names while remaining valid on Windows.
    query = "mode=ro&immutable=1" if immutable else "mode=ro"
    return "file:" + quote(str(path.resolve()), safe="/:\\") + "?" + query


def _copy_sqlite_snapshot(source: Path, target: Path) -> None:
    """Copy a SQLite database and its WAL to a private path.

    Older SQLite builds may checkpoint a WAL database when the final read-only
    connection closes.  A preflight must be physically side-effect free, so
    callers that need that guarantee inspect this private copy instead of the
    live database.  Copying the main file before the WAL lets SQLite reject a
    concurrently changing source as a stale/inconsistent snapshot.  The SHM
    file is a derived WAL index with live process locks, not durable data.
    CopyFile2 on Windows rejects it while another connection holds a lock.
    Let SQLite rebuild that index from the copied WAL in the private directory.
    """

    for suffix in ("", "-wal"):
        source_file = Path(str(source) + suffix)
        if source_file.is_file():
            shutil.copy2(source_file, Path(str(target) + suffix))


# Snapshot reuse within one process.  A hook invocation performs on the order of
# a hundred bounded reads; copying the same unchanged database for every one of
# them dominated hook latency.  The fingerprint covers the main file and both
# WAL companions, so any source write invalidates the cached copy.
_SNAPSHOT_LOCK = threading.Lock()
_SNAPSHOT_CACHE: dict[tuple, tuple[tempfile.TemporaryDirectory, Path]] = {}


def _snapshot_fingerprint(source: Path) -> tuple:
    parts: list[tuple[str, int, int]] = []
    for suffix in ("", "-wal", "-shm"):
        companion = Path(str(source) + suffix)
        try:
            stat = companion.stat()
        except OSError:
            continue
        parts.append((suffix, stat.st_size, stat.st_mtime_ns))
    return (str(source), tuple(parts))


def _discard_snapshot(holder: tempfile.TemporaryDirectory) -> None:
    try:
        holder.cleanup()
    except Exception:
        # A locked temporary file must never break the caller; the OS reclaims
        # the directory when the process exits.
        pass


def release_database_snapshots() -> None:
    """Drop every cached snapshot copy held by this process."""

    with _SNAPSHOT_LOCK:
        for holder, _ in _SNAPSHOT_CACHE.values():
            _discard_snapshot(holder)
        _SNAPSHOT_CACHE.clear()


atexit.register(release_database_snapshots)


def _acquire_snapshot(source: Path) -> Path:
    key = _snapshot_fingerprint(source)
    with _SNAPSHOT_LOCK:
        cached = _SNAPSHOT_CACHE.get(key)
        if cached is not None:
            return cached[1]
        # The source moved on; retire any copy still held for this path.
        for stale in [item for item in _SNAPSHOT_CACHE if item[0] == str(source)]:
            holder, _ = _SNAPSHOT_CACHE.pop(stale)
            _discard_snapshot(holder)
        holder = tempfile.TemporaryDirectory(prefix="memoryguard-db-read-")
        snapshot = Path(holder.name) / source.name
        try:
            _copy_sqlite_snapshot(source, snapshot)
        except Exception:
            _discard_snapshot(holder)
            raise
        _SNAPSHOT_CACHE[key] = (holder, snapshot)
        return snapshot


def connect_database(
    path: str | Path,
    *,
    readonly: bool = False,
    read_only: bool | None = None,
    timeout: float = 5.0,
    busy_timeout_ms: int = 5_000,
    immutable: bool = False,
) -> sqlite3.Connection:
    """Open and configure one SQLite connection.

    ``readonly`` and ``read_only`` are accepted as aliases so integrations can
    use either spelling.  Read-only mode uses SQLite's URI ``mode=ro`` and
    never creates parent directories, tables, or a WAL sidecar.
    """

    if read_only is not None:
        readonly = bool(read_only)
    if timeout < 0:
        raise ValueError("SQLite timeout must be >= 0")
    if busy_timeout_ms < 0:
        raise ValueError("busy timeout must be >= 0")
    db_path = Path(path).expanduser().resolve()
    if readonly:
        if not db_path.is_file():
            raise FileNotFoundError(db_path)
        connection = sqlite3.connect(
            _readonly_uri(db_path, immutable=immutable), uri=True, timeout=float(timeout)
        )
    else:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(db_path), timeout=float(timeout))
    connection.row_factory = sqlite3.Row
    try:
        # These PRAGMAs are connection-local except journal_mode.  In
        # particular, no write-capable PRAGMA is issued on a read-only handle.
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        if not readonly:
            # Phase 7 requires newly-created V2 databases to support bounded
            # incremental reclamation.  SQLite only accepts this setting
            # before the first user table is created.  Existing databases are
            # deliberately left untouched: changing their mode would require
            # a full VACUUM and belongs to the explicit maintenance workflow.
            has_user_table = connection.execute(
                "SELECT 1 FROM sqlite_schema "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' LIMIT 1"
            ).fetchone()
            user_version = connection.execute("PRAGMA user_version").fetchone()[0]
            if has_user_table is None and int(user_version) == 0:
                connection.execute("PRAGMA auto_vacuum=INCREMENTAL")
                if int(connection.execute("PRAGMA auto_vacuum").fetchone()[0]) != 2:
                    raise sqlite3.DatabaseError("failed to enable incremental auto_vacuum")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
    except Exception:
        connection.close()
        raise
    return connection


@contextmanager
def open_database(
    path: str | Path,
    *,
    readonly: bool = False,
    read_only: bool | None = None,
    timeout: float = 5.0,
    busy_timeout_ms: int = 5_000,
    immutable: bool = False,
) -> Iterator[sqlite3.Connection]:
    """Context manager wrapper around :func:`connect_database`."""

    conn = connect_database(
        path,
        readonly=readonly,
        read_only=read_only,
        timeout=timeout,
        busy_timeout_ms=busy_timeout_ms,
        immutable=immutable,
    )
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def open_database_snapshot(
    path: str | Path,
    *,
    timeout: float = 5.0,
    busy_timeout_ms: int = 5_000,
) -> Iterator[sqlite3.Connection]:
    """Read a private SQLite snapshot without mutating the source database.

    The snapshot includes the main file and its ``-wal`` companion, so
    uncheckpointed metadata remains visible; its SHM index is rebuilt locally.
    SQLite may checkpoint while
    closing the temporary connection, but that can only change the temporary
    directory.  The connection is write-capable only against the copied files,
    never against ``source``.

    The copy is reused for the lifetime of the process while ``source`` and its
    WAL companions keep the same size and mtime, so repeated reads share one
    warm copy instead of re-copying the database.  Because the copy is shared,
    a caller that writes to the snapshot exposes those writes to later readers
    in the same process; this API is for reading, and such writes are discarded
    either way.
    """

    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    snapshot = _acquire_snapshot(source)
    conn = connect_database(
        snapshot,
        timeout=timeout,
        busy_timeout_ms=busy_timeout_ms,
    )
    try:
        yield conn
    finally:
        conn.close()


def execute_sql_script(conn: sqlite3.Connection, script: str) -> None:
    """Execute complete SQL statements without ``executescript``.

    ``Connection.executescript`` performs an implicit commit before running a
    script, which would break migration rollback.  The standard-library
    ``complete_statement`` parser correctly handles trigger bodies and quoted
    semicolons, so each complete statement is sent through ``execute`` while
    the caller retains transaction ownership.
    """

    buffer = ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if not sqlite3.complete_statement(buffer):
            continue
        statement = buffer.strip()
        buffer = ""
        if statement:
            conn.execute(statement)
    if buffer.strip():
        raise sqlite3.OperationalError("incomplete SQL schema statement")


class SQLiteDatabase:
    """Small object wrapper useful for services and tests."""

    def __init__(
        self,
        path: str | Path,
        *,
        readonly: bool = False,
        read_only: bool | None = None,
        timeout: float = 5.0,
        busy_timeout_ms: int = 5_000,
    ) -> None:
        self.path = Path(path).expanduser().resolve()
        if read_only is not None:
            readonly = bool(read_only)
        self.readonly = bool(readonly)
        self.timeout = float(timeout)
        self.busy_timeout_ms = int(busy_timeout_ms)
        self._connection: sqlite3.Connection | None = None

    @property
    def read_only(self) -> bool:
        return self.readonly

    def connect(self) -> sqlite3.Connection:
        if self._connection is None:
            self._connection = connect_database(
                self.path,
                readonly=self.readonly,
                timeout=self.timeout,
                busy_timeout_ms=self.busy_timeout_ms,
            )
        return self._connection

    @property
    def connection(self) -> sqlite3.Connection:
        return self.connect()

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def __enter__(self) -> sqlite3.Connection:
        return self.connect()

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        self.close()
        return False
