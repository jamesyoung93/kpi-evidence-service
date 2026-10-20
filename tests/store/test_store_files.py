from __future__ import annotations

import dataclasses
import io
from pathlib import Path

import pytest
from databricks.sdk.errors import AlreadyExists, NotFound, PermissionDenied
from databricks.sdk.service.files import DirectoryEntry, DownloadResponse

from kpi_evidence.settings import Settings
from kpi_evidence.store.files import FileStore, files_for, safe_name

ROOT = "/Volumes/main/kpi_evidence/reports"


@pytest.fixture()
def local(settings) -> FileStore:
    return files_for(settings)


def test_local_mode_is_the_default_for_local_settings(local, settings):
    assert local.mode == "local"
    assert local.root == settings.resolved_volume_path()


def test_local_write_read_exists(local):
    path = local.write_bytes("inbox/2026/06/10/abc_deck.pptx", b"one")
    assert Path(path).is_absolute() and path.startswith(local.root)
    assert local.exists(path) and local.read_bytes(path) == b"one"
    assert not local.exists(local.full_path("inbox/missing.pptx"))


def test_local_write_keeps_existing_files_unless_overwrite(local):
    path = local.write_bytes("inbox/a.pdf", b"first")
    assert local.write_bytes("inbox/a.pdf", b"second") == path
    assert local.read_bytes(path) == b"first"
    local.write_bytes("inbox/a.pdf", b"third", overwrite=True)
    assert local.read_bytes(path) == b"third"


def test_local_write_leaves_no_temp_files(local):
    local.write_bytes("inbox/a.pdf", b"x")
    local.write_bytes("inbox/a.pdf", b"y", overwrite=True)
    names = [p.name for p in Path(local.full_path("inbox")).iterdir()]
    assert names == ["a.pdf"]


def test_local_list_files_is_recursive_sorted_and_skips_partials(local):
    for rel in ("inbox/2026/06/b.pptx", "inbox/2026/05/a.pdf", "artifacts/sql/x.sql"):
        local.write_bytes(rel, b"x")
    Path(local.full_path("inbox/2026/06/c.pptx.part")).write_bytes(b"partial")
    listed = local.list_files("inbox")
    assert listed == sorted(listed) and len(listed) == 2
    assert [Path(p).name for p in listed] == ["a.pdf", "b.pptx"]
    assert local.list_files("does/not/exist") == []


@pytest.mark.parametrize("rel", ["../escape.txt", "inbox/../../escape.txt", "inbox\\..\\..\\x"])
def test_paths_cannot_escape_the_root(local, rel):
    with pytest.raises(ValueError):
        local.write_bytes(rel, b"x")


def test_root_itself_is_not_a_file(local):
    for rel in ("", ".", "/"):
        with pytest.raises(ValueError):
            local.write_bytes(rel, b"x")


def test_full_paths_under_the_root_are_accepted(local):
    full = local.full_path("inbox/a.pdf")
    assert local.full_path(full) == full
    assert local.write_bytes(full, b"x") == full


def test_unknown_mode_is_rejected(settings):
    with pytest.raises(ValueError):
        FileStore(settings, mode="s3")


@pytest.mark.parametrize("name, expected", [
    ("Kestrel Monthly (v2).pptx", "Kestrel_Monthly_v2_.pptx"),
    ("..\\..\\evil.pdf", "evil.pdf"),
    ("...", "file"),
])
def test_safe_name(name, expected):
    assert safe_name(name) == expected


class FakeFiles:
    def __init__(self):
        self.store: dict[str, bytes] = {}
        self.uploads: list[tuple[str, bool]] = []
        self.raise_on_metadata: Exception | None = None
        self.race = False

    def upload(self, file_path, contents, *, overwrite=None):
        self.uploads.append((file_path, overwrite))
        if self.race:
            self.store[file_path] = b"theirs"
        if file_path in self.store and not overwrite:
            raise AlreadyExists(f"file already exists: {file_path}", error_code="ALREADY_EXISTS")
        self.store[file_path] = contents.read()

    def download(self, file_path):
        if file_path not in self.store:
            raise NotFound(f"not found: {file_path}", error_code="NOT_FOUND")
        return DownloadResponse(contents=io.BytesIO(self.store[file_path]))

    def get_metadata(self, file_path):
        if self.raise_on_metadata is not None:
            raise self.raise_on_metadata
        if file_path not in self.store:
            raise NotFound(f"not found: {file_path}", error_code="NOT_FOUND")
        return object()

    def list_directory_contents(self, directory_path):
        prefix = directory_path.rstrip("/") + "/"
        children = {p[len(prefix):].split("/")[0] for p in self.store if p.startswith(prefix)}
        if not children:
            raise NotFound(f"directory not found: {directory_path}", error_code="NOT_FOUND")
        for child in sorted(children):
            path = prefix + child
            yield DirectoryEntry(path=path, name=child, is_directory=path not in self.store)


class FakeClient:
    def __init__(self):
        self.files = FakeFiles()


@pytest.fixture()
def sdk(tmp_path):
    settings = Settings.from_env(backend="databricks", catalog="main", local_dir=tmp_path / ".k")
    client = FakeClient()
    return FileStore(settings, mode="sdk", client=client), client.files


def test_sdk_write_uploads_once_and_returns_the_volume_path(sdk):
    store, files = sdk
    path = store.write_bytes("inbox/2026/06/10/abc_deck.pptx", b"deck")
    assert path == f"{ROOT}/inbox/2026/06/10/abc_deck.pptx"
    assert files.store[path] == b"deck" and files.uploads == [(path, False)]
    assert store.write_bytes("inbox/2026/06/10/abc_deck.pptx", b"other") == path
    assert files.uploads == [(path, False)], "existing content-addressed files are not re-uploaded"
    store.write_bytes("inbox/2026/06/10/abc_deck.pptx", b"new", overwrite=True)
    assert files.store[path] == b"new" and files.uploads[-1] == (path, True)


def test_sdk_write_tolerates_losing_an_upload_race(sdk):
    store, files = sdk
    files.race = True
    path = store.write_bytes("inbox/a.pdf", b"mine")
    assert files.store[path] == b"theirs"


def test_sdk_write_surfaces_other_upload_errors(sdk):
    store, files = sdk

    def denied(*args, **kwargs):
        raise PermissionDenied("no WRITE_VOLUME", error_code="PERMISSION_DENIED")

    files.upload = denied
    with pytest.raises(PermissionDenied):
        store.write_bytes("inbox/a.pdf", b"x")


def test_sdk_read_and_exists(sdk):
    store, files = sdk
    path = store.write_bytes("inbox/a.pdf", b"%PDF-1.7")
    assert store.read_bytes(path) == b"%PDF-1.7"
    assert store.exists(path)
    assert not store.exists(f"{ROOT}/inbox/missing.pdf")
    with pytest.raises(NotFound):
        store.read_bytes(f"{ROOT}/inbox/missing.pdf")


def test_sdk_exists_treats_permission_as_absent_but_raises_other_errors(sdk):
    store, files = sdk
    files.raise_on_metadata = PermissionDenied("denied", error_code="PERMISSION_DENIED")
    assert store.exists(f"{ROOT}/inbox/a.pdf") is False
    files.raise_on_metadata = ConnectionError("network down")
    with pytest.raises(ConnectionError):
        store.exists(f"{ROOT}/inbox/a.pdf")


def test_sdk_list_files_walks_directories(sdk):
    store, files = sdk
    for rel in ("inbox/2026/06/b.pptx", "inbox/2026/05/a.pdf", "inbox/c.pdf", "artifacts/sql/x.sql"):
        store.write_bytes(rel, b"x")
    assert store.list_files("inbox") == [f"{ROOT}/inbox/2026/05/a.pdf", f"{ROOT}/inbox/2026/06/b.pptx",
                                         f"{ROOT}/inbox/c.pdf"]
    assert store.list_files("nothing/here") == []


def test_sdk_mode_reads_non_volume_paths_locally(sdk, tmp_path):
    store, _ = sdk
    local_file = tmp_path / "local.bin"
    local_file.write_bytes(b"local")
    assert store.read_bytes(str(local_file)) == b"local" and store.exists(str(local_file))


def test_fuse_mode_writes_directly(tmp_path):
    settings = Settings.from_env(backend="databricks", catalog="main", volume_path=str(tmp_path / "vol"),
                                 local_dir=tmp_path / ".k")
    store = FileStore(settings, mode="fuse")
    path = store.write_bytes("inbox/a.pdf", b"x")
    assert Path(path).read_bytes() == b"x"
    assert sorted(p.name for p in Path(path).parent.iterdir()) == ["a.pdf"]
    assert store.write_bytes("inbox/a.pdf", b"y") == path and Path(path).read_bytes() == b"x"


def test_mode_auto_detection(tmp_path):
    db = Settings.from_env(backend="databricks", catalog="main", local_dir=tmp_path / ".k")
    assert FileStore(db, client=FakeClient()).mode in ("fuse", "sdk")
    on_fuse = dataclasses.replace(db, volume_path=str(tmp_path))
    assert FileStore(on_fuse).mode == "fuse"
