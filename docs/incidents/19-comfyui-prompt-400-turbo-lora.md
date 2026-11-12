# Incident: ComfyUI /prompt HTTP 400 and Turbo LoRA residue

STATUS: CONFIRMED_FIXED

## What happened

The server validates the whole reachable graph including the unselected branch. Node 145 (Turbo LoRA, 1.82 GB, not in the 4-file depot) made /prompt reject the submission with HTTP 400. Fix: patch_api_workflow deletes node 145 when it is a LoraLoaderModelOnly and re-points node 141 (ComfySwitchNode) at the plain model (node 127); node 146 forces the switch off.

## Evidence

run_id: h3-20261112T062103Z-901bc6 (HTTP 400); h3-20261112T090027Z-8b07c6 (/prompt no longer 400)
  file_path: h3_contract.py
  line_or_function: h3_contract.patch_api_workflow (node 145/141 handling)
  test_name: tests/test_contract.py (patch contract)
  source_report: h3_contract.patch_api_workflow comment
