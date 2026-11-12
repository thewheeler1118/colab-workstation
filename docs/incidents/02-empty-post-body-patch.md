# Incident: Empty POST body proxy-compatibility patch

STATUS: CONFIRMED_FIXED

## What happened

Vendored patch: POSTs without an explicit body send kwargs['data'] = b'{}' (Content-Length: 2, preserved by the proxy; the Colab backend ignores the body). GETs and POSTs with a body are untouched.

## Evidence

run_id: (2026-11-11 live: invalid token → 400, valid G4 token → 503, valid A100 token → 200 in 2.7s, unassign → 204)
  file_path: patches/google-colab-cli-0.7.5/proxy_post_body.patch
  line_or_function: h3_controller.cli_post_body_patch_present (fail-closed preflight gate)
  test_name: test_h3_production.ProductionTest.test_cli_post_body_patch_present
  source_report: docs/audits/v3_cli_proxy_patch.md
