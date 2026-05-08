"""ClickUp CSV to GitHub Projects importer.

Reads an EOS Rock CSV export from ClickUp and creates GitHub Issues
in leapfinancial/management, then adds them to the project-management
GitHub Project (v2).
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from dotenv import load_dotenv
import requests

load_dotenv(Path(__file__).parent / ".env")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
GITHUB_OWNER = "leapfinancial"
GITHUB_REPO = "project-management"
PROJECT_NAME = "EOS Board"
CSV_PATH = Path(__file__).parent / "input_data" / "eos-rock.csv"

REST_BASE = "https://api.github.com"
GRAPHQL_URL = "https://api.github.com/graphql"

PRIORITY_LABEL_COLORS: dict[str, str] = {
    "URGENT": "d73a4a",   # red
    "HIGH": "e99d42",     # orange
    "NORMAL": "0075ca",   # blue
}

STATUS_LABEL_COLORS: dict[str, str] = {
    "on target": "0e8a16",  # green
    "to do": "cfd3d7",      # light gray
}

ASSIGNEE_LABEL_COLOR = "c5def5"  # light blue

REQUEST_DELAY = 1.0  # seconds between mutating API calls


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _rest_get(token: str, path: str, params: dict | None = None) -> Any:
    url = f"{REST_BASE}{path}"
    resp = requests.get(url, headers=_headers(token), params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _rest_post(token: str, path: str, json_body: dict) -> Any:
    url = f"{REST_BASE}{path}"
    resp = requests.post(url, headers=_headers(token), json=json_body, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _graphql(token: str, query: str, variables: dict | None = None) -> Any:
    payload: dict[str, Any] = {"query": query}
    if variables:
        payload["variables"] = variables
    resp = requests.post(
        GRAPHQL_URL, headers=_headers(token), json=payload, timeout=30
    )
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise RuntimeError(f"GraphQL errors: {data['errors']}")
    return data["data"]


# ---------------------------------------------------------------------------
# CSV parsing
# ---------------------------------------------------------------------------

def parse_csv(csv_path: Path) -> list[dict[str, str]]:
    with open(csv_path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return list(reader)


def _clean_list_field(raw: str) -> list[str]:
    """Parse ClickUp list-style fields like '[url1, url2]' into a Python list."""
    raw = raw.strip()
    if not raw or raw == "[]":
        return []
    raw = raw.strip("[]")
    return [item.strip() for item in raw.split(",") if item.strip()]


def _parse_assignees(raw: str) -> list[str]:
    """Extract assignee names from '[Name1, Name2]' format."""
    return _clean_list_field(raw)


# ---------------------------------------------------------------------------
# Label management
# ---------------------------------------------------------------------------

def _label_slug(prefix: str, value: str) -> str:
    slug = value.strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug).strip("-")
    return f"{prefix}:{slug}"


def ensure_label(token: str, name: str, color: str, *, dry_run: bool) -> None:
    if dry_run:
        print(f"  [dry-run] Would ensure label '{name}' (#{color})")
        return

    try:
        _rest_get(token, f"/repos/{GITHUB_OWNER}/{GITHUB_REPO}/labels/{quote(name, safe='')}")
        return  # already exists
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            pass  # need to create
        else:
            raise

    print(f"  Creating label '{name}' (#{color})")
    _rest_post(
        token,
        f"/repos/{GITHUB_OWNER}/{GITHUB_REPO}/labels",
        {"name": name, "color": color},
    )
    time.sleep(REQUEST_DELAY)


def ensure_all_labels(token: str, rows: list[dict[str, str]], *, dry_run: bool) -> None:
    print("\n=== Ensuring labels exist ===")
    seen: set[str] = set()

    for row in rows:
        priority = row.get("Priority", "").strip()
        if priority and priority not in seen:
            seen.add(priority)
            label = _label_slug("priority", priority)
            ensure_label(token, label, PRIORITY_LABEL_COLORS.get(priority, "cfd3d7"), dry_run=dry_run)

        status = row.get("Status", "").strip()
        if status and status not in seen:
            seen.add(status)
            label = _label_slug("status", status)
            ensure_label(token, label, STATUS_LABEL_COLORS.get(status, "cfd3d7"), dry_run=dry_run)

        for assignee in _parse_assignees(row.get("Assignee", "")):
            key = f"assignee-{assignee}"
            if key not in seen:
                seen.add(key)
                label = _label_slug("assignee", assignee)
                ensure_label(token, label, ASSIGNEE_LABEL_COLOR, dry_run=dry_run)


# ---------------------------------------------------------------------------
# Issue creation
# ---------------------------------------------------------------------------

def _build_issue_body(row: dict[str, str]) -> str:
    content = row.get("Task Content", "").strip()
    task_id = row.get("Task ID", "").strip()
    assignees = row.get("Assignee", "").strip()
    due_date = row.get("Due Date", "").strip()
    start_date = row.get("Start Date", "").strip()
    priority = row.get("Priority", "").strip()
    progress = row.get("Progress (manual progress)", "").strip()
    subtask_urls = _clean_list_field(row.get("Subtask URL's", ""))

    lines = ["## Rock Statement (EOS)", "", content, "", "---", ""]

    lines.append(f"**ClickUp ID:** `{task_id}`")
    if assignees:
        lines.append(f"**Assignee(s):** {assignees}")
    if due_date:
        lines.append(f"**Due Date:** {due_date}")
    if start_date:
        lines.append(f"**Start Date:** {start_date}")
    if priority:
        lines.append(f"**Priority:** {priority}")
    if progress:
        lines.append(f"**Progress:** {progress}%")
    if subtask_urls:
        links = ", ".join(subtask_urls)
        lines.append(f"**ClickUp Subtasks:** {links}")

    return "\n".join(lines)


def _collect_labels(row: dict[str, str]) -> list[str]:
    labels: list[str] = []
    priority = row.get("Priority", "").strip()
    if priority:
        labels.append(_label_slug("priority", priority))

    status = row.get("Status", "").strip()
    if status:
        labels.append(_label_slug("status", status))

    for assignee in _parse_assignees(row.get("Assignee", "")):
        labels.append(_label_slug("assignee", assignee))

    return labels


def _find_existing_issue(token: str, title: str) -> int | None:
    """Return issue number if an open issue with the exact title exists."""
    params = {"state": "open", "per_page": "100"}
    issues = _rest_get(
        token, f"/repos/{GITHUB_OWNER}/{GITHUB_REPO}/issues", params=params
    )
    for issue in issues:
        if issue.get("title") == title:
            return issue["number"]
    return None


def create_issue(token: str, row: dict[str, str], *, dry_run: bool) -> str | None:
    """Create a GitHub issue for one CSV row. Returns the issue node_id or None."""
    title = row.get("Task Name", "").strip()
    if not title:
        print("  [skip] Row has no Task Name")
        return None

    body = _build_issue_body(row)
    labels = _collect_labels(row)

    if dry_run:
        print(f"  [dry-run] Would create issue: {title}")
        print(f"            Labels: {labels}")
        return None

    existing = _find_existing_issue(token, title)
    if existing:
        print(f"  [skip] Issue already exists: #{existing} — {title}")
        issue_data = _rest_get(
            token, f"/repos/{GITHUB_OWNER}/{GITHUB_REPO}/issues/{existing}"
        )
        return issue_data.get("node_id")

    print(f"  Creating issue: {title}")
    data = _rest_post(
        token,
        f"/repos/{GITHUB_OWNER}/{GITHUB_REPO}/issues",
        {"title": title, "body": body, "labels": labels},
    )
    time.sleep(REQUEST_DELAY)
    print(f"  -> Created #{data['number']}")
    return data.get("node_id")


# ---------------------------------------------------------------------------
# GitHub Projects v2 (GraphQL)
# ---------------------------------------------------------------------------

FIND_PROJECT_QUERY = """
query($org: String!, $cursor: String) {
  organization(login: $org) {
    projectsV2(first: 20, after: $cursor) {
      nodes {
        id
        title
      }
      pageInfo {
        hasNextPage
        endCursor
      }
    }
  }
}
"""

ADD_ITEM_MUTATION = """
mutation($projectId: ID!, $contentId: ID!) {
  addProjectV2ItemById(input: {projectId: $projectId, contentId: $contentId}) {
    item {
      id
    }
  }
}
"""


def find_project_id(token: str) -> str:
    """Find the node ID of the target GitHub Project (v2)."""
    cursor = None
    while True:
        data = _graphql(token, FIND_PROJECT_QUERY, {"org": GITHUB_OWNER, "cursor": cursor})
        projects = data["organization"]["projectsV2"]
        for node in projects["nodes"]:
            if node["title"] == PROJECT_NAME:
                return node["id"]
        if not projects["pageInfo"]["hasNextPage"]:
            break
        cursor = projects["pageInfo"]["endCursor"]
    raise RuntimeError(f"Project '{PROJECT_NAME}' not found in org '{GITHUB_OWNER}'")


def add_issue_to_project(token: str, project_id: str, issue_node_id: str, *, dry_run: bool) -> None:
    if dry_run:
        print(f"  [dry-run] Would add issue to project {PROJECT_NAME}")
        return

    print(f"  Adding issue to project '{PROJECT_NAME}'")
    _graphql(
        token,
        ADD_ITEM_MUTATION,
        {"projectId": project_id, "contentId": issue_node_id},
    )
    time.sleep(REQUEST_DELAY)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Import ClickUp CSV into GitHub Issues + Project")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without creating anything")
    parser.add_argument("--csv", type=Path, default=CSV_PATH, help="Path to the ClickUp CSV file")
    args = parser.parse_args()

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("ERROR: GITHUB_TOKEN environment variable is not set.", file=sys.stderr)
        sys.exit(1)

    csv_path: Path = args.csv
    if not csv_path.exists():
        print(f"ERROR: CSV file not found: {csv_path}", file=sys.stderr)
        sys.exit(1)

    rows = parse_csv(csv_path)
    print(f"Parsed {len(rows)} tasks from {csv_path.name}")

    if args.dry_run:
        print("\n*** DRY RUN — no changes will be made ***\n")

    ensure_all_labels(token, rows, dry_run=args.dry_run)

    project_id: str | None = None
    if not args.dry_run:
        print("\n=== Looking up GitHub Project ===")
        project_id = find_project_id(token)
        print(f"  Found project '{PROJECT_NAME}' -> {project_id}")

    print("\n=== Creating issues ===")
    created = 0
    skipped = 0
    errors = 0

    for i, row in enumerate(rows, 1):
        task_name = row.get("Task Name", "(unnamed)")
        print(f"\n[{i}/{len(rows)}] {task_name}")
        try:
            issue_node_id = create_issue(token, row, dry_run=args.dry_run)
            if issue_node_id and project_id:
                add_issue_to_project(token, project_id, issue_node_id, dry_run=args.dry_run)
                created += 1
            elif issue_node_id is None and not args.dry_run:
                skipped += 1
            else:
                created += 1  # dry-run counts as "would create"
        except Exception as exc:
            print(f"  [ERROR] {exc}")
            errors += 1

    print("\n=== Summary ===")
    print(f"  Created: {created}")
    print(f"  Skipped (duplicate): {skipped}")
    print(f"  Errors:  {errors}")
    print(f"  Total:   {len(rows)}")


if __name__ == "__main__":
    main()
