"""Independent backup GPU shutdown executor (TASK-006 P0-C, Issue #46).

Runs WITHOUT the main controller: it reads the lease registry
(Drive mirror first, local cache as degraded fallback) and stops
leases past their final stop deadline. It is meant to be driven by a
Muse platform cron (see docs/runbooks/independent-shutdown-cron.md) —
never enable that cron without the user's approval.

Safety rules (all enforced here, all covered by tests):
- Audit-only by default. Stops happen only with --confirm-stop.
- Only leases in the registry are ever stopped. The live session list
  is used solely for per-lease verdicts; the executor never enumerates
  it looking for things to stop.
- Only leases whose owner_id matches this host's ADC identity are
  stopped. An unestablishable owner fails closed (no stops at all).
- Only leases in the pass's production_task_id are stopped. A stop
  pass without --production-task-id is refused outright (exit 2, no
  stop attempted); an audit pass with a task id reports only that
  task's leases, the rest as out_of_task_scope.
- Only leases with a FINAL deadline (deadline_final=True) are stopped.
  A lease whose post-probe deadline update never landed keeps an early
  initial deadline and is never stopped here — except past its
  emergency cap (emergency_deadline_utc, TASK-007), where the executor
  queries the live session once: STOPPED archives the lease, ACTIVE or
  UNKNOWN enters the stop flow, a failed query skips it.
- Only leases past their stop deadline, and not already STOPPED, are
  stopped. A stop is confirmed ONLY by the three-state query returning
  STOPPED (TASK-006 P0-A rule); anything else stays pending with
  attempts +1 and a retry time.
- One executor claims a lease at a time (registry claim, 15-min TTL);
  a claim conflict means the other executor owns it.
- The executor never allocates, never creates a GPU, never runs
  inference. It only stops.

Exit codes: 0 = no overdue leases, or every overdue lease handled;
1 = overdue leases remain pending (surface it: the cron must report
this); 2 = configuration / registry / owner error (nothing was
stopped on this run).
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from h3_gpu_lease_registry import (  # noqa: E402
    DriveMirrorBackend,
    LeaseRegistry,
    current_owner_id,
)

REPORT_DIR = ROOT / "model_cache" / "h3_shutdown_executor"


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _parse_utc(value) -> datetime.datetime | None:
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(
            str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _default_stop(session_id: str) -> dict:
    from h3_session_watchdog import stop_session

    return stop_session(session_id)


def _executor_id() -> str:
    return "muse-h3-shutdown-executor-" + uuid.uuid4().hex[:6]


def audit_leases(registry: LeaseRegistry, now: datetime.datetime | None = None,
                 owner_id: str | None = None,
                 production_task_id: str | None = None,
                 query_fn=None) -> dict:
    """Read-only report: which leases are overdue, which would be
    stopped, which are skipped and why. Makes zero CLI stop calls.

    `production_task_id` scopes the pass: a lease from another task is
    skipped as out_of_task_scope and can never reach the stop loop.
    None means no filtering (audit callers that only report).

    Emergency cap (TASK-007): a lease whose deadline never finalised
    is normally never stopped — except past its absolute physical cap
    `emergency_deadline_utc` (started + max_wall_sec + 900 s). Past
    that cap the live session is queried once: STOPPED archives the
    lease (a registry bookkeeping write, still no stop call), ACTIVE
    or UNKNOWN marks it overdue for the stop loop with
    reason="emergency_unfinalized_deadline", and a failed query skips
    it — never stop what cannot be seen. A lease without an emergency
    cap keeps the old never-stop rule."""
    now = now or _now()
    leases, source, degraded = registry.load_for_executor()
    overdue = []
    skipped = []
    for lease in leases:
        sid = lease.get("session_id")
        state = lease.get("last_confirmed_state")
        if state == "STOPPED":
            skipped.append({"session_id": sid, "reason": "already_stopped"})
            continue
        if production_task_id is not None and \
                lease.get("production_task_id") != production_task_id:
            skipped.append({"session_id": sid, "reason": "out_of_task_scope"})
            continue
        deadline = _parse_utc(lease.get("stop_deadline_utc"))
        if deadline is None:
            skipped.append({"session_id": sid, "reason": "unparseable_deadline"})
            continue
        if now < deadline:
            skipped.append({"session_id": sid, "reason": "not_yet_due"})
            continue
        if lease.get("deadline_final"):
            retry_at = _parse_utc(lease.get("next_retry_at_utc"))
            if retry_at is not None and now < retry_at:
                skipped.append({"session_id": sid, "reason": "retry_not_due"})
                continue
            overdue.append(lease)
            continue
        # Non-final lease: only the emergency cap can make it stoppable.
        emergency = _parse_utc(lease.get("emergency_deadline_utc"))
        if emergency is None or now < emergency:
            skipped.append({"session_id": sid, "reason": "deadline_not_final"})
            continue
        retry_at = _parse_utc(lease.get("next_retry_at_utc"))
        if retry_at is not None and now < retry_at:
            skipped.append({"session_id": sid, "reason": "retry_not_due"})
            continue
        if query_fn is None:
            from h3_session_watchdog import query_session_state as query_fn
        try:
            live_state = query_fn(sid)
        except Exception as e:  # noqa: BLE001 - unobservable: never stop blind
            skipped.append({"session_id": sid,
                            "reason": "emergency_query_failed",
                            "error": type(e).__name__})
            continue
        state_name = getattr(live_state, "value", None) or str(live_state)
        if state_name == "STOPPED":
            try:
                registry.confirm_stopped(sid, "STOPPED")
            except Exception as e:  # noqa: BLE001 - archive failed, report it
                skipped.append({"session_id": sid,
                                "reason": "emergency_cleanup_failed",
                                "error": type(e).__name__})
                continue
            skipped.append({"session_id": sid,
                            "reason": "emergency_stopped_archived"})
            continue
        if state_name in ("ACTIVE", "UNKNOWN"):
            entry = dict(lease)
            entry["reason"] = "emergency_unfinalized_deadline"
            overdue.append(entry)
            continue
        skipped.append({"session_id": sid,
                        "reason": "emergency_query_failed",
                        "error": f"unexpected state {state_name!r}"})
    return {
        "action": "audit",
        "checked_at_utc": now.isoformat(),
        "registry_source": source,
        "registry_degraded_read": degraded,
        "owner_id": owner_id,
        "production_task_id": production_task_id,
        "leases_seen": len(leases),
        "overdue": [{"session_id": x.get("session_id"),
                     "run_id": x.get("run_id"),
                     "production_task_id": x.get("production_task_id"),
                     "gpu_model": x.get("gpu_model"),
                     "stop_deadline_utc": x.get("stop_deadline_utc"),
                     "stop_attempts": x.get("stop_attempts"),
                     **({"reason": x["reason"]} if x.get("reason") else {})}
                    for x in overdue],
        "skipped": skipped,
    }


def run_executor(registry: LeaseRegistry, confirm_stop: bool = False,
                 max_stops: int = 3, executor_id: str | None = None,
                 now: datetime.datetime | None = None,
                 stop_fn=None, owner_id_fn=None,
                 production_task_id: str | None = None,
                 query_fn=None) -> dict:
    """One backup-executor pass. Returns the report dict (also printed
    as JSON by main). `stop_fn` defaults to the watchdog's stop_session;
    tests inject a fake. `owner_id_fn` defaults to current_owner_id.
    `production_task_id` scopes the pass to one task (audit filter +
    stop filter); a stop pass without it is refused outright (exit_hint
    2, nothing stopped) — the executor must never stop a session from
    a task it was not told to cover. `query_fn` is the live-state query
    for the emergency path (tests inject a fake)."""
    now = now or _now()
    executor_id = executor_id or _executor_id()
    stop_fn = stop_fn or _default_stop
    owner_id_fn = owner_id_fn or current_owner_id

    if confirm_stop and production_task_id is None:
        return {"action": "stop",
                "checked_at_utc": now.isoformat(),
                "executor_id": executor_id,
                "error": "production_task_id is required for stop "
                         "--confirm-stop; refusing a cross-task stop",
                "stopped": [], "pending": [], "refused": [], "skipped": [],
                "exit_hint": 2}

    try:
        own_owner_id = owner_id_fn()
    except Exception as e:  # noqa: BLE001 - owner unknown: fail closed
        return {"action": "stop" if confirm_stop else "audit",
                "checked_at_utc": now.isoformat(),
                "executor_id": executor_id,
                "owner_error": type(e).__name__,
                "stopped": [], "pending": [], "refused": [], "skipped": [],
                "exit_hint": 2}

    report = audit_leases(registry, now=now, owner_id=own_owner_id,
                          production_task_id=production_task_id,
                          query_fn=query_fn)
    report["action"] = "stop" if confirm_stop else "audit"
    report["executor_id"] = executor_id
    report["stopped"] = []
    report["pending"] = []
    report["refused"] = []
    if not confirm_stop:
        report["note"] = ("audit only: no stop attempted; "
                          "re-run with --confirm-stop to act")
        return report

    stopped_count = 0
    for entry in report["overdue"]:
        if stopped_count >= max_stops:
            report["pending"].append(
                {**entry, "reason": "max_stops_reached"})
            continue
        sid = entry["session_id"]
        lease = registry.get_lease(sid)
        if lease is None:
            report["pending"].append({**entry, "reason": "lease_vanished"})
            continue
        if lease.get("owner_id") != own_owner_id:
            report["refused"].append(
                {**entry, "reason": "owner_mismatch"})
            continue
        claimed = registry.claim_lease(sid, executor_id)
        if claimed is None:
            report["pending"].append(
                {**entry, "reason": "claimed_by_other_executor"})
            continue
        try:
            result = stop_fn(sid)
        except Exception as e:  # noqa: BLE001 - stop failure stays pending
            result = {"state": "UNKNOWN", "error": type(e).__name__}
        state = (result or {}).get("state")
        if state == "STOPPED":
            try:
                registry.confirm_stopped(sid, "STOPPED")
            except Exception as e:  # noqa: BLE001 - confirmed stop stands
                report["pending"].append(
                    {**entry, "reason": "stop_confirmed_but_mark_failed",
                     "error": type(e).__name__})
                registry.release_claim(sid, executor_id)
                continue
            report["stopped"].append(
                {**entry, "attempts": (result or {}).get("attempts")})
            stopped_count += 1
        else:
            registry.note_stop_attempt(
                sid, (result or {}).get("error") or state or "UNKNOWN")
            report["pending"].append(
                {**entry, "reason": "stop_unconfirmed",
                 "state": state,
                 "error": (result or {}).get("error")})
        registry.release_claim(sid, executor_id)
    report["exit_hint"] = 1 if report["pending"] else 0
    return report


def _write_report(report: dict) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = _now().strftime("%Y%m%dT%H%M%SZ")
    path = REPORT_DIR / f"executor_{stamp}.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True),
                    encoding="utf-8")
    audit_path = REPORT_DIR / "executor_audit.jsonl"
    with open(audit_path, "a", encoding="utf-8") as fp:
        fp.write(json.dumps(report, sort_keys=True) + "\n")
    return path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Independent backup GPU shutdown executor (TASK-006 P0-C).")
    parser.add_argument("mode", nargs="?", default="audit",
                        choices=("audit", "stop"),
                        help="audit (default, read-only) or stop (acts only "
                             "with --confirm-stop)")
    parser.add_argument("--confirm-stop", action="store_true",
                        help="Required for mode=stop to actually stop "
                             "anything. Without it, stop mode behaves as audit.")
    parser.add_argument("--max-stops", type=int, default=3,
                        help="Max leases stopped in one pass (default 3).")
    parser.add_argument("--production-task-id", default=None,
                        help="Scope this pass to one production task. "
                             "REQUIRED for stop --confirm-stop (a stop pass "
                             "without it is refused outright); in audit "
                             "mode it filters the report to that task.")
    parser.add_argument("--registry", default=str(LeaseRegistry().path),
                        help="Local registry file path.")
    parser.add_argument("--no-mirror", action="store_true",
                        help="Do not read the Drive mirror (local cache only).")
    args = parser.parse_args(argv)

    if args.mode == "stop" and args.confirm_stop \
            and not args.production_task_id:
        print(json.dumps({"action": "stop",
                          "error": "production_task_id is required for "
                                   "stop --confirm-stop",
                          "detail": "refusing a cross-task stop"},
                         indent=2))
        return 2

    try:
        mirror = None if args.no_mirror else DriveMirrorBackend()
        registry = LeaseRegistry(path=Path(args.registry), mirror=mirror)
    except Exception as e:  # noqa: BLE001 - nothing can run without a registry
        print(json.dumps({"action": args.mode, "error": type(e).__name__,
                          "detail": "registry init failed"}, indent=2))
        return 2

    confirm = args.mode == "stop" and args.confirm_stop
    try:
        report = run_executor(registry, confirm_stop=confirm,
                              max_stops=args.max_stops,
                              production_task_id=args.production_task_id)
    except Exception as e:  # noqa: BLE001 - report the failure, do not crash bare
        print(json.dumps({"action": args.mode, "error": type(e).__name__,
                          "detail": "executor pass failed"}, indent=2))
        return 2
    try:
        report_path = _write_report(report)
        report["report_path"] = str(report_path)
    except Exception:  # noqa: BLE001 - report file is best-effort
        pass
    print(json.dumps(report, indent=2, sort_keys=True))
    if report.get("owner_error"):
        return 2
    return int(report.get("exit_hint", 0))


if __name__ == "__main__":
    raise SystemExit(main())
