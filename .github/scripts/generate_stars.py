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
from pathlib import Path

USER = "Metalhearf"
README = Path(__file__).resolve().parents[2] / "README.md"
START = "<!-- STARS:START -->"
END = "<!-- STARS:END -->"

ICONS = {
    "Self-Hosted": "🏠",
    "Tools": "🔧",
    "Games": "🎮",
    "Hacking": "🛡️",
    "Arch": "🐧",
    "Media": "🎵",
    "Resources": "📚",
    "Fun": "🎉",
    "Work": "💼",
    "OSINT": "🕵️",
    "Android": "🤖",
    "Privacy": "🔒",
    "AI": "🧠",
    "GitHub": "🐙",
    "Blog": "✍️",
}

ORDER = [
    "Self-Hosted", "Tools", "Hacking", "OSINT", "Privacy",
    "Arch", "Android", "AI",
    "Media", "Games", "Fun",
    "Resources", "Work", "GitHub", "Blog",
]


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
  primaryLanguage { name }
  url
}
"""


def fetch_lists() -> list[dict]:
    data = graphql(
        """
        query {
          viewer {
            lists(first: 100) {
              nodes {
                id name slug
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
            "total": node["items"]["totalCount"],
            "repos": repos,
        })
    return lists


def fmt_stars(n: int) -> str:
    if n >= 1000:
        return f"{n/1000:.1f}k"
    return str(n)


def fmt_row(r: dict) -> str:
    desc = (r.get("description") or "").strip().replace("|", "\\|").replace("\n", " ")
    if len(desc) > 110:
        desc = desc[:107] + "..."
    lang = (r.get("primaryLanguage") or {}).get("name") or ""
    name = r["nameWithOwner"]
    url = r["url"]
    return f"| [`{name}`]({url}) | {lang} | ⭐{fmt_stars(r.get('stargazerCount', 0))} | {desc or '_(no description)_'} |"


def render(lists: list[dict]) -> str:
    by_name = {l["name"]: l for l in lists}
    out = [
        "## ⭐ Curated Stars",
        "",
        "I keep my GitHub stars grouped by topic. Expand a section to browse what I've collected.",
        "",
    ]
    for name in ORDER:
        info = by_name.get(name)
        if not info:
            continue
        icon = ICONS.get(name, "📁")
        list_url = f"https://github.com/stars/{USER}/lists/{info['slug']}"
        out.append("<details>")
        out.append(
            f'<summary><b>{icon} {name}</b> &nbsp;·&nbsp; {info["total"]} repos &nbsp;·&nbsp; '
            f'<a href="{list_url}">view on GitHub →</a></summary>'
        )
        out.append("")
        out.append("| Repo | Lang | Stars | Description |")
        out.append("| --- | --- | --- | --- |")
        for r in sorted(info["repos"], key=lambda x: x.get("stargazerCount", 0), reverse=True):
            out.append(fmt_row(r))
        out.append("")
        out.append("</details>")
        out.append("")
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
