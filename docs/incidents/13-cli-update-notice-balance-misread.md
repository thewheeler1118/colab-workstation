# Incident: Colab CLI update notice caused 0.8 CU balance misread

STATUS: CONFIRMED_FIXED

## What happened

The CLI intermittently prints an update-check notice ('available: 0.8.0') to stdout before the real output; the loose reference parser read it as a CU balance of 0.8, falsely tripping the hard-stop gate and killing a healthy G4 run on 2026-11-11. Fix: _strip_cli_notice drops notice lines and the strict parser requires the exact 'Current balance:' line.

## Evidence

run_id: (2026-11-11 G4 run killed by the false 0.8 reading; run id not recorded in the code comment)
  file_path: h3_controller.py
  line_or_function: h3_controller._strip_cli_notice / inspect / parse_usage_text
  test_name: test_h3_production.ProductionTest.test_inspect_ignores_cli_update_notice; test_loose_parser_no_longer_reads_version_as_balance
  source_report: h3_controller.inspect docstring
