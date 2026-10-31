"""Check the built wiki for broken links.

Run after `npx quartz build`:  python3 check_links.py
"""

import json
import sys
from pathlib import Path

INDEX = Path(__file__).parent / "public" / "static" / "contentIndex.json"


def simplify(slug: str) -> str:
    """Match Quartz's simplified slugs: 'a/index' -> 'a/', 'index' -> '/'."""
    if slug == "index":
        return "/"
    return slug[: -len("index")] if slug.endswith("/index") else slug


def main() -> int:
    if not INDEX.exists():
        print(f"✗ {INDEX} not found. Run `npx quartz build` first.")
        return 1

    pages = {k: v for k, v in json.loads(INDEX.read_text()).items() if not k.startswith("tags/")}
    known = {simplify(k) for k in pages}

    broken = []
    for slug, page in sorted(pages.items()):
        missing = [link for link in page.get("links", []) if link not in known]
        if missing:
            broken.append((slug, missing))

    if broken:
        print(f"✗ {len(broken)} page(s) with broken links:")
        for slug, missing in broken:
            print(f"  {slug}")
            for link in missing:
                print(f"    → {link}")
        print("  Hint: link Nodes by full path, e.g. [[Section/Topic/index|Topic]].")
        return 1

    print(f"✓ {len(pages)} pages, all links resolve.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
