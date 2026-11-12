"""Offline API-workflow schema check (stdlib only, no network).

Run from the repo root:
    python3 scripts/check_workflow_schema.py

Checks:
1. workflow_api.json at the repo root is byte-identical to
   workflows/api/workflow_api.json.
2. The root workflow is API format (no 'nodes'/'links' keys).
3. h3_contract.patch_api_workflow succeeds for variants A and B, and the
   patched workflow satisfies the pinned-node requirements in
   workflows/schemas/api_workflow.schema.json (required node ids,
   class_types, and the const inputs the schema pins).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    errors = []
    root_wf = ROOT / "workflow_api.json"
    api_wf = ROOT / "workflows" / "api" / "workflow_api.json"
    if root_wf.read_bytes() != api_wf.read_bytes():
        errors.append("workflow_api.json differs from workflows/api/workflow_api.json")
    raw = json.loads(root_wf.read_text())
    if "nodes" in raw or "links" in raw:
        errors.append("workflow_api.json is UI format, expected API format")
    schema = json.loads((ROOT / "workflows" / "schemas" / "api_workflow.schema.json").read_text())
    sys.path.insert(0, str(ROOT))
    from h3_contract import patch_api_workflow

    for variant in ("A", "B"):
        try:
            patched = patch_api_workflow(raw, variant, seed=42 if variant == "A" else 43)
        except Exception as e:  # noqa: BLE001
            errors.append(f"patch_api_workflow({variant}) failed: {e}")
            continue
        for key in schema["required"]:
            if key not in patched:
                errors.append(f"variant {variant}: missing required node {key}")
                continue
            want = schema["properties"].get(key, {})
            if patched[key].get("class_type") != want.get("properties", {}).get("class_type", {}).get("const"):
                errors.append(f"variant {variant}: node {key} class_type mismatch")
        node136 = patched.get("136", {}).get("inputs", {})
        if (node136.get("width"), node136.get("height"), node136.get("length")) != (736, 1312, 226):
            errors.append(f"variant {variant}: node 136 dims/frames mismatch")
    if errors:
        print("WORKFLOW SCHEMA CHECK FAILED:")
        for e in errors:
            print("  " + e)
        return 1
    print("WORKFLOW SCHEMA CHECK OK: root == api copy, API format, patch A/B satisfy pinned nodes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
