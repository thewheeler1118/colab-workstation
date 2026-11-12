# Incident: Colab allocation request modified by proxy → HTTP 411

STATUS: CONFIRMED_FIXED

## What happened

The VM egress proxy strips Content-Length: 0 from empty POSTs; Google's frontend returned 411 Length Required for /tun/m/assign and /tun/m/unassign, and empty POSTs failed 4/4 (ProxyError / RemoteDisconnected / ChunkedEncodingError).

## Evidence

run_id: (2026-11-11 live isolation, no GPU run id; assign 200 / unassign 204 after fix)
  file_path: patches/google-colab-cli-0.7.5/proxy_post_body.patch (colab_cli/client.py, Client._issue_request)
  line_or_function: h3_controller.cli_post_body_patch_present
  test_name: test_h3_production.ProductionTest.test_cli_post_body_patch_present; test_h3_mount_repair (test_post_body_patch_still_present)
  source_report: docs/audits/v3_cli_proxy_patch.md
