#!/usr/bin/env python3
"""Report translation keys present in en.json but missing from each other locale.

Maintainer tool. en.json is the source of truth; every other
translations/<lang>.json must carry the same flattened key set (the
test_every_locale_carries_every_en_key guard enforces this in CI). Run this
after adding a new string to en.json to see what still needs translating.

    uv run scripts/i18n_missing_keys.py          # print missing per locale
    uv run scripts/i18n_missing_keys.py --extra  # also list stale extra keys

Exit code is 1 when any locale is missing keys, so it can gate a commit.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

TRANSLATIONS = Path(__file__).resolve().parent.parent / "custom_components" / "pushward" / "translations"


def flatten(node: object, prefix: str = "") -> set[str]:
    keys: set[str] = set()
    if isinstance(node, dict):
        for key, val in node.items():
            path = f"{prefix}.{key}" if prefix else key
            if isinstance(val, dict):
                keys |= flatten(val, path)
            else:
                keys.add(path)
    return keys


def main() -> int:
    show_extra = "--extra" in sys.argv[1:]
    en = flatten(json.loads((TRANSLATIONS / "en.json").read_text()))

    had_missing = False
    for locale in sorted(TRANSLATIONS.glob("*.json")):
        if locale.name == "en.json":
            continue
        keys = flatten(json.loads(locale.read_text()))
        missing = sorted(en - keys)
        extra = sorted(keys - en)
        if missing:
            had_missing = True
            print(f"{locale.name}: {len(missing)} missing")
            for key in missing:
                print(f"  - {key}")
        if show_extra and extra:
            print(f"{locale.name}: {len(extra)} extra (stale)")
            for key in extra:
                print(f"  + {key}")

    if not had_missing:
        print("all locales carry every en.json key")
    return 1 if had_missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
