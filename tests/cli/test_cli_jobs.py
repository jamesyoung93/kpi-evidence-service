from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from cli_support import REPORTS, RecordingStore, install_standins
from kpi_evidence import cli
from kpi_evidence.contracts import Document, DocumentStatus
from kpi_evidence.settings import Settings
from kpi_evidence.store.duckdb_store import DuckDBStore


@pytest.fixture()
def local_env(tmp_path, monkeypatch):
    monkeypatch.setenv("KPI_EVIDENCE_LOCAL_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("KPI_EVIDENCE_AI_MODE", "off")
    return Settings.from_env(backend="local")


def test_parse_job_args_ignores_pushed_down_parameters_and_unresolved_refs():
    args = cli.parse_job_args(["--catalog=main", "--gov-schema=kpi_gov", "--max_docs=200", "--extra", "x",
                               "--trigger_location={{job.trigger.file_arrival.location}}", "--schema="],
                              "trigger_location")
    assert args.catalog == "main" and args.gov_schema == "kpi_gov"
    assert args.trigger_location is None and args.schema is None and args.run_id is None


def test_job_settings_from_parameters():
    args = cli.parse_job_args(["--backend=databricks", "--catalog=corp", "--schema=kpi_ev", "--ai_mode=off",
                               "--volume_path=/Volumes/corp/kpi_ev/reports"])
    s = cli.job_settings(args)
    assert (s.backend, s.catalog, s.schema, s.ai_mode) == ("databricks", "corp", "kpi_ev", "off")
    assert s.resolved_volume_path() == "/Volumes/corp/kpi_ev/reports"


def test_pipeline_entry_points_end_to_end_locally(local_env, capsys):
    common = ["--backend=local", "--run_id=job-1", "--max_docs=200"]
    assert cli.bootstrap_entry(common + ["--load_demo=true", "--seed_samples=true", "--app_sp=x"]) == 0
    inbox = Path(local_env.resolved_volume_path()) / "inbox"
    assert len([p for p in inbox.rglob("*") if p.is_file()]) == 5
    assert "bootstrap:" in capsys.readouterr().out

    assert cli.register_entry(common + ["--trigger_location={{job.trigger.file_arrival.location}}"]) == 0
    store = DuckDBStore(local_env)
    docs = store.read(Document, order_by="file_name")
    assert len(docs) == 5 and {d.status for d in docs} == {DocumentStatus.REGISTERED}

    for doc in docs:
        assert cli.main(["extract", *common, f"--doc_id={doc.doc_id}"]) == 0
    statuses = {d.file_name: d.status.value for d in store.read(Document)}
    assert statuses["kestrel-update.pptx"] == "AWAITING_CONFIRMATION"
    assert statuses["Kestrel_Executive_Summary_Q2-2026.pdf"] == "READY"

    assert cli.process_entry(common) == 0
    statuses = {d.file_name: d.status.value for d in store.read(Document)}
    assert statuses["Kestrel_Executive_Summary_Q2-2026.pdf"] == "PROCESSED"
    decks = sorted(statuses[f"Kestrel_Monthly_Brand_Review_2026-06{v}.pptx"] for v in ("", "_v2"))
    assert decks == ["PROCESSED", "SUPERSEDED"]
    out = capsys.readouterr().out
    assert "process:" in out and "VERIFIED" in out

    assert cli.publish_entry(common) == 0
    assert '"published": 0' in capsys.readouterr().out
    assert cli.dq_entry(common) == 0
    assert "gv_rx_account_monthly" in capsys.readouterr().out


def test_extract_entry_needs_a_known_doc_id(local_env):
    cli.bootstrap_entry(["--backend=local", "--load_demo=true"])
    with pytest.raises(ValueError, match="--doc_id"):
        cli.extract_entry(["--backend=local"])
    with pytest.raises(KeyError, match="unknown document"):
        cli.extract_entry(["--backend=local", "--doc_id=" + "0" * 64])


def test_evaluate_entry_isolates_the_evaluation_on_databricks(monkeypatch):
    from kpi_evidence.eval import evaluate

    seen = {}

    def fake_run_evaluation(settings, store=None, **kwargs):
        seen.update(backend=settings.backend, ai_mode=settings.ai_mode, local_dir=Path(settings.local_dir),
                    store=store, mlflow=settings.mlflow_experiment)
        return SimpleNamespace(eval_id="e1", dataset="kestrel@abc", metrics={"status_accuracy": 0.97, "note": "x"})

    store = RecordingStore()
    monkeypatch.setattr(evaluate, "run_evaluation", fake_run_evaluation)
    monkeypatch.setattr(cli, "open_job_store", lambda settings: store)
    assert cli.evaluate_entry(["--backend=databricks", "--catalog=main", "--ai_mode=ai_query",
                               "--mlflow_experiment=123"]) == 0
    assert seen["backend"] == "local" and seen["ai_mode"] == "off" and seen["store"] is None
    assert seen["mlflow"] == "123" and not seen["local_dir"].exists()
    assert [r.eval_id for r in store.inserted] == ["e1"]


def test_check_entry_writes_timestamped_reports(monkeypatch, tmp_path, capsys):
    install_standins(monkeypatch)
    from kpi_evidence.store import databricks_store

    monkeypatch.setattr(databricks_store, "active_spark", lambda: None)
    out_dir = tmp_path / "out"
    code = cli.check_entry([f"--reports={REPORTS / 'Kestrel_Executive_Summary_Q2-2026.pdf'}",
                            f"--out_dir={out_dir}", "--brand=Kestrel", "--kpis=", "--run_id=7", "--wheels=x"])
    assert code == 0
    names = sorted(p.suffix for p in out_dir.iterdir())
    assert names == [".html", ".xlsx"] and all(p.name.startswith("kpi_check_") for p in out_dir.iterdir())
    assert "check:" in capsys.readouterr().out
    with pytest.raises(ValueError, match="--reports"):
        cli.check_entry([f"--out_dir={out_dir}"])


def test_main_dispatches_job_stages(local_env):
    assert cli.main(["bootstrap", "--backend=local", "--load_demo=true"]) == 0
    assert cli.main(["dq", "--backend=local"]) == 0


def test_apply_grants_validates_and_skips_missing_principals():
    settings = Settings.from_env(backend="databricks", catalog="main", schema="kpi_evidence", gov_schema="kpi_gov")
    store = RecordingStore()
    out = cli.apply_grants(store, settings, app_sp="1234-abcd", reviewer_group="kpi-reviewers", consumer_group=None)
    assert out["applied"] == len(store.statements) and out["skipped"] > 0
    assert all("${" not in s for s in store.statements)
    assert any(s.startswith("GRANT USE SCHEMA, SELECT, MODIFY ON SCHEMA main.kpi_evidence TO `1234-abcd`")
               for s in store.statements)
    with pytest.raises(ValueError, match="invalid principal"):
        cli.apply_grants(store, settings, app_sp="x`; DROP", reviewer_group=None, consumer_group=None)


def test_consumer_views_render_the_schema(local_env):
    store = RecordingStore()
    views = cli.apply_consumer_views(store, local_env)
    assert views == ["kpi_evidence.kpi_status_overview"]
    assert all("${schema}" not in s for s in store.statements)


def test_update_genie_space_replaces_placeholders():
    settings = Settings.from_env(backend="databricks", catalog="main", schema="kpi_evidence")
    calls = {}

    class Genie:
        def get_space(self, space_id, include_serialized_space=False):
            return SimpleNamespace(serialized_space='{"t": "__KPI_CATALOG__.__KPI_SCHEMA__.published_kpis_current"}')

        def update_space(self, space_id, serialized_space=None):
            calls["text"] = serialized_space

    client = SimpleNamespace(genie=Genie())
    assert cli.update_genie_space(settings, "abc", client=client) == {"updated": True}
    assert calls["text"] == '{"t": "main.kpi_evidence.published_kpis_current"}'
    assert cli.update_genie_space(settings, None) == {"skipped": "no genie_space_id"}
    broken = SimpleNamespace(genie=SimpleNamespace(get_space=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("off"))))
    result = cli.update_genie_space(settings, "abc", client=broken)
    assert result["updated"] is False and "RuntimeError: off" in result["error"]


def test_volume_dirs_and_seeding_locally(local_env):
    paths = cli.ensure_volume_dirs(local_env)
    assert all(Path(p).is_dir() for p in paths)
    written = cli.seed_samples(local_env)
    assert len(written) == 5 and all("/inbox/" in Path(p).as_posix() for p in written)
    assert cli.seed_samples(local_env) == written
