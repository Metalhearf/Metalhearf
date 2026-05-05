#!/usr/bin/env python3
"""Update README.md with the user's curated GitHub star lists.

Reads the GitHub GraphQL API (undocumented `viewer.lists`) and writes a
collapsible-per-category section between the markers
`<!-- STARS:START -->` and `<!-- STARS:END -->`.

Auth: STARS_TOKEN env var, must be a classic PAT with the `user` scope.
"""
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

README = Path(__file__).resolve().parents[2] / "README.md"
START = "<!-- STARS:START -->"
END = "<!-- STARS:END -->"
STALE_DAYS = 365
HOT_DAYS = 7

def graphql(query: str, variables: dict | None = None) -> dict:
    token = os.environ["STARS_TOKEN"]
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=body,
        headers={
            "Authorization": f"bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "stars-readme-updater",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read())
    if "errors" in payload:
        raise RuntimeError(f"GraphQL errors: {payload['errors']}")
    return payload["data"]


REPO_FRAG = """
... on Repository {
  nameWithOwner
  description
  stargazerCount
  url
  isArchived
  pushedAt
}
"""


def fetch_lists() -> list[dict]:
    data = graphql(
        """
        query {
          viewer {
            lists(first: 100) {
              nodes {
                id name slug description
                items(first: 100) {
                  totalCount
                  pageInfo { hasNextPage endCursor }
                  nodes { __typename %s }
                }
              }
            }
          }
        }
        """ % REPO_FRAG
    )
    lists = []
    for node in data["viewer"]["lists"]["nodes"]:
        repos = [n for n in node["items"]["nodes"] if n.get("__typename") == "Repository"]
        cursor = node["items"]["pageInfo"]["endCursor"]
        has_next = node["items"]["pageInfo"]["hasNextPage"]
        while has_next:
            page = graphql(
                """
                query($listId: ID!, $cursor: String) {
                  node(id: $listId) {
                    ... on UserList {
                      items(first: 100, after: $cursor) {
                        pageInfo { hasNextPage endCursor }
                        nodes { __typename %s }
                      }
                    }
                  }
                }
                """ % REPO_FRAG,
                {"listId": node["id"], "cursor": cursor},
            )
            extra = [n for n in page["node"]["items"]["nodes"] if n.get("__typename") == "Repository"]
            repos.extend(extra)
            cursor = page["node"]["items"]["pageInfo"]["endCursor"]
            has_next = page["node"]["items"]["pageInfo"]["hasNextPage"]
        lists.append({
            "name": node["name"],
            "slug": node["slug"],
            "description": node.get("description") or "",
            "total": node["items"]["totalCount"],
            "repos": repos,
        })
    return lists


def fmt_stars(n: int) -> str:
    if n >= 1000:
        return f"{n/1000:.1f}k"
    return str(n)


def status_emoji(r: dict, now: datetime) -> str:
    if r.get("isArchived"):
        return "📦"
    pushed = r.get("pushedAt")
    if not pushed:
        return ""
    pushed_dt = datetime.fromisoformat(pushed.replace("Z", "+00:00"))
    age_days = (now - pushed_dt).days
    if age_days >= STALE_DAYS:
        return "💤"
    if age_days <= HOT_DAYS:
        return "🔥"
    return ""


_STATUS_RANK = {"🔥": 0, "": 1, "💤": 2, "📦": 3}


def fmt_row(r: dict, now: datetime) -> str:
    desc = (r.get("description") or "").strip().replace("|", "\\|").replace("\n", " ")
    if len(desc) > 110:
        desc = desc[:107] + "..."
    name = r["nameWithOwner"]
    url = r["url"]
    stars = fmt_stars(r.get("stargazerCount", 0))
    status = status_emoji(r, now)
    return f"| [`{name}`]({url}) | ⭐{stars} | {status} | {desc or '_(no description)_'} |"


def render(lists: list[dict]) -> str:
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    out = [
        "## ⭐ Curated Stars",
        "",
        "I keep my GitHub stars grouped by topic. Expand a section to browse what I've collected.",
        "",
    ]
    sorted_lists = sorted(
        lists,
        key=lambda l: (l["total"], sum(r.get("stargazerCount", 0) for r in l["repos"])),
        reverse=True,
    )
    for info in sorted_lists:
        if info["total"] == 0:
            continue
        out.append("<details>")
        summary = f'<summary><b>{info["name"]}</b> &nbsp;·&nbsp; {info["total"]} repos'
        if info.get("description"):
            summary += f' &nbsp;·&nbsp; <i>{info["description"]}</i>'
        summary += "</summary>"
        out.append(summary)
        out.append("")
        out.append("| Repo | Stars | Status | Description |")
        out.append("| --- | --- | --- | --- |")
        for r in sorted(info["repos"], key=lambda x: (_STATUS_RANK[status_emoji(x, now)], -x.get("stargazerCount", 0))):
            out.append(fmt_row(r, now))
        out.append("")
        out.append("</details>")
        out.append("")
    out.append(
        f"_Last updated: {today} · Status legend: 🔥 hot (pushed in last {HOT_DAYS} days) · 💤 stale (no push in {STALE_DAYS}+ days) · 📦 archived_"
    )
    return "\n".join(out)


def update_readme(block: str) -> bool:
    text = README.read_text()
    pattern = re.compile(re.escape(START) + r".*?" + re.escape(END), re.DOTALL)
    new_block = f"{START}\n{block}\n{END}"
    if not pattern.search(text):
        raise RuntimeError(f"Markers {START} / {END} not found in README.md")
    new_text = pattern.sub(lambda _m: new_block, text)
    if new_text == text:
        return False
    README.write_text(new_text)
    return True


def main() -> int:
    if "STARS_TOKEN" not in os.environ:
        print("STARS_TOKEN env var is required", file=sys.stderr)
        return 1
    lists = fetch_lists()
    block = render(lists)
    changed = update_readme(block)
    print("Updated README.md" if changed else "No changes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
