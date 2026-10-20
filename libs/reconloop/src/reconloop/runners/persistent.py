from hashlib import sha256
import json
from math import isfinite
import sqlite3
from threading import Lock
import time

from .base import scalar_value
from ..storage import local_path, normalize_volume_path


class PersistentCache:
    def __init__(self, path, *, namespace, source_version, ttl_seconds=86400):
        if any(not isinstance(v, str) or not v.strip() for v in (namespace, source_version)):
            raise ValueError("persistent cache requires explicit namespace and immutable source_version")
        if not isfinite(ttl_seconds) or ttl_seconds <= 0:
            raise ValueError("cache TTL must be positive and finite")
        if normalize_volume_path(str(path)):
            raise ValueError("SQLite cache requires a local filesystem with locking, not an object-backed volume")
        self.path = local_path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.scope = json.dumps([namespace, source_version])
        self.ttl = ttl_seconds
        self.locks = [Lock() for _ in range(64)]
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS results (key TEXT PRIMARY KEY, value REAL NOT NULL, created REAL NOT NULL)")

    def connect(self):
        from contextlib import closing
        return closing(sqlite3.connect(self.path, timeout=30, isolation_level=None))

    def get_or_run(self, sql, operation):
        key = sha256((self.scope + "\0" + sql).encode()).hexdigest()
        with self.locks[int(key[:4], 16) % len(self.locks)]:
            with self.connect() as db:
                row = db.execute("SELECT value, created FROM results WHERE key = ?", (key,)).fetchone()
            now = time.time()
            if row and 0 <= now - row[1] <= self.ttl:
                return scalar_value(row[0])
            value = scalar_value(operation(sql))
            with self.connect() as db:
                db.execute("INSERT OR REPLACE INTO results VALUES (?, ?, ?)", (key, value, time.time()))
            return value


class CachedRunner:
    def __init__(self, runner, cache):
        self.runner, self.persistent = runner, cache

    def run(self, sql):
        return self.persistent.get_or_run(sql, self.runner.run)

    def close(self):
        if hasattr(self.runner, "close"):
            self.runner.close()
