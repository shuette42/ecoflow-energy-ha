"""Tests for the discussion board sync script and its workflow. No network: the board
and the repository are replaced by plain values and a fake GraphQL layer."""

from __future__ import annotations

import datetime
import importlib.util
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github" / "tools" / "discussion_sync.py"
WORKFLOW = ROOT / ".github" / "workflows" / "discussion-sync.yml"

_spec = importlib.util.spec_from_file_location("discussion_sync", SCRIPT)
assert _spec is not None
assert _spec.loader is not None
bd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bd)

TODAY = "2026-10-21"
ME = "shuette42"


def disc(
    number=1,
    closed=False,
    closed_at=None,
    author="alice",
    comments=(),
    title="T",
    created="2026-10-01T10:00:00Z",
    reply=None,
    updated="2026-10-20T00:00:00Z",
):
    """A GraphQL discussion node parsed the way the script does.

    comments: (login, createdAt) pairs; reply: (comment index, login, createdAt).
    """
    nodes = [
        {"author": {"login": a}, "createdAt": t, "replies": {"nodes": []}}
        for a, t in comments
    ]
    if reply is not None:
        index, login, at = reply
        nodes[index]["replies"] = {
            "nodes": [{"author": {"login": login}, "createdAt": at}]
        }
    return bd.parse_discussion(
        {
            "number": number,
            "title": title,
            "url": f"{bd.SOURCE_PREFIX}{number}",
            "closed": closed,
            "closedAt": closed_at,
            "createdAt": created,
            "updatedAt": updated,
            "author": {"login": author},
            "comments": {"totalCount": len(nodes), "nodes": nodes},
        }
    )


def card(
    number=1,
    status=None,
    waiting=None,
    event=None,
    changed=None,
    next_step=None,
    title=None,
    source="",
    assignees=(),
    item=None,
    body="",
    typ="Discussion",
):
    return bd.Card(
        item_id=item or f"item-{number}",
        draft_id=f"draft-{number}",
        title=title if title is not None else f"Discussion #{number}: T",
        body=body,
        typ=typ,
        status=status,
        waiting=waiting,
        event=event,
        changed=changed,
        source=source or f"{bd.SOURCE_PREFIX}{number}",
        next_step=next_step,
        assignees=tuple(assignees),
    )


def settled(d):
    """The card a discussion's own plan_changes output would produce."""
    (change,), _ = bd.plan_changes([d], {}, TODAY)
    v = change.values
    return card(
        d.number,
        v["Status"],
        v["Wartet auf"],
        v["Letztes Ereignis"],
        v["Letzte Änderung"],
        v.get("Nächster Schritt"),
        title=change.title,
        assignees=change.assignees or (),
    )


# ------------------------------------------------------------- derivation


def test_closed_discussion_is_abgeschlossen_with_the_close_date():
    w = bd.derive(
        disc(
            closed=True,
            closed_at="2026-08-29T07:00:00Z",
            comments=[("alice", "2026-10-02T10:00:00Z")],
        ),
        TODAY,
    )
    assert w.values == {
        "Status": "Abgeschlossen",
        "Wartet auf": "Keine",
        "Letztes Ereignis": "Geschlossen",
        "Letzte Änderung": "2026-08-29",
    }
    assert w.next_step is None  # hand text is the history
    assert w.assignees == ()


def test_open_discussion_is_never_abgeschlossen():
    # Negative control for the closed rule: open is never Abgeschlossen.
    w = bd.derive(disc(closed=False), TODAY)
    assert w.values["Status"] != "Abgeschlossen"
    assert w.values["Letztes Ereignis"] != "Geschlossen"


def test_someone_else_last_means_we_owe_a_reply():
    d = disc(
        author="alice",
        comments=[(ME, "2026-10-05T10:00:00Z"), ("alice", "2026-10-19T10:45:00Z")],
    )
    w = bd.derive(d, TODAY)
    assert w.values == {
        "Status": "Bereit",
        "Wartet auf": "Keine",
        "Letztes Ereignis": "Kommentiert",
        "Letzte Änderung": "2026-10-19",
    }
    assert w.assignees == (ME,)
    assert w.next_step == "Antworten: letzter Beitrag von @alice am 2026-10-19"


def test_we_spoke_last_means_the_reporter_owes():
    d = disc(
        author="alice",
        comments=[("alice", "2026-10-05T10:00:00Z"), (ME, "2026-10-11T10:00:00Z")],
    )
    w = bd.derive(d, TODAY)
    assert w.values["Status"] == "Wartet"
    assert w.values["Wartet auf"] == "Reporter"
    assert w.values["Letzte Änderung"] == "2026-10-11"
    assert w.assignees == ()
    assert w.next_step == "Wartet auf @alice seit 2026-10-11"


def test_the_bot_counts_as_us():
    d = disc(author="alice", comments=[("shuette42-bot", "2026-10-11T10:00:00Z")])
    assert bd.derive(d, TODAY).values["Status"] == "Wartet"


def test_own_post_without_comments_is_open_for_answers():
    w = bd.derive(disc(author=ME, created="2026-09-12T08:00:00Z"), TODAY)
    assert w.values["Status"] == "Wartet"
    assert w.values["Letztes Ereignis"] == "Neu"
    assert w.values["Letzte Änderung"] == "2026-09-12"
    assert w.next_step == "Eigener Beitrag, offen für Antworten"


def test_a_foreign_post_without_comments_is_new_and_owed():
    w = bd.derive(disc(author="alice", created="2026-10-20T08:00:00Z"), TODAY)
    assert w.values["Status"] == "Bereit"
    assert w.values["Letztes Ereignis"] == "Neu"


def test_a_reply_newer_than_the_last_comment_decides():
    # Our comment is the last top-level one; alice replied under an older one later.
    d = disc(
        author="alice",
        comments=[("alice", "2026-10-02T10:00:00Z"), (ME, "2026-10-12T10:00:00Z")],
        reply=(0, "alice", "2026-10-15T10:00:00Z"),
    )
    w = bd.derive(d, TODAY)
    assert w.values["Status"] == "Bereit"
    assert w.values["Letzte Änderung"] == "2026-10-15"
    # Negative control: without the reply the same discussion is ours.
    plain = disc(
        author="alice",
        comments=[("alice", "2026-10-02T10:00:00Z"), (ME, "2026-10-12T10:00:00Z")],
    )
    assert bd.derive(plain, TODAY).values["Status"] == "Wartet"


def test_updated_at_is_not_activity():
    d = disc(
        author="alice",
        created="2026-07-01T10:00:00Z",
        comments=[("alice", "2026-07-25T10:00:00Z")],
        updated="2026-10-20T00:00:00Z",
    )
    assert bd.derive(d, TODAY).values["Letzte Änderung"] == "2026-07-25"


def test_a_deleted_account_is_someone_else():
    d = bd.parse_discussion(
        {
            "number": 7,
            "title": "T",
            "url": "u",
            "closed": False,
            "closedAt": None,
            "createdAt": "2026-10-01T10:00:00Z",
            "author": None,
            "comments": {"totalCount": 0, "nodes": []},
        }
    )
    assert d.author == "ghost"
    assert bd.derive(d, TODAY).values["Status"] == "Bereit"


def test_waiting_on_is_the_newest_outside_voice_for_a_maintainer_post():
    d = disc(
        author=ME,
        comments=[
            ("bob", "2026-10-02T10:00:00Z"),
            ("carol", "2026-10-03T10:00:00Z"),
            (ME, "2026-10-04T10:00:00Z"),
        ],
    )
    assert bd.waiting_on(d) == "carol"
    assert bd.waiting_on(disc(author=ME)) is None
    assert (
        bd.waiting_on(disc(author="alice", comments=[("bob", "2026-10-02T10:00:00Z")]))
        == "alice"
    )


# ------------------------------------------------------- Nächster Schritt


def test_hand_text_in_next_step_is_never_replaced():
    d = disc(author="alice", comments=[("alice", "2026-10-19T10:45:00Z")])
    assert (
        bd.derive(d, TODAY, "ask the reporter for the meter reading").next_step is None
    )


def test_script_text_in_next_step_follows_the_state():
    d = disc(author="alice", comments=[("alice", "2026-10-19T10:45:00Z")])
    for old in (
        "Wartet auf @alice seit 2026-10-11",
        "Eigener Beitrag, offen für Antworten",
        "Antworten: letzter Beitrag von @bob am 2026-10-01",
    ):
        assert (
            bd.derive(d, TODAY, old).next_step
            == "Antworten: letzter Beitrag von @alice am 2026-10-19"
        )


def test_empty_next_step_is_filled():
    d = disc(author="alice", comments=[("alice", "2026-10-19T10:45:00Z")])
    assert bd.derive(d, TODAY, "").next_step is not None
    assert bd.derive(d, TODAY, None).next_step is not None


# ----------------------------------------------------------- card matching


def test_card_number_prefers_the_source_url_over_the_title():
    c = card(1, source=f"{bd.SOURCE_PREFIX}509", title="Discussion #12: x")
    assert bd.card_number(c) == 509


def test_card_number_falls_back_to_the_title_and_can_fail():
    assert (
        bd.card_number(
            card(1, source="", title="Discussion #12: x")._replace(source=None)
        )
        == 12
    )
    assert (
        bd.card_number(
            card(1, title="PLAN-181: x")._replace(source="https://example.org/x")
        )
        is None
    )


def test_card_from_node_skips_non_discussion_cards():
    node = {
        "id": "i",
        "content": {"__typename": "DraftIssue", "id": "d", "title": "PLAN-1: x"},
        "typ": {"name": "Issue"},
    }
    assert bd.card_from_node(node) is None
    node["typ"] = {"name": "Discussion"}
    assert bd.card_from_node(node) is not None
    assert (
        bd.card_from_node(
            {
                "id": "i",
                "content": {"__typename": "Issue"},
                "typ": {"name": "Discussion"},
            }
        )
        is None
    )


# ---------------------------------------------------------------- planning


def test_missing_card_is_created_with_type_source_and_no_steuerung():
    d = disc(7, author="alice", comments=[("alice", "2026-10-19T10:45:00Z")])
    (change,), _ = bd.plan_changes([d], {}, TODAY)
    assert change.action == "create"
    assert change.title == "Discussion #7: T"
    assert change.values["Typ"] == "Discussion"
    assert change.values["Quelle"] == f"{bd.SOURCE_PREFIX}7"
    assert "Steuerung" not in change.values
    assert change.assignees == (ME,)


def test_update_never_writes_the_create_only_fields():
    d = disc(author="alice", comments=[("alice", "2026-10-19T10:45:00Z")])
    (change,), _ = bd.plan_changes([d], {1: card(1, "Eingang", "Keine")}, TODAY)
    assert change.action == "update"
    assert "Typ" not in change.values and "Quelle" not in change.values
    assert change.values["Status"] == "Bereit"
    assert change.values["Letzter Abgleich"] == TODAY


def test_a_settled_card_means_no_change_and_a_second_run_is_empty():
    ds = [
        disc(1, author="alice", comments=[("alice", "2026-10-19T10:45:00Z")]),
        disc(2, closed=True, closed_at="2026-08-29T07:00:00Z", author="bob"),
        disc(3, author=ME, created="2026-09-12T08:00:00Z"),
    ]
    cards = {d.number: settled(d) for d in ds}
    assert bd.plan_changes(ds, cards, TODAY)[0] == []
    # Positive control: one differing field on one card is exactly one change.
    cards[1] = cards[1]._replace(status="Wartet")
    (change,), _ = bd.plan_changes(ds, cards, TODAY)
    assert change.number == 1 and change.values["Status"] == "Bereit"


def test_closed_card_with_hand_text_keeps_it_and_only_the_stamps_change():
    d = disc(4, closed=True, closed_at="2026-10-11T07:00:00Z")
    c = card(4, "Abgeschlossen", "Keine", None, None, next_step="hand text")
    (change,), _ = bd.plan_changes([d], {4: c}, TODAY)
    assert set(change.values) == {
        "Letztes Ereignis",
        "Letzte Änderung",
        "Letzter Abgleich",
    }
    assert change.assignees is None


def test_assignee_is_set_for_owed_and_cleared_for_waiting():
    owed = disc(1, author="alice", comments=[("alice", "2026-10-19T10:45:00Z")])
    ours = disc(2, author="alice", comments=[(ME, "2026-10-19T10:45:00Z")])
    cards = {
        1: settled(owed)._replace(assignees=()),
        2: settled(ours)._replace(assignees=(ME,)),
    }
    changes, _ = bd.plan_changes([owed, ours], cards, TODAY)
    assert {c.number: c.assignees for c in changes} == {1: (ME,), 2: ()}


def test_retitle_when_the_discussion_title_changed():
    d = disc(
        title="new title", author="alice", comments=[("alice", "2026-10-19T10:45:00Z")]
    )
    (change,), _ = bd.plan_changes(
        [d], {1: settled(d)._replace(title="Discussion #1: old")}, TODAY
    )
    assert change.retitle and change.title == "Discussion #1: new title"


def test_card_without_a_discussion_is_reported_and_left_alone():
    changes, notes = bd.plan_changes([], {9: card(9)}, TODAY)
    assert changes == [] and any("#9" in n for n in notes)


def test_check_own_refuses_steuerung_and_create_only_fields_on_update():
    bd.check_own(
        {"Status": "Bereit", "Nächster Schritt": "x", "Letzter Abgleich": TODAY}
    )
    with pytest.raises(RuntimeError, match="Steuerung"):
        bd.check_own({"Steuerung": "Freigegeben"}, creating=True)
    with pytest.raises(RuntimeError):
        bd.check_own({"Typ": "Discussion"})
    bd.check_own({"Typ": "Discussion", "Quelle": "u"}, creating=True)


# --------------------------------------------- fake GraphQL: dry run and apply


def item_node(c):
    def sel(v):
        return {"name": v} if v is not None else None

    return {
        "id": c.item_id,
        "content": {
            "__typename": "DraftIssue",
            "id": c.draft_id,
            "title": c.title,
            "body": c.body,
            "assignees": {"nodes": [{"login": a} for a in c.assignees]},
        },
        "typ": sel(c.typ),
        "status": sel(c.status),
        "waiting": sel(c.waiting),
        "event": sel(c.event),
        "changed": {"date": c.changed} if c.changed else None,
        "source": {"text": c.source},
        "next": {"text": c.next_step} if c.next_step else None,
    }


def disc_node(
    number,
    closed=False,
    closed_at=None,
    author="alice",
    comments=(),
    created="2026-10-01T10:00:00Z",
    total=None,
):
    return {
        "number": number,
        "title": "T",
        "url": f"{bd.SOURCE_PREFIX}{number}",
        "closed": closed,
        "closedAt": closed_at,
        "createdAt": created,
        "author": {"login": author},
        "comments": {
            "totalCount": len(comments) if total is None else total,
            "nodes": [
                {"author": {"login": a}, "createdAt": t, "replies": {"nodes": []}}
                for a, t in comments
            ],
        },
    }


class FakeBoard:
    """Answers the script's queries. The item listing never shows a card made during
    the test, which is how the real one behaves for minutes after a write."""

    def __init__(self, nodes, cards, page_size=None):
        self.nodes, self.cards, self.calls = nodes, cards, []
        self.page_size = page_size
        self.fail_field = None
        self.written = []

    def page(self, rows, cursor):
        start = int(cursor) if cursor else 0
        size = self.page_size or max(len(rows), 1)
        end = start + size
        return {
            "pageInfo": {"hasNextPage": end < len(rows), "endCursor": str(end)}
        }, rows[start:end]

    def __call__(self, query, **variables):
        self.calls.append((query, variables))
        if query == bd.DISCUSSIONS_QUERY:
            info, rows = self.page(self.nodes, variables.get("cursor"))
            return {"repository": {"discussions": {**info, "nodes": rows}}}
        if query == bd.DISCUSSION_QUERY:
            hit = [n for n in self.nodes if n["number"] == variables["number"]]
            return {"repository": {"discussion": hit[0] if hit else None}}
        if query == bd.ITEMS_QUERY:
            info, rows = self.page(
                [item_node(c) for c in self.cards], variables.get("cursor")
            )
            return {"user": {"projectV2": {"items": {**info, "nodes": rows}}}}
        if query == bd.ADD_DRAFT_MUTATION:
            return {"addProjectV2DraftIssue": {"projectItem": {"id": "new-item"}}}
        if query == bd.USER_QUERY:
            return {"user": {"id": "U1"}}
        if query == bd.ps.SET_VALUE_MUTATION:
            if variables["field"] == self.fail_field:
                raise RuntimeError("boom")
            self.written.append(variables["field"])
        return {}


@pytest.fixture
def board(monkeypatch):
    def make(nodes, cards, page_size=None):
        fake = FakeBoard(nodes, cards, page_size)
        monkeypatch.setattr(bd.ps, "run_graphql", fake)
        monkeypatch.setattr(
            bd.ps,
            "load_project",
            lambda: (
                "P1",
                {
                    n: {"id": f"F-{n}", "type": "TEXT", "options": {}}
                    for n in bd.OWN_FIELDS + bd.CREATE_ONLY_FIELDS
                },
            ),
        )
        return fake

    return make


def test_dry_run_lists_one_line_per_change_and_exits_1(board, capsys):
    fake = board(
        [
            disc_node(1, closed=True, closed_at="2026-08-29T07:00:00Z"),
            disc_node(2, comments=[("alice", "2026-10-19T10:45:00Z")]),
        ],
        [card(1, "Eingang", "Keine")],
    )
    assert bd.cmd_sync(False, TODAY) == 1
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("update #1 [Status: 'Eingang' -> 'Abgeschlossen'")
    assert out[1].startswith("create Discussion #2: T [Typ='Discussion', Quelle=")
    assert out[-1] == "1 create, 1 update pending (dry run)"
    assert not any(
        "Mutation" in q or "mutation" in q for q, _ in fake.calls
    )  # nothing written


def test_dry_run_on_a_settled_board_prints_zero_changes_and_exits_0(board, capsys):
    nodes = [disc_node(1, closed=True, closed_at="2026-08-29T07:00:00Z")]
    board(nodes, [settled(bd.parse_discussion(nodes[0]))])
    assert bd.cmd_sync(False, TODAY) == 0
    assert capsys.readouterr().out.strip() == "0 changes"


def test_apply_writes_an_update_through_the_owned_fields_only(board):
    fake = board(
        [disc_node(1, closed=True, closed_at="2026-08-29T07:00:00Z")],
        [card(1, "Eingang", "Keine")],
    )
    assert bd.cmd_sync(True, TODAY) == 0
    written = {v["field"] for q, v in fake.calls if q == bd.ps.SET_VALUE_MUTATION}
    assert written == {
        "F-Status",
        "F-Letztes Ereignis",
        "F-Letzte Änderung",
        "F-Letzter Abgleich",
    }
    assert "F-Steuerung" not in written


# ------------------------------------------------------------------- view


def test_view_names_a_missing_field_and_touches_nothing(monkeypatch):
    calls = []

    def fake(query, **variables):
        calls.append(query)
        return {}

    monkeypatch.setattr(bd.ps, "run_graphql", fake)
    monkeypatch.setattr(bd.ps, "load_project", lambda: ("P1", {"Status": {"id": "x"}}))
    with pytest.raises(RuntimeError, match="Nächster Schritt"):
        bd.cmd_view(True)
    assert calls == []


def test_view_dry_run_reports_drift_and_apply_reads_back(monkeypatch, capsys):
    fields = {n: {"id": f"id-{n}"} for n in bd.VIEW_FIELDS}
    state = {"filter": "typ:Discussion", "ids": ["id-Title"]}

    def fake(query, **v):
        if query == bd.VIEW_QUERY:
            return {
                "node": {
                    "filter": state["filter"],
                    "visibleFields": {"nodes": [{"id": i} for i in state["ids"]]},
                }
            }
        state["filter"], state["ids"] = v["filter"], v["fields"]
        return {}

    monkeypatch.setattr(bd.ps, "run_graphql", fake)
    monkeypatch.setattr(bd.ps, "load_project", lambda: ("P1", fields))
    assert bd.cmd_view(False) == 1
    assert state["filter"] == "typ:Discussion"  # dry run wrote nothing
    assert bd.cmd_view(True) == 0
    assert state["filter"] == bd.VIEW_FILTER
    assert state["ids"] == [f"id-{n}" for n in bd.VIEW_FIELDS]
    capsys.readouterr()
    assert bd.cmd_view(False) == 0 and "0 changes" in capsys.readouterr().out


# ------------------------------------------------- the trigger gate on creating

OWED = [("alice", "2026-10-19T10:45:00Z")]
UTC = datetime.UTC


def created(fake):
    return [v for q, v in fake.calls if q == bd.ADD_DRAFT_MUTATION]


def queried(fake, query):
    return [v for q, v in fake.calls if q == query]


@pytest.mark.parametrize(
    ("event", "action", "allowed"),
    [
        ("", "", True),  # local run
        ("schedule", "", True),
        ("workflow_dispatch", "", True),
        ("discussion", "created", True),
        ("discussion", "closed", False),
        ("discussion", "reopened", False),
        ("discussion", "edited", False),
        ("discussion", "deleted", False),
        ("discussion_comment", "created", False),
        ("discussion_comment", "edited", False),
        ("discussion_comment", "deleted", False),
        ("issues", "created", False),
    ],
)
def test_only_schedule_dispatch_and_a_new_discussion_may_create(event, action, allowed):
    assert bd.may_create(event, action) is allowed


def test_an_event_run_reads_and_updates_only_its_own_discussion(board, capsys):
    nodes = [
        disc_node(1, closed=True, closed_at="2026-08-29T07:00:00Z"),
        disc_node(2, comments=OWED),
        disc_node(3, closed=True, closed_at="2026-08-30T07:00:00Z"),
    ]
    fake = board(nodes, [card(1, "Eingang", "Keine"), card(3, "Eingang", "Keine")])
    assert bd.cmd_sync(True, TODAY, "discussion_comment", "created", 1) == 0
    out = capsys.readouterr().out
    assert queried(fake, bd.DISCUSSIONS_QUERY) == []  # never the whole list
    assert [v["number"] for v in queried(fake, bd.DISCUSSION_QUERY)] == [1]
    assert "update #1" in out and "#3" not in out and "#2" not in out
    assert out.splitlines()[-1] == "0 creates, 1 update applied"
    assert created(fake) == []
    items = {v["item"] for q, v in fake.calls if q == bd.ps.SET_VALUE_MUTATION}
    assert items == {"item-1"}


def test_a_comment_event_does_not_create_its_missing_card(board, capsys):
    fake = board([disc_node(2, comments=OWED)], [])
    assert bd.cmd_sync(True, TODAY, "discussion_comment", "created", 2) == 0
    out = capsys.readouterr().out
    assert "#2: no card yet" in out and out.splitlines()[-1] == "0 changes"
    assert created(fake) == []


def test_a_close_event_does_not_create_a_missing_card_in_a_dry_run_either(
    board, capsys
):
    fake = board([disc_node(2, comments=OWED)], [])
    assert bd.cmd_sync(False, TODAY, "discussion", "closed", 2) == 0
    out = capsys.readouterr().out
    assert "no card yet" in out and out.splitlines()[-1] == "0 changes"
    assert created(fake) == []


@pytest.mark.parametrize(
    ("event", "action"),
    [
        ("", ""),
        ("schedule", ""),
        ("workflow_dispatch", ""),
        ("discussion", "created"),
    ],
)
def test_the_same_board_creates_the_card_on_a_creating_trigger(board, event, action):
    # Positive control for the gate: only the trigger differs from the tests above.
    fake = board([disc_node(2, comments=OWED)], [])
    number = 2 if event == "discussion" else None
    assert bd.cmd_sync(True, TODAY, event, action, number) == 0
    (call,) = created(fake)
    assert call["title"] == "Discussion #2: T"


def test_two_created_events_on_a_lagging_listing_add_one_card_per_number(board):
    # The listing never shows the card the first run made. Run two must still not
    # make it again: it reads discussion 3 only.
    fake = board([disc_node(2, comments=OWED), disc_node(3, comments=OWED)], [])
    assert bd.cmd_sync(True, TODAY, "discussion", "created", 2) == 0
    assert bd.cmd_sync(True, TODAY, "discussion", "created", 3) == 0
    assert [c["title"] for c in created(fake)] == [
        "Discussion #2: T",
        "Discussion #3: T",
    ]


def test_an_event_run_for_a_deleted_discussion_changes_nothing(board, capsys):
    fake = board([], [card(9, "Eingang", "Keine")])
    assert bd.cmd_sync(True, TODAY, "discussion", "deleted", 9) == 0
    out = capsys.readouterr().out
    assert "#9: no discussion any more" in out and out.splitlines()[-1] == "0 changes"
    assert not any(q == bd.ps.SET_VALUE_MUTATION for q, _ in fake.calls)


def test_an_event_run_without_a_discussion_number_stops(board):
    board([disc_node(2, comments=OWED)], [])
    with pytest.raises(RuntimeError, match="DISCUSSION_NUMBER"):
        bd.cmd_sync(True, TODAY, "discussion", "created", None)


def test_main_reads_the_trigger_and_number_from_the_environment(board, monkeypatch):
    nodes = [disc_node(2, comments=OWED), disc_node(3, comments=OWED)]
    fake = board(nodes, [])
    monkeypatch.setenv("EVENT_NAME", "discussion_comment")
    monkeypatch.setenv("EVENT_ACTION", "created")
    monkeypatch.setenv("DISCUSSION_NUMBER", "2")
    assert bd.main(["--apply"]) == 0
    assert created(fake) == []
    monkeypatch.setenv("EVENT_NAME", "discussion")
    monkeypatch.setenv("DISCUSSION_NUMBER", "3")
    assert bd.main(["--apply"]) == 0
    assert [c["title"] for c in created(fake)] == ["Discussion #3: T"]
    monkeypatch.delenv("EVENT_NAME")
    monkeypatch.delenv("EVENT_ACTION")
    monkeypatch.delenv("DISCUSSION_NUMBER")
    assert bd.main(["--apply"]) == 0
    assert len(created(fake)) == 3  # the full run takes both, no number needed


# --------------------------------------------- a full run leaves fresh posts alone


def test_a_full_run_skips_a_post_younger_than_the_grace_window(board, capsys):
    posted = "2026-10-21T10:00:00Z"
    fake = board([disc_node(2, created=posted, comments=[("alice", posted)])], [])
    soon = datetime.datetime(2026, 10, 21, 10, 20, tzinfo=UTC)
    assert bd.cmd_sync(True, TODAY, "schedule", "", None, soon) == 0
    out = capsys.readouterr().out
    assert "posted less than 30 minutes ago" in out
    assert created(fake) == []
    # Positive control: the same post, ten minutes past the window.
    later = datetime.datetime(2026, 10, 21, 10, 40, tzinfo=UTC)
    assert bd.cmd_sync(True, TODAY, "schedule", "", None, later) == 0
    assert len(created(fake)) == 1


def test_the_created_event_run_creates_a_fresh_post(board):
    posted = "2026-10-21T10:00:00Z"
    fake = board([disc_node(2, created=posted, comments=[("alice", posted)])], [])
    soon = datetime.datetime(2026, 10, 21, 10, 1, tzinfo=UTC)
    assert bd.cmd_sync(True, TODAY, "discussion", "created", 2, soon) == 0
    assert len(created(fake)) == 1


# ------------------------------------------------- order of a create's writes


def test_a_create_writes_typ_and_quelle_before_anything_else():
    d = disc(7, author="alice", comments=OWED)
    (change,), _ = bd.plan_changes([d], {}, TODAY)
    assert list(change.values)[:2] == ["Typ", "Quelle"]
    assert list(change.values)[-1] == "Letzter Abgleich"


def test_a_failed_status_write_leaves_typ_and_quelle_on_the_new_card(board):
    fake = board([disc_node(2, comments=OWED)], [])
    fake.fail_field = "F-Status"
    with pytest.raises(RuntimeError, match="boom"):
        bd.cmd_sync(True, TODAY, "schedule", "")
    assert fake.written == ["F-Typ", "F-Quelle"]


def test_a_draft_without_typ_is_found_by_its_title_and_completed(board):
    # What a create that stopped after the add leaves behind.
    half = card(2, typ=None, source=None, title="Discussion #2: T")
    half = half._replace(source=None)
    fake = board([disc_node(2, comments=OWED)], [half])
    assert bd.cmd_sync(True, TODAY, "schedule", "") == 0
    assert created(fake) == []
    assert {"F-Typ", "F-Quelle", "F-Status"} <= set(fake.written)


def test_card_from_node_takes_an_untyped_discussion_draft_but_not_a_stranger():
    def draft(title: str) -> dict:
        return {
            "id": "i",
            "content": {"__typename": "DraftIssue", "id": "d", "title": title},
        }

    mine = draft("Discussion #4: x")
    other = draft("PLAN-4: x")
    assert bd.card_from_node(mine) is not None
    assert bd.card_from_node(other) is None


# ----------------------------------------------------------------- pagination


def test_discussions_are_read_across_pages(board):
    nodes = [disc_node(n, comments=OWED) for n in (1, 2, 3)]
    fake = board(nodes, [], page_size=2)
    assert [d.number for d in bd.load_discussions()] == [1, 2, 3]
    cursors = [v["cursor"] for v in queried(fake, bd.DISCUSSIONS_QUERY)]
    assert cursors == [None, "2"]


def test_cards_are_read_across_pages(board):
    cards = [card(n, "Eingang", "Keine") for n in (1, 2, 3)]
    fake = board([], cards, page_size=2)
    assert [bd.card_number(c) for c in bd.load_cards()] == [1, 2, 3]
    cursors = [v["cursor"] for v in queried(fake, bd.ITEMS_QUERY)]
    assert cursors == [None, "2"]


# ------------------------------------------------------------ notes and titles


def test_a_discussion_over_100_comments_is_noted_even_before_its_card_exists():
    node = disc_node(5, comments=OWED, total=140)
    d = bd.parse_discussion(node)
    _, notes = bd.plan_changes([d], {}, TODAY)
    assert any("#5: 140 comments" in n for n in notes)
    _, quiet = bd.plan_changes([bd.parse_discussion(disc_node(6))], {}, TODAY)
    assert not any("comments" in n for n in quiet)  # negative control


def test_a_card_title_has_no_control_characters():
    d = disc(1, title="a\x00b\tc")
    assert bd.card_title(d) == "Discussion #1: ab c"


# ------------------------------------------------------------ doubled cards


def test_duplicate_numbers_counts_a_doubled_source_and_ignores_singles():
    a, b = card(5, item="a"), card(5, item="b")
    c = card(6, item="c")
    assert bd.duplicate_numbers([a, b, c]) == {5: 2}
    assert bd.duplicate_numbers([a, c]) == {}  # negative control
    # The title prefix keys a card without a Quelle, so it counts too.
    d = card(7, item="d")._replace(source=None, title="Discussion #7: x")
    e = card(7, item="e")
    assert bd.duplicate_numbers([d, e]) == {7: 2}


def test_a_doubled_card_fails_the_run_even_when_everything_else_is_settled(
    board, capsys
):
    nodes = [disc_node(1, closed=True, closed_at="2026-08-29T07:00:00Z")]
    settled_card = settled(bd.parse_discussion(nodes[0]))
    fake = board(nodes, [settled_card, settled_card._replace(item_id="second")])
    assert bd.cmd_sync(True, TODAY, "schedule", "") == 1
    captured = capsys.readouterr()
    assert "discussion #1 has 2 cards" in captured.err
    assert captured.out.strip() == "0 changes"
    assert created(fake) == []  # a doubled number is never created a third time


def test_a_single_card_per_discussion_passes(board, capsys):
    # Negative control for the duplicate check.
    nodes = [
        disc_node(1, closed=True, closed_at="2026-08-29T07:00:00Z"),
        disc_node(2, closed=True, closed_at="2026-08-30T07:00:00Z"),
    ]
    board(nodes, [settled(bd.parse_discussion(n)) for n in nodes])
    assert bd.cmd_sync(True, TODAY, "schedule", "") == 0
    assert capsys.readouterr().err == ""


# ----------------------------------------------------------------- workflow


def workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_workflow_triggers():
    doc = workflow()
    triggers = doc[True]  # YAML reads the bare key `on` as the boolean True
    assert set(triggers) == {
        "discussion",
        "discussion_comment",
        "schedule",
        "workflow_dispatch",
    }
    assert triggers["discussion"]["types"] == [
        "created",
        "closed",
        "reopened",
        "edited",
        "deleted",
    ]
    assert triggers["discussion_comment"]["types"] == ["created", "edited", "deleted"]
    assert triggers["schedule"] == [{"cron": "30 5 * * *"}]


def test_workflow_gives_the_trigger_gate_what_it_reads():
    doc = workflow()
    (step,) = [s for s in doc["jobs"]["sync"]["steps"] if "run" in s]
    assert step["run"] == "python3 .github/tools/discussion_sync.py --apply"
    assert step["env"] == {
        "GH_TOKEN": "${{ secrets.PROJECT_SYNC_TOKEN }}",
        "EVENT_NAME": "${{ github.event_name }}",
        "EVENT_ACTION": "${{ github.event.action }}",
        "DISCUSSION_NUMBER": "${{ github.event.discussion.number }}",
    }
    assert "secrets." not in str({k: v for k, v in step.items() if k != "env"})


def test_workflow_serialises_runs_and_limits_permissions():
    doc = workflow()
    assert doc["permissions"] == {"contents": "read"}
    assert doc["concurrency"] == {
        "group": "discussion-sync-${{ github.event.discussion.number || 'all' }}",
        "cancel-in-progress": False,
    }


def test_workflow_runs_only_in_the_main_repository():
    job = workflow()["jobs"]["sync"]
    assert job["if"] == "github.repository == 'shuette42/ecoflow-energy-ha'"


def test_workflow_pins_every_action_to_a_full_sha_and_checks_out_only_the_tools():
    steps = workflow()["jobs"]["sync"]["steps"]
    uses = [s["uses"] for s in steps if "uses" in s]
    assert uses and all(re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", u) for u in uses)
    checkout = next(
        s for s in steps if s.get("uses", "").startswith("actions/checkout@")
    )
    assert checkout["with"] == {"sparse-checkout": ".github/tools"}


def test_script_and_workflow_agree_on_which_events_create():
    # Every (event, action) the workflow can fire, judged by the script's own gate.
    triggers = workflow()[True]
    fired = {
        (name, action)
        for name, spec in triggers.items()
        for action in (spec.get("types", [""]) if isinstance(spec, dict) else [""])
    }
    creating = {pair for pair in fired if bd.may_create(*pair)}
    assert creating == {
        ("discussion", "created"),
        ("schedule", ""),
        ("workflow_dispatch", ""),
    }
