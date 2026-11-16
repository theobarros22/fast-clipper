"""
Camada de persistência local em SQLite (WAL) do Fast Clipper Studio.
"""

from fastclipper.storage.sqlite_store import SQLiteStore, SQLiteTaskQueue

__all__ = ["SQLiteStore", "SQLiteTaskQueue"]
