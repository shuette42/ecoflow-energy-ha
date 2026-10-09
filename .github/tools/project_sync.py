"""Mirror issue events onto the EcoFlow GitHub Project board.

GitHub's built-in project workflows cover "added", "closed" and "reopened" for
the Status field. They have no trigger for an edited issue, a new comment or a
label change, and they set only one field. This script fills that gap: for every
issue event it makes sure the issue sits on the board, stamps what just happened
and when, gives a brand-new issue its default Type and Control values, and
derives Status and "Wartet auf" from the issue's labels.

The labels are the source and the board follows them. Triage and the maintainer
only ever set labels, so whoever changes a label has changed the board.

It never writes to the issue itself, so it cannot trigger itself.

Everything the event carries arrives through environment variables. Nothing
user-controlled (title, body, comment text) is read or interpolated.
"""

from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys

PROJECT_OWNER = "shuette42"
PROJECT_NUMBER = 2

# Board field names. The board itself is German, so the values stay German.
EVENT_FIELD = "Letztes Ereignis"
DATE_FIELD = "Letzte Änderung"

# (event name, action) -> option of the "last event" single-select field.
EVENT_OPTIONS = {
    ("issues", "opened"): "Neu",
    ("issues", "edited"): "Bearbeitet",
    ("issues", "closed"): "Geschlossen",
    ("issues", "reopened"): "Wieder geöffnet",
    ("issue_comment", "created"): "Kommentiert",
}

# Set once, when the issue is first opened. Later events leave them alone so a
# hand-made change on the board is never overwritten. "Steuerung" is deliberately
# not here: an empty field means "observe", and the first value has to come from
# the maintainer so that the field value's creator names the maintainer.
NEW_ISSUE_DEFAULTS = {"Typ": "Issue"}

# A label change has no "last event" option of its own; it gets the date stamp
# and the derived Status.
LABEL_EVENTS = {("issues", "labeled"), ("issues", "unlabeled")}

STATUS_FIELD = "Status"
WAITING_FIELD = "Wartet auf"
NO_WAIT = "Keine"

# Labels that say who holds the issue. "in-progress", "ship-ready" and
# "analysis-ready" are claims, every "action:" label means the maintainer owes
# the next step, and "needs-info" means a person outside owes it. The two
# "-ready" labels both wait for the maintainer, but for different things:
# "analysis-ready" for the decision on an analysis, "ship-ready" for the click
# that delivers a finished, staged change.
IN_PROGRESS = "in-progress"
SHIP_READY = "ship-ready"
ANALYSIS_READY = "analysis-ready"
NEEDS_INFO = "needs-info"
ACTION_PREFIX = "action:"


def is_claim_label(name: str) -> bool:
    """True for a label whose removal hands the issue to somebody else."""
    return name.startswith(ACTION_PREFIX) or name in (
        IN_PROGRESS,
        SHIP_READY,
        ANALYSIS_READY,
    )


def plan_status(
    labels: list[str],
    waiting_on: str | None,
    action: str,
    label_name: str = "",
) -> list[tuple[str, str]]:
    """Return the Status and "Wartet auf" values the labels imply.

    The first matching rule wins:

    1. in-progress        -> In Arbeit, nothing to wait for
    2. ship-ready         -> Auslieferung, waiting for the maintainer's approval
    3. any action: label  -> Bereit, the maintainer owes the next step
    4. analysis-ready     -> Review, waiting for the maintainer's decision
    5. needs-info         -> Wartet, on what the board already names
                             (Hardwaredaten, Release, ...) or else the reporter
    6. a claim label was just removed and none of the above holds
                          -> Wartet, as in rule 5: our step is done

    Anything else returns nothing, so a value set by hand on the board stays.
    """
    names = set(labels)
    named_wait = waiting_on if waiting_on and waiting_on != NO_WAIT else "Reporter"
    if IN_PROGRESS in names:
        return [(STATUS_FIELD, "In Arbeit"), (WAITING_FIELD, NO_WAIT)]
    if SHIP_READY in names:
        return [(STATUS_FIELD, "Auslieferung"), (WAITING_FIELD, "Freigabe")]
    if any(n.startswith(ACTION_PREFIX) for n in names):
        return [(STATUS_FIELD, "Bereit"), (WAITING_FIELD, NO_WAIT)]
    if ANALYSIS_READY in names:
        return [(STATUS_FIELD, "Review"), (WAITING_FIELD, "Freigabe")]
    if NEEDS_INFO in names:
        return [(STATUS_FIELD, "Wartet"), (WAITING_FIELD, named_wait)]
    if action == "unlabeled" and is_claim_label(label_name):
        return [(STATUS_FIELD, "Wartet"), (WAITING_FIELD, named_wait)]
    return []


FIELDS_QUERY = """
query($owner: String!, $number: Int!) {
  user(login: $owner) {
    projectV2(number: $number) {
      id
      fields(first: 50) {
        nodes {
          ... on ProjectV2Field { id name dataType }
          ... on ProjectV2SingleSelectField { id name options { id name } }
        }
      }
    }
  }
}
"""

ADD_ITEM_MUTATION = """
mutation($project: ID!, $content: ID!) {
  addProjectV2ItemById(input: {projectId: $project, contentId: $content}) {
    item { id }
  }
}
"""

ISSUE_QUERY = """
query($id: ID!) {
  node(id: $id) {
    ... on Issue {
      state
      labels(first: 50) { nodes { name } }
      projectItems(first: 20) {
        nodes {
          project { id }
          fieldValueByName(name: "Wartet auf") {
            ... on ProjectV2ItemFieldSingleSelectValue { name }
          }
        }
      }
    }
  }
}
"""

SET_VALUE_MUTATION = """
mutation($project: ID!, $item: ID!, $field: ID!, $value: ProjectV2FieldValue!) {
  updateProjectV2ItemFieldValue(
    input: {projectId: $project, itemId: $item, fieldId: $field, value: $value}
  ) {
    projectV2Item { id }
  }
}
"""


def run_graphql(query: str, **variables: object) -> dict:
    """Run one GraphQL call through the gh CLI and return its data object."""
    # A JSON body keeps input-object variables (the field value) typed correctly,
    # which the -f/-F flags cannot do.
    body = json.dumps({"query": query, "variables": variables})
    result = subprocess.run(
        ["gh", "api", "graphql", "--input", "-"],
        input=body,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"gh api graphql failed: {result.stderr.strip()}")
    payload = json.loads(result.stdout)
    if payload.get("errors"):
        raise RuntimeError(f"GraphQL errors: {payload['errors']}")
    return payload["data"]


def load_project() -> tuple[str, dict[str, dict]]:
    """Return the project id and its fields keyed by name."""
    data = run_graphql(FIELDS_QUERY, owner=PROJECT_OWNER, number=PROJECT_NUMBER)
    project = (data.get("user") or {}).get("projectV2")
    if not project:
        raise RuntimeError(f"Project {PROJECT_OWNER}/{PROJECT_NUMBER} not found")
    fields = {}
    for node in project["fields"]["nodes"]:
        if not node or "name" not in node:
            continue
        options = {o["name"]: o["id"] for o in node.get("options", [])}
        fields[node["name"]] = {
            "id": node["id"],
            "type": node.get("dataType", "SINGLE_SELECT"),
            "options": options,
        }
    return project["id"], fields


def plan_updates(event_name: str, action: str, today: str) -> list[tuple[str, str]]:
    """Return the stamp (field name, value) pairs to write for this event.

    The value is an option name for single-select fields and an ISO date for the
    date field. A label change gets the date only. An event the board does not
    track yields no updates.
    """
    if (event_name, action) in LABEL_EVENTS:
        return [(DATE_FIELD, today)]
    option = EVENT_OPTIONS.get((event_name, action))
    if option is None:
        return []
    updates = [(EVENT_FIELD, option), (DATE_FIELD, today)]
    if (event_name, action) == ("issues", "opened"):
        updates += list(NEW_ISSUE_DEFAULTS.items())
    return updates


def to_field_value(field_name: str, field: dict, value: str) -> dict:
    """Build the ProjectV2FieldValue payload for one field."""
    if field["type"] == "DATE":
        return {"date": value}
    if field["type"] == "TEXT":
        return {"text": value}
    option_id = field["options"].get(value)
    if option_id is None:
        raise RuntimeError(
            f"Field '{field_name}' has no option '{value}' "
            f"(has: {sorted(field['options'])})"
        )
    return {"singleSelectOptionId": option_id}


def read_issue(
    issue_node_id: str, project_id: str
) -> tuple[str, list[str], str | None]:
    """Return the issue's state, its label names and its current "Wartet auf"."""
    data = run_graphql(ISSUE_QUERY, id=issue_node_id)
    issue = data.get("node") or {}
    labels = [n["name"] for n in (issue.get("labels") or {}).get("nodes", []) if n]
    waiting_on = None
    for item in (issue.get("projectItems") or {}).get("nodes", []):
        if item and (item.get("project") or {}).get("id") == project_id:
            waiting_on = (item.get("fieldValueByName") or {}).get("name")
    return issue.get("state", ""), labels, waiting_on


def sync(
    event_name: str,
    action: str,
    issue_node_id: str,
    today: str,
    label_name: str = "",
) -> int:
    """Write the planned updates for one event. Returns the number written."""
    updates = plan_updates(event_name, action, today)
    if not updates:
        print(f"No board update for {event_name}/{action}")
        return 0

    project_id, fields = load_project()

    # A closed issue gets its Status from the built-in workflow; the labels it
    # still carries say nothing about who holds it.
    state, labels, waiting_on = read_issue(issue_node_id, project_id)
    if state == "OPEN":
        updates += plan_status(labels, waiting_on, action, label_name)

    # The two stamp fields are the point of this workflow: when they are missing
    # the run must fail loudly instead of looking like it worked.
    for required in (EVENT_FIELD, DATE_FIELD):
        if required not in fields:
            raise RuntimeError(
                f"Project field '{required}' is missing; "
                "create it before enabling this workflow"
            )

    added = run_graphql(ADD_ITEM_MUTATION, project=project_id, content=issue_node_id)
    item_id = added["addProjectV2ItemById"]["item"]["id"]

    written = 0
    for name, value in updates:
        field = fields.get(name)
        if field is None:
            print(f"::warning::Project field '{name}' not found, skipped")
            continue
        run_graphql(
            SET_VALUE_MUTATION,
            project=project_id,
            item=item_id,
            field=field["id"],
            value=to_field_value(name, field, value),
        )
        written += 1
    print(f"Wrote {written} field(s) for {event_name}/{action}")
    return written


def main() -> int:
    today = datetime.datetime.now(datetime.UTC).date().isoformat()
    try:
        sync(
            os.environ["EVENT_NAME"],
            os.environ["EVENT_ACTION"],
            os.environ["ISSUE_NODE_ID"],
            today,
            os.environ.get("LABEL_NAME", ""),
        )
    except (RuntimeError, KeyError) as err:
        print(f"::error::{err}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
