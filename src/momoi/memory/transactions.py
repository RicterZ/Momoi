"""Composable SQLite units of work; never commit a caller's open transaction."""
from contextlib import contextmanager
from itertools import count
import sqlite3
from collections.abc import Iterator

_savepoints = count()


@contextmanager
def transaction(database: sqlite3.Connection) -> Iterator[None]:
    nested = database.in_transaction
    name = f"momoi_uow_{next(_savepoints)}"
    database.execute(f"SAVEPOINT {name}" if nested else "BEGIN IMMEDIATE")
    try:
        yield
        database.execute(f"RELEASE SAVEPOINT {name}" if nested else "COMMIT")
    except BaseException:
        if database.in_transaction:
            if nested:
                database.execute(f"ROLLBACK TO SAVEPOINT {name}")
                database.execute(f"RELEASE SAVEPOINT {name}")
            else:
                database.rollback()
        raise
