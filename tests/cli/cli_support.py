from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location("kpi_cli_check_standins", ROOT / "tests" / "check" / "check_standins.py")
standins = importlib.util.module_from_spec(_SPEC)
sys.modules["kpi_cli_check_standins"] = standins
_SPEC.loader.exec_module(standins)

REPORTS = standins.REPORTS
install_standins = standins.install_standins


class RecordingStore:
    def __init__(self):
        self.statements: list[str] = []
        self.inserted: list = []

    def execute(self, sql, params=None):
        self.statements.append(sql)

    def execute_script(self, script):
        from kpi_evidence.store.base import split_sql

        self.statements.extend(split_sql(script))

    def insert(self, records):
        self.inserted.extend(records)
        return len(records)
