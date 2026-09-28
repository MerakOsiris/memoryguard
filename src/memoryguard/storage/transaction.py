"""Explicit SQLite transaction helpers for V2.

Every top-level write starts ``BEGIN IMMEDIATE``.  Nested calls on the same
connection borrow the outer transaction and never commit it implicitly.  A
nested failure marks the outer transaction rollback-only, preventing a caller
from accidentally committing a partially failed operation after catching an
exception.
"""

from __future__ import annotations

from dataclasses import dataclass
import sqlite3
import threading
from typing import Iterator
from contextlib import contextmanager


class TransactionError(RuntimeError):
    """An invalid transaction composition was requested."""


@dataclass
class _State:
    conn: sqlite3.Connection
    owner: bool
    depth: int = 1
    rollback_only: bool = False


_LOCK = threading.RLock()
_STATES: dict[tuple[int, int], _State] = {}


def _key(conn: sqlite3.Connection) -> tuple[int, int]:
    return (threading.get_ident(), id(conn))


class Transaction:
    """Context manager for one explicit transaction unit."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        immediate: bool = True,
        reuse_existing: bool = False,
        reuse: bool | None = None,
    ) -> None:
        self.conn = conn
        self.immediate = bool(immediate)
        self.reuse_existing = bool(reuse_existing if reuse is None else reuse)
        self._state: _State | None = None
        self._nested = False
        self._entered = False
        self._finished = False

    def __enter__(self) -> sqlite3.Connection:
        if self._entered:
            raise TransactionError("transaction context cannot be entered twice")
        self._entered = True
        self._finished = False
        key = _key(self.conn)
        with _LOCK:
            state = _STATES.get(key)
            if state is not None:
                state.depth += 1
                self._state = state
                self._nested = True
                return self.conn
            if self.conn.in_transaction:
                if not self.reuse_existing:
                    self._entered = False
                    raise TransactionError(
                        "connection already has a transaction; pass reuse_existing=True"
                    )
                state = _State(self.conn, owner=False)
                _STATES[key] = state
                self._state = state
                self._nested = True
                return self.conn
        # BEGIN may wait for a different thread/process holding SQLite's
        # writer lock.  Never hold the bookkeeping lock while waiting: that
        # writer needs it to enter nested transactions and finish its commit.
        self.conn.execute("BEGIN IMMEDIATE" if self.immediate else "BEGIN")
        with _LOCK:
            state = _State(self.conn, owner=True)
            _STATES[key] = state
            self._state = state
            return self.conn

    def commit(self) -> None:
        """Commit an explicitly owned top-level transaction.

        Context-manager exit normally performs this operation.  Calling it from
        a nested/reused transaction is rejected rather than silently splitting
        the caller's atomic unit.
        """

        if not self._entered or self._state is None:
            raise TransactionError("transaction is not active")
        if self._nested or not self._state.owner:
            raise TransactionError("nested/reused transaction cannot commit")
        if self._state.rollback_only:
            raise TransactionError("transaction is rollback-only")
        self.conn.commit()
        self._finished = True
        self._remove_state()

    def rollback(self) -> None:
        if not self._entered or self._state is None:
            raise TransactionError("transaction is not active")
        if self._nested or not self._state.owner:
            self._state.rollback_only = True
            raise TransactionError("nested/reused transaction cannot rollback")
        self.conn.rollback()
        self._finished = True
        self._remove_state()

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        state = self._state
        try:
            if self._finished:
                return False
            if state is None:
                return False
            if self._nested:
                if exc_type is not None:
                    state.rollback_only = True
                state.depth -= 1
                if not state.owner and state.depth <= 0:
                    self._remove_state()
                return False
            if exc_type is not None or state.rollback_only:
                self.conn.rollback()
            else:
                self.conn.commit()
            return False
        finally:
            if not self._nested:
                self._remove_state()
            self._state = None
            self._entered = False

    def _remove_state(self) -> None:
        with _LOCK:
            _STATES.pop(_key(self.conn), None)


@contextmanager
def transaction(
    conn: sqlite3.Connection,
    *,
    immediate: bool = True,
    reuse_existing: bool = False,
    reuse: bool | None = None,
) -> Iterator[sqlite3.Connection]:
    """Yield a connection inside an explicit transaction."""

    with Transaction(
        conn, immediate=immediate, reuse_existing=reuse_existing, reuse=reuse
    ) as connection:
        yield connection


begin_transaction = transaction
begin_immediate = transaction
atomic = transaction
