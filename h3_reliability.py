"""TASK-005 §12 (Issue #40): reliability metrics from durable evidence only.

The five brief §12 rates, computed from run manifests, their audit logs,
and budget ledgers under a root directory. No rate is ever claimed from
unit tests, Mock tests, or fault-injection runs: those prove code paths,
not production reliability.

Definitions
-----------
- A first_attempt_success_rate: terminal runs reaching TASK_COMPLETED
  with recovery_generation == 0 / all terminal runs started.
- B eventual_task_success_rate: terminal runs reaching TASK_COMPLETED
  (including via resume or archive-only recovery) / all terminal runs
  started. This is the 95% target metric.
- C artifact_preservation_rate: terminal runs whose validated local MP4
  survived (delivery_state at least COMPLETE_LOCAL, and — when an
  output root is given — the local file still exists with the recorded
  SHA-256) / terminal runs that produced a validated local MP4.
- D verified_shutdown_rate: terminal runs with a session allocated
  whose audit log records a GPU_STOP_CONFIRMED stage / terminal runs
  with a session allocated. The audit event is the evidence: reaching
  DRIVE_VERIFIED via the stopped-session recovery path does NOT record
  GPU_STOP_CONFIRMED, and must not count.
- E budget_compliance_rate: runs whose task ledger reports
  max_observed_spent_cu <= project_budget_cu (30.00 CU) / runs whose
  task ledger exists. The ledger is per-task, so runs inherit their
  task's verdict; runs with no readable ledger are excluded from the
  denominator and listed separately.

Rules
-----
- In-flight (non-terminal) runs are excluded from every denominator and
  reported separately: an unfinished run is neither a success nor a
  failure yet.
- denominator == 0 -> rate None, status UNKNOWN, with the reason named.
- 0 < denominator < 30 -> status INSUFFICIENT_SAMPLE. 30 is a
  conventional minimum for quoting a proportion as a reliability claim,
  not a statistical guarantee; the rate and its Wilson interval are
  still reported.
- denominator >= 30 -> status MEASURED.
- target_95_verified is True only when B is measured and its 95%
  Wilson lower bound is >= 0.95. With zero failures that needs about
  73 completed runs (60/60 only reaches a 0.9398 lower bound); any
  failure needs more. The module reports the interval; it never rounds
  a small-sample rate up to a claim.

Current honest baseline (2026-11-13): the only real production runs
(A first-attempt success, B interrupted by a VM restart) predate the
TASK-005 manifests, so the production sample for the new architecture
is n = 0 and all five rates are UNKNOWN.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import h3_run_manifest as rm
from h3_budget import PROJECT_BUDGET_CU, ledger_path_for_task

WILSON_Z = 1.96
TARGET_RATE = 0.95
MIN_SAMPLE_FOR_MEASURED = 30

# Delivery states at or after which a validated local MP4 exists.
_DELIVERY_AT_LEAST_LOCAL = {
    "COMPLETE_LOCAL",
    "DURABLE_UPLOAD_STARTED",
    "DURABLE_UPLOAD_VERIFIED",
    "DELIVERED",
}


def wilson_interval(k: int, n: int, z: float = WILSON_Z) -> list[float] | None:
    """95% Wilson score interval for k successes in n trials, or None."""
    if n <= 0:
        return None
    p = k / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return [round(max(0.0, (centre - margin) / denom), 4),
            round(min(1.0, (centre + margin) / denom), 4)]


def _metric(k: int, n: int, definition: str,
            unknown_reason: str | None = None) -> dict:
    if n == 0:
        return {"definition": definition, "rate": None, "numerator": k,
                "denominator": 0, "confidence_interval_95": None,
                "status": "UNKNOWN",
                "unknown_reason": unknown_reason or "empty sample"}
    interval = wilson_interval(k, n)
    status = ("MEASURED" if n >= MIN_SAMPLE_FOR_MEASURED
              else "INSUFFICIENT_SAMPLE")
    return {"definition": definition, "rate": round(k / n, 4),
            "numerator": k, "denominator": n,
            "confidence_interval_95": interval, "status": status,
            "unknown_reason": None}


def _audit_stages(root: Path, run_id: str) -> set[str]:
    try:
        return {e.get("detail", {}).get("to")
                for e in rm.read_audit_log(root, run_id)
                if e.get("event") == "STAGE"}
    except Exception:
        return set()


def _load_manifests(root: Path, production_task_id: str | None = None,
                    since: str | None = None,
                    until: str | None = None) -> list[dict]:
    mdir = rm.manifest_dir(root)
    if not mdir.is_dir():
        return []
    out = []
    for path in sorted(mdir.glob("*.json")):
        if path.name.endswith(".audit.jsonl"):
            continue
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(manifest, dict) or not manifest.get("run_id"):
            continue
        if production_task_id and \
                manifest.get("production_task_id") != production_task_id:
            continue
        created = manifest.get("created_at_utc") or ""
        if since and created < since:
            continue
        if until and created > until:
            continue
        out.append(manifest)
    return out


def _ledger_compliant(root: Path, task_id: str) -> bool | None:
    """True/False from the task ledger; None when it is missing/unreadable."""
    try:
        path = ledger_path_for_task(root, task_id)
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        budget = float(data.get("project_budget_cu", PROJECT_BUDGET_CU))
        spent = float(data.get("max_observed_spent_cu"))
        return spent <= budget
    except Exception:
        return None


def compute_reliability(root: str | Path, production_task_id: str | None = None,
                        since: str | None = None, until: str | None = None,
                        output_root: str | Path | None = None) -> dict:
    """Compute the five §12 rates over the manifests under `root`.

    `output_root`, when given, is the directory holding
    `<run_id>/<variant>.mp4` files; metric C then also verifies the
    local file still exists with the recorded SHA-256. Without it, C
    is judged from manifest states only.
    """
    root = Path(root)
    output_root = Path(output_root) if output_root else None
    manifests = _load_manifests(root, production_task_id, since, until)
    terminal = [m for m in manifests
                if m.get("task_state") in rm.TERMINAL_STATES]
    in_flight = [m for m in manifests
                 if m.get("task_state") not in rm.TERMINAL_STATES]
    created = sorted(m.get("created_at_utc") or ""
                     for m in manifests if m.get("created_at_utc"))

    completed = [m for m in terminal
                 if m.get("task_state") == "TASK_COMPLETED"]
    failed = [m for m in terminal if m.get("task_state") == "FAILED"]
    awaiting = [m for m in terminal
                if m.get("task_state") == "COMPLETED_AWAITING_ARCHIVE"]

    metric_a = _metric(
        sum(1 for m in completed if m.get("recovery_generation", 0) == 0),
        len(terminal),
        "TASK_COMPLETED with recovery_generation == 0 / terminal runs",
        unknown_reason="no terminal runs in scope")
    metric_b = _metric(
        len(completed), len(terminal),
        "TASK_COMPLETED (incl. via resume / archive-only) / terminal runs",
        unknown_reason="no terminal runs in scope")

    produced = []
    preserved = []
    for m in terminal:
        artifacts = m.get("artifacts") or {}
        if artifacts.get("local_sha256") and \
                artifacts.get("delivery_state") in _DELIVERY_AT_LEAST_LOCAL:
            produced.append(m)
            ok = True
            if output_root is not None:
                variant = m.get("variant") or "A"
                local = (output_root / m["run_id"] / f"{variant}.mp4")
                try:
                    ok = local.is_file() and \
                        hashlib.sha256(local.read_bytes()).hexdigest() == \
                        artifacts["local_sha256"]
                except Exception:
                    ok = False
            if ok:
                preserved.append(m)
    metric_c = _metric(
        len(preserved), len(produced),
        "validated local MP4 survived / runs that produced a validated MP4",
        unknown_reason="no run in scope produced a validated local MP4")

    allocated = [m for m in terminal if m.get("session_id")]
    verified_shutdowns = sum(
        1 for m in allocated
        if "GPU_STOP_CONFIRMED" in _audit_stages(root, m["run_id"]))
    metric_d = _metric(
        verified_shutdowns, len(allocated),
        "GPU_STOP_CONFIRMED in audit log / terminal runs with a session",
        unknown_reason="no terminal run in scope allocated a GPU")

    e_numerator = 0
    e_denominator = 0
    e_unknown_runs: list[str] = []
    ledger_cache: dict[str, bool | None] = {}
    for m in terminal:
        task_id = m.get("production_task_id") or ""
        if task_id not in ledger_cache:
            ledger_cache[task_id] = _ledger_compliant(root, task_id)
        verdict = ledger_cache[task_id]
        if verdict is None:
            e_unknown_runs.append(m["run_id"])
        else:
            e_denominator += 1
            if verdict:
                e_numerator += 1
    metric_e = _metric(
        e_numerator, e_denominator,
        "task ledger max_observed_spent_cu <= project_budget_cu / "
        "terminal runs with a readable task ledger",
        unknown_reason="no terminal run in scope has a readable task ledger")

    target_verified = bool(
        metric_b["status"] == "MEASURED"
        and metric_b["confidence_interval_95"] is not None
        and metric_b["confidence_interval_95"][0] >= TARGET_RATE)
    if metric_b["status"] == "UNKNOWN":
        target_evidence = ("No terminal production runs in scope; "
                           "the 95% target is unverified.")
    elif metric_b["status"] == "INSUFFICIENT_SAMPLE":
        target_evidence = (
            f"Only {metric_b['denominator']} terminal run(s) in scope; "
            f"Wilson 95% interval for eventual success is "
            f"{metric_b['confidence_interval_95']} — too small a sample "
            f"to verify a 95% rate.")
    elif target_verified:
        target_evidence = (
            f"Wilson 95% lower bound for eventual success is "
            f"{metric_b['confidence_interval_95'][0]} (>= 0.95) over "
            f"{metric_b['denominator']} terminal runs.")
    else:
        target_evidence = (
            f"Wilson 95% lower bound for eventual success is "
            f"{metric_b['confidence_interval_95'][0]} (< 0.95) over "
            f"{metric_b['denominator']} terminal runs; "
            f"the 95% target is not verified.")

    return {
        "root": str(root),
        "sample_scope": {"production_task_id": production_task_id,
                         "since": since, "until": until},
        "sample_size": len(terminal),
        "runs_sampled": len(manifests),
        "observation_dates": [created[0], created[-1]] if created else None,
        "breakdown": {"completed": len(completed), "failed": len(failed),
                      "awaiting_archive": len(awaiting),
                      "in_flight": len(in_flight)},
        "in_flight_run_ids": sorted(m["run_id"] for m in in_flight),
        "metrics": {
            "first_attempt_success_rate": metric_a,
            "eventual_task_success_rate": metric_b,
            "artifact_preservation_rate": metric_c,
            "verified_shutdown_rate": metric_d,
            "budget_compliance_rate": metric_e,
        },
        "target_95_verified": target_verified,
        "target_95_evidence": target_evidence,
        "e_unknown_ledger_runs": sorted(e_unknown_runs),
    }
