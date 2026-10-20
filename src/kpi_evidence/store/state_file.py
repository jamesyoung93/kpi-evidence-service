from __future__ import annotations

import contextlib
import os
import shutil
from pathlib import Path


def state_location(path: str | Path) -> Path:
    """Filesystem path for a state file; ``dbfs:/Volumes/...`` becomes the ``/Volumes/...`` mount path."""
    text = str(path)
    if text.lower().startswith("dbfs:/"):
        text = text[5:]
    return Path(text).expanduser()


def pull_state(state_path: str | Path, local_path: str | Path) -> bool:
    """Copy the state file to a local working path; False when there is no state file yet."""
    source = state_location(state_path)
    if not source.is_file():
        return False
    target = Path(local_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    return True


def push_state(local_path: str | Path, state_path: str | Path) -> str:
    """Write the local state to ``state_path`` through ``<state_path>.tmp``; the previous file survives a failure."""
    target = state_location(state_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    try:
        shutil.copyfile(local_path, tmp)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    try:
        os.replace(tmp, target)
    except OSError:
        shutil.copyfile(tmp, target)
        with contextlib.suppress(OSError):
            tmp.unlink()
    return str(target)
