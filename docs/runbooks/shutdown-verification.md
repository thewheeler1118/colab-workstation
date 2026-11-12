# Shutdown verification runbook (read-only)

Goal: prove no GPU session is still active and billing.

## Steps

1. `./scripts/verify_shutdown.sh [SESSION_ID]` — runs
   `python3 h3_controller.py check` (read-only `colab usage` + `colab sessions`)
   and fails unless the sessions list is empty. With a session id it also
   greps the raw `colab --auth=adc sessions` output.
2. If a session is still listed: stop it with the watchdog
   (`h3_session_watchdog.stop_session`, 3 attempts with re-checks) or
   `h3_controller.stop_and_verify_session`, then re-run this script.
   The watchdog's reconcile_on_startup NEVER auto-stops a session that is
   not in its own managed-session file — stopping is always explicit.
3. Confirm the CU balance: `./scripts/check_budget.sh`. The rate should be
   0.00/hr with no active session.

## Evidence to record

Run id, session id, the `check` JSON output, and the post-stop balance.
A shutdown is verified only when `sessions == []` AND the balance rate is 0.
