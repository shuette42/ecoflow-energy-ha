"""Tests for the project board sync script and its workflow."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github" / "tools" / "project_sync.py"
WORKFLOW = ROOT / ".github" / "workflows" / "project-sync.yml"

spec = importlib.util.spec_from_file_location("project_sync", SCRIPT)
assert spec is not None
assert spec.loader is not None
project_sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(project_sync)

EVENT_OPTIONS = ["Neu", "Bearbeitet", "Geschlossen", "Wieder geöffnet", "Kommentiert"]

FIELD_NODES: list[dict[str, Any]] = [
    {
        "id": "F_EVENT",
        "name": "Letztes Ereignis",
        "options": [
            {"id": f"O_{i}", "name": name} for i, name in enumerate(EVENT_OPTIONS)
        ],
    },
    {"id": "F_DATE", "name": "Letzte Änderung", "dataType": "DATE"},
    {"id": "F_TYPE", "name": "Typ", "options": [{"id": "O_ISSUE", "name": "Issue"}]},
    {
        "id": "F_CTRL",
        "name": "Steuerung",
        "options": [{"id": "O_WATCH", "name": "Nur beobachten"}],
    },
    {},
]


def project_with(nodes: list[dict]) -> dict:
    return {"id": "PROJECT", "fields": {"nodes": nodes}}


class FakeGraphQL:
    """Records calls and answers the three queries the script makes."""

    def __init__(self, nodes: list[dict] | None = None) -> None:
        self.project = project_with(FIELD_NODES if nodes is None else nodes)
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, query: str, **variables: object) -> dict:
        self.calls.append((query, variables))
        if "projectV2(number" in query:
            return {"user": {"projectV2": self.project}}
        if "addProjectV2ItemById" in query:
            return {"addProjectV2ItemById": {"item": {"id": "ITEM"}}}
        return {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "ITEM"}}}

    def kinds(self) -> list[str]:
        out = []
        for query, _ in self.calls:
            if "projectV2(number" in query:
                out.append("read")
            elif "addProjectV2ItemById" in query:
                out.append("add")
            else:
                out.append("set")
        return out

    def writes(self) -> list[dict]:
        return [v for q, v in self.calls if "updateProjectV2ItemFieldValue" in q]


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeGraphQL:
    fake = FakeGraphQL()
    monkeypatch.setattr(project_sync, "run_graphql", fake)
    return fake


@pytest.mark.parametrize(
    ("event", "action", "label"),
    [
        ("issues", "opened", "Neu"),
        ("issues", "edited", "Bearbeitet"),
        ("issues", "closed", "Geschlossen"),
        ("issues", "reopened", "Wieder geöffnet"),
        ("issue_comment", "created", "Kommentiert"),
    ],
)
def test_each_tracked_event_stamps_its_label_and_date(event, action, label):
    updates = project_sync.plan_updates(event, action, "2026-10-07")
    assert updates[:2] == [
        ("Letztes Ereignis", label),
        ("Letzte Änderung", "2026-10-07"),
    ]


def test_untracked_event_plans_nothing():
    assert project_sync.plan_updates("issues", "labeled", "2026-10-07") == []


def test_only_opened_sets_type_and_control_defaults():
    opened = dict(project_sync.plan_updates("issues", "opened", "2026-10-07"))
    edited = dict(project_sync.plan_updates("issues", "edited", "2026-10-07"))
    assert opened["Typ"] == "Issue"
    assert opened["Steuerung"] == "Nur beobachten"
    assert "Typ" not in edited
    assert "Steuerung" not in edited


def test_edited_issue_writes_option_and_date_to_the_board(fake):
    assert project_sync.sync("issues", "edited", "ISSUE_NODE", "2026-10-07") == 2
    values = {w["field"]: w["value"] for w in fake.writes()}
    assert values == {
        "F_EVENT": {"singleSelectOptionId": "O_1"},
        "F_DATE": {"date": "2026-10-07"},
    }
    assert all(w["item"] == "ITEM" and w["project"] == "PROJECT" for w in fake.writes())


def test_issue_is_added_before_any_field_is_written(fake):
    project_sync.sync("issues", "opened", "ISSUE_NODE", "2026-10-07")
    kinds = fake.kinds()
    assert kinds[:2] == ["read", "add"]
    assert set(kinds[2:]) == {"set"}
    assert fake.calls[1][1]["content"] == "ISSUE_NODE"


def test_opened_issue_gets_four_fields(fake):
    assert project_sync.sync("issues", "opened", "ISSUE_NODE", "2026-10-07") == 4


def test_untracked_event_touches_nothing(fake):
    assert project_sync.sync("issues", "labeled", "ISSUE_NODE", "2026-10-07") == 0
    assert fake.calls == []


def test_missing_stamp_field_fails_instead_of_passing_quietly(monkeypatch):
    nodes = [n for n in FIELD_NODES if n.get("name") != "Letzte Änderung"]
    broken = FakeGraphQL(nodes)
    monkeypatch.setattr(project_sync, "run_graphql", broken)
    with pytest.raises(RuntimeError, match="Letzte Änderung"):
        project_sync.sync("issues", "edited", "ISSUE_NODE", "2026-10-07")
    assert "add" not in broken.kinds()


def test_missing_default_field_is_skipped_but_stamps_still_land(monkeypatch, capsys):
    nodes = [n for n in FIELD_NODES if n.get("name") != "Steuerung"]
    monkeypatch.setattr(project_sync, "run_graphql", FakeGraphQL(nodes))
    assert project_sync.sync("issues", "opened", "ISSUE_NODE", "2026-10-07") == 3
    assert "Steuerung" in capsys.readouterr().out


def test_unknown_option_name_is_an_error():
    field = {"id": "F", "type": "SINGLE_SELECT", "options": {"Neu": "O"}}
    with pytest.raises(RuntimeError, match="no option 'Bearbeitet'"):
        project_sync.to_field_value("Letztes Ereignis", field, "Bearbeitet")


def test_main_reports_failure_through_the_exit_code(monkeypatch, capsys):
    monkeypatch.setenv("EVENT_NAME", "issues")
    monkeypatch.setenv("EVENT_ACTION", "edited")
    monkeypatch.setenv("ISSUE_NODE_ID", "ISSUE_NODE")

    def boom(*_args, **_kwargs):
        raise RuntimeError("token rejected")

    monkeypatch.setattr(project_sync, "run_graphql", boom)
    assert project_sync.main() == 1
    assert "::error::token rejected" in capsys.readouterr().out


def test_main_fails_when_the_event_environment_is_missing(monkeypatch):
    monkeypatch.delenv("EVENT_NAME", raising=False)
    assert project_sync.main() == 1


def test_workflow_never_interpolates_event_text_into_the_shell():
    text = WORKFLOW.read_text()
    run_blocks = re.findall(r"^\s+run:\s*(.+)$", text, flags=re.MULTILINE)
    assert run_blocks, "workflow has no run step"
    assert not any("${{" in block for block in run_blocks)
    assert "github.event.issue.title" not in text
    assert "github.event.issue.body" not in text
    assert "github.event.comment" not in text


def test_workflow_skips_pull_requests_and_other_repositories():
    text = WORKFLOW.read_text()
    assert "!github.event.issue.pull_request" in text
    assert "github.repository ==" in text


def test_workflow_requests_read_only_repository_permissions():
    text = WORKFLOW.read_text()
    assert re.search(r"^permissions:\n  contents: read$", text, flags=re.MULTILINE)
