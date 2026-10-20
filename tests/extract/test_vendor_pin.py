from __future__ import annotations

import hashlib
import json
from pathlib import Path

from kpi_evidence.extract._vendor import pptx_extract

VENDOR_DIR = Path(pptx_extract.__file__).resolve().parent


def test_vendored_file_hash_matches_manifest():
    manifest = json.loads((VENDOR_DIR / "VENDOR.json").read_text(encoding="utf-8"))
    digest = hashlib.sha256((VENDOR_DIR / manifest["file"]).read_bytes()).hexdigest()
    assert digest == manifest["sha256"], "pptx_extract.py was edited; fixes belong in kpi_evidence.extract.pptx"


def test_vendored_version_matches_manifest():
    manifest = json.loads((VENDOR_DIR / "VENDOR.json").read_text(encoding="utf-8"))
    assert pptx_extract.__version__ == manifest["upstream_version"]


def test_adapter_reports_pinned_versions():
    from kpi_evidence.extract.pptx import extractor_versions

    manifest = json.loads((VENDOR_DIR / "VENDOR.json").read_text(encoding="utf-8"))
    versions = extractor_versions()
    assert versions["pptx_extract_sha256"] == manifest["sha256"]
    assert versions["pptx_extract_commit"] == manifest["commit"]
