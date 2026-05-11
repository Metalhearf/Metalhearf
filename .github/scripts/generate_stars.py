#!/usr/bin/env python3
"""Rebuild the curated-stars catalog in README.md.

Two modes (selected via --mode):

  daily   — Detects list-membership changes and newly-starred repos.
            Reuses cached metadata for known repos (no churn on counts /
            descriptions / freshness badges between refreshes).

  refresh — Re-fetches per-repo metadata (stargazerCount, description,
            pushedAt, isArchived) for every starred repo and recomputes the
            freshness badge. Intended to run every 14 days.

Cache file: .github/stars_cache.json (committed). Holds metadata + a frozen
`status` badge per repo so daily renders stay stable until the next refresh.

Auth: STARS_TOKEN env var. Must be a classic PAT with `read:user` scope.
Fine-grained tokens do not work; the lists endpoint is undocumented and
only the classic PAT format honors it reliably.
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
README = REPO_ROOT / "README.md"
CACHE = REPO_ROOT / ".github" / "stars_cache.json"
START = "<!-- STARS:START -->"
END = "<!-- STARS:END -->"
STALE_DAYS = 365
HOT_DAYS = 7
UNSORTED_LABEL = "Unsorted"
UNSORTED_DESCRIPTION = "Starred but not yet sorted into any list."

RETRY_STATUS = {502, 503, 504}


def graphql(query: str, variables: dict | None = None, attempts: int = 4) -> dict:
    token = os.environ["STARS_TOKEN"]
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=body,
        headers={
            "Authorization": f"bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "metalhearf-profile",
        },
    )
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = json.loads(resp.read())
            if "errors" in payload:
                raise RuntimeError(f"GraphQL errors: {payload['errors']}")
            return payload["data"]
        except urllib.error.HTTPError as e:
            if e.code in RETRY_STATUS and attempt < attempts:
                wait = 2 ** attempt
                print(f"HTTP {e.code} (attempt {attempt}/{attempts}), retrying in {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            raise
        except urllib.error.URLError as e:
            if attempt < attempts:
                wait = 2 ** attempt
                print(f"Network error: {e} (attempt {attempt}/{attempts}), retrying in {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            raise
    raise RuntimeError("Exhausted retries")


REPO_FRAG_FULL = """
nameWithOwner
description
stargazerCount
url
isArchived
pushedAt
"""

REPO_FRAG_LIGHT = """
nameWithOwner
url
"""


def fetch_viewer() -> str:
    return graphql("query { viewer { login } }")["viewer"]["login"]


def _page_list_items(list_id: str, cursor: str, frag: str) -> dict:
    return graphql(
        """
        query($listId: ID!, $cursor: String) {
          node(id: $listId) {
            ... on UserList {
              items(first: 100, after: $cursor) {
                pageInfo { hasNextPage endCursor }
                nodes { __typename ... on Repository { %s } }
              }
            }
          }
        }
        """ % frag,
        {"listId": list_id, "cursor": cursor},
    )["node"]["items"]


def fetch_lists(full: bool) -> list[dict]:
    """Returns [{name, description, total, repos}]. repos is a list of dicts.
    full=True includes metadata fields; full=False is light (just identifiers).
    """
    frag = REPO_FRAG_FULL if full else REPO_FRAG_LIGHT
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
                  nodes { __typename ... on Repository { %s } }
                }
              }
            }
          }
        }
        """ % frag
    )
    lists = []
    for node in data["viewer"]["lists"]["nodes"]:
        repos = [n for n in node["items"]["nodes"] if n.get("__typename") == "Repository"]
        cursor = node["items"]["pageInfo"]["endCursor"]
        has_next = node["items"]["pageInfo"]["hasNextPage"]
        while has_next:
            page = _page_list_items(node["id"], cursor, frag)
            extra = [n for n in page["nodes"] if n.get("__typename") == "Repository"]
            repos.extend(extra)
            cursor = page["pageInfo"]["endCursor"]
            has_next = page["pageInfo"]["hasNextPage"]
        lists.append({
            "name": node["name"],
            "slug": node["slug"],
            "description": node.get("description") or "",
            "total": node["items"]["totalCount"],
            "repos": repos,
        })
    return lists


def fetch_starred(full: bool) -> dict[str, dict]:
    """Returns {nameWithOwner: repo_dict}. repo_dict has full metadata when
    full=True, otherwise just identifiers."""
    frag = REPO_FRAG_FULL if full else REPO_FRAG_LIGHT
    starred = {}
    cursor = None
    while True:
        data = graphql(
            """
            query($cursor: String) {
              viewer {
                starredRepositories(first: 100, after: $cursor) {
                  pageInfo { hasNextPage endCursor }
                  nodes { %s }
                }
              }
            }
            """ % frag,
            {"cursor": cursor},
        )
        page = data["viewer"]["starredRepositories"]
        for r in page["nodes"]:
            starred[r["nameWithOwner"]] = r
        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]
    return starred


def fetch_repo(name_with_owner: str) -> dict | None:
    """One-off full metadata fetch for a single repo. Used by daily mode when a
    newly-starred repo isn't yet in the cache."""
    owner, name = name_with_owner.split("/", 1)
    data = graphql(
        """
        query($owner: String!, $name: String!) {
          repository(owner: $owner, name: $name) {
            nameWithOwner description stargazerCount url isArchived pushedAt
          }
        }
        """,
        {"owner": owner, "name": name},
    )
    return data.get("repository")


def fmt_stars(n: int) -> str:
    if n >= 1000:
        return f"{n/1000:.1f}k"
    return str(n)


def compute_status(meta: dict, now: datetime) -> str:
    if meta.get("isArchived"):
        return "📦"
    pushed = meta.get("pushedAt")
    if not pushed:
        return ""
    pushed_dt = datetime.fromisoformat(pushed.replace("Z", "+00:00"))
    age_days = (now - pushed_dt).days
    if age_days >= STALE_DAYS:
        return "💤"
    if age_days <= HOT_DAYS:
        return "🔥"
    return ""


def to_cache_entry(repo: dict, now: datetime) -> dict:
    """Build a cache entry from a full-metadata repo dict."""
    return {
        "url": repo.get("url") or f"https://github.com/{repo['nameWithOwner']}",
        "description": repo.get("description") or "",
        "stargazerCount": repo.get("stargazerCount", 0),
        "pushedAt": repo.get("pushedAt"),
        "isArchived": bool(repo.get("isArchived")),
        "status": compute_status(repo, now),
    }


_STATUS_RANK = {"🔥": 0, "": 1, "💤": 2, "📦": 3}


def fmt_row(name: str, entry: dict) -> str:
    desc = (entry.get("description") or "").strip().replace("|", "\\|").replace("\n", " ")
    if len(desc) > 110:
        desc = desc[:107] + "..."
    url = entry.get("url") or f"https://github.com/{name}"
    stars = fmt_stars(entry.get("stargazerCount", 0))
    status = entry.get("status", "")
    return f"| [`{name}`]({url}) | ⭐{stars} | {status} | {desc or '_(no description)_'} |"


def render_section(name: str, description: str, total: int, repos: list[str], cache: dict) -> list[str]:
    lines = ["<details>"]
    summary = f'<summary><b>{name}</b> &nbsp;·&nbsp; {total} ⭐'
    if description:
        summary += f' &nbsp;·&nbsp; <i>{description}</i>'
    summary += "</summary>"
    lines.append(summary)
    lines.append("")
    lines.append("| Repo | Stars | Status | Description |")
    lines.append("| --- | --- | --- | --- |")
    sorted_repos = sorted(
        repos,
        key=lambda n: (
            _STATUS_RANK.get(cache.get(n, {}).get("status", ""), 1),
            -cache.get(n, {}).get("stargazerCount", 0),
        ),
    )
    for n in sorted_repos:
        entry = cache.get(n, {"url": f"https://github.com/{n}", "description": "", "stargazerCount": 0, "status": ""})
        lines.append(fmt_row(n, entry))
    lines.append("")
    lines.append("</details>")
    lines.append("")
    return lines


def render(lists: list[dict], unsorted: list[str], cache: dict) -> str:
    non_empty = [l for l in lists if l["total"] > 0]
    all_listed = {n for l in non_empty for n in l["repos"]}
    visible_repos = all_listed | set(unsorted)
    cat_count = len(non_empty) + (1 if unsorted else 0)
    out = [
        "## ⭐ Curated Stars",
        "",
        f"**{len(visible_repos)} repos** across **{cat_count} categories**. Click any section to expand.",
        "",
    ]
    sorted_lists = sorted(
        non_empty,
        key=lambda l: (l["total"], sum(cache.get(n, {}).get("stargazerCount", 0) for n in l["repos"])),
        reverse=True,
    )
    for info in sorted_lists:
        out.extend(render_section(info["name"], info["description"], info["total"], info["repos"], cache))
    if unsorted:
        out.extend(render_section(UNSORTED_LABEL, UNSORTED_DESCRIPTION, len(unsorted), unsorted, cache))
    refreshed = cache.get("_meta", {}).get("last_refresh_at", "never")
    if refreshed != "never":
        refreshed = refreshed.split("T")[0]
    out.append(
        f"<sub>"
        f"Metadata last refreshed: {refreshed}<br>"
        f"🔥 hot: pushed in last {HOT_DAYS} days<br>"
        f"💤 stale: no push in {STALE_DAYS}+ days<br>"
        f"📦 archived"
        f"</sub>"
    )
    return "\n".join(out)


def load_cache() -> dict:
    if not CACHE.exists():
        return {"_meta": {"last_refresh_at": None}}
    data = json.loads(CACHE.read_text())
    data.setdefault("_meta", {"last_refresh_at": None})
    return data


def save_cache(cache: dict) -> None:
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(cache, indent=2, sort_keys=True, ensure_ascii=False) + "\n")


def update_readme(block: str) -> bool:
    text = README.read_text()
    pattern = re.compile(re.escape(START) + r".*?" + re.escape(END), re.DOTALL)
    matches = list(pattern.finditer(text))
    if not matches:
        raise RuntimeError(f"Markers {START} / {END} not found in README.md")
    last = matches[-1]
    new_block = f"{START}\n{block}\n{END}"
    new_text = text[:last.start()] + new_block + text[last.end():]
    if new_text == text:
        return False
    README.write_text(new_text)
    return True


def lists_to_names(lists: list[dict]) -> list[dict]:
    """Strip per-repo dicts down to just nameWithOwner strings for rendering."""
    return [
        {
            "name": l["name"],
            "description": l["description"],
            "total": l["total"],
            "repos": [r["nameWithOwner"] for r in l["repos"]],
        }
        for l in lists
    ]


def run_refresh() -> None:
    print(f"Refreshing full metadata for @{fetch_viewer()}")
    now = datetime.now(timezone.utc)
    lists = fetch_lists(full=True)
    starred = fetch_starred(full=True)
    cache = {"_meta": {"last_refresh_at": now.isoformat()}}
    for l in lists:
        for r in l["repos"]:
            cache[r["nameWithOwner"]] = to_cache_entry(r, now)
    listed = set(cache.keys()) - {"_meta"}
    unsorted_names = sorted(set(starred.keys()) - listed)
    for n in unsorted_names:
        cache[n] = to_cache_entry(starred[n], now)
    save_cache(cache)
    block = render(lists_to_names(lists), unsorted_names, cache)
    print("Updated README.md" if update_readme(block) else "README unchanged")


def run_daily() -> None:
    print(f"Daily sync for @{fetch_viewer()}")
    now = datetime.now(timezone.utc)
    cache = load_cache()
    lists = fetch_lists(full=False)
    starred = fetch_starred(full=False)
    all_listed = {r["nameWithOwner"] for l in lists for r in l["repos"]}
    unsorted_names = sorted(set(starred.keys()) - all_listed)
    needed = (all_listed | set(unsorted_names)) - (set(cache.keys()) - {"_meta"})
    if needed:
        print(f"Fetching metadata for {len(needed)} new repo(s): {sorted(needed)}")
        for name in sorted(needed):
            meta = fetch_repo(name)
            if meta:
                cache[name] = to_cache_entry(meta, now)
    save_cache(cache)
    block = render(lists_to_names(lists), unsorted_names, cache)
    print("Updated README.md" if update_readme(block) else "README unchanged")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("daily", "refresh"), default="daily")
    args = parser.parse_args()
    if "STARS_TOKEN" not in os.environ:
        print("STARS_TOKEN env var is required", file=sys.stderr)
        return 1
    if args.mode == "refresh":
        run_refresh()
    else:
        run_daily()
    return 0


if __name__ == "__main__":
    sys.exit(main())
