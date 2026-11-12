"""Docs-index consistency check (stdlib only, no network).

Run from the repo root:
    python3 scripts/check_docs_index.py

Checks:
1. Every relative file path listed in the docs/module_inventory.md table
   exists in the repo.
2. SOURCE_MANIFEST.json verifies (delegates to scripts/verify_manifest.py).
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    errors = []
    inventory = ROOT / "docs" / "module_inventory.md"
    text = inventory.read_text()
    # Inventory rows look like: | `path/to/file` | size | sha | ...
    paths = sorted(set(re.findall(r"\| `(?!sha256:)([A-Za-z0-9_./\-]+)` \|", text)))
    for rel in paths:
        if not (ROOT / rel).exists():
            errors.append(f"module_inventory.md lists missing file: {rel}")
    if not paths:
        errors.append("module_inventory.md: no file paths found in table")
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "verify_manifest.py")],
                       capture_output=True, text=True)
    print(r.stdout.strip())
    if r.returncode != 0:
        errors.append("verify_manifest.py failed (see above)")
    if errors:
        print("DOCS INDEX CHECK FAILED:")
        for e in errors:
            print("  " + e)
        return 1
    print(f"DOCS INDEX CHECK OK: {len(paths)} inventory paths exist, manifest verifies.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
