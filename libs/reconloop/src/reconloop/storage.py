from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
from uuid import uuid4

from .extract.csv_targets import load_targets
from .schema import Target


def normalize_volume_path(value: str) -> str | None:
    value = str(value)
    if value.startswith("dbfs:/Volumes/"):
        value = value[5:]
    if value.startswith(("dbfs:", "/dbfs/", "/volumes", "/Volume/")):
        raise ValueError("use /Volumes/catalog/schema/volume/path, not legacy DBFS or misspelled volume paths")
    if value == "/Volumes" or value.startswith("/Volumes/"):
        parts = value.split("/")
        if len(parts) < 5 or any(p in {"", ".", ".."} for p in parts[2:]) or "\\" in value:
            raise ValueError("volume path needs catalog/schema/volume and cannot contain traversal or empty segments")
        return value
    return None


def local_path(value, base=None) -> Path:
    """Convert mounted volume URIs for driver-side Python I/O; fail on unsupported runtimes."""
    volume = normalize_volume_path(str(value))
    if volume:
        if os.name != "posix":
            raise RuntimeError("mounted /Volumes paths require a supported Linux cluster; they are not local Windows paths")
        runtime = os.environ.get("DATABRICKS_RUNTIME_VERSION", "")
        version = re.match(r"(\d+)\.(\d+)", runtime)
        if version is None or tuple(map(int, version.groups())) < (13, 3):
            raise RuntimeError("volume I/O needs a verified runtime >=13.3; DATABRICKS_RUNTIME_VERSION is missing or unsupported")
        root = Path("/".join(volume.split("/")[:5]))
        if not root.is_dir():
            raise FileNotFoundError(f"volume is not mounted or accessible: {root}; create/grant it through the platform")
        return Path(volume)
    path = Path(value)
    return path if path.is_absolute() or base is None else Path(base) / path


def _digest(path):
    with Path(path).open("rb") as stream:
        from hashlib import file_digest
        return file_digest(stream, "sha256").hexdigest()


def publish_targets(extraction_dir, store_root, *, bundle_id=None):
    """Publish a complete, reviewed ExtractionResult.write directory without overwriting."""
    source, root = local_path(extraction_dir), local_path(store_root)
    document = json.loads((source / "extraction.json").read_text(encoding="utf-8"))
    if document.get("complete") is not True or document.get("issues"):
        raise ValueError("resolve all extraction issues before publishing targets")
    from dataclasses import asdict
    targets = load_targets(source / "targets.csv")
    if [asdict(t) for t in targets] != [asdict(Target(**t)) for t in document.get("targets", [])]:
        raise ValueError("targets.csv differs from extraction.json; regenerate a consistent reviewed package")
    bundle_id = bundle_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid4().hex[:12]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", bundle_id):
        raise ValueError("bundle_id must be 1..64 letters, digits, underscores or hyphens")
    out = root / bundle_id
    out.mkdir(parents=True, exist_ok=False)
    files = [source / "targets.csv", source / "extraction.json"]
    if (source / "crops").exists():
        files.extend(sorted((source / "crops").glob("*.png")))
    hashes = {}
    for file in files:
        if file.is_symlink() or not file.is_file() or not file.resolve().is_relative_to(source.resolve()):
            raise ValueError("bundle inputs must be regular files inside the extraction directory")
        relative = file.relative_to(source)
        destination = out / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(file, destination)
        digest = _digest(destination)
        if digest != _digest(file):
            raise ValueError("source changed during publication or copy verification failed")
        hashes[relative.as_posix()] = digest
    manifest = dict(schema_version=1, bundle_id=bundle_id, target_count=len(targets),
                    created_at=datetime.now(timezone.utc).isoformat(), files=hashes)
    (out / "READY.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    load_bundle(out)
    return out


def load_bundle(path):
    path = local_path(path)
    manifest = json.loads((path / "READY.json").read_text(encoding="utf-8"))
    files = manifest.get("files", {})
    if manifest.get("schema_version") != 1 or not {"targets.csv", "extraction.json"} <= files.keys():
        raise ValueError("invalid or incomplete target bundle manifest")
    for name, expected in files.items():
        if not (name in {"targets.csv", "extraction.json"} or re.fullmatch(r"crops/[A-Za-z0-9_-]+\.png", name)):
            raise ValueError("invalid bundle file path")
        file = path / name
        if file.is_symlink() or not file.resolve().is_relative_to(path.resolve()) or _digest(file) != expected:
            raise ValueError(f"bundle integrity check failed: {name}")
    from dataclasses import asdict
    document = json.loads((path / "extraction.json").read_text(encoding="utf-8"))
    targets = load_targets(path / "targets.csv")
    if (document.get("complete") is not True or document.get("issues")
            or [asdict(Target(**t)) for t in document.get("targets", [])] != [asdict(t) for t in targets]
            or manifest.get("target_count") != len(targets)):
        raise ValueError("target bundle is incomplete or inconsistent")
    return targets
