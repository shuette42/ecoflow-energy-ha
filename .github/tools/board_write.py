"""Read or set one field of one issue on the EcoFlow project board.

The Project Sync workflow derives Status and "Wartet auf" from labels. This
tool covers what no label can say: the next step, the delivery state, the
target version, and a wait reason such as a pending release. It is the only
way the daily triage run touches the board, so it is deliberately narrow:

- one open issue of this repository, one field, one value per call
- only the fields in WRITABLE; "Steuerung" and "Typ" belong to the maintainer
- Status only on an issue whose labels decide nothing, because otherwise the
  labels are the source and the next label event would overwrite it anyway
- text is flattened to one line and capped

The token comes from BOARD_TOKEN and is never printed. It belongs to a bot
account with the Write role on this one board, so it reaches nothing else.
Without BOARD_TOKEN the local gh login is used.

    python3 .github/tools/board_write.py show 296
    python3 .github/tools/board_write.py set 296 Zielversion 1.25.0
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "project_sync", Path(__file__).with_name("project_sync.py")
)
assert _spec is not None
assert _spec.loader is not None
ps = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ps)

REPO_OWNER, REPO_NAME = "shuette42", "ecoflow-energy-ha"
GRAPHQL_URL = "https://api.github.com/graphql"
TOKEN_ENV = "BOARD_TOKEN"
TEXT_LIMIT = 400
# Per-field cap for free text that needs more room than TEXT_LIMIT.
# A Projects text field takes 1024 characters (measured: 1025 is rejected by the API).
TEXT_LIMITS: dict[str, int] = {"Stand / Beleg": 1024}

# Field -> allowed values. None means free text (one line, TEXT_LIMIT chars).
WRITABLE: dict[str, set[str] | None] = {
    "Nächster Schritt": None,
    "Stand / Beleg": None,
    "Zielversion": None,
    "Lieferstand": {
        "Ungeprüft",
        "Nicht implementiert",
        "Implementiert",
        "Teilweise veröffentlicht",
        "Veröffentlicht",
        "Bestätigt",
        "Nicht erforderlich",
    },
    "Wartet auf": {
        "Keine",
        "Hardwaredaten",
        "Reporter",
        "Freigabe",
        "Release",
        "Andere Aufgabe",
    },
    "Status": {"Eingang", "Bereit", "Wartet", "Zurückgestellt"},
}
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(-beta\.\d+)?$")

ISSUE_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      id state
      labels(first: 50) { nodes { name } }
      projectItems(first: 20) {
        nodes {
          id
          project { id }
          fieldValues(first: 30) {
            nodes {
              ... on ProjectV2ItemFieldSingleSelectValue {
                name field { ... on ProjectV2SingleSelectField { name } }
              }
              ... on ProjectV2ItemFieldTextValue {
                text field { ... on ProjectV2Field { name } }
              }
            }
          }
        }
      }
    }
  }
}
"""


def http_graphql(query: str, **variables: object) -> dict:
    """POST one GraphQL call with the bot token; return its data object."""
    token = os.environ.get(TOKEN_ENV)
    if not token:
        raise RuntimeError(f"{TOKEN_ENV} is not set")
    request = urllib.request.Request(
        GRAPHQL_URL,
        data=json.dumps({"query": query, "variables": variables}).encode(),
        headers={
            "Authorization": f"bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "ecoflow-board-write",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as err:
        # The status line only; the response body is not echoed.
        raise RuntimeError(f"GitHub API returned HTTP {err.code}") from None
    if payload.get("errors"):
        messages = [e.get("message", "?") for e in payload["errors"]]
        raise RuntimeError(f"GraphQL errors: {messages}")
    return payload["data"]


# Fields shown to the maintainer as a whole text (the notice draft): line
# breaks stay so the paragraphs read as written.
MULTILINE = {"Stand / Beleg"}


def clean_text(value: str, limit: int = TEXT_LIMIT, multiline: bool = False) -> str:
    """No control characters, at most `limit` characters; one line unless multiline."""
    if multiline:
        lines = value.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        kept = [
            "".join(ch for ch in line if ch.isprintable()).rstrip() for line in lines
        ]
        return "\n".join(kept).strip()[:limit]
    flat = " ".join(value.split())
    flat = "".join(ch for ch in flat if ch.isprintable())
    return flat[:limit]


def check_value(field: str, value: str) -> str:
    """Return the value to write, or raise if the field or value is not allowed."""
    if field not in WRITABLE:
        raise RuntimeError(
            f"Field '{field}' is not writable (allowed: {sorted(WRITABLE)})"
        )
    allowed = WRITABLE[field]
    if allowed is not None:
        if value not in allowed:
            raise RuntimeError(
                f"'{value}' is not allowed for '{field}' (allowed: {sorted(allowed)})"
            )
        return value
    value = clean_text(value, TEXT_LIMITS.get(field, TEXT_LIMIT), field in MULTILINE)
    if field == "Zielversion" and value and not VERSION_RE.match(value):
        raise RuntimeError(
            f"Zielversion must look like 1.25.0 or 1.25.0-beta.17, got '{value}'"
        )
    return value


def load_issue(number: int, project_id: str) -> dict:
    """Return the open issue with its labels and its item on this board."""
    data = ps.run_graphql(ISSUE_QUERY, owner=REPO_OWNER, name=REPO_NAME, number=number)
    issue = (data.get("repository") or {}).get("issue")
    if not issue:
        raise RuntimeError(f"#{number} is not an issue of {REPO_OWNER}/{REPO_NAME}")
    if issue["state"] != "OPEN":
        raise RuntimeError(f"#{number} is closed; the board sets closed issues itself")
    issue["labels"] = [n["name"] for n in issue["labels"]["nodes"]]
    issue["item"] = next(
        (n for n in issue["projectItems"]["nodes"] if n["project"]["id"] == project_id),
        None,
    )
    return issue


def item_values(item: dict | None) -> dict[str, str]:
    values: dict[str, str] = {}
    for node in (item or {}).get("fieldValues", {}).get("nodes", []):
        name = ((node or {}).get("field") or {}).get("name")
        if name:
            values[name] = node.get("name", node.get("text", ""))
    return values


def show(number: int) -> int:
    project_id, _ = ps.load_project()
    issue = load_issue(number, project_id)
    print(f"#{number} labels: {', '.join(issue['labels']) or '-'}")
    if issue["item"] is None:
        print("  not on the board yet")
        return 0
    values = item_values(issue["item"])
    for name in WRITABLE:
        print(f"  {name}: {values.get(name, '-')}")
    return 0


def set_field(number: int, field: str, value: str) -> int:
    value = check_value(field, value)
    project_id, fields = ps.load_project()
    issue = load_issue(number, project_id)

    # Labels decide Status whenever plan_status says anything. "Wartet auf" is
    # only decided by a claim label: under needs-info a hand-set reason stays.
    decided = (
        field == ps.STATUS_FIELD and ps.plan_status(issue["labels"], None, "sync")
    ) or (field == ps.WAITING_FIELD and any(map(ps.is_claim_label, issue["labels"])))
    if decided:
        raise RuntimeError(
            f"#{number} carries labels that decide '{field}' "
            f"({', '.join(issue['labels'])}); change the label instead"
        )
    if field not in fields:
        raise RuntimeError(f"The board has no field '{field}'")

    item = issue["item"]
    if item is None:
        added = ps.run_graphql(
            ps.ADD_ITEM_MUTATION, project=project_id, content=issue["id"]
        )
        item = added["addProjectV2ItemById"]["item"]

    ps.run_graphql(
        ps.SET_VALUE_MUTATION,
        project=project_id,
        item=item["id"],
        field=fields[field]["id"],
        value=ps.to_field_value(field, fields[field], value),
    )
    print(f"#{number}: {field} = {value}")
    return 0


def main(argv: list[str]) -> int:
    # With BOARD_TOKEN (the cloud run) the call goes straight to the API as the
    # bot. Without it, the local maintainer's gh login is used.
    if os.environ.get(TOKEN_ENV):
        ps.run_graphql = http_graphql
    try:
        if len(argv) == 2 and argv[0] == "show":
            return show(int(argv[1]))
        if len(argv) == 4 and argv[0] == "set":
            return set_field(int(argv[1]), argv[2], argv[3])
    except (RuntimeError, ValueError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    print("usage: board_write.py show N | set N FIELD VALUE", file=sys.stderr)
    return 64


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
