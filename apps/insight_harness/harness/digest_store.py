"""History stores for deterministic digest snapshots.

The public UI uses an in-memory store owned by one session. Durable JSONL
history remains available behind an identity boundary and namespaces novelty
by owner, persona, scope, and watch/input fingerprint.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from copy import deepcopy
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Mapping


DIGEST_HISTORY_PATH = Path(__file__).parent.parent / "data" / "digest_history.jsonl"
_WRITE_LOCK = threading.Lock()


def _stable_hash(value: object, length: int = 16) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:length]


def _normal_scope(scope: Mapping | None) -> dict:
    out = {}
    for key, value in sorted((scope or {}).items()):
        if isinstance(value, (list, tuple, set)):
            out[str(key)] = sorted(str(item) for item in value)
        else:
            out[str(key)] = str(value)
    return out


def history_fingerprint(records: Iterable[dict]) -> str:
    """Hash selection-relevant fields, never timestamps."""

    stable = [{
        "digest_key": record.get("digest_key"),
        "owner_namespace": record.get("owner_namespace"),
        "persona": record.get("persona"),
        "scope": _normal_scope(record.get("scope", {})),
        "input_fingerprint": record.get("input_fingerprint"),
        "data_version": record.get("data_version"),
        "governance_fingerprint": record.get("governance_fingerprint"),
        "item_keys": record.get("item_keys", []),
        "item_fact_hashes": record.get("item_fact_hashes", []),
    } for record in records]
    return _stable_hash(stable)


def _recent(records: Iterable[dict], *, persona: str, scope: Mapping | None,
            input_fingerprint: str | None, owner_namespace: str | None,
            exclude_key: str | None, limit: int) -> list[dict]:
    if limit <= 0:
        return []
    normalized_scope = _normal_scope(scope)
    matching = [dict(record) for record in records if (
        record.get("persona") == persona
        and _normal_scope(record.get("scope", {})) == normalized_scope
        and record.get("input_fingerprint") == input_fingerprint
        and record.get("owner_namespace") == owner_namespace
        and (exclude_key is None or record.get("digest_key") != exclude_key)
    )]
    return matching[-limit:]


class InMemoryDigestHistoryStore:
    """Thread-safe history owned by one UI session; never touches disk."""

    def __init__(self, records: Iterable[dict] = ()) -> None:
        self._records: dict[str, dict] = {}
        self._lock = threading.RLock()
        for record in records:
            self.record_once(record)

    def load(self) -> list[dict]:
        with self._lock:
            return deepcopy(list(self._records.values()))

    def get(self, digest_key: str) -> dict | None:
        with self._lock:
            record = self._records.get(digest_key)
            return deepcopy(record) if record is not None else None

    def recent(self, *, persona: str, scope: Mapping | None = None,
               input_fingerprint: str | None = None,
               owner_namespace: str | None = None,
               exclude_key: str | None = None, limit: int = 5) -> list[dict]:
        with self._lock:
            return _recent(
                self._records.values(), persona=persona, scope=scope,
                input_fingerprint=input_fingerprint,
                owner_namespace=owner_namespace, exclude_key=exclude_key, limit=limit)

    def record_once(self, record: dict) -> dict:
        digest_key = record.get("digest_key")
        if not isinstance(digest_key, str) or not digest_key:
            raise ValueError("digest history records require a non-empty digest_key")
        with self._lock:
            existing = self._records.get(digest_key)
            if existing is not None:
                return deepcopy(existing)
            self._records[digest_key] = deepcopy(record)
            return deepcopy(record)


@contextmanager
def _path_lock(path: Path, timeout_seconds: float = 5.0):
    """Cross-process lock based on atomic lock-file creation."""

    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_seconds
    descriptor = None
    while descriptor is None:
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(descriptor, f"{os.getpid()}\n".encode("ascii"))
        except FileExistsError:
            try:
                if time.time() - lock_path.stat().st_mtime > 30.0:
                    lock_path.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            if time.monotonic() >= deadline:
                raise TimeoutError(f"timed out acquiring digest history lock: {lock_path}")
            time.sleep(0.01)
    try:
        yield
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            pass


class DigestHistoryStore:
    """Corruption-tolerant JSONL history with process-safe record-once writes."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path if path is not None else DIGEST_HISTORY_PATH)

    def _load_unlocked(self) -> list[dict]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except (FileNotFoundError, OSError):
            return []
        records: list[dict] = []
        for line in lines:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict) and isinstance(record.get("digest_key"), str):
                records.append(record)
        return records

    def load(self) -> list[dict]:
        return self._load_unlocked()

    def get(self, digest_key: str) -> dict | None:
        return next((record for record in self.load()
                     if record.get("digest_key") == digest_key), None)

    def recent(self, *, persona: str, scope: Mapping | None = None,
               input_fingerprint: str | None = None,
               owner_namespace: str | None = None,
               exclude_key: str | None = None, limit: int = 5) -> list[dict]:
        return _recent(
            self.load(), persona=persona, scope=scope,
            input_fingerprint=input_fingerprint,
            owner_namespace=owner_namespace, exclude_key=exclude_key, limit=limit)

    def record_once(self, record: dict) -> dict:
        digest_key = record.get("digest_key")
        if not isinstance(digest_key, str) or not digest_key:
            raise ValueError("digest history records require a non-empty digest_key")
        with _WRITE_LOCK:
            with _path_lock(self.path):
                existing = next((item for item in self._load_unlocked()
                                 if item.get("digest_key") == digest_key), None)
                if existing is not None:
                    return existing
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, sort_keys=True, default=str) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                return dict(record)
