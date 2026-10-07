"""Mirror issue events onto the EcoFlow GitHub Project board.

GitHub's built-in project workflows cover "added", "closed" and "reopened" for
the Status field. They have no trigger for an edited issue or a new comment, and
they set only one field. This script fills that gap: for every issue event it
makes sure the issue sits on the board, stamps what just happened and when, and
gives a brand-new issue its default Type and Control values.

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
# hand-made change on the board is never overwritten.
NEW_ISSUE_DEFAULTS = {"Typ": "Issue", "Steuerung": "Nur beobachten"}

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
    """Return (field name, value) pairs to write for this event.

    The value is an option name for single-select fields and an ISO date for the
    date field. An event the board does not track yields no updates.
    """
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
    option_id = field["options"].get(value)
    if option_id is None:
        raise RuntimeError(
            f"Field '{field_name}' has no option '{value}' "
            f"(has: {sorted(field['options'])})"
        )
    return {"singleSelectOptionId": option_id}


def sync(event_name: str, action: str, issue_node_id: str, today: str) -> int:
    """Write the planned updates for one event. Returns the number written."""
    updates = plan_updates(event_name, action, today)
    if not updates:
        print(f"No board update for {event_name}/{action}")
        return 0

    project_id, fields = load_project()

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
        )
    except (RuntimeError, KeyError) as err:
        print(f"::error::{err}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
