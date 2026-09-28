"""One SQLite transaction for a governed memory publication.

Memory rows, revisions, evidence projection and decision receipts share the
same connection.  Exceptions roll back the whole operation; nested writes
borrow it and cannot commit independently.  Connections are thread-local and
path-bound, so unrelated workspaces and concurrent requests cannot borrow it.

SQLite provides transaction rollback across the attached databases.  With
WAL, power-loss atomicity across files is not guaranteed by SQLite; the
existing outbox/receipt reconciliation remains necessary after such a crash.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import os
import sqlite3
import threading
from typing import Any, Iterator

from ..storage.database import connect_database
from ..storage.transaction import transaction


_LOCAL = threading.local()


def _key(path: str | Path) -> str:
    return os.path.normcase(str(Path(path).resolve()))


class _MemoryLease:
    """A stable transaction identity whose borrowers cannot close it."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.connection = conn

    def __getattr__(self, name: str) -> Any:
        return getattr(self.connection, name)

    def close(self) -> None:
        pass


class _LedgerLease(_MemoryLease):
    # Ledger methods historically own short transactions.  In a publication
    # their statements belong to the caller's transaction instead.
    def execute(self, sql: str, parameters: Any = ()) -> Any:
        if sql.strip().upper() == "BEGIN IMMEDIATE":
            return self.connection.cursor()
        return self.connection.execute(sql, parameters)

    @property
    def row_factory(self) -> Any:
        return self.connection.row_factory

    @row_factory.setter
    def row_factory(self, value: Any) -> None:
        self.connection.row_factory = value

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        # The exception must escape the outer transaction to discard it.
        pass


def memory_connection(path: str | Path) -> Any | None:
    state = getattr(_LOCAL, "state", None)
    return state["memory"] if state and state["memory_path"] == _key(path) else None


def ledger_connection(path: str | Path) -> Any | None:
    state = getattr(_LOCAL, "state", None)
    return state["ledger"] if state and state["ledger_path"] == _key(path) else None


def evidence_connection(path: str | Path) -> Any | None:
    state = getattr(_LOCAL, "state", None)
    return state["memory"] if state and state["evidence_path"] == _key(path) else None


@contextmanager
def memory_publication(memory_path: Path, ledger_path: Path, evidence_path: Path) -> Iterator[bool]:
    """Yield whether this call owns the publication transaction."""
    previous = getattr(_LOCAL, "state", None)
    paths = {"memory_path": _key(memory_path), "ledger_path": _key(ledger_path),
             "evidence_path": _key(evidence_path)}
    if previous:
        if any(previous[key] != value for key, value in paths.items()):
            raise RuntimeError("cross-workspace memory publication")
        with transaction(previous["memory"]):
            yield False
        return
    # Stores/schema leases have already been validated by the governance
    # boundary.  Missing attached files must never be created implicitly.
    if not all(Path(path).is_file() for path in (memory_path, ledger_path, evidence_path)):
        raise RuntimeError("memory publication store missing")
    conn = connect_database(memory_path)
    try:
        conn.execute("ATTACH DATABASE ? AS governance", (str(ledger_path),))
        conn.execute("ATTACH DATABASE ? AS publication_evidence", (str(evidence_path),))
        lease = _MemoryLease(conn)
        _LOCAL.state = {**paths, "memory": lease, "ledger": _LedgerLease(conn)}
        with transaction(lease):
            yield True
    finally:
        _LOCAL.state = previous
        conn.close()
