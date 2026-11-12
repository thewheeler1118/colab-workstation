# Issue draft: P1: ComfyUI API workflow contract tests

STATUS: PROPOSED_ONLY (schema check and contract tests exist; the drifted-node / missing-object_info cases are the new work).

## Context

h3_contract.py — patch_api_workflow; h3_remote_worker.py — validate_api_against_object_info. Historical run: h3-20261112T062103Z-901bc6 (/prompt HTTP 400, Turbo LoRA branch).

## Reproduction

1. Feed patch_api_workflow a workflow with a drifted node id/type and assert it refuses.
2. Feed validate_api_against_object_info a workflow whose node is missing from object_info and assert it refuses.
3. Confirm scripts/check_workflow_schema.py covers the pinned nodes (92/124/127/128/129/136/137/139/146).

## Acceptance

Acceptance: a drifted-node test and a missing-object_info test exist and pass offline; the schema check runs in CI. Expected impact: workflow drift is caught before a paid GPU session, not by a live /prompt 400.

## Process

Issue → feature branch → tests → pull request → review → merge. Never commit directly to main.
