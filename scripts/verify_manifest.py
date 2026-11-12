"""Recompute SOURCE_MANIFEST.json hashes and report drift.

Stdlib only. Run from the repo root:
    python3 scripts/verify_manifest.py

Exits 0 only when every file listed in SOURCE_MANIFEST.json exists with a
matching SHA-256 and size. Files present in the repo but missing from the
manifest are reported (excluding .git/ and SOURCE_MANIFEST.json itself).
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "SOURCE_MANIFEST.json"


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for part in iter(lambda: f.read(4 * 1024 * 1024), b""):
            h.update(part)
    return h.hexdigest()


def main() -> int:
    if not MANIFEST.is_file():
        print("SOURCE_MANIFEST.json missing", file=sys.stderr)
        return 2
    data = json.loads(MANIFEST.read_text())
    entries = data["files"]
    errors = []
    seen = set()
    for e in entries:
        rel = e["path"]
        seen.add(rel)
        p = ROOT / rel
        if not p.is_file():
            errors.append(f"MISSING: {rel}")
            continue
        actual_size = p.stat().st_size
        actual_sha = sha256_file(p)
        if actual_size != e["size_bytes"]:
            errors.append(f"SIZE MISMATCH: {rel} manifest={e['size_bytes']} actual={actual_size}")
        if actual_sha != e["sha256"]:
            errors.append(f"SHA MISMATCH: {rel}")
    for p in sorted(ROOT.rglob("*")):
        if not p.is_file():
            continue
        rel = str(p.relative_to(ROOT))
        if rel.startswith(".git/") or rel == "SOURCE_MANIFEST.json":
            continue
        if "__pycache__" in p.parts or p.suffix == ".pyc":
            continue  # gitignored bytecode, never part of the manifest
        if rel not in seen:
            errors.append(f"UNLISTED FILE (present but not in manifest): {rel}")
    if errors:
        print(f"MANIFEST VERIFY FAILED ({len(errors)} problem(s), {len(entries)} entries):")
        for e in errors:
            print("  " + e)
        return 1
    print(f"MANIFEST OK: {len(entries)} files, all sizes and SHA-256 match.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
