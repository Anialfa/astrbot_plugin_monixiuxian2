"""Task-local transactions for operations that change several inventory records."""

from contextlib import asynccontextmanager
from contextvars import ContextVar
from functools import wraps
import inspect

import aiosqlite


class TransactionConnection:
    def __init__(self, connection):
        self.connection = connection
        self.rollback_only = False

    def __getattr__(self, name):
        return getattr(self.connection, name)

    async def commit(self):
        # Existing helpers may request commit; only the outer operation commits.
        pass

    async def rollback(self):
        self.rollback_only = True


class Transactions:
    def __init__(self, db):
        self.db = db
        self.current = ContextVar("xiuxian_transaction", default=None)

    @asynccontextmanager
    async def open(self):
        if self.current.get() is not None:
            yield
            return
        connection = await aiosqlite.connect(self.db.db_path, timeout=30)
        connection.row_factory = aiosqlite.Row
        transaction = TransactionConnection(connection)
        token = self.current.set(transaction)
        try:
            await connection.execute("PRAGMA foreign_keys = ON")
            await connection.execute("BEGIN IMMEDIATE")
            yield
            if transaction.rollback_only:
                await connection.rollback()
            else:
                await connection.commit()
        except BaseException:
            await connection.rollback()
            raise
        finally:
            self.current.reset(token)
            await connection.close()


def atomic_operation(function):
    """Commit before returning results or yielding a success message."""
    if inspect.isasyncgenfunction(function):
        @wraps(function)
        async def generator(self, *args, **kwargs):
            async with self.db.transaction():
                results = [result async for result in function(self, *args, **kwargs)]
            for result in results:
                yield result
        return generator

    @wraps(function)
    async def coroutine(self, *args, **kwargs):
        async with self.db.transaction():
            return await function(self, *args, **kwargs)
    return coroutine
