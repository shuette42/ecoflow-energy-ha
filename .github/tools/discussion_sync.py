#!/usr/bin/env python3
"""Mirror the repository's GitHub Discussions onto the project board.

A Discussion cannot be project item content, so each one is a DraftIssue card of
type "Discussion". The workflow discussion-sync.yml runs this on every discussion
event and once a day, so closed discussions leave "Eingang" and new ones get a
card.

Rules, in one place:

- Key: the card's "Quelle" URL (.../discussions/N), else the title prefix
  "Discussion #N:".
- The project item listing lags a write by minutes and nothing is remembered
  between runs, so a card created moments ago can be missing from the next
  listing. Two rules keep that from doubling a card. An event run (EVENT_NAME
  discussion or discussion_comment, with DISCUSSION_NUMBER) reads and updates
  only that one discussion, and creates its card only on "discussion created".
  A full run (schedule, manual dispatch, or local) reconciles every discussion
  but skips one younger than FRESH_MINUTES, which its own created-event run
  owns. After every run, a discussion that has two cards fails the run (exit 1)
  instead of being synced twice.
- Last activity is the newest of the post, every comment and every reply.
  Never `updatedAt`: edits, reactions and bulk closes move it.
- Closed -> Abgeschlossen / Keine. Open and someone else spoke last -> Bereit /
  Keine, assignee shuette42. Open and we spoke last -> Wartet / Reporter.
- "Nächster Schritt" is written only when empty or carrying this script's own
  prefixes; hand text is never overwritten.
- "Typ" and "Quelle" are written on create only. "Steuerung" is never written.

    python3 .github/tools/discussion_sync.py                 # dry run, exit 1 on drift
    python3 .github/tools/discussion_sync.py --apply         # write cards
    python3 .github/tools/discussion_sync.py --view          # dry run of the view
    python3 .github/tools/discussion_sync.py --view --apply  # set filter and columns

The gh CLI supplies the token: GH_TOKEN in CI (a classic token with the "project"
scope, the board belongs to a user account), else the local gh login. The trigger
comes from EVENT_NAME, EVENT_ACTION and DISCUSSION_NUMBER, all unset on a local run.
"""

from __future__ import annotations

import argparse
import datetime
import importlib.util
import os
import re
import sys
from pathlib import Path
from typing import NamedTuple

_spec = importlib.util.spec_from_file_location(
    "board_write", Path(__file__).with_name("board_write.py")
)
assert _spec is not None
assert _spec.loader is not None
bw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bw)
# Same module object board_write uses, so replacing run_graphql below reaches both.
ps = bw.ps

REPO_OWNER = "shuette42"
REPO_NAME = "ecoflow-energy-ha"
SOURCE_PREFIX = f"https://github.com/{REPO_OWNER}/{REPO_NAME}/discussions/"
MAINTAINERS = frozenset({"shuette42", "shuette42-bot"})
ASSIGNEE = "shuette42"

STATUS_FIELD = ps.STATUS_FIELD
WAITING_FIELD = ps.WAITING_FIELD
EVENT_FIELD = ps.EVENT_FIELD
DATE_FIELD = ps.DATE_FIELD
NEXT_FIELD = "Nächster Schritt"
SYNCED_FIELD = "Letzter Abgleich"
TYPE_FIELD = "Typ"
SOURCE_FIELD = "Quelle"
TYPE_VALUE = "Discussion"

# Fields the sync keeps current, and the two it sets once, on create.
OWN_FIELDS = (
    STATUS_FIELD,
    WAITING_FIELD,
    EVENT_FIELD,
    DATE_FIELD,
    NEXT_FIELD,
    SYNCED_FIELD,
)
CREATE_ONLY_FIELDS = (TYPE_FIELD, SOURCE_FIELD)

# "Nächster Schritt" text starting with one of these belongs to the script.
OWN_PREFIXES = ("Antworten:", "Wartet auf @", "Eigener Beitrag")

TITLE_LIMIT = 120
SOURCE_RE = re.compile(r"/discussions/(\d+)/?$")
TITLE_RE = re.compile(r"^Discussion #(\d+):")

# Triggers that may create a card (see the module docstring). Anything else only
# updates. A run with no trigger at all is a local one.
CREATING_EVENTS = frozenset({"schedule", "workflow_dispatch"})
CREATING_DISCUSSION_ACTION = "created"
# Events about one discussion: the run reads and updates only that discussion.
EVENT_RUNS = frozenset({"discussion", "discussion_comment"})
# A full run leaves a discussion younger than this to its own created-event run.
FRESH_MINUTES = 30

# The view this script shapes.
VIEW_ID = "PVTV_lAHOBZ43ys4BmDG7zgL8_9Q"
VIEW_FILTER = "typ:Discussion -status:Abgeschlossen"
VIEW_FIELDS = (
    "Title",
    "Status",
    WAITING_FIELD,
    DATE_FIELD,
    EVENT_FIELD,
    NEXT_FIELD,
    SOURCE_FIELD,
)

DISCUSSION_FIELDS = """
        number title url closed closedAt createdAt
        author { login }
        comments(last: 100) {
          totalCount
          nodes {
            author { login } createdAt
            replies(last: 1) { nodes { author { login } createdAt } }
          }
        }
"""

DISCUSSIONS_QUERY = (
    """
query($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    discussions(first: 50, after: $cursor) {
      pageInfo { hasNextPage endCursor }
      nodes {"""
    + DISCUSSION_FIELDS
    + """}
    }
  }
}
"""
)

DISCUSSION_QUERY = (
    """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    discussion(number: $number) {"""
    + DISCUSSION_FIELDS
    + """}
  }
}
"""
)

ITEM_FIELDS = """
  id
  content {
    __typename
    ... on DraftIssue { id title body assignees(first: 10) { nodes { login } } }
  }
  typ: fieldValueByName(name: "Typ") {
    ... on ProjectV2ItemFieldSingleSelectValue { name }
  }
  status: fieldValueByName(name: "Status") {
    ... on ProjectV2ItemFieldSingleSelectValue { name }
  }
  waiting: fieldValueByName(name: "Wartet auf") {
    ... on ProjectV2ItemFieldSingleSelectValue { name }
  }
  event: fieldValueByName(name: "Letztes Ereignis") {
    ... on ProjectV2ItemFieldSingleSelectValue { name }
  }
  changed: fieldValueByName(name: "Letzte Änderung") {
    ... on ProjectV2ItemFieldDateValue { date }
  }
  source: fieldValueByName(name: "Quelle") {
    ... on ProjectV2ItemFieldTextValue { text }
  }
  next: fieldValueByName(name: "Nächster Schritt") {
    ... on ProjectV2ItemFieldTextValue { text }
  }
"""

ITEMS_QUERY = (
    """
query($owner: String!, $number: Int!, $cursor: String) {
  user(login: $owner) {
    projectV2(number: $number) {
      items(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {"""
    + ITEM_FIELDS
    + """}
      }
    }
  }
}
"""
)

USER_QUERY = "query($login: String!) { user(login: $login) { id } }"

ADD_DRAFT_MUTATION = """
mutation($project: ID!, $title: String!, $body: String, $assignees: [ID!]) {
  addProjectV2DraftIssue(
    input: {projectId: $project, title: $title, body: $body, assigneeIds: $assignees}
  ) { projectItem { id } }
}
"""

UPDATE_DRAFT_MUTATION = """
mutation($draft: ID!, $title: String!, $body: String, $assignees: [ID!]) {
  updateProjectV2DraftIssue(
    input: {draftIssueId: $draft, title: $title, body: $body, assigneeIds: $assignees}
  ) { draftIssue { id } }
}
"""

VIEW_QUERY = """
query($id: ID!) {
  node(id: $id) {
    ... on ProjectV2View {
      filter
      visibleFields(first: 30) {
        nodes { id name }
      }
    }
  }
}
"""

VIEW_MUTATION = """
mutation($view: ID!, $filter: String!, $fields: [ID!]) {
  updateProjectV2View(
    input: {viewId: $view, filter: $filter, configuration: {visibleFieldIds: $fields}}
  ) { projectV2View { id } }
}
"""


class Event(NamedTuple):
    at: str  # ISO timestamp, sorts as text
    login: str


class Discussion(NamedTuple):
    number: int
    title: str
    url: str
    closed: bool
    closed_at: str | None
    author: str
    comment_count: int
    events: tuple[Event, ...]  # the post, every comment, the newest reply of each
    created_at: str  # ISO timestamp of the post


class Card(NamedTuple):
    item_id: str
    draft_id: str
    title: str
    body: str
    typ: str | None
    status: str | None
    waiting: str | None
    event: str | None
    changed: str | None
    source: str | None
    next_step: str | None
    assignees: tuple[str, ...]


class Wanted(NamedTuple):
    values: dict[str, str]
    next_step: str | None  # None: leave the field alone (closed)
    assignees: tuple[str, ...]


class Change(NamedTuple):
    action: str  # "create" or "update"
    number: int
    title: str
    body: str
    source: str
    card: Card | None
    values: dict[str, str]  # board field -> value to write
    assignees: tuple[str, ...] | None  # None: leave as is
    retitle: bool


# ------------------------------------------------------------- derivation


def login_of(author: dict | None) -> str:
    return (author or {}).get("login") or "ghost"  # a deleted account has no author


def parse_discussion(node: dict) -> Discussion:
    """A Discussion from a GraphQL node: the post, comments and newest replies."""
    author = login_of(node.get("author"))
    events = [Event(node["createdAt"], author)]
    comments = node.get("comments") or {}
    for comment in comments.get("nodes") or []:
        events.append(Event(comment["createdAt"], login_of(comment.get("author"))))
        for reply in (comment.get("replies") or {}).get("nodes") or []:
            events.append(Event(reply["createdAt"], login_of(reply.get("author"))))
    return Discussion(
        number=node["number"],
        title=node["title"],
        url=node["url"],
        closed=bool(node["closed"]),
        closed_at=node.get("closedAt"),
        author=author,
        comment_count=comments.get("totalCount", 0),
        events=tuple(events),
        created_at=node["createdAt"],
    )


def last_event(d: Discussion) -> Event:
    return max(d.events, key=lambda e: e.at)


def waiting_on(d: Discussion) -> str | None:
    """Who we wait for: the author, else the newest outside voice, else nobody."""
    if d.author not in MAINTAINERS:
        return d.author
    outside = [e for e in d.events if e.login not in MAINTAINERS]
    return max(outside, key=lambda e: e.at).login if outside else None


def is_script_text(text: str | None) -> bool:
    return not text or text.startswith(OWN_PREFIXES)


def derive(d: Discussion, today: str, current_next: str | None = None) -> Wanted:
    """The card fields a discussion calls for (first match wins)."""
    if d.closed:
        day = (d.closed_at or last_event(d).at)[:10]
        values = {
            STATUS_FIELD: "Abgeschlossen",
            WAITING_FIELD: ps.NO_WAIT,
            EVENT_FIELD: "Geschlossen",
            DATE_FIELD: day,
        }
        return Wanted(values, None, ())
    last = last_event(d)
    day = last.at[:10]
    event = "Kommentiert" if d.comment_count else "Neu"
    if last.login not in MAINTAINERS:
        values = {
            STATUS_FIELD: "Bereit",
            WAITING_FIELD: ps.NO_WAIT,
            EVENT_FIELD: event,
            DATE_FIELD: day,
        }
        step = f"Antworten: letzter Beitrag von @{last.login} am {day}"
        assignees: tuple[str, ...] = (ASSIGNEE,)
    else:
        values = {
            STATUS_FIELD: "Wartet",
            WAITING_FIELD: "Reporter",
            EVENT_FIELD: event,
            DATE_FIELD: day,
        }
        who = waiting_on(d)
        step = (
            f"Wartet auf @{who} seit {day}"
            if who
            else "Eigener Beitrag, offen für Antworten"
        )
        assignees = ()
    limit = bw.TEXT_LIMITS.get(NEXT_FIELD, bw.TEXT_LIMIT)
    return Wanted(
        values,
        bw.clean_text(step, limit) if is_script_text(current_next) else None,
        assignees,
    )


def card_number(card: Card) -> int | None:
    """Discussion number from the Quelle URL, else from the title prefix."""
    for pattern, text in ((SOURCE_RE, card.source), (TITLE_RE, card.title)):
        match = pattern.search(text or "")
        if match:
            return int(match.group(1))
    return None


def card_title(d: Discussion) -> str:
    flat = bw.clean_text(d.title, 4 * TITLE_LIMIT)
    cut = (
        flat
        if len(flat) <= TITLE_LIMIT
        else flat[: TITLE_LIMIT - 3].rsplit(" ", 1)[0] + "..."
    )
    return f"Discussion #{d.number}: {cut}"


def plan_changes(
    discussions: list[Discussion],
    cards: dict[int, Card],
    today: str,
    orphans: bool = True,
) -> tuple[list[Change], list[str]]:
    """The writes that make the cards match the discussions, and notes. Pure.

    `orphans` reports cards without a discussion; an event run sees one
    discussion only and turns it off.
    """
    changes: list[Change] = []
    notes: list[str] = []
    for d in sorted(discussions, key=lambda x: x.number):
        if d.comment_count > 100:
            notes.append(
                f"#{d.number}: {d.comment_count} comments, only the newest 100 are read"
            )
        card = cards.get(d.number)
        wanted = derive(d, today, card.next_step if card else None)
        title = card_title(d)
        source = f"{SOURCE_PREFIX}{d.number}"
        values = dict(wanted.values)
        if wanted.next_step:
            values[NEXT_FIELD] = wanted.next_step
        if card is None:
            # Typ and Quelle first: if a later write fails, the half-made card
            # is still recognised as this discussion's and is not made again.
            values = {
                TYPE_FIELD: TYPE_VALUE,
                SOURCE_FIELD: source,
                **values,
                SYNCED_FIELD: today,
            }
            changes.append(
                Change(
                    "create",
                    d.number,
                    title,
                    source,
                    source,
                    None,
                    values,
                    wanted.assignees,
                    False,
                )
            )
            continue
        current = {
            STATUS_FIELD: card.status,
            WAITING_FIELD: card.waiting,
            EVENT_FIELD: card.event,
            DATE_FIELD: card.changed,
            NEXT_FIELD: card.next_step or "",
        }
        diff = {k: v for k, v in values.items() if (current[k] or "") != v}
        if card.typ is None:
            # A card whose create stopped before Typ was written: complete it.
            diff = {TYPE_FIELD: TYPE_VALUE, SOURCE_FIELD: source, **diff}
        assignees = (
            wanted.assignees
            if tuple(sorted(card.assignees)) != wanted.assignees
            else None
        )
        retitle = card.title != title
        if diff or assignees is not None or retitle:
            diff[SYNCED_FIELD] = today
            changes.append(
                Change(
                    "update",
                    d.number,
                    title,
                    card.body,
                    source,
                    card,
                    diff,
                    assignees,
                    retitle,
                )
            )
    if orphans:
        for number in sorted(set(cards) - {d.number for d in discussions}):
            notes.append(f"#{number}: has a card but no discussion, left as is")
    return changes, notes


def check_own(values: dict[str, str], creating: bool = False) -> None:
    """Refuse any field this script does not own (Steuerung above all)."""
    allowed = OWN_FIELDS + (CREATE_ONLY_FIELDS if creating else ())
    for name in values:
        if name not in allowed:
            raise RuntimeError(f"discussion_sync.py may not write field '{name}'")


# ------------------------------------------------------------------ board


def load_discussions() -> list[Discussion]:
    found: list[Discussion] = []
    cursor = None
    while True:
        data = ps.run_graphql(
            DISCUSSIONS_QUERY, owner=REPO_OWNER, name=REPO_NAME, cursor=cursor
        )
        page = data["repository"]["discussions"]
        found += [parse_discussion(n) for n in page["nodes"]]
        if not page["pageInfo"]["hasNextPage"]:
            return found
        cursor = page["pageInfo"]["endCursor"]


def load_discussion(number: int) -> Discussion | None:
    """One discussion by number, None when it does not exist (deleted)."""
    data = ps.run_graphql(
        DISCUSSION_QUERY, owner=REPO_OWNER, name=REPO_NAME, number=number
    )
    node = data["repository"]["discussion"]
    return parse_discussion(node) if node else None


def card_from_node(node: dict) -> Card | None:
    """A Card from an item node: a Discussion-typed draft issue, else None."""
    content = node.get("content") or {}
    if content.get("__typename") != "DraftIssue":
        return None

    def name(key: str, attr: str = "name") -> str | None:
        return (node.get(key) or {}).get(attr)

    typ = name("typ")
    # A draft whose create stopped before "Typ" was written still counts, by its
    # title, so the next run completes it instead of making a second one.
    untyped_card = typ is None and TITLE_RE.match(content.get("title") or "")
    if typ != TYPE_VALUE and not untyped_card:
        return None
    return Card(
        item_id=node["id"],
        draft_id=content["id"],
        title=content.get("title", ""),
        body=content.get("body") or "",
        typ=typ,
        status=name("status"),
        waiting=name("waiting"),
        event=name("event"),
        changed=name("changed", "date"),
        source=name("source", "text"),
        next_step=name("next", "text"),
        assignees=tuple(
            sorted(
                n["login"] for n in (content.get("assignees") or {}).get("nodes", [])
            )
        ),
    )


def load_cards() -> list[Card]:
    cards: list[Card] = []
    cursor = None
    while True:
        data = ps.run_graphql(
            ITEMS_QUERY, owner=ps.PROJECT_OWNER, number=ps.PROJECT_NUMBER, cursor=cursor
        )
        page = data["user"]["projectV2"]["items"]
        cards += [c for c in map(card_from_node, page["nodes"]) if c is not None]
        if not page["pageInfo"]["hasNextPage"]:
            return cards
        cursor = page["pageInfo"]["endCursor"]


def cards_by_number(cards: list[Card]) -> dict[int, Card]:
    """Index by discussion number; of a doubled number the first listed card is used."""
    index: dict[int, Card] = {}
    for card in cards:
        number = card_number(card)
        if number is not None:
            index.setdefault(number, card)
    return index


def duplicate_numbers(cards: list[Card]) -> dict[int, int]:
    """Discussion number -> card count, for the numbers carried by two cards or more."""
    seen: dict[int, int] = {}
    for card in cards:
        number = card_number(card)
        if number is not None:
            seen[number] = seen.get(number, 0) + 1
    return {n: c for n, c in sorted(seen.items()) if c > 1}


def may_create(event_name: str, event_action: str) -> bool:
    """Whether this trigger may create cards. No trigger at all is a local run."""
    if not event_name:
        return True
    if event_name in CREATING_EVENTS:
        return True
    return event_name == "discussion" and event_action == CREATING_DISCUSSION_ACTION


def apply_change(
    project_id: str, fields: dict[str, dict], change: Change, user_ids: dict[str, str]
) -> None:
    """Write one change."""
    check_own(change.values, creating=change.card is None or change.card.typ is None)
    ids = None if change.assignees is None else [user_ids[a] for a in change.assignees]
    if change.card is None:
        data = ps.run_graphql(
            ADD_DRAFT_MUTATION,
            project=project_id,
            title=change.title,
            body=change.body,
            assignees=ids,
        )
        item_id = data["addProjectV2DraftIssue"]["projectItem"]["id"]
    else:
        item_id = change.card.item_id
        if change.retitle or ids is not None:
            ps.run_graphql(
                UPDATE_DRAFT_MUTATION,
                draft=change.card.draft_id,
                title=change.title,
                body=change.card.body,
                assignees=ids,
            )
    for name, value in change.values.items():
        field = fields.get(name)
        if field is None:
            raise RuntimeError(f"The board has no field '{name}'")
        ps.run_graphql(
            ps.SET_VALUE_MUTATION,
            project=project_id,
            item=item_id,
            field=field["id"],
            value=ps.to_field_value(name, field, value),
        )


def describe(change: Change) -> str:
    assignee = f", assignee={list(change.assignees)}" if change.assignees else ""
    if change.card is None:
        shown = ", ".join(f"{k}={v!r}" for k, v in change.values.items())
        return f"create {change.title} [{shown}{assignee}]"
    card = change.card
    old = {
        STATUS_FIELD: card.status,
        WAITING_FIELD: card.waiting,
        EVENT_FIELD: card.event,
        DATE_FIELD: card.changed,
        NEXT_FIELD: card.next_step,
        SYNCED_FIELD: None,
        TYPE_FIELD: card.typ,
        SOURCE_FIELD: card.source,
    }
    parts = ["title"] if change.retitle else []
    parts += [f"{k}: {old[k] or '-'!r} -> {v!r}" for k, v in change.values.items()]
    if change.assignees is not None:
        parts.append(f"assignees: {list(card.assignees)} -> {list(change.assignees)}")
    return f"update #{change.number} [{'; '.join(parts)}]"


def count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


# --------------------------------------------------------------- commands


def fresh_cutoff(now: datetime.datetime) -> str:
    """Posts made after this timestamp are too young for a full run to create."""
    cutoff = now - datetime.timedelta(minutes=FRESH_MINUTES)
    return cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")


def cmd_sync(
    apply: bool,
    today: str,
    event_name: str = "",
    event_action: str = "",
    number: int | None = None,
    now: datetime.datetime | None = None,
) -> int:
    event_run = event_name in EVENT_RUNS
    notes: list[str] = []
    if event_run:
        if number is None:
            raise RuntimeError(f"{event_name} event without DISCUSSION_NUMBER")
        one = load_discussion(number)
        discussions = [one] if one else []
        if one is None:
            notes.append(f"#{number}: no discussion any more, its card is left as is")
    else:
        discussions = load_discussions()
    listed = load_cards()
    changes, plan_notes = plan_changes(
        discussions, cards_by_number(listed), today, orphans=not event_run
    )
    notes += plan_notes
    cutoff = fresh_cutoff(now or datetime.datetime.now(datetime.UTC))
    created_at = {d.number: d.created_at for d in discussions}
    allowed = may_create(event_name, event_action)
    kept: list[Change] = []
    for change in changes:
        if change.action == "create" and not allowed:
            notes.append(
                f"#{change.number}: no card yet, not created on {event_name}/"
                f"{event_action} (the listing can lag; a daily run creates it)"
            )
        elif (
            change.action == "create"
            and not event_run
            and created_at[change.number] > cutoff
        ):
            notes.append(
                f"#{change.number}: no card yet, posted less than {FRESH_MINUTES} "
                "minutes ago, its own created-event run creates it"
            )
        else:
            kept.append(change)
    changes = kept
    for note in notes:
        print(f"note: {note}")
    for change in changes:
        print(describe(change))
    if apply and changes:
        project_id, fields = ps.load_project()
        logins = {a for c in changes for a in (c.assignees or ())}
        user_ids = {
            a: ps.run_graphql(USER_QUERY, login=a)["user"]["id"] for a in logins
        }
        for change in changes:
            apply_change(project_id, fields, change, user_ids)
    creates = sum(c.action == "create" for c in changes)
    summary = f"{count(creates, 'create')}, {count(len(changes) - creates, 'update')}"
    mode = "applied" if apply else "pending (dry run)"
    print(f"{summary} {mode}" if changes else "0 changes")
    doubled = duplicate_numbers(listed)
    for doubled_number, cards in doubled.items():
        print(
            f"error: discussion #{doubled_number} has {cards} cards on the board, "
            "remove all but one (only the first is synced)",
            file=sys.stderr,
        )
    return 1 if doubled or (changes and not apply) else 0


def cmd_view(apply: bool) -> int:
    """Set the Discussions view's filter and columns, by field name."""
    _, fields = ps.load_project()
    missing = [n for n in VIEW_FIELDS if n not in fields]
    if missing:
        raise RuntimeError(f"The board has no field named {missing}; view not touched")
    wanted = [fields[n]["id"] for n in VIEW_FIELDS]
    view = ps.run_graphql(VIEW_QUERY, id=VIEW_ID).get("node") or {}
    if "filter" not in view:
        raise RuntimeError(f"View {VIEW_ID} not found")
    have = [n["id"] for n in view["visibleFields"]["nodes"] if n]
    drift = view["filter"] != VIEW_FILTER or have != wanted
    print(f"view filter: {view['filter']!r} -> {VIEW_FILTER!r}")
    state = "differ" if have != wanted else "same"
    print(f"view columns: {list(VIEW_FIELDS)} ({state})")
    if not drift:
        print("0 changes")
        return 0
    if not apply:
        print("1 change pending (dry run)")
        return 1
    ps.run_graphql(VIEW_MUTATION, view=VIEW_ID, filter=VIEW_FILTER, fields=wanted)
    after = ps.run_graphql(VIEW_QUERY, id=VIEW_ID)["node"]
    ok = (
        after["filter"] == VIEW_FILTER
        and [n["id"] for n in after["visibleFields"]["nodes"]] == wanted
    )
    print(
        "view applied and read back" if ok else "view applied but the read-back differs"
    )
    return 0 if ok else 1


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="write to the board")
    parser.add_argument(
        "--view", action="store_true", help="shape the Discussions view only"
    )
    args = parser.parse_args(argv)
    try:
        if args.view:
            return cmd_view(args.apply)
        today = datetime.datetime.now(datetime.UTC).date().isoformat()
        number = os.environ.get("DISCUSSION_NUMBER", "")
        return cmd_sync(
            args.apply,
            today,
            os.environ.get("EVENT_NAME", ""),
            os.environ.get("EVENT_ACTION", ""),
            int(number) if number else None,
        )
    except (RuntimeError, OSError, ValueError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
