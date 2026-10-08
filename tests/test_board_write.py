"""Tests for the narrow board write tool used by the daily triage run."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "board_write", ROOT / ".github" / "tools" / "board_write.py"
)
assert spec is not None
assert spec.loader is not None
bw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bw)

FIELDS = {
    "Status": {"id": "F_S", "type": "SINGLE_SELECT", "options": {"Wartet": "S_W"}},
    "Wartet auf": {
        "id": "F_W",
        "type": "SINGLE_SELECT",
        "options": {"Release": "W_R", "Hardwaredaten": "W_H"},
    },
    "Nächster Schritt": {"id": "F_N", "type": "TEXT", "options": {}},
    "Zielversion": {"id": "F_Z", "type": "TEXT", "options": {}},
}


class FakeBoard:
    def __init__(self, labels: list[str], state: str = "OPEN", on_board: bool = True):
        self.labels = labels
        self.state = state
        self.on_board = on_board
        self.writes: list[dict] = []
        self.added = False

    def __call__(self, query: str, **variables: object) -> dict:
        if "repository(owner" in query:
            items = (
                [
                    {
                        "id": "ITEM",
                        "project": {"id": "PROJECT"},
                        "fieldValues": {"nodes": []},
                    }
                ]
                if self.on_board
                else []
            )
            return {
                "repository": {
                    "issue": {
                        "id": "ISSUE",
                        "state": self.state,
                        "labels": {"nodes": [{"name": n} for n in self.labels]},
                        "projectItems": {"nodes": items},
                    }
                }
            }
        if "addProjectV2ItemById" in query:
            self.added = True
            return {"addProjectV2ItemById": {"item": {"id": "NEW_ITEM"}}}
        self.writes.append(variables)
        return {}


@pytest.fixture
def board(monkeypatch: pytest.MonkeyPatch):
    def make(labels, **kwargs) -> FakeBoard:
        fake = FakeBoard(labels, **kwargs)
        monkeypatch.setattr(bw.ps, "run_graphql", fake)
        monkeypatch.setattr(bw.ps, "load_project", lambda: ("PROJECT", FIELDS))
        return fake

    return make


def test_owner_fields_are_not_writable():
    for field in ("Steuerung", "Typ", "Letztes Ereignis"):
        with pytest.raises(RuntimeError, match="not writable"):
            bw.check_value(field, "x")


def test_select_values_are_checked_against_the_allowed_set():
    assert bw.check_value("Wartet auf", "Release") == "Release"
    with pytest.raises(RuntimeError, match="not allowed"):
        bw.check_value("Status", "In Arbeit")


def test_text_is_flattened_and_capped():
    value = bw.check_value("Nächster Schritt", "line one\n\tline two" + "x" * 500)
    assert "\n" not in value
    assert value.startswith("line one line two")
    assert len(value) == bw.TEXT_LIMIT


def test_evidence_field_takes_a_longer_text_than_the_default_cap():
    long_text = "word " * 600
    value = bw.check_value("Stand / Beleg", long_text)
    assert len(value) == bw.TEXT_LIMITS["Stand / Beleg"] == 1024
    assert bw.check_value("Stand / Beleg", "a\nb") == "a\nb"  # paragraphs kept
    # the other free-text fields keep the short cap
    assert len(bw.check_value("Nächster Schritt", long_text)) == bw.TEXT_LIMIT == 400


def test_target_version_must_be_a_version():
    assert bw.check_value("Zielversion", "1.25.0-beta.17") == "1.25.0-beta.17"
    with pytest.raises(RuntimeError, match="Zielversion"):
        bw.check_value("Zielversion", "next week")


def test_text_field_is_written_as_text(board):
    fake = board(["enhancement"])
    assert bw.set_field(296, "Nächster Schritt", "Wait for the R631 report") == 0
    assert fake.writes == [
        {
            "project": "PROJECT",
            "item": "ITEM",
            "field": "F_N",
            "value": {"text": "Wait for the R631 report"},
        }
    ]


def test_status_is_refused_while_labels_decide_it(board):
    fake = board(["bug", "action:respond"])
    with pytest.raises(RuntimeError, match="change the label"):
        bw.set_field(458, "Status", "Wartet")
    assert fake.writes == []


def test_status_is_allowed_when_no_label_decides(board):
    fake = board(["enhancement", "priority:p2"])
    bw.set_field(296, "Status", "Wartet")
    assert fake.writes[0]["value"] == {"singleSelectOptionId": "S_W"}


def test_wait_reason_survives_needs_info_but_not_a_claim(board):
    fake = board(["needs-info"])
    bw.set_field(434, "Wartet auf", "Hardwaredaten")
    assert fake.writes
    board(["action:bugfix"])
    with pytest.raises(RuntimeError, match="change the label"):
        bw.set_field(434, "Wartet auf", "Release")


def test_closed_issue_is_refused(board):
    fake = board(["bug"], state="CLOSED")
    with pytest.raises(RuntimeError, match="closed"):
        bw.set_field(437, "Nächster Schritt", "x")
    assert fake.writes == []


def test_issue_missing_from_the_board_is_added_first(board):
    fake = board(["enhancement"], on_board=False)
    bw.set_field(510, "Zielversion", "1.26.0")
    assert fake.added
    assert fake.writes[0]["item"] == "NEW_ITEM"


def test_http_transport_needs_the_token(monkeypatch):
    monkeypatch.delenv("BOARD_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="BOARD_TOKEN is not set"):
        bw.http_graphql("query { viewer { login } }")


def test_main_reports_errors_without_a_traceback(board, capsys):
    board(["bug", "action:respond"])
    assert bw.main(["set", "458", "Status", "Wartet"]) == 1
    assert "change the label" in capsys.readouterr().err


def test_main_rejects_unknown_usage(capsys):
    assert bw.main(["delete", "296"]) == 64
    assert "usage" in capsys.readouterr().err


def test_notice_field_keeps_paragraphs_but_not_control_characters():
    value = bw.check_value("Stand / Beleg", "Hi,\r\n\r\nline two\x07 here.\nend")
    assert value == "Hi,\n\nline two here.\nend"
    assert bw.check_value("Nächster Schritt", "a\nb") == "a b"
