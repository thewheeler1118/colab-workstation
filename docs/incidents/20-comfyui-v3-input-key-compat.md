# Incident: ComfyUI V3 input key compatibility

STATUS: CONFIRMED_FIXED

## What happened

The validator was fixed for autogrow dotted keys (nodes 131/136) and the control-after-generate mode key 'fixed' (nodes 143/144). The patched workflow then passed live /object_info validation in the A run.

## Evidence

run_id: h3-20261112T090027Z-8b07c6 (COMFY_OBJECT_INFO_VALIDATED)
  file_path: h3_contract.py (patch_api_workflow); h3_remote_worker.validate_api_against_object_info
  line_or_function: h3_remote_worker.validate_api_against_object_info
  test_name: tests/test_contract.py
  source_report: (production-run evidence; validator fix predates this repo)
