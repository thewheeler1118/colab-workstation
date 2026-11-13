"""Bounded real technical acceptance for the patched Drive mount.

Drives ONLY the mount path of the production controller, reusing its
gates, allocation loop, watchdog, GPU probe, signature probe, official
drivemount (patched remote timeout_ms=600000), drive probe, and stop
logic. It NEVER installs ComfyUI, NEVER copies the full 40GB, and NEVER
runs inference. After the drive probe it runs the 256MB read-throughput
probe and projects the full 40GB restore cost, then stops the GPU.

Usage (under a real PTY, background exec):
  script -qec "python h3_mount_acceptance.py --run-id <RUN_ID> \
    --execute --confirm-gpu --authorize-task-gpu --allow-g4 \
    --allow-a100-highmem --approve-interactive-drive-mount" /dev/null
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from h3_budget import (
    PROJECT_BUDGET_CU,
    PER_SESSION_BUDGET_CU,
    MAX_SESSION_SECONDS,
    allocation_command,
    choose_gpu_candidate,
    estimate_unbilled_cu,
    is_capacity_failure,
    is_transient_transport_error,
    reconcile_budget,
    allowed_runtime_seconds,
)
from h3_controller import (
    ROOT,
    RUNS,
    REMOTE,
    LEDGER_PATH,
    record,
    run_cli,
    inspect,
    exec_script,
    probe_actual_gpu,
    check_mount_signature,
    mount_drive,
    validate_local,
    ensure_no_foreign_sessions,
    initialize_project_budget,
    stop_and_verify_session,
    create_run_id,
)
from h3_session_watchdog import reconcile_on_startup, record_session
from h3_remote_worker import estimate_restore

TOTAL_MODEL_BYTES = 40_073_842_159


def parse_restore_probe(text: str) -> dict:
    out = {}
    for key, pat in (
        ("disk_free_bytes", r"RESTORE_DISK_FREE_BYTES:\s*(\d+)"),
        ("file", r"RESTORE_256MB_FILE:\s*(\S+)"),
        ("bytes_256mb", r"RESTORE_256MB_BYTES:\s*(\d+)"),
        ("sec_256mb", r"RESTORE_256MB_SEC:\s*([0-9.]+)"),
        ("mbps_256mb", r"RESTORE_256MB_MBPS:\s*([0-9.]+)"),
    ):
        m = re.search(pat, text or "")
        if m:
            out[key] = m.group(1)
    if "disk_free_bytes" in out:
        out["disk_free_bytes"] = int(out["disk_free_bytes"])
    for k in ("bytes_256mb",):
        if k in out:
            out[k] = int(out[k])
    for k in ("sec_256mb", "mbps_256mb"):
        if k in out:
            out[k] = float(out[k])
    out["ok"] = "RESTORE_PROBE_OK" in (text or "")
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--run-id", required=True)
    p.add_argument("--drive-cache-dir", default="/content/drive/MyDrive/Muse-H3-Model-Depot")
    p.add_argument("--comfy-ref", default="61e524bdba7a4e193b45c2697f1af83c1beb86d6")
    p.add_argument("--project-budget-cu", type=float, default=30.0)
    p.add_argument("--per-session-budget-cu", type=float, default=25.0)
    p.add_argument("--gpu-policy", default="G4_THEN_A100_HIGHMEM")
    p.add_argument("--max-rate-cu-h", type=float, default=14.0)
    p.add_argument("--max-wall-sec", type=int, default=14400)
    p.add_argument("--approve-interactive-drive-mount", action="store_true")
    for flag in ("execute", "confirm-gpu", "authorize-task-gpu", "allow-g4", "allow-a100-highmem"):
        p.add_argument("--" + flag, action="store_true")
    args = p.parse_args()

    if not all((args.execute, args.confirm_gpu, args.authorize_task_gpu, args.allow_g4, args.allow_a100_highmem)):
        raise RuntimeError("GPU blocked: require --execute --confirm-gpu --authorize-task-gpu --allow-g4 --allow-a100-highmem")
    if float(args.project_budget_cu) != 30.0 or float(args.per_session_budget_cu) != 25.0:
        raise RuntimeError("Project budget must be 30 CU, per-session 25 CU")
    if args.gpu_policy != "G4_THEN_A100_HIGHMEM":
        raise RuntimeError("gpu-policy must be G4_THEN_A100_HIGHMEM")
    if not args.drive_cache_dir.startswith("/content/drive/"):
        raise RuntimeError("Expected explicitly mounted Drive cache path under /content/drive")
    if not args.approve_interactive_drive_mount:
        raise RuntimeError("G4 blocked: --approve-interactive-drive-mount required")

    # Read-only gates (same as run_variant, minus the inference workflow).
    validate_local(ROOT / "workflow_api.json", ROOT / "model_cache" / "verified_manifest.json")
    from drive_depot import drive_service, account_matches, verify_ready

    drive = drive_service()
    if not account_matches(drive, "adc"):
        raise RuntimeError("ACCOUNT_MISMATCH")
    verify_ready(drive, True, ROOT / "model_cache" / "verified_manifest.json")
    ledger = initialize_project_budget(args.run_id)
    pre = inspect()
    if pre["cu_balance"] is None:
        raise RuntimeError("Cannot parse CU balance; abort")
    ensure_no_foreign_sessions(pre)
    rec = reconcile_on_startup("adc")
    if rec.get("managed_active_sessions"):
        raise RuntimeError(f"Managed GPU sessions still active: {rec['managed_active_sessions']}; stop them first")
    probe = run_cli(["drivemount", "--help"], timeout=20, check=False)
    if probe["returncode"] != 0:
        raise RuntimeError("Official Colab drivemount unavailable")
    if not sys.stdin.isatty():
        raise RuntimeError("Official drivemount requires interactive terminal; no TTY, abort BEFORE GPU allocation")

    run_dir = RUNS / args.run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    # Persist every remote exec result to this run dir (same rule as run_variant).
    import h3_controller as _hc

    _hc._EXEC_LOG_DIR = run_dir
    session = None
    created = False
    allocation_attempted = False
    stop_ok = False
    watchdog_proc = None
    gpu_info = None
    chosen_key = None
    attempts = []
    start = time.monotonic()
    session_spent_start = float(ledger.get("max_observed_spent_cu", 0.0))
    timeline = None
    result = {
        "acceptance": True,
        "run_id": args.run_id,
        "signature_supported": False,
        "mount_success": False,
        "four_files_visible": False,
        "restore": None,
        "timeline": None,
    }
    try:
        for attempt in range(3):
            choice = choose_gpu_candidate(attempt)
            chosen_key = choice["key"]
            session = "muse-h3-" + __import__("uuid").uuid4().hex[:10]
            allocation_attempted = True
            record({"event": "allocation_attempt", "session": session, "gpu": chosen_key,
                    "variant": "ACCEPTANCE", "run_id": args.run_id})
            try:
                cmd = allocation_command(session, chosen_key, allow_g4=args.allow_g4,
                                         allow_a100_highmem=args.allow_a100_highmem)
                run_cli(cmd, timeout=420)
            except Exception as e:
                attempts.append({"session": session, "gpu": chosen_key, "status": "allocation_failed",
                                 "error": str(e)[-1500:]})
                time.sleep(5)
                try:
                    live_sessions = inspect()["sessions"]
                    if any(s["name"] == session for s in live_sessions):
                        raise RuntimeError(f"Late allocation appeared for {session} after a failed `new`; stop it and abort")
                except RuntimeError:
                    raise
                except Exception:
                    pass
                if is_capacity_failure(str(e)) and attempt < 2:
                    continue
                if is_transient_transport_error(str(e)) and chosen_key == "G4" and attempt == 0:
                    record({"event": "allocation_transport_retry", "session": session, "gpu": chosen_key,
                            "error": str(e)[:300]})
                    continue
                raise RuntimeError(f"GPU allocation failed ({chosen_key}): {str(e)[:500]}; no fallback unless capacity failure")
            created = True
            record({"event": "allocated", "session": session, "gpu": chosen_key})
            break
        if not created or session is None:
            raise RuntimeError("GPU allocation did not complete")
        deadline = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=MAX_SESSION_SECONDS)
        session_rec = record_session(session, chosen_key, args.run_id, deadline.isoformat())
        watchdog_proc = None
        try:
            watchdog_proc = subprocess.Popen(
                [sys.executable, str(ROOT / "h3_session_watchdog.py"), "watch", "--session", session,
                 "--production-task-id", "h3-ab-20261111"],
                stdout=open(run_dir / "watchdog_acceptance.log", "a"),
                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True,
            )
        except Exception as e:  # noqa: BLE001
            record({"event": "watchdog_start_failed", "session": session, "error": str(e)[:300]})
        gpu_info = probe_actual_gpu(session, chosen_key)
        record({"event": "gpu_probed", "session": session, **gpu_info})
        live = inspect()
        if live["rate"] is None or live["rate"] <= 0 or live["rate"] > args.max_rate_cu_h:
            raise RuntimeError("Unknown/excessive live CU/hr rate")
        ledger = reconcile_budget(ledger, live["cu_balance"], LEDGER_PATH)
        unbilled = estimate_unbilled_cu(session_rec.get("started_at_utc"), live["rate"])
        record({"event": "unbilled_estimate", "session": session, "unbilled_cu": unbilled,
                "rate_cu_h": live["rate"], "posted_balance_cu": live["cu_balance"]})
        budget_sec = allowed_runtime_seconds(live["rate"], live["cu_balance"], ledger,
                                             session_spent_cu=unbilled, unbilled_cu=unbilled)
        budget_sec = min(budget_sec, args.max_wall_sec)
        if budget_sec < 300:
            raise RuntimeError("Budget cannot cover minimal task + cleanup reserve")
        # Step 3: remote signature probe FIRST. No timeout_ms -> stop immediately.
        sig = exec_script(session, ROOT / "h3_mount_signature_probe.py", timeout=120)
        result["signature_output"] = (sig["stdout"] + "\n" + sig["stderr"]).strip()[-500:]
        if not check_mount_signature(sig):
            raise RuntimeError("Remote drive.mount does not accept timeout_ms; abort before drivemount")
        result["signature_supported"] = True
        # Step 4: official interactive Drive mount (patched remote timeout 600s).
        timeline = mount_drive(session, deadline_sec=budget_sec, run_id=args.run_id,
                               timeline_path=run_dir / "mount_timeline.json")
        result["timeline"] = timeline
        result["mount_success"] = timeline.get("outcome") == "mounted"
        if not result["mount_success"]:
            raise RuntimeError(f"Drive mount failed: outcome={timeline.get('outcome')} error={timeline.get('error')}")
        # Step 5: drive probe — four weights must be visible (returncode 0 is not enough).
        exec_script(session, ROOT / "h3_init_remote.py", timeout=60)
        mount_cfg = RUNS / f"mount_{session}.json"
        mount_cfg.write_text(json.dumps({"drive_cache_dir": args.drive_cache_dir}))
        run_cli(["upload", "-s", session, str(ROOT / "model_cache" / "verified_manifest.json"),
                 REMOTE + "/model_manifest.json"], timeout=120)
        run_cli(["upload", "-s", session, str(mount_cfg), REMOTE + "/drive_mount_config.json"], timeout=120)
        mount_probe = exec_script(session, ROOT / "h3_drive_probe.py", timeout=180)
        if "MUSE_DRIVE_MOUNT_MODELS_VISIBLE 4" not in mount_probe["stdout"]:
            raise RuntimeError("Mounted Drive model cache not visible; release GPU")
        result["four_files_visible"] = True
        # Step 6: disk space + first-256MB real read throughput + full restore projection.
        restore = exec_script(session, ROOT / "h3_restore_probe.py", timeout=600)
        parsed = parse_restore_probe(restore["stdout"] + "\n" + restore["stderr"])
        if not parsed.get("ok"):
            raise RuntimeError("Remote 256MB restore probe did not complete: " + str(parsed)[:300])
        projection = estimate_restore(
            TOTAL_MODEL_BYTES,
            free_bytes=parsed.get("disk_free_bytes"),
            measured_mbps=parsed.get("mbps_256mb"),
            rate_cu_h=live["rate"],
            budget_cu=float(args.per_session_budget_cu),
        )
        result["restore"] = {**parsed, "projection": projection}
        result["gpu"] = gpu_info
        result["attempts"] = attempts
        result["live_rate_cu_h"] = live["rate"]
        result["session_budget_sec"] = budget_sec
        live = inspect()
        ledger = reconcile_budget(ledger, live["cu_balance"], LEDGER_PATH)
        result["live_balance_cu"] = live["cu_balance"]
        result["ledger_spent_cu"] = ledger["max_observed_spent_cu"]
        (run_dir / "acceptance.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
        return result
    finally:
        if allocation_attempted and session is not None:
            stop_result = stop_and_verify_session(session)
            stop_ok = stop_result["stopped_confirmed"]
            if watchdog_proc is not None:
                if stop_ok:
                    try:
                        watchdog_proc.terminate()
                    except Exception:
                        pass
                else:
                    record({"event": "watchdog_left_running",
                            "session": session, "run_id": args.run_id,
                            "production_task_id": "h3-ab-20261111",
                            "stop_state": stop_result.get("state"),
                            "reason": "stop not confirmed; watchdog keeps deadline protection"})
            record({"event": "finished", "session": session, "variant": "ACCEPTANCE", "stop_verified": stop_ok,
                    "run_id": args.run_id, "stop_state": stop_result.get("state"),
                    "record_marked": stop_result.get("record_marked", False),
                    "allocation_attempted": allocation_attempted, "elapsed_sec": round(time.monotonic() - start, 1)})
            result["stop_verified"] = stop_ok
            result["elapsed_sec"] = round(time.monotonic() - start, 1)
            # Preserve allocation failure detail even when the run dies in
            # the allocation loop (result["attempts"] is otherwise only set
            # on the success path, losing the underlying CLI error).
            if attempts:
                result["attempts"] = attempts
            # Attach the timeline even on failure (sanitized by construction: no URIs).
            if timeline is None:
                tl_path = run_dir / "mount_timeline.json"
                if tl_path.exists():
                    try:
                        timeline = json.loads(tl_path.read_text())
                        result["timeline"] = timeline
                    except Exception:
                        pass
            try:
                (run_dir / "acceptance.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
            except Exception:
                pass
    if allocation_attempted and not created:
        raise RuntimeError("GPU allocation did not complete; repeated session checks found no allocation, but delayed assignment remains possible. Recheck sessions later.")
    if not stop_ok:
        raise RuntimeError("Stop not verified; acceptance FAILED regardless of earlier results")
    raise RuntimeError("Acceptance driver reached unreachable state")


if __name__ == "__main__":
    try:
        out = main()
        print(json.dumps(out, ensure_ascii=False, indent=2))
    except Exception as e:
        print("ERROR:", str(e), file=sys.stderr)
        sys.exit(1)
