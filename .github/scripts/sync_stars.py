#!/usr/bin/env python3
"""Copy the rendered stars block from awesome-stars into the profile README.

Reads /tmp/upstream.md (downloaded by the workflow), extracts the last marker
pair and pastes it into README.md at the same position. No GraphQL calls; the
upstream README is public.
"""
import re
import sys
from pathlib import Path

START = "<!-- STARS:START -->"
END = "<!-- STARS:END -->"

PROFILE = Path(__file__).resolve().parents[2] / "README.md"
UPSTREAM = Path("/tmp/upstream.md")


def last_block(text: str) -> str:
    pattern = re.compile(re.escape(START) + r".*?" + re.escape(END), re.DOTALL)
    matches = list(pattern.finditer(text))
    if not matches:
        raise SystemExit(f"Markers {START} / {END} not found")
    return matches[-1].group(0)


def main() -> int:
    block = last_block(UPSTREAM.read_text())
    text = PROFILE.read_text()
    pattern = re.compile(re.escape(START) + r".*?" + re.escape(END), re.DOTALL)
    matches = list(pattern.finditer(text))
    if not matches:
        print(f"Markers {START} / {END} not found in profile README", file=sys.stderr)
        return 1
    last = matches[-1]
    new_text = text[:last.start()] + block + text[last.end():]
    if new_text == text:
        print("No changes")
        return 0
    PROFILE.write_text(new_text)
    print("Updated profile README")
    return 0


if __name__ == "__main__":
    sys.exit(main())
