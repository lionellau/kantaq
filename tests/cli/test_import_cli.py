"""``kantaq import linear`` CLI glue (E23-T3 / DEBT-33).

The import LOGIC is covered by ``apps/local-runtime/tests/test_linear_import.py``
(it calls ``import_linear`` directly). These tests pin the CLI *wrapper*
``cmd_import`` — the seam the unit tests skip, where the v0.2 UAT found two
defects (F-01/F-02):

* **F-01** — the success-path summary print used to run after the ``with
  Session`` block closed, detaching the ``project`` ORM instance and raising
  ``DetachedInstanceError`` on a *successful* import (rows commit, then the
  command crashes). The crash test below fails without the fix.
* **F-02** — the wrapper created a fresh project on every run, so re-running the
  documented command orphaned empty duplicate projects. The idempotency test
  asserts a second run reuses the same project (and imports zero new tickets).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlmodel import Session, SQLModel, select

from kantaq.cli import main
from kantaq_core.identity import IdentityService
from kantaq_db.models import Project
from kantaq_db.session import get_engine, sqlite_url


def _seed_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    """A booted local runtime: a workspace + an active Owner, the minimum
    ``cmd_import`` needs. Returns the configured DB path."""
    db_path = tmp_path / "data" / "local.sqlite"
    engine = get_engine(sqlite_url(db_path))
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        assert IdentityService(session).bootstrap_owner() is not None
    monkeypatch.chdir(tmp_path)  # away from any developer .env
    monkeypatch.setenv("HUB_MODE", "local")
    monkeypatch.setenv("LOCAL_DB_PATH", str(db_path))
    return str(db_path)


def _export(tmp_path: Path) -> Path:
    """A minimal two-ticket Linear export (one is an epic + a parent link)."""
    path = tmp_path / "linear-export.json"
    path.write_text(
        json.dumps(
            {
                "tickets": [
                    {"id": "L-1", "title": "[Epic] Onboarding", "status": "Backlog"},
                    {
                        "id": "L-2",
                        "title": "Write the QUICKSTART",
                        "status": "In Progress",
                        "parent": "L-1",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    return path


def _projects_named(db_path: str, name: str) -> list[Project]:
    with Session(get_engine(sqlite_url(db_path))) as session:
        return [p for p in session.exec(select(Project)).all() if p.name == name]


def test_import_linear_succeeds_and_prints_summary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """F-01: a successful import exits 0 and prints the summary — it must not
    crash on the success path (DetachedInstanceError). This fails without the
    fix that captures ``project.name`` inside the session."""
    db_path = _seed_runtime(monkeypatch, tmp_path)
    export = _export(tmp_path)

    rc = main(["import", "linear", str(export), "--project", "UAT Linear Import"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "imported 2 tickets" in out
    assert "into 'UAT Linear Import'" in out
    assert len(_projects_named(db_path, "UAT Linear Import")) == 1


def test_import_linear_is_project_idempotent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """F-02: re-running the documented command reuses the same project instead of
    orphaning a fresh empty duplicate, and imports zero new tickets the 2nd run."""
    db_path = _seed_runtime(monkeypatch, tmp_path)
    export = _export(tmp_path)

    assert main(["import", "linear", str(export), "--project", "UAT Linear Import"]) == 0
    capsys.readouterr()  # drain the first run's summary

    assert main(["import", "linear", str(export), "--project", "UAT Linear Import"]) == 0
    out = capsys.readouterr().out
    assert "imported 0 tickets" in out  # ticket idempotency (D-19)
    assert "skipped 2 tickets" in out
    assert len(_projects_named(db_path, "UAT Linear Import")) == 1  # no duplicate project
