from __future__ import annotations

import io
import os
import re
import uuid
from pathlib import Path, PurePosixPath

from ..settings import Settings

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")
MODES = ("local", "fuse", "sdk")


def safe_name(name: str, max_len: int = 120) -> str:
    stem = _SAFE.sub("_", Path(name).name).strip("._") or "file"
    return stem[-max_len:]


def _error_names(exc: BaseException) -> set[str]:
    names = {cls.__name__ for cls in type(exc).__mro__}
    code = getattr(exc, "error_code", None)
    if code:
        names.add(str(code))
    return names


def _is_not_found(exc: BaseException) -> bool:
    return bool(_error_names(exc) & {"NotFound", "ResourceDoesNotExist", "NOT_FOUND", "RESOURCE_DOES_NOT_EXIST",
                                     "FileNotFoundError"})


def _is_already_exists(exc: BaseException) -> bool:
    return bool(_error_names(exc) & {"AlreadyExists", "ResourceAlreadyExists", "ALREADY_EXISTS",
                                     "RESOURCE_ALREADY_EXISTS"})


def _is_permission(exc: BaseException) -> bool:
    return bool(_error_names(exc) & {"PermissionDenied", "PERMISSION_DENIED"})


class FileStore:
    def __init__(self, settings: Settings, mode: str | None = None, client=None):
        self.settings = settings
        self.root = settings.resolved_volume_path()
        if mode is None:
            if settings.is_local:
                mode = "local"
            elif os.path.isdir(self.root) or os.path.isdir("/Volumes"):
                mode = "fuse"
            else:
                mode = "sdk"
        if mode not in MODES:
            raise ValueError(f"file store mode must be one of {MODES}, got {mode!r}")
        self.mode = mode
        self._client = client

    @property
    def client(self):
        if self._client is None:
            from databricks.sdk import WorkspaceClient

            self._client = WorkspaceClient()
        return self._client

    def _uses_sdk(self, path: str) -> bool:
        return self.mode == "sdk" and path.startswith("/Volumes/")

    def full_path(self, rel_path: str) -> str:
        """Full path for a path relative to the volume root; rejects '..' so callers cannot escape it."""
        text = rel_path.replace("\\", "/")
        root = self.root.replace("\\", "/").rstrip("/")
        if text == root or text.startswith(root + "/"):
            text = text[len(root):]
        rel = str(PurePosixPath(text)).lstrip("/")
        if ".." in PurePosixPath(rel).parts:
            raise ValueError("relative path may not contain '..'")
        if rel in ("", "."):
            rel = ""
        if self.mode == "local":
            return str(Path(self.root) / rel) if rel else str(Path(self.root))
        return f"{self.root.rstrip('/')}/{rel}" if rel else self.root.rstrip("/")

    def write_bytes(self, rel_path: str, data: bytes, overwrite: bool = False) -> str:
        """Store ``data`` under the volume root and return the full path (existing files kept unless overwrite)."""
        path = self.full_path(rel_path)
        if path.rstrip("/\\") == self.full_path("").rstrip("/\\"):
            raise ValueError("write_bytes needs a file path below the volume root")
        if self.mode == "local":
            p = Path(path)
            if p.exists() and not overwrite:
                return path
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_name(f".{p.name}.{uuid.uuid4().hex[:8]}.part")
            try:
                tmp.write_bytes(data)
                os.replace(tmp, p)
            finally:
                if tmp.exists():
                    tmp.unlink()
            return path
        if self.mode == "fuse":
            p = Path(path)
            if p.exists() and not overwrite:
                return path
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
            return path
        if not overwrite and self.exists(path):
            return path
        try:
            self.client.files.upload(path, io.BytesIO(data), overwrite=overwrite)
        except Exception as exc:
            if overwrite or not _is_already_exists(exc):
                raise
        return path

    def read_bytes(self, path: str) -> bytes:
        if not self._uses_sdk(path):
            return Path(path).read_bytes()
        resp = self.client.files.download(path)
        if resp is None or resp.contents is None:
            raise FileNotFoundError(f"Files API returned no contents for {path}")
        try:
            return resp.contents.read()
        finally:
            close = getattr(resp.contents, "close", None)
            if callable(close):
                close()

    def exists(self, path: str) -> bool:
        """True if the file exists and is readable here (not-found and permission errors mean False)."""
        if not self._uses_sdk(path):
            return Path(path).exists()
        try:
            self.client.files.get_metadata(path)
            return True
        except Exception as exc:
            if _is_not_found(exc) or _is_permission(exc):
                return False
            raise

    def list_files(self, rel_prefix: str = "inbox") -> list[str]:
        """Every file below ``rel_prefix`` (recursive, sorted; in-progress ``.part`` files skipped)."""
        base = self.full_path(rel_prefix)
        if not self._uses_sdk(base):
            root = Path(base)
            if not root.exists():
                return []
            return sorted(str(p) for p in root.rglob("*") if p.is_file() and not p.name.endswith(".part"))
        out: list[str] = []
        stack = [base]
        while stack:
            current = stack.pop()
            try:
                entries = list(self.client.files.list_directory_contents(current))
            except Exception as exc:
                if current == base and _is_not_found(exc):
                    return []
                raise
            for entry in entries:
                path = (entry.path or f"{current.rstrip('/')}/{entry.name}").rstrip("/")
                if entry.is_directory:
                    stack.append(path)
                elif not path.endswith(".part"):
                    out.append(path)
        return sorted(out)


def files_for(settings: Settings, mode: str | None = None, client=None) -> FileStore:
    return FileStore(settings, mode=mode, client=client)
