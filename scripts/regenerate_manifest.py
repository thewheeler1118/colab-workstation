"""Regenerate SOURCE_MANIFEST.json deterministically from the worktree.

Stdlib only. Run from the repo root after any tracked-file change:

    python3 scripts/regenerate_manifest.py
    python3 scripts/verify_manifest.py

Rules (mirror scripts/verify_manifest.py):
- Every file in the repo except SOURCE_MANIFEST.json itself, anything
  under .git/, and __pycache__ / *.pyc bytecode is listed, sorted by path,
  with size_bytes and SHA-256.
- The manifest never lists itself (no self-hash cycle).
- `role` is preserved from the previous manifest for paths it already
  knew; new paths get a role from ROLE_DEFAULTS (edit the table when a
  genuinely new kind of file appears — do not hand-edit hashes).
- `generated_utc` is today's UTC date; `source` / `note` are preserved
  from the previous manifest (update them deliberately, in the same
  commit, when the baseline they describe actually changes).
"""
from __future__ import annotations

import datetime
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "SOURCE_MANIFEST.json"

ROLE_DEFAULTS = [
    (".github/", "CI / templates"),
    ("patches/", "Vendored CLI patch"),
    ("tests/fixtures/", "Test fixture (pristine upstream source, read-only)"),
    ("tests/", "Tests"),
    ("scripts/", "Operator wrapper / offline check"),
    ("docs/", "Documentation / evidence"),
    ("config/", "Configuration (mirrors code constants; asserted by tests)"),
    ("requirements-ci.txt", "CI-only offline test dependencies (pins from requirements.lock)"),
    ("requirements-local.txt", "Direct dependency floors (local/CPU env only)"),
    ("requirements.lock", "Pinned dependencies (verified source venv)"),
]


def role_for(rel: str, old_roles: dict) -> str:
    if rel in old_roles:
        return old_roles[rel]
    for prefix, role in ROLE_DEFAULTS:
        if rel == prefix or rel.startswith(prefix):
            return role
    return "Supporting file"


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for part in iter(lambda: f.read(4 * 1024 * 1024), b""):
            h.update(part)
    return h.hexdigest()


def main() -> int:
    old = {}
    header = {}
    if MANIFEST.is_file():
        data = json.loads(MANIFEST.read_text())
        old = {e["path"]: e["role"] for e in data.get("files", [])}
        header = {k: v for k, v in data.items() if k != "files"}
    header.setdefault("source", "~/workspace/Muse_Colab_H3_Drive_v3/ (production baseline)")
    header.setdefault(
        "note",
        "Every file in this repo except SOURCE_MANIFEST.json itself. Regenerate after any change; "
        "verify with scripts/verify_manifest.py. Excluded by policy and therefore absent: model weights, "
        "poster binary, MP4s, live ledgers/sessions/depot state, .venv, __pycache__.",
    )
    header["generated_utc"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")

    files = []
    for p in sorted(ROOT.rglob("*")):
        if not p.is_file():
            continue
        rel = str(p.relative_to(ROOT))
        if rel.startswith(".git/") or rel == "SOURCE_MANIFEST.json":
            continue
        if "__pycache__" in p.parts or p.suffix == ".pyc":
            continue
        files.append(
            {
                "path": rel,
                "size_bytes": p.stat().st_size,
                "sha256": sha256_file(p),
                "role": role_for(rel, old),
            }
        )
    out = {**header, "files": files}
    MANIFEST.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n")
    print(f"MANIFEST REGENERATED: {len(files)} files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
