"""Muse -> official colab CLI -> H3 G4/A100 orchestration.

GPU DISABLED unless explicit flags. Single-variant `run` per invocation;
A and B share one persistent per-task 30 CU ledger
(model_cache/h3_production_budget_<production_task_id>.json).
The pre-2026-11-11-08:25 ledger (model_cache/h3_production_budget_30cu.json)
is archived read-only and never written again.

Historical constants HIST_REMAIN=11.45 / HARD_STOP=153.82 are audit history
only and are NEVER used as a budget gate in this file. The live gate is
h3_budget.py: PROJECT_BUDGET_CU=30.0, PER_SESSION_BUDGET_CU=25.0.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from h3_budget import (
    PROJECT_BUDGET_CU,
    PER_SESSION_BUDGET_CU,
    CLEANUP_RESERVE_CU,
    MAX_SESSION_SECONDS,
    DEFAULT_PRODUCTION_TASK_ID,
    GPU_CHOICES,
    initial_session_wall_seconds,
    ledger_path_for_task,
    effective_hard_stop_cu,
    allocation_command,
    choose_gpu_candidate,
    estimate_unbilled_cu,
    effective_balance_cu,
    is_capacity_failure,
    is_transient_transport_error,
    load_or_create_budget_ledger,
    parse_gpu_probe_output,
    parse_usage_text,
    read_live_usage,
    reconcile_budget,
    allowed_runtime_seconds,
)
from h3_mount_timeline import (
    build_mount_timeline,
    read_history_events,
    summarize_timeline,
)
from h3_contract import (
    validate_model_cache_manifest,
    validate_poster,
    POSTER_SHA256,
    POSTER_DRIVE_ID,
    STORY_DRIVE_ID,
)
from auth_support import resolve_colab_executable, audit_user_adc_file

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "reference_v2"))
from colab_manager import parse_usage_output, parse_sessions_output, redact_sensitive_text

RUNS = ROOT / "logs"
REMOTE = "/content/muse_h3"
OUTPUT_ROOT = ROOT / "output"
# Per-task ledger (user budget rule 2026-11-11 09:12: one independent 30 CU
# ledger per explicitly started production task). main() rebinds this from
# --production-task-id; the default is the current task h3-ab-20261111.
PRODUCTION_TASK_ID = DEFAULT_PRODUCTION_TASK_ID
LEDGER_PATH = ledger_path_for_task(ROOT, PRODUCTION_TASK_ID)
# Prior task's unbilled estimate for the current task (CU). Fixed at the
# new ledger's creation and stored on it; the CLI default only matters
# when the ledger file does not exist yet.
PRIOR_TASK_UNBILLED_RESERVE_CU = 0.14
_COLAB_AUTH = "adc"
# Historical audit values only. Do not use as a gate.
HIST_START = 171.82
HIST_SPENT = 6.55
HARD_STOP = 153.82
HIST_REMAIN = 11.45
COMFY_REF_DEFAULT = "61e524bdba7a4e193b45c2697f1af83c1beb86d6"


def _comfyui_cache_task():
    """ComfyUI source-cache descriptor for the remote worker's task.json,
    from the local manifest built by h3_comfyui_cache.py. None when the
    cache has not been built: the worker then falls back to git clone."""
    manifest_path = ROOT / "model_cache" / "comfyui_source_manifest.json"
    if not manifest_path.exists():
        return None
    manifest = json.loads(manifest_path.read_text())
    return {
        "dir": "/content/drive/MyDrive/Muse-H3-ComfyUI-Source",
        "archive": manifest["archive_name"],
        "sha256": manifest["sha256"],
        "commit": manifest["commit"],
    }


def create_run_id() -> str:
    return "h3-" + datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + __import__("secrets").token_hex(3)


def record(data):
    RUNS.mkdir(exist_ok=True)
    data = {"utc": datetime.datetime.now(datetime.timezone.utc).isoformat(), **data}
    with (RUNS / "h3_audit.jsonl").open("a") as f:
        f.write(json.dumps(data, ensure_ascii=False) + "\n")


def run_cli(cmd, timeout=90, check=True):
    exe = resolve_colab_executable()
    args = [exe, "--auth=" + _COLAB_AUTH, *cmd]
    # 2026-11-11: jupyter-kernel-client's REQUEST_TIMEOUT defaults to 10s
    # (env REQUEST_TIMEOUT in its constants.py). A freshly allocated
    # A100-HM runtime's first kernel-start request takes longer than 10s
    # through the egress proxy, so `colab exec` failed with
    # ReadTimeout (read timeout=10) even though the runtime was healthy.
    # Raise it for every CLI subprocess; COLAB_CLI_REQUEST_TIMEOUT does
    # the same for the assign/usage requests in client.py.
    env = {**os.environ, "REQUEST_TIMEOUT": "120", "COLAB_CLI_REQUEST_TIMEOUT": "180"}
    p = subprocess.run(args, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=timeout, check=False, env=env)
    result = {"args": cmd, "returncode": p.returncode, "stdout": redact_sensitive_text(p.stdout), "stderr": redact_sensitive_text(p.stderr)}
    if check and p.returncode != 0:
        raise RuntimeError("CLI failed: " + str(cmd) + "\n" + result["stderr"][-1400:])
    return result


def mount_drive_interactive(session: str, timeout: float = 300, run_id: str = "", timeline_path=None) -> dict:
    """Run the official `colab drivemount` interactively and capture a
    structured mount timeline (see h3_mount_timeline.py).

    Stdout/stderr are piped and forwarded verbatim to the controller's own
    stdout (so the 20s log monitor still sees the authorization link), with
    each line stamped on arrival. Stdin stays inherited from the PTY: the
    CLI's credential hook reads /dev/tty directly for the Enter keypress.
    Chunked reads are used because the CLI prints "Press Enter..." with no
    trailing newline — line iteration would delay that prompt.

    Returns the timeline dict. Raises on any non-mounted outcome; the
    timeline JSON is written before raising so the failure is diagnosable.
    """
    if not sys.stdin.isatty():
        raise RuntimeError("Interactive terminal required for Drive authorization")
    exe = resolve_colab_executable()
    started_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    lines: list = []
    lock = threading.Lock()
    proc = subprocess.Popen(
        [exe, '--auth=' + _COLAB_AUTH, 'drivemount', '-s', session],
        stdin=None, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )

    def _reader():
        buf = b""
        try:
            while True:
                chunk = os.read(proc.stdout.fileno(), 4096)
                if not chunk:
                    break
                with lock:
                    sys.stdout.write(chunk.decode("utf-8", errors="replace"))
                    sys.stdout.flush()
                buf += chunk
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    lines.append((datetime.datetime.now(datetime.timezone.utc).isoformat(),
                                  raw.decode("utf-8", errors="replace")))
        except OSError:
            pass
        if buf:
            lines.append((datetime.datetime.now(datetime.timezone.utc).isoformat(),
                          buf.decode("utf-8", errors="replace")))

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()
    timed_out = False
    try:
        returncode = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.kill()
        returncode = proc.wait()
    reader.join(timeout=10)
    finished_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    timeline = build_mount_timeline(
        session, run_id, lines, read_history_events(session), returncode,
        started_at, finished_at, timed_out=timed_out,
    )
    target = Path(timeline_path) if timeline_path else RUNS / f"mount_timeline_{session}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(timeline, ensure_ascii=False, indent=2))
    record({"event": "mount_timeline", "session": session, "run_id": run_id,
            "summary": summarize_timeline(timeline), "timeline_file": str(target)})
    if timeline["outcome"] != "mounted":
        raise RuntimeError("Official Colab Drive mount failed; halt and release GPU -- " + summarize_timeline(timeline))
    return timeline


def mount_drive(session: str, deadline_sec: int, run_id: str = "", timeline_path=None) -> dict:
    # 2026-11-11: floor 720s = 600s remote mount timeout + 120s margin, so
    # the controller never kills a mount that still has remote time left.
    # The 900s cap is unchanged and is a LOCAL ceiling only; the remote fix
    # is timeout_ms=600000 in the injected drivemount code, not this wait.
    return mount_drive_interactive(session, timeout=min(900, max(720, deadline_sec // 3)),
                                   run_id=run_id, timeline_path=timeline_path)


def check_mount_signature(exec_result: dict) -> bool:
    """Fail-closed check that the remote google.colab.drive.mount accepts
    timeout_ms. Returns True when the probe printed a signature containing
    it; raises when the probe produced no signature line at all."""
    text = (exec_result or {}).get("stdout", "") + "\n" + (exec_result or {}).get("stderr", "")
    for line in text.splitlines():
        if "MOUNT_SIGNATURE:" in line:
            return "timeout_ms" in line
    raise RuntimeError("Mount signature probe produced no MOUNT_SIGNATURE line; cannot verify remote drive.mount timeout_ms")


def persist_exec_result(run_dir, name: str, result: dict) -> Path:
    """Persist a remote exec result (stdout/stderr/returncode) to the run
    dir. 2026-11-11 lesson: run A failed at the final download and the
    worker's own progress output had been discarded, so the failure stage
    could not be identified after the session was stopped. Every exec
    result is now kept on disk. Repeated calls with the same name (e.g.
    probe retries) get _2, _3 suffixes instead of overwriting."""
    run_dir = Path(run_dir)
    base = run_dir / f"exec_{name}.json"
    path = base
    n = 2
    while path.exists():
        path = run_dir / f"exec_{name}_{n}.json"
        n += 1
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def download_remote_comfyui_log(session: str, run_dir: Path) -> str | None:
    """Best-effort download of the remote ComfyUI server log after a worker
    failure. The server logs its /prompt validation rejections there (the
    400 body alone does not always name the node), and the runtime disk is
    ephemeral — once the session stops, the log is gone. Returns the local
    path, or None when the download fails (never raises)."""
    local = run_dir / "comfyui_remote.log"
    try:
        run_cli(["download", "-s", session, REMOTE + "/comfyui.log", str(local)], timeout=180)
        return str(local) if local.is_file() and local.stat().st_size > 0 else None
    except Exception:
        return None


def check_worker_done_marker(exec_result: dict, variant: str) -> dict:
    """Fail-closed check that the remote worker finished the variant: it
    only prints MUSE_H3_DONE_JSON after assemble_video wrote the final
    MP4. `colab exec` may return 0 even when the remote cell raised, so a
    bare returncode is NOT proof the worker succeeded — the marker is.
    Returns the parsed done-info dict. Raises with the tail of the worker
    output so the failure stage stays diagnosable after the session stops."""
    text = (exec_result or {}).get("stdout", "") or ""
    for line in text.splitlines():
        if line.startswith("MUSE_H3_DONE_JSON:"):
            return json.loads(line.split("MUSE_H3_DONE_JSON:", 1)[1].strip())
    tail = (text + "\n" + ((exec_result or {}).get("stderr", "") or ""))[-1500:]
    raise RuntimeError(
        f"Remote worker for variant {variant} did not print MUSE_H3_DONE_JSON; "
        f"the output file was never produced. Worker output tail:\n{tail}")


def check_remote_output(exec_result: dict, variant: str) -> dict:
    """Fail-closed gate on the remote MP4 BEFORE any download: the remote
    output-check script must report OUTPUT_CHECK_JSON with ok=true, the
    file existing, and ffprobe reading exactly 274 frames at 24 fps and
    1080x1920. The worker's MUSE_H3_DONE_JSON marker alone is a claim;
    this is the independent remote evidence the file is real and
    decodable. Raises with the check output tail otherwise."""
    text = (exec_result or {}).get("stdout", "") or ""
    for line in text.splitlines():
        if line.startswith("OUTPUT_CHECK_JSON:"):
            info = json.loads(line.split("OUTPUT_CHECK_JSON:", 1)[1].strip())
            if info.get("ok") and info.get("exists") and info.get("frames") == 274 \
                    and info.get("fps") == 24 and info.get("width") == 1080 and info.get("height") == 1920:
                return info
            raise RuntimeError(
                f"Remote output check failed for variant {variant}: {json.dumps(info, ensure_ascii=False)[:800]}")
    tail = (text + "\n" + ((exec_result or {}).get("stderr", "") or ""))[-1500:]
    raise RuntimeError(
        f"Remote output check for variant {variant} produced no OUTPUT_CHECK_JSON; "
        f"the MP4 was not verified on the remote. Output tail:\n{tail}")


def _strip_cli_notice(text: str) -> str:
    """Drop the Colab CLI's intermittent update-check notice. It is printed
    to stdout before the real output and contains 'available: 0.8.0'."""
    return "\n".join(
        line for line in (text or "").splitlines()
        if "new version of Colab CLI" not in line
        and "colab update" not in line
        and "enable_update_check" not in line
    )


def inspect():
    usage = run_cli(["usage"])
    sessions = run_cli(["sessions"])
    # 2026-11-11: the loose reference parser read the update notice
    # ('available: 0.8.0') as a CU balance of 0.8, which falsely tripped
    # the hard-stop gate and killed a healthy G4 run. Use the strict
    # parser (exact 'Current balance:' line) and strip the notice first.
    usage_text = _strip_cli_notice(usage["stdout"] + "\n" + usage["stderr"])
    parsed = parse_usage_text(usage_text)
    ses = parse_sessions_output(_strip_cli_notice(sessions["stdout"] + "\n" + sessions["stderr"]))
    return {"cu_balance": parsed["balance"], "rate": parsed["rate"],
            "sessions": [{"name": s.session_id, "gpu": s.gpu_type} for s in ses]}


def validate_local(api, proof):
    validate_poster(ROOT / "assets" / "original_poster.jpg")
    manifest = json.loads(Path(proof).read_text())
    validate_model_cache_manifest(manifest)
    from h3_contract import patch_api_workflow

    raw = json.loads(Path(api).read_text())
    if "nodes" in raw:
        raise ValueError("Expected ComfyUI API format, not UI-format workflow (nodes/links)")
    for variant in ("A", "B"):
        patched = patch_api_workflow(raw, variant)
        node = patched["136"]["inputs"]
        assert node["width"] == 736 and node["height"] == 1312 and node["length"] == 226
        assert patched["124"]["inputs"]["steps"] == 25
        assert patched["124"]["inputs"]["scheduler"] == "beta"
        assert patched["146"]["inputs"]["value"] is False
    return manifest


def exec_script(sess, path, timeout, env=None):
    cmd = ["exec", "-s", sess, "-f", str(path), "--timeout", "600"]
    if env:
        for k, v in env.items():
            cmd += ["--env", f"{k}={v}"]
    result = run_cli(cmd, timeout=timeout)
    # Every remote exec's stdout/stderr/returncode is persisted to the
    # current run dir (user requirement 2026-11-11 09:12). _EXEC_LOG_DIR is
    # set by run_variant (and by the acceptance driver) before any exec.
    if _EXEC_LOG_DIR is not None:
        try:
            persist_exec_result(_EXEC_LOG_DIR, Path(path).stem, result)
        except Exception:
            pass
    return result


# Run dir for exec-result persistence; set by run_variant / acceptance
# driver, cleared in their finally blocks. None means "do not persist".
_EXEC_LOG_DIR = None


def ensure_no_foreign_sessions(pre):
    if pre["sessions"]:
        raise RuntimeError("Existing Colab sessions present; fail closed. Check remote sessions manually.")


def initialize_project_budget(run_id: str, prior_unbilled_reserve_cu: float = PRIOR_TASK_UNBILLED_RESERVE_CU,
                              authorize_new_task: bool = False) -> dict:
    live = read_live_usage(_COLAB_AUTH)
    if live.get("balance") is None:
        raise RuntimeError("Cannot parse CU balance; abort")
    return load_or_create_budget_ledger(LEDGER_PATH, live["balance"], run_id,
                                        prior_unbilled_reserve_cu=prior_unbilled_reserve_cu,
                                        authorize_new_task=authorize_new_task)


def probe_actual_gpu(session: str, choice_key: str) -> dict:
    """GPU probe with bounded retries for cold-start kernel timeouts.

    The first exec after allocation can fail with a ReadTimeout while the
    runtime kernel is still starting (2026-11-11: A100-HM allocated fine,
    probe exec failed with read timeout=10). Retry the same session up to
    3 times, 15s apart; any other error (wrong GPU, insufficient VRAM)
    fails immediately and is never retried."""
    last_error = None
    for attempt in range(3):
        try:
            result = exec_script(session, ROOT / "h3_gpu_probe.py", timeout=180, env={"H3_GPU_CHOICE": choice_key})
            return parse_gpu_probe_output(result["stdout"] + "\n" + result["stderr"], choice_key)
        except Exception as e:  # noqa: BLE001
            last_error = e
            text = str(e)
            if "ReadTimeout" in text or "Read timed out" in text or "TimeoutError" in text:
                record({"event": "gpu_probe_timeout_retry", "session": session, "attempt": attempt + 1, "error": text[:300]})
                time.sleep(15)
                continue
            raise
    raise RuntimeError(f"GPU probe failed after 3 attempts: {last_error}")


def validate_comfy_workflow(api_path: Path, variant: str) -> dict:
    """Static + contract validation of the API workflow for one variant."""
    raw = json.loads(Path(api_path).read_text())
    if "nodes" in raw:
        raise ValueError("Expected ComfyUI API format, not UI-format workflow (nodes/links)")
    from h3_contract import patch_api_workflow

    api = patch_api_workflow(raw, variant, "muse_original_poster.jpg", seed=42 if variant == "A" else 43)
    node = api["136"]["inputs"]
    assert node["width"] == 736 and node["height"] == 1312 and node["length"] == 226
    assert node["ref_image_size"] == "match"
    assert api["124"]["inputs"]["steps"] == 25 and api["124"]["inputs"]["scheduler"] == "beta"
    assert api["146"]["inputs"]["value"] is False
    return api


def preflight(args) -> dict:
    """Fully read-only. Never creates a GPU. Returns a checks dict; raises on failure."""
    checks = {}

    def check(name, fn):
        try:
            checks[name] = {"pass": True, "detail": fn()}
        except Exception as e:  # noqa: BLE001 - preflight must record, not hide
            checks[name] = {"pass": False, "detail": str(e)[:500]}

    # 1. source files + existing tests (files exist; full unittest is run by the caller before preflight)
    def _sources():
        required = ["h3_controller.py", "h3_contract.py", "h3_remote_worker.py", "h3_gpu_probe.py",
                    "h3_drive_probe.py", "h3_init_remote.py", "assemble.py", "build_api_candidate.py",
                    "drive_depot.py", "drive_api.py", "drive_resumable.py", "auth_support.py",
                    "workflow_api.json", "model_cache/verified_manifest.json", "h3_budget.py",
                    "h3_session_watchdog.py", "h3_video_qa.py", "h3_mount_timeline.py",
                    "h3_mount_signature_probe.py", "h3_comfyui_cache.py",
                    "V3_DRIVEMOUNT_TIMEOUT_PATCH.md"]
        missing = [f for f in required if not (ROOT / f).exists()]
        if missing:
            raise RuntimeError("Missing project files: " + ", ".join(missing))
        return f"{len(required)} files present"

    check("project_sources", _sources)

    def _comfyui_cache():
        # Informational only, never a gate: a missing cache means the
        # worker falls back to git clone (atomic, bounded retry).
        cache = _comfyui_cache_task()
        if cache is None:
            return "not built; worker falls back to git clone"
        return ("ready: " + cache["archive"] + " sha256=" + cache["sha256"][:16]
                + "... commit=" + cache["commit"][:12])

    check("comfyui_source_cache", _comfyui_cache)
    check("adc_valid", lambda: audit_user_adc_file())
    # 2-4. same account + Drive READY (live)
    def _drive_ready():
        from drive_depot import drive_service, account_matches, verify_ready

        drive = drive_service()
        same = account_matches(drive, _COLAB_AUTH)
        if not same:
            raise RuntimeError("ACCOUNT_MISMATCH")
        result = verify_ready(drive, same, Path(args.cache_manifest))
        return result

    check("same_account_and_drive_ready", _drive_ready)
    check("poster_sha256", lambda: (validate_poster(ROOT / "assets" / "original_poster.jpg"), "sha256 ok")[1])
    check("storyboard_drive_id", lambda: STORY_DRIVE_ID == "1TTv-xyLsSLSvTL1jy9ewOFM4qXUKBwpP" or (_ for _ in ()).throw(RuntimeError("Storyboard Drive ID mismatch")))

    def _workflow():
        validate_local(args.workflow_api, args.cache_manifest)
        return "API workflow A/B patch ok, 736x1312, 226 frames, 25 steps, non-Turbo"

    check("comfy_api_candidate_static", _workflow)
    check("comfy_ref_pinned", lambda: args.comfy_ref == COMFY_REF_DEFAULT or (_ for _ in ()).throw(RuntimeError("ComfyUI ref is not the pinned commit")))

    def _cli():
        probe = run_cli(['drivemount','--help'], timeout=20, check=False)
        if probe["returncode"] != 0:
            raise RuntimeError("Official Colab drivemount unavailable")
        ver = run_cli(["version"], timeout=20, check=False)
        if "0.7.5" not in ver["stdout"] + ver["stderr"]:
            raise RuntimeError("Colab CLI is not 0.7.5")
        return "colab 0.7.5, drivemount --help ok"

    check("cli_0_7_5_commands", _cli)

    def _cli_post_patch():
        if not cli_post_body_patch_present():
            raise RuntimeError("Colab CLI POST-body patch missing: empty POSTs get 411 from the egress proxy; reinstall the patch before GPU")
        return "colab_cli client.py sends data=b'{}' on POSTs (411 workaround, verified live)"

    check("cli_post_body_patch", _cli_post_patch)

    def _cli_drivemount_patch():
        if not cli_drivemount_timeout_patch_present():
            raise RuntimeError("Colab CLI drivemount timeout patch missing: remote drive.mount would use the 120s default; reinstall the patch before GPU")
        return "colab_cli automation.py injects drive.mount(..., timeout_ms=600000), local ceiling 660s"

    check("cli_drivemount_timeout_patch", _cli_drivemount_patch)

    def _pty():
        # sys.executable, not bare `python`: the check must pass in any launch
        # environment (the exec sandbox has python3 but no `python` alias).
        p = subprocess.run(
            ["script", "-qec", f'{sys.executable} -c "import os,sys; assert sys.stdin.isatty(); fd=os.open(\\"/dev/tty\\",os.O_RDONLY); os.close(fd); print(\\"TTY_OK\\")"',
             "/dev/null"],
            capture_output=True, text=True, timeout=30,
        )
        if "TTY_OK" not in p.stdout + p.stderr:
            raise RuntimeError("PTY capability failed")
        return "script PTY + /dev/tty ok"

    check("pty_capability", _pty)

    def _budget():
        live = read_live_usage(_COLAB_AUTH)
        if LEDGER_PATH.exists():
            ledger = json.loads(LEDGER_PATH.read_text())
            reconcile_budget(ledger, live["balance"], LEDGER_PATH)
            return {"production_task_id": ledger.get("production_task_id"),
                    "initial_balance_cu": ledger["initial_balance_cu"], "hard_stop_balance_cu": ledger["hard_stop_balance_cu"],
                    "prior_task_unbilled_reserve_cu": ledger.get("prior_task_unbilled_reserve_cu", 0.0),
                    "effective_hard_stop_balance_cu": effective_hard_stop_cu(ledger),
                    "max_observed_spent_cu": ledger["max_observed_spent_cu"], "live_balance_cu": live["balance"]}
        return {"production_task_id": PRODUCTION_TASK_ID,
                "initial_balance_cu": live["balance"], "hard_stop_balance_cu": live["balance"] - PROJECT_BUDGET_CU,
                "live_balance_cu": live["balance"], "ledger": "not yet created; will be created before first GPU"}

    check("budget_ledger_task", _budget)

    def _restore_capacity():
        # Local planning estimate only: the real gate runs remotely in the
        # worker (shutil.disk_usage + measured throughput on the first 256MB).
        # No Drive->VM copy throughput has ever been measured (the mount
        # never succeeded), so the 50-150 MB/s range is an assumption,
        # labelled as such — the remote worker replaces it with a measurement.
        manifest = json.loads(Path(args.cache_manifest).read_text())
        total = sum(int(f["size"]) for f in manifest["files"])
        reserve = 5 * 1024 ** 3
        lo_s = round(total / (150 * 1_000_000))
        hi_s = round(total / (50 * 1_000_000))
        return {"total_bytes": total, "required_free_bytes": total + reserve,
                "eta_sec_at_50_150_mbps": [hi_s, lo_s],
                "eta_cost_cu_at_50_150_mbps_10cu_h": [round(hi_s / 3600 * 10.05, 2), round(lo_s / 3600 * 10.05, 2)],
                "note": "assumed throughput range, unmeasured; remote worker measures it"}

    check("restore_capacity_estimate", _restore_capacity)

    def _sessions():
        pre = inspect()
        if pre["sessions"]:
            raise RuntimeError(f"Active sessions present: {pre['sessions']}")
        if pre["cu_balance"] is None:
            raise RuntimeError("Cannot parse CU balance")
        return {"cu_balance": pre["cu_balance"], "rate": pre["rate"], "sessions": []}

    check("no_active_sessions", _sessions)

    def _policy():
        if args.gpu_policy != "G4_THEN_A100_HIGHMEM":
            raise RuntimeError("gpu-policy must be G4_THEN_A100_HIGHMEM")
        if float(args.project_budget_cu) != PROJECT_BUDGET_CU:
            raise RuntimeError(f"project-budget-cu must be {PROJECT_BUDGET_CU:.0f}")
        if float(args.per_session_budget_cu) != PER_SESSION_BUDGET_CU:
            raise RuntimeError(f"per-session-budget-cu must be {PER_SESSION_BUDGET_CU:.0f}")
        return f"G4_THEN_A100_HIGHMEM, {PROJECT_BUDGET_CU:.0f} CU project, {PER_SESSION_BUDGET_CU:.0f} CU per session"

    check("gpu_policy_and_budget", _policy)

    failed = [k for k, v in checks.items() if not v["pass"]]
    result = {"run_id": args.run_id, "checks": checks, "passed": not failed, "failed": failed}
    if failed:
        raise RuntimeError("Preflight failed: " + ", ".join(failed) + " -- " + json.dumps(checks, ensure_ascii=False)[:2000])
    return result


def cli_drivemount_timeout_patch_present(search_roots=None) -> bool:
    """Fail-closed check for the 2026-11-11 drivemount timeout patch in the
    vendored Colab CLI: automation.py must inject timeout_ms=600000 into the
    remote drive.mount() call, and the local automation ceiling must be 660s
    so the local execute deadline never cuts the remote 600s mount short.
    If the venv is rebuilt, this check fails before any GPU is requested.

    `search_roots` is a dependency-injection hook for tests: a list of
    site-packages-like directories to scan instead of the interpreter's own
    site-packages. Production preflight always calls it with no argument.
    """
    import site

    roots = search_roots if search_roots is not None else site.getsitepackages()
    for base in roots:
        candidate = Path(base) / "colab_cli" / "commands" / "automation.py"
        if candidate.exists():
            text = candidate.read_text(encoding="utf-8", errors="ignore")
            return "timeout_ms=600000" in text and "INTERACTIVE_AUTOMATION_TIMEOUT_SEC = 660" in text
    return False


def cli_post_body_patch_present(search_roots=None) -> bool:
    """Fail-closed check for the 2026-11-11 VM transport workaround in the
    vendored Colab CLI: _issue_request must send a non-empty body on POSTs,
    otherwise the egress proxy strips Content-Length: 0 and Google returns
    411 for /tun/m/assign and /tun/m/unassign. If the venv is rebuilt, this
    check fails before any GPU is requested.

    `search_roots` is a dependency-injection hook for tests (see
    cli_drivemount_timeout_patch_present). Production preflight always
    calls it with no argument.
    """
    import site

    roots = search_roots if search_roots is not None else site.getsitepackages()
    for base in roots:
        candidate = Path(base) / "colab_cli" / "client.py"
        if candidate.exists():
            text = candidate.read_text(encoding="utf-8", errors="ignore")
            return 'kwargs["data"] = b"{}"' in text and 'if method == "POST"' in text
    return False


def allocate_managed_session(gpu_choice: dict, run_id: str) -> str:
    return "muse-h3-" + uuid.uuid4().hex[:10]


def download_and_validate_variant(session: str, variant: str, run_id: str) -> dict:
    from h3_video_qa import validate_final_video

    out_dir = OUTPUT_ROOT / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    # Remote worker writes output/shao_nian_h3_<variant>.mp4 (already assembled 274f)
    target = out_dir / f"{variant}.mp4"
    run_cli(["download", "-s", session, REMOTE + f"/output/shao_nian_h3_{variant}.mp4", str(target)], timeout=600)
    info = validate_final_video(target)
    # Also download the worker metadata json if present.
    meta_target = out_dir / f"{variant}.json"
    try:
        run_cli(["download", "-s", session, REMOTE + f"/output/{variant}.json", str(meta_target)], timeout=120)
    except Exception:
        pass
    return info


def check_archive_metadata(meta: dict, expected_parent: str) -> bool:
    """Drive output parent verification: file must exist, not be trashed,
    and its parents must be exactly the RUN_ID output folder."""
    if not meta or meta.get("trashed"):
        return False
    return expected_parent in (meta.get("parents") or [])


def archive_variant_to_drive(variant: str, run_id: str, qa_files: list | None = None) -> dict:
    """Upload MP4 (+ QA files) to Muse-H3-Video-Outputs/<RUN_ID>/ and
    re-query each file's server metadata. Returns Drive ids."""
    from drive_depot import drive_service, ensure_folder, upload_one, get_file_evidence
    from drive_api import file_digests
    from h3_contract import sha256_file

    service = drive_service()
    out_root = ensure_folder(service, None, "Muse-H3-Video-Outputs", allow_write=True)
    run_folder = ensure_folder(service, out_root, run_id, allow_write=True)
    uploaded = {}
    files = [(OUTPUT_ROOT / run_id / f"{variant}.mp4", f"{variant}.mp4")]
    for extra in (qa_files or []):
        files.append((Path(extra), Path(extra).name))
    for local_path, name in files:
        local_path = Path(local_path)
        if not local_path.is_file():
            raise RuntimeError(f"Archive file missing: {local_path}")
        sha = sha256_file(local_path)
        entry = upload_one(service, local_path, run_folder, name, name, sha, "54472221d27c695fca9621b0c3ed52f06c4af707")
        meta = get_file_evidence(service, entry["drive_file_id"])
        if not check_archive_metadata(meta, run_folder):
            raise RuntimeError(f"Drive archive parent/trash check failed for {name}")
        uploaded[name] = {"drive_file_id": entry["drive_file_id"], "size": entry["size"],
                          "server_md5": entry["server_md5"], "sha256": sha, "parent": run_folder}
    return {"output_folder_id": run_folder, "files": uploaded}


def deliver_variant_to_drive(variant: str, run_id: str, video_info: dict,
                             archive_fn=None, max_attempts: int = 3) -> dict:
    """Archive a locally validated MP4 to Drive AFTER the GPU is stopped.

    Call this only after download_and_validate_variant() has already run
    validate_final_video() on the local file. This helper re-proves the
    file on disk is that validated file (it must exist and its SHA-256
    must match video_info), then uploads with bounded retries and returns
    the delivery evidence. The server-side parent/trash check inside
    archive_variant_to_drive() is what makes drive_upload_verified true —
    a bare upload return is never enough.

    Delivery states (TASK-002 §4.5): INFERENCE_COMPLETED /
    REMOTE_MP4_VALIDATED / LOCAL_MP4_DOWNLOADED / LOCAL_MP4_VALIDATED are
    recorded by run_variant before this call; this helper covers
    DRIVE_UPLOAD_STARTED / DRIVE_UPLOAD_VERIFIED / DELIVERY_COMPLETED.

    On failure after max_attempts it raises RuntimeError stating the
    local MP4 path: the GPU is already stopped, so the correct recovery
    is an archive-only retry — never a re-run of the inference.
    """
    from h3_contract import sha256_file

    archive_fn = archive_fn or archive_variant_to_drive
    local_path = OUTPUT_ROOT / run_id / f"{variant}.mp4"
    if not local_path.is_file():
        raise RuntimeError(f"Delivery refused: local MP4 missing: {local_path}")
    for key in ("frame_count", "width", "height", "fps", "sha256"):
        if key not in video_info:
            raise RuntimeError(f"Delivery refused: video_info lacks {key}")
    if (video_info["frame_count"], video_info["width"], video_info["height"]) != (274, 1080, 1920):
        raise RuntimeError("Delivery refused: local video failed final dimensions/frames")
    if sha256_file(local_path) != video_info["sha256"]:
        raise RuntimeError(f"Delivery refused: local MP4 SHA-256 does not match validated video_info: {local_path}")
    last_error = None
    for attempt in range(1, max_attempts + 1):
        record({"event": "drive_upload_started", "variant": variant, "run_id": run_id, "attempt": attempt})
        try:
            result = archive_fn(variant, run_id)
            entry = (result.get("files") or {}).get(f"{variant}.mp4")
            if not entry or not entry.get("drive_file_id"):
                raise RuntimeError("Archive returned no Drive file id for the MP4")
            evidence = {
                "variant": variant, "run_id": run_id,
                "local_video_validated": True,
                "drive_file_id": entry["drive_file_id"],
                "drive_upload_verified": True,
                "frames": video_info["frame_count"], "width": video_info["width"],
                "height": video_info["height"], "fps": video_info["fps"],
                "sha256": video_info["sha256"],
                "drive_output_folder_id": result.get("output_folder_id"),
                "state": "DELIVERY_COMPLETED",
            }
            record({"event": "drive_upload_verified", "variant": variant, "run_id": run_id,
                    "drive_file_id": entry["drive_file_id"]})
            return evidence
        except Exception as e:  # noqa: BLE001 - retry, then report the last error
            last_error = e
            record({"event": "drive_upload_failed", "variant": variant, "run_id": run_id,
                    "attempt": attempt, "error": str(e)[:300]})
            if attempt < max_attempts:
                time.sleep(5)
    raise RuntimeError(
        f"Drive delivery failed after {max_attempts} attempts; local MP4 preserved at {local_path} "
        f"(sha256 {video_info['sha256'][:16]}...). Re-run archive only, do NOT re-run inference. "
        f"Last error: {last_error}")


def stop_and_verify_session(session: str) -> dict:
    """Stop a managed session and verify it is gone. The stop verdict
    comes only from the three-state query (TASK-005 §3, Issue #24):
    STOPPED = confirmed gone; ACTIVE = still there, retry the stop;
    UNKNOWN = cannot confirm — record a stop_error, retry bounded, and
    never report stopped_confirmed. Balance/rate for the audit record is
    best-effort and never decides the verdict."""
    from h3_session_watchdog import SessionState, query_session_state

    stop_ok = False
    last_state = SessionState.UNKNOWN
    for attempt in range(3):
        try:
            state = query_session_state(session)
            last_state = state
            if state is SessionState.STOPPED:
                stop_ok = True
                break
            if state is SessionState.UNKNOWN:
                record({"event": "stop_error", "session": session, "attempt": attempt + 1,
                        "error": "session state UNKNOWN after stop query; not treating as stopped"})
                time.sleep(3)
                continue
            run_cli(["stop", "-s", session], timeout=65)
            state = query_session_state(session)
            last_state = state
            if state is SessionState.STOPPED:
                stop_ok = True
                try:
                    live = read_live_usage(_COLAB_AUTH)
                    record({"event": "stopped", "session": session,
                            "balance": live.get("balance"), "rate": live.get("rate")})
                except Exception:  # noqa: BLE001 - audit record is best-effort
                    record({"event": "stopped", "session": session})
                break
            if state is SessionState.UNKNOWN:
                record({"event": "stop_error", "session": session, "attempt": attempt + 1,
                        "error": "session state UNKNOWN after stop; not treating as stopped"})
        except Exception as e:  # noqa: BLE001
            record({"event": "stop_error", "session": session, "attempt": attempt + 1, "error": str(e)[-250:]})
        time.sleep(3)
    if not stop_ok:
        record({"event": "CRITICAL_ORPHAN_RISK", "session": session})
    return {"session": session, "stopped_confirmed": stop_ok, "state": last_state.value}


def reconcile_run_state(run_id: str) -> dict:
    from h3_session_watchdog import reconcile_on_startup

    ledger = None
    if LEDGER_PATH.exists():
        ledger = json.loads(LEDGER_PATH.read_text())
        try:
            live = read_live_usage(_COLAB_AUTH)
            reconcile_budget(ledger, live["balance"], LEDGER_PATH)
        except Exception as e:  # noqa: BLE001
            ledger = {**ledger, "reconcile_error": str(e)[:300]}
    return {"run_id": run_id, "ledger": ledger, "watchdog": reconcile_on_startup(_COLAB_AUTH)}


def _wait_watchdog_ready(proc, ready_path: Path, session: str, production_task_id: str,
                         timeout_sec: float = 15.0) -> dict:
    """Fail-closed watchdog startup gate (TASK-002 §6.1). Wait until the
    watchdog process proves it is ready — alive, holding this session's
    record, bound to the same production task — by writing `ready_path`.
    Any other outcome (early exit, readiness mismatch, timeout) raises
    WATCHDOG_NOT_RUNNING so the caller aborts before inference and the
    outer finally releases the just-created GPU session."""
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"WATCHDOG_NOT_RUNNING: watchdog exited with code {proc.returncode} "
                f"before signalling readiness for session {session}")
        if ready_path.exists():
            try:
                ready = json.loads(ready_path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001 - partial write; keep waiting
                pass
            else:
                if ready.get("session_id") == session and ready.get("production_task_id") == production_task_id:
                    return ready
                raise RuntimeError(
                    "WATCHDOG_NOT_RUNNING: watchdog readiness mismatch for session "
                    f"{session}: got {ready}")
        time.sleep(0.25)
    raise RuntimeError(
        f"WATCHDOG_NOT_RUNNING: watchdog did not signal readiness for session {session} "
        f"within {timeout_sec:g}s")


def _manifest_update(run_id: str, **kwargs) -> None:
    """Best-effort durable-manifest update (TASK-005 §4.1). Never raises
    into the run: the manifest is observability, and GPU/budget safety
    never depends on it. A failure is recorded as a manifest_error event."""
    try:
        from h3_run_manifest import update_run

        update_run(ROOT, run_id, **kwargs)
    except Exception as e:  # noqa: BLE001 - manifest must not break the run
        try:
            record({"event": "manifest_error", "run_id": run_id, "error": str(e)[:300]})
        except Exception:
            pass


def _manifest_create(args, ledger) -> None:
    """Best-effort manifest creation at task authorization (TASK-005
    §4.1). Never raises into the run."""
    try:
        import hashlib
        import subprocess

        from h3_run_manifest import create_run_manifest

        try:
            code_commit = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=str(ROOT),
                capture_output=True, text=True, timeout=10).stdout.strip()
        except Exception:
            code_commit = ""

        def _sha(path):
            try:
                return hashlib.sha256(Path(path).read_bytes()).hexdigest()
            except Exception:
                return ""

        budget = {}
        if ledger:
            budget = {"project_budget_cu": ledger.get("project_budget_cu"),
                      "spent_cu": ledger.get("max_observed_spent_cu"),
                      "hard_stop_balance_cu": ledger.get("hard_stop_balance_cu")}
        create_run_manifest(ROOT, args.production_task_id, args.run_id,
                            variant=args.variant, code_commit=code_commit,
                            workflow_sha256=_sha(args.workflow_api),
                            model_manifest_sha256=_sha(args.cache_manifest),
                            budget=budget)
    except Exception as e:  # noqa: BLE001
        try:
            record({"event": "manifest_error", "run_id": args.run_id, "error": str(e)[:300]})
        except Exception:
            pass


# ---------------------------------------------------------------------------
# TASK-005 §4.2 (Issue #27): recoverable control plane.
#
# The old controller held ONE long blocking `colab exec` of the remote
# worker script as its only control channel. When the Muse VM
# restarted on 2026-11-12 (B run, ~09:48 UTC) that exec died with the
# controller and nobody could tell where the worker had got to — the
# orphaned A100 had to be stopped blind and the run was lost.
#
# The new shape: the worker runs DETACHED (launcher starts it and returns
# immediately), writes a durable job state file on the VM after every
# phase, and the controller polls that file with short exec calls. A
# controller restart is recovered by `resume`, which reconciles the
# manifest, the real session state, the worker state, the ledger, local
# outputs and Drive evidence before doing anything.
# ---------------------------------------------------------------------------

class WorkerPollError(RuntimeError):
    """One worker-state poll failed (exec error, timeout, missing or
    corrupt state file). The worker may still be alive; callers retry a
    bounded number of times, then fail closed."""


# Job stages (h3_job_state) -> manifest stages (h3_run_manifest).
# None means "heartbeat only": the manifest already sits at that stage.
JOB_STAGE_TO_MANIFEST_STAGE = {
    "JOB_STARTED": None,
    "RESTORING_MODELS": None,
    "MODELS_READY": "MODELS_READY",
    "INFERENCE_RUNNING": None,
    "INFERENCE_COMPLETED": "INFERENCE_COMPLETED",
    "VIDEO_READY": None,  # handled by the caller (download path)
    "FAILED": None,  # handled by the caller (raises)
}


def _manifest_worker_state(run_id: str, state: dict) -> None:
    """Record a polled worker state in the durable manifest (best-effort).
    Forward-only enforcement lives in update_run: a stale or out-of-order
    poll is refused and logged as an anomaly, never applied."""
    stage = (state or {}).get("stage")
    manifest_stage = JOB_STAGE_TO_MANIFEST_STAGE.get(stage)
    if manifest_stage:
        _manifest_update(run_id, task_state=manifest_stage,
                         last_verified_stage=manifest_stage)
    else:
        _manifest_update(run_id, heartbeat=True)


def launch_worker(session: str) -> dict:
    """Short-start the detached worker. Returns the launch record
    (run_id, variant, pid, state_file, log_file). Raises on any launch
    failure: a launch that cannot be observed fails fast here instead of
    leaving an untracked worker behind on a paid GPU."""
    result = exec_script(session, ROOT / "h3_worker_launcher.py", timeout=180)
    text = (result or {}).get("stdout", "") or ""
    for line in text.splitlines():
        if line.startswith("MUSE_H3_LAUNCH_JSON:"):
            return json.loads(line.split("MUSE_H3_LAUNCH_JSON:", 1)[1].strip())
    tail = (text + "\n" + ((result or {}).get("stderr", "") or ""))[-1500:]
    raise RuntimeError(
        f"Worker launcher did not print MUSE_H3_LAUNCH_JSON; launcher output tail:\n{tail}")


def poll_worker(session: str) -> dict:
    """One short probe of the worker's durable job state. Returns the
    state dict. Raises WorkerPollError on exec failure, timeout, a
    missing/corrupt state file, or an incomplete record — never a silent
    'no worker' answer."""
    try:
        result = exec_script(session, ROOT / "h3_job_probe.py", timeout=90)
    except Exception as e:  # noqa: BLE001 - any exec failure is a poll failure
        raise WorkerPollError(f"probe exec failed: {e}") from e
    text = (result or {}).get("stdout", "") or ""
    for line in text.splitlines():
        if line.startswith("MUSE_H3_JOB_STATE_JSON:"):
            try:
                state = json.loads(line.split("MUSE_H3_JOB_STATE_JSON:", 1)[1].strip())
            except json.JSONDecodeError as e:
                raise WorkerPollError(f"probe returned unparseable state JSON: {e}") from e
            if not isinstance(state, dict) or not state.get("run_id") \
                    or not state.get("variant") or not state.get("stage"):
                raise WorkerPollError(f"probe returned an incomplete state record: {state!r}"[:300])
            return state
        if line.startswith("MUSE_H3_JOB_STATE_MISSING"):
            raise WorkerPollError(
                "job state file missing on the remote (worker never started or the VM lost the workdir)")
        if line.startswith("MUSE_H3_JOB_STATE_CORRUPT"):
            raise WorkerPollError(f"job state file corrupt on the remote: {line[:300]}")
    tail = (text + "\n" + ((result or {}).get("stderr", "") or ""))[-1000:]
    raise WorkerPollError(f"probe printed no job state marker; probe output tail:\n{tail}")


def check_job_done_info(done_info, variant: str) -> dict:
    """Fail-closed validation of a VIDEO_READY output record, mirroring
    check_worker_done_marker's contract for the detached path: the record
    must name a 274-frame final MP4 with a SHA-256. The remote ffprobe
    check and the local validation after download are still the real
    gates; this only proves the worker claims a complete output."""
    if not isinstance(done_info, dict):
        raise RuntimeError(
            f"Worker VIDEO_READY record is not an object for variant {variant}: {done_info!r}"[:300])
    if done_info.get("frames") != 274:
        raise RuntimeError(
            f"Worker VIDEO_READY record has frames={done_info.get('frames')!r}, expected 274")
    if not done_info.get("file") or not done_info.get("sha256"):
        raise RuntimeError(
            f"Worker VIDEO_READY record is missing file/sha256: {done_info!r}"[:300])
    return done_info


def wait_for_worker(session: str, run_id: str, variant: str,
                    deadline_monotonic: float, poll_interval_sec: int = 60,
                    max_consecutive_poll_failures: int = 3,
                    on_state=None) -> dict:
    """Poll the detached worker until VIDEO_READY. Returns the final
    worker state. Raises on worker FAILED, on the budget wall time, on
    too many consecutive poll failures, on an unknown stage, or on a
    state record that belongs to another run. A failed poll never means
    'the worker is dead': the loop retries, and only the bounded failure
    count turns unobservability into a run failure (the GPU is then
    stopped by the caller's finally block, exactly as before)."""
    failures = 0
    while True:
        if time.monotonic() > deadline_monotonic:
            raise TimeoutError(
                f"Worker for run {run_id} did not finish before the budget wall time")
        try:
            state = poll_worker(session)
        except WorkerPollError as e:
            failures += 1
            if failures >= max_consecutive_poll_failures:
                raise RuntimeError(
                    f"Worker state unobservable after {failures} consecutive poll "
                    f"failures: {e}") from e
            time.sleep(poll_interval_sec)
            continue
        failures = 0
        if state.get("run_id") != run_id or state.get("variant") != variant:
            raise RuntimeError(
                f"Worker state belongs to another run: {state.get('run_id')!r}/"
                f"{state.get('variant')!r}, expected {run_id}/{variant}")
        if on_state is not None:
            on_state(state)
        stage = state.get("stage")
        if stage == "VIDEO_READY":
            return state
        if stage == "FAILED":
            err = state.get("error") or {}
            raise RuntimeError(
                f"Remote worker failed at stage {err.get('stage')}: "
                f"{err.get('error_type')}: {err.get('error')}")
        if stage not in JOB_STAGE_TO_MANIFEST_STAGE:
            raise RuntimeError(f"Unknown worker job stage: {stage!r}")
        time.sleep(poll_interval_sec)


def _archive_local_mp4(variant: str, run_id: str) -> dict:
    """Archive-only delivery (TASK-003 §1, shared by the `archive` action
    and by `resume`): re-validate the local MP4 and upload it with
    bounded retries. No GPU, no session, no ledger, never a re-run of
    the inference."""
    from h3_video_qa import validate_final_video

    info = validate_final_video(OUTPUT_ROOT / run_id / f"{variant}.mp4")
    return deliver_variant_to_drive(variant, run_id, info)


def _resume_archive_only(args, manifest: dict) -> dict:
    """COMPLETED_AWAITING_ARCHIVE: the local MP4 is durable; only the
    Drive upload is missing. Archive it and complete the run."""
    run_id = manifest["run_id"]
    variant = manifest["variant"]
    try:
        delivery = _archive_local_mp4(variant, run_id)
    except Exception as e:  # noqa: BLE001 - stays awaiting archive
        return {"resumed": False, "run_id": run_id,
                "reason": f"archive-only retry failed, still awaiting archive: {e}",
                "manifest_task_state": "COMPLETED_AWAITING_ARCHIVE"}
    _manifest_update(run_id, task_state="DRIVE_VERIFIED",
                     last_verified_stage="DRIVE_VERIFIED",
                     artifacts={"drive_file_id": delivery.get("drive_file_id"),
                                "drive_output_folder_id": delivery.get("drive_output_folder_id")},
                     reopen=True)  # COMPLETED_AWAITING_ARCHIVE is terminal; resume reopens it.
    _manifest_update(run_id, task_state="TASK_COMPLETED")
    return {"resumed": True, "run_id": run_id, "variant": variant,
            "outcome": "archive-only retry succeeded", "delivery": delivery}


def _resume_stopped_session(args, manifest: dict) -> dict:
    """The session is gone, so the remote temp disk is gone with it.
    Recover only from durable copies: a validated local MP4 (archive it),
    or a Drive file id the manifest already recorded (verify it
    server-side first). Never re-infer, never allocate a new GPU here."""
    from h3_run_manifest import update_run

    run_id = manifest["run_id"]
    variant = manifest["variant"]
    local_mp4 = OUTPUT_ROOT / run_id / f"{variant}.mp4"
    if local_mp4.is_file():
        try:
            delivery = _archive_local_mp4(variant, run_id)
        except Exception as e:  # noqa: BLE001 - local copy unusable
            return {"resumed": False, "run_id": run_id,
                    "reason": f"session stopped and the local MP4 cannot be archived: {e}"}
        _manifest_update(run_id, task_state="DRIVE_VERIFIED",
                         last_verified_stage="DRIVE_VERIFIED",
                         artifacts={"drive_file_id": delivery.get("drive_file_id"),
                                    "drive_output_folder_id": delivery.get("drive_output_folder_id")})
        _manifest_update(run_id, task_state="TASK_COMPLETED")
        return {"resumed": True, "run_id": run_id, "variant": variant,
                "outcome": "session stopped; validated local MP4 archived",
                "delivery": delivery}
    drive_file_id = (manifest.get("artifacts") or {}).get("drive_file_id")
    if drive_file_id:
        try:
            from drive_depot import drive_service

            drive = drive_service()
            meta = drive.files().get(fileId=drive_file_id,
                                     fields="id,name,size,trashed,md5Checksum").execute()
            if meta.get("id") == drive_file_id and not meta.get("trashed"):
                _manifest_update(run_id, task_state="DRIVE_VERIFIED",
                                 last_verified_stage="DRIVE_VERIFIED")
                _manifest_update(run_id, task_state="TASK_COMPLETED")
                return {"resumed": True, "run_id": run_id, "variant": variant,
                        "outcome": "session stopped; Drive file already verified server-side",
                        "drive_file_id": drive_file_id}
        except Exception as e:  # noqa: BLE001 - Drive check unavailable
            return {"resumed": False, "run_id": run_id,
                    "reason": f"session stopped; Drive verification unavailable: {e}"}
    update_run(ROOT, run_id, task_state="FAILED",
               last_error="Session stopped with no durable artifact: no validated "
                          "local MP4 and no verified Drive file")
    return {"resumed": False, "run_id": run_id,
            "reason": "session stopped and no durable artifact (no validated local "
                      "MP4, no verified Drive file); nothing recoverable — start a new run"}


def _resume_finish(args, manifest: dict, worker_state: dict) -> dict:
    """Complete a run whose worker reached VIDEO_READY. Mirrors
    run_variant's post-worker section in the same order: remote output
    check, download (skipped when the local MP4 already validates —
    never re-download a validated file), stop, Drive deliver. Never
    re-infers."""
    run_id = manifest["run_id"]
    variant = manifest["variant"]
    session_id = manifest["session_id"]
    done_info = check_job_done_info(worker_state.get("output"), variant)
    output_check = exec_script(session_id, ROOT / "h3_output_check.py", timeout=180,
                               env={"H3_VARIANT": variant})
    check_remote_output(output_check, variant)
    record({"event": "resume_remote_output_checked", "variant": variant,
            "session": session_id, "done": done_info})
    from h3_video_qa import validate_final_video

    info = None
    local_mp4 = OUTPUT_ROOT / run_id / f"{variant}.mp4"
    if local_mp4.is_file():
        try:
            info = validate_final_video(local_mp4)
            record({"event": "resume_local_mp4_reused", "variant": variant,
                    "run_id": run_id, "sha256": info["sha256"]})
        except Exception:
            info = None  # re-download below; a stale local file is never trusted
    if info is None:
        info = download_and_validate_variant(session_id, variant, run_id)
    _manifest_update(run_id, task_state="VIDEO_VALIDATED",
                     last_verified_stage="VIDEO_VALIDATED",
                     artifacts={"local_sha256": info["sha256"],
                                "local_frames": info["frame_count"]})
    _manifest_update(run_id, task_state="ARTIFACT_DURABLE")
    live = inspect()
    production_result = {
        "variant": variant, "run_id": run_id, "session": session_id,
        "video": info, "live_balance_cu": live["cu_balance"],
        "live_rate_cu_h": live["rate"],
        "delivery": {"state": "LOCAL_MP4_VALIDATED", "variant": variant,
                     "run_id": run_id, "local_video_validated": True},
        "resumed": True,
    }
    # The GPU is stopped and verified BEFORE any Drive upload, exactly as
    # in run_variant's tail.
    stop_result = stop_and_verify_session(session_id)
    stop_ok = stop_result["stopped_confirmed"]
    if not stop_ok:
        _manifest_update(run_id, task_state="FAILED", session_state="UNKNOWN",
                         last_error="Stop not verified; run FAILED regardless of generated files")
        raise RuntimeError("Stop not verified; run FAILED regardless of generated files")
    _manifest_update(run_id, task_state="GPU_STOP_CONFIRMED", session_state="STOPPED")
    try:
        delivery = deliver_variant_to_drive(variant, run_id, info)
    except Exception as e:  # noqa: BLE001 - video is durable locally
        _manifest_update(run_id, task_state="COMPLETED_AWAITING_ARCHIVE",
                         last_error=str(e)[:500])
        raise
    production_result["delivery"] = delivery
    _manifest_update(run_id, task_state="DRIVE_VERIFIED",
                     last_verified_stage="DRIVE_VERIFIED",
                     artifacts={"drive_file_id": delivery.get("drive_file_id"),
                                "drive_output_folder_id": delivery.get("drive_output_folder_id")})
    _manifest_update(run_id, task_state="TASK_COMPLETED")
    return production_result


def _resume_run(args) -> dict:
    """Reconcile a run from its durable manifest and resume it if it is
    safe to do so. The checklist (TASK-005 §4.3), in order:
    terminal states, code version, task ledger, real session state,
    worker state, local outputs, Drive evidence.
    Refusals never allocate a GPU, never stop a session blindly, never
    re-generate a completed video, never re-upload into a conflicting
    deliverable, and never create a 30 CU ledger."""
    from h3_run_manifest import resume_run

    run_id = args.run_id
    view = resume_run(ROOT, run_id)
    manifest = view["manifest"]
    state = manifest.get("task_state")
    if state == "TASK_COMPLETED":
        return {"resumed": False, "run_id": run_id,
                "reason": "already completed", "view": view}
    if state == "FAILED":
        return {"resumed": False, "run_id": run_id,
                "reason": view.get("reason") or "run FAILED; start a new run id",
                "view": view}
    if state == "COMPLETED_AWAITING_ARCHIVE":
        return _resume_archive_only(args, manifest)
    # Code version: resume only with the exact code the run started with.
    # A synced newer controller is a new decision, not a silent upgrade
    # of a half-finished run.
    try:
        import subprocess

        current_commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(ROOT), capture_output=True,
            text=True, timeout=10).stdout.strip()
    except Exception:
        current_commit = ""
    started_commit = manifest.get("code_commit") or ""
    if started_commit and current_commit and started_commit != current_commit:
        return {"resumed": False, "run_id": run_id,
                "reason": f"code mismatch: run started at {started_commit}, "
                          f"current HEAD is {current_commit}; refusing to resume "
                          f"with different controller code",
                "view": view}
    # Ledger must already exist: resume never creates a 30 CU ledger.
    task_id = manifest.get("production_task_id")
    ledger_path = ledger_path_for_task(ROOT, task_id)
    if not ledger_path.is_file():
        return {"resumed": False, "run_id": run_id,
                "reason": f"no budget ledger for task {task_id}; resume never "
                          f"creates a 30 CU ledger",
                "view": view}
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    session_id = manifest.get("session_id")
    if not session_id:
        return {"resumed": False, "run_id": run_id,
                "reason": "manifest has no session id; nothing to resume",
                "view": view}
    # Real session state (tristate, PR-1). UNKNOWN refuses everything.
    from h3_session_watchdog import SessionQueryError, query_session_state
    from h3_session_watchdog import SessionState

    try:
        session_state = query_session_state(session_id, colab_auth=_COLAB_AUTH)
    except SessionQueryError as e:
        return {"resumed": False, "run_id": run_id,
                "reason": f"session state UNKNOWN ({e}); refusing to allocate "
                          f"a second GPU or stop blindly",
                "view": view}
    if session_state == SessionState.STOPPED:
        return _resume_stopped_session(args, manifest)
    if session_state == SessionState.UNKNOWN:
        return {"resumed": False, "run_id": run_id,
                "reason": "session state UNKNOWN; refusing to allocate a "
                          "second GPU or stop blindly",
                "view": view}
    # Session ACTIVE: probe the worker (a single unobservable probe here
    # refuses instead of guessing; the session is left running).
    try:
        worker_state = poll_worker(session_id)
    except WorkerPollError as e:
        return {"resumed": False, "run_id": run_id,
                "reason": f"worker state unobservable ({e}); session left "
                          f"running — stop it explicitly with the stop action "
                          f"if it should not continue",
                "view": view}
    if worker_state.get("run_id") != run_id \
            or worker_state.get("variant") != manifest.get("variant"):
        return {"resumed": False, "run_id": run_id,
                "reason": f"worker state belongs to another run: "
                          f"{worker_state.get('run_id')!r}/{worker_state.get('variant')!r}",
                "view": view}
    variant = manifest["variant"]
    if worker_state.get("stage") == "FAILED":
        err = worker_state.get("error") or {}
        stop_result = stop_and_verify_session(session_id)
        stop_ok = stop_result["stopped_confirmed"]
        _manifest_update(run_id, task_state="FAILED",
                         session_state="STOPPED" if stop_ok else "UNKNOWN",
                         last_error=f"Remote worker failed at stage {err.get('stage')}: "
                                    f"{err.get('error_type')}: {err.get('error')}"[:500])
        return {"resumed": True, "run_id": run_id,
                "outcome": "worker failed; session stopped and run marked FAILED",
                "stop_verified": stop_ok}
    if worker_state.get("stage") == "VIDEO_READY":
        return _resume_finish(args, manifest, worker_state)
    # Still running: re-enter the poll loop under the remaining budget,
    # then finish the run the same way.
    live = inspect()
    if live["rate"] is None or live["rate"] <= 0:
        return {"resumed": False, "run_id": run_id,
                "reason": "live CU/hr rate unreadable; cannot bound a resumed poll loop",
                "view": view}
    budget_sec = allowed_runtime_seconds(live["rate"], live["cu_balance"], ledger)
    budget_sec = min(budget_sec, getattr(args, "max_wall_sec", MAX_SESSION_SECONDS))
    if budget_sec < 300:
        return {"resumed": False, "run_id": run_id,
                "reason": "remaining budget cannot cover a minimal resumed run + cleanup reserve",
                "view": view}
    worker_state = wait_for_worker(
        session_id, run_id, variant,
        deadline_monotonic=time.monotonic() + budget_sec,
        poll_interval_sec=getattr(args, "poll_interval_sec", 60),
        on_state=lambda s: _manifest_worker_state(run_id, s))
    return _resume_finish(args, manifest, worker_state)


def _stop_run(args) -> dict:
    """Stop a run's managed session via the structured stop contract
    (PR-2). Refuses when the manifest has no session, or the session is
    not in the managed file — a non-project session is never stopped."""
    from h3_run_manifest import load_run_manifest, update_run
    from h3_session_watchdog import stop_managed_session

    if not args.run_id:
        raise ValueError("--run-id is required for stop")
    manifest = load_run_manifest(ROOT, args.run_id)
    session_id = manifest.get("session_id")
    if not session_id:
        raise ValueError(f"manifest for run {args.run_id} has no session id; nothing to stop")
    result = stop_managed_session(session_id, colab_auth=_COLAB_AUTH)
    if result.get("state") == "STOPPED":
        update_run(ROOT, args.run_id, session_state="STOPPED")
    return result


def _reconcile_task(args) -> dict:
    """Reconcile every manifest of one production task against the real
    session state. Corrects a manifest's session_state only from a
    definitive tristate answer; UNKNOWN is reported and left alone.
    Never stops anything, never allocates anything."""
    from h3_run_manifest import list_runs, update_run
    from h3_session_watchdog import SessionQueryError, query_session_state

    if not args.production_task_id:
        raise ValueError("--production-task-id is required for reconcile")
    report = {"production_task_id": args.production_task_id, "runs": [],
              "corrections": 0, "unknown": []}
    for manifest in list_runs(ROOT):
        if manifest.get("production_task_id") != args.production_task_id:
            continue
        entry = {"run_id": manifest["run_id"],
                 "task_state": manifest.get("task_state"),
                 "manifest_session_state": manifest.get("session_state"),
                 "session_id": manifest.get("session_id"),
                 "live_session_state": None}
        session_id = manifest.get("session_id")
        if session_id:
            try:
                live = query_session_state(session_id, colab_auth=_COLAB_AUTH)
            except SessionQueryError as e:
                entry["live_session_state"] = "UNKNOWN"
                entry["error"] = str(e)[:300]
                report["unknown"].append(manifest["run_id"])
                report["runs"].append(entry)
                continue
            entry["live_session_state"] = live.value
            if live.value != manifest.get("session_state"):
                update_run(ROOT, manifest["run_id"], session_state=live)
                entry["corrected_to"] = live.value
                report["corrections"] += 1
        report["runs"].append(entry)
    return report


def run_variant(args) -> dict:
    global _EXEC_LOG_DIR, LEDGER_PATH, PRODUCTION_TASK_ID
    variant = args.variant
    if variant not in ("A", "B"):
        raise ValueError("variant must be A or B")
    # TASK-002 §5.2: the production task must be named explicitly. The
    # historical default h3-ab-20261111 is never reused silently: a new
    # RUN_ID without --production-task-id used to bind to the old task's
    # ledger. An explicit old id still continues that task (e.g. the B
    # re-run), but it must be typed out.
    if not getattr(args, "production_task_id", ""):
        raise RuntimeError("--production-task-id is required for run; the historical default is never reused silently")
    # Bind the module ledger to this run's task (main() does the same for
    # CLI runs; this keeps programmatic calls consistent too).
    PRODUCTION_TASK_ID = args.production_task_id
    LEDGER_PATH = ledger_path_for_task(ROOT, PRODUCTION_TASK_ID)
    if not all((args.execute, args.confirm_gpu, args.authorize_task_gpu, args.allow_g4, args.allow_a100_highmem)):
        raise RuntimeError("GPU blocked: require --execute --confirm-gpu --authorize-task-gpu --allow-g4 --allow-a100-highmem")
    if float(args.project_budget_cu) != PROJECT_BUDGET_CU or float(args.per_session_budget_cu) != PER_SESSION_BUDGET_CU:
        raise RuntimeError(f"Project budget must be {PROJECT_BUDGET_CU:.0f} CU, per-session {PER_SESSION_BUDGET_CU:.0f} CU")
    if args.gpu_policy != "G4_THEN_A100_HIGHMEM":
        raise RuntimeError("gpu-policy must be G4_THEN_A100_HIGHMEM")
    if not args.comfy_ref or len(args.comfy_ref) < 7:
        raise RuntimeError("Pin ComfyUI commit hash or tag")
    if not args.drive_cache_dir.startswith("/content/drive/"):
        raise RuntimeError("Expected explicitly mounted Drive cache path under /content/drive")
    if not args.approve_interactive_drive_mount:
        raise RuntimeError("G4 blocked: --approve-interactive-drive-mount required")
    if not args.fresh_inference:
        raise RuntimeError("--fresh-inference required for a from-zero production run")
    validate_local(args.workflow_api, args.cache_manifest)
    validate_comfy_workflow(args.workflow_api, variant)
    # Live Drive READY + same account, before any paid GPU.
    from drive_depot import drive_service, account_matches, verify_ready

    drive = drive_service()
    same_account = account_matches(drive, _COLAB_AUTH)
    if not same_account:
        raise RuntimeError("ACCOUNT_MISMATCH")
    verify_ready(drive, same_account, Path(args.cache_manifest))
    ledger = initialize_project_budget(args.run_id,
                                       prior_unbilled_reserve_cu=getattr(args, "prior_unbilled_reserve_cu",
                                                                         PRIOR_TASK_UNBILLED_RESERVE_CU),
                                       authorize_new_task=getattr(args, "authorize_new_task", False))
    # Durable run manifest (TASK-005 §4.1): the task is authorised here,
    # so the persistent record starts now and survives a controller restart.
    _manifest_create(args, ledger)
    pre = inspect()
    if pre["cu_balance"] is None:
        raise RuntimeError("Cannot parse CU balance; abort")
    ensure_no_foreign_sessions(pre)
    # Watchdog reconciliation: any managed session still active blocks a
    # new allocation. An unconfirmable leftover is a hard stop, never a
    # reason to run a second GPU in parallel — and never a reason to stop
    # a session that is not one of ours.
    from h3_session_watchdog import reconcile_on_startup

    rec = reconcile_on_startup(_COLAB_AUTH)
    if rec.get("managed_active_sessions"):
        detail = rec.get("managed_active_detail") or [
            {"session_id": s} for s in rec["managed_active_sessions"]]
        lines = []
        for d in detail:
            est = d.get("unbilled_estimate_cu")
            lines.append(
                f"{d.get('session_id')} (run {d.get('run_id')}, task {d.get('production_task_id')}, "
                f"deadline {d.get('deadline_utc')}, unbilled estimate "
                f"{est if est is not None else 'unconfirmable'} CU)")
        raise RuntimeError(
            "CRITICAL_ORPHAN_GPU_SESSION: managed GPU session(s) still active; refusing to allocate "
            "another GPU in parallel. Stop them first (never stop non-project sessions): "
            + "; ".join(lines))
    probe = run_cli(['drivemount','--help'], timeout=20, check=False)
    if probe["returncode"] != 0:
        raise RuntimeError("Official Colab drivemount unavailable")
    if not sys.stdin.isatty():
        raise RuntimeError("Official drivemount requires interactive terminal; no TTY, abort BEFORE GPU allocation")

    run_dir = RUNS / args.run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    # From here on, every remote exec result is persisted to this run dir.
    _EXEC_LOG_DIR = run_dir
    out_dir = OUTPUT_ROOT / args.run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    session = None
    created = False
    allocation_attempted = False
    stop_ok = False
    # TASK-003 (Issue #18): the production result and the stop result are
    # separate names. The old code reused `result` for both, so the outer
    # finally overwrote the production dict (run_id, video) with the stop
    # dict and the delivery call below raised KeyError on the success path.
    production_result = None
    stop_result = None
    gpu_info = None
    chosen_key = None
    attempts = []
    start = time.monotonic()
    session_spent_start = float(ledger.get("max_observed_spent_cu", 0.0))
    try:
        # Allocation loop: G4, G4 retry, A100_HIGH_MEM (only on capacity failure).
        # Transport errors retry the same G4 once and never trigger A100 fallback.
        # Empty POSTs carry data=b'{}' via the vendored CLI patch (see
        # cli_post_body_patch_present / V3_CLI_PROXY_PATCH.md); without it
        # the egress proxy strips Content-Length: 0 and Google returns 411.
        for attempt in range(3):
            choice = choose_gpu_candidate(attempt)
            chosen_key = choice["key"]
            session = allocate_managed_session(choice, args.run_id)
            allocation_attempted = True
            record({"event": "allocation_attempt", "session": session, "gpu": chosen_key, "variant": variant, "run_id": args.run_id})
            try:
                cmd = allocation_command(session, chosen_key, allow_g4=args.allow_g4, allow_a100_highmem=args.allow_a100_highmem)
                # 420s: with the POST-body patch a G4 503 is retried inside
                # the CLI (up to 3 retries, 30s each + 10/20/40s backoff).
                run_cli(cmd, timeout=420)
            except Exception as e:
                attempts.append({"session": session, "gpu": chosen_key, "status": "allocation_failed", "error": str(e)[:500]})
                # Reconcile late allocation before deciding on fallback.
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
                # Transport errors retry the SAME GPU once (attempt 0 -> 1,
                # both G4). They never justify falling back to A100.
                if is_transient_transport_error(str(e)) and chosen_key == "G4" and attempt == 0:
                    record({"event": "allocation_transport_retry", "session": session, "gpu": chosen_key, "error": str(e)[:300]})
                    continue
                raise RuntimeError(f"GPU allocation failed ({chosen_key}): {str(e)[:500]}; no fallback unless capacity failure")
            created = True
            record({"event": "allocated", "session": session, "gpu": chosen_key})
            _manifest_update(args.run_id, task_state="GPU_ALLOCATED",
                             session_id=session, session_state="ACTIVE")
            break
        if not created or session is None:
            raise RuntimeError("GPU allocation did not complete")
        # Watchdog record + background watcher. The initial deadline is
        # derived from the 25 CU per-session cap minus the cleanup reserve
        # at the chosen GPU's maximum allowed rate (no fixed 3600 s cutoff
        # anymore); after the GPU probe the deadline is replaced with the
        # budget-derived value from allowed_runtime_seconds() using the
        # actual live rate.
        initial_sec = initial_session_wall_seconds(GPU_CHOICES[chosen_key]["max_rate_cu_h"])
        deadline = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=initial_sec)
        from h3_session_watchdog import record_session

        session_rec = record_session(session, chosen_key, args.run_id, deadline.isoformat(),
                                     production_task_id=args.production_task_id)
        # Fail-closed watchdog startup (TASK-002 §6.1): the watcher must
        # prove it is alive, holding this session's record, and bound to
        # this task before any inference. On any startup failure the
        # exception propagates to the outer finally, which stops and
        # verifies the just-created GPU session.
        ready_path = run_dir / f"watchdog_{variant}_ready.json"
        watchdog_proc = None
        try:
            watchdog_proc = subprocess.Popen(
                [sys.executable, str(ROOT / "h3_session_watchdog.py"), "watch", "--session", session,
                 "--production-task-id", getattr(args, "production_task_id", PRODUCTION_TASK_ID),
                 "--ready-file", str(ready_path)],
                stdout=open(run_dir / f"watchdog_{variant}.log", "a"),
                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True,
            )
            _wait_watchdog_ready(watchdog_proc, ready_path, session, args.production_task_id)
            record({"event": "watchdog_ready", "session": session, "ready_file": str(ready_path)})
        except Exception as e:  # noqa: BLE001
            record({"event": "watchdog_start_failed", "session": session, "error": str(e)[:300]})
            if watchdog_proc is not None:
                try:
                    if watchdog_proc.poll() is None:
                        watchdog_proc.terminate()
                except Exception:  # noqa: BLE001 - already failing; do not mask it
                    pass
            if isinstance(e, RuntimeError) and str(e).startswith("WATCHDOG_NOT_RUNNING"):
                raise
            raise RuntimeError(f"WATCHDOG_NOT_RUNNING: watchdog failed to start: {e}") from e
        try:
            # GPU hardware probe before any expensive work.
            gpu_info = probe_actual_gpu(session, chosen_key)
            record({"event": "gpu_probed", "session": session, **gpu_info})
            live = inspect()
            if live["rate"] is None or live["rate"] <= 0 or live["rate"] > args.max_rate_cu_h:
                raise RuntimeError("Unknown/excessive live CU/hr rate")
            # Reconcile ledger with the live balance after allocation.
            ledger = reconcile_budget(ledger, live["cu_balance"], LEDGER_PATH)
            # Unbilled-session estimate: the posted balance lags the real
            # burn, so gate on the effective balance too (item 7). At this
            # point only ~1-2 min have elapsed, so the estimate is small;
            # the watchdog applies the same estimate on every poll.
            unbilled = estimate_unbilled_cu(session_rec.get("started_at_utc"), live["rate"])
            record({"event": "unbilled_estimate", "session": session, "unbilled_cu": unbilled,
                    "rate_cu_h": live["rate"], "posted_balance_cu": live["cu_balance"]})
            budget_sec = allowed_runtime_seconds(live["rate"], live["cu_balance"], ledger,
                                                 session_spent_cu=unbilled, unbilled_cu=unbilled)
            budget_sec = min(budget_sec, args.max_wall_sec)
            if budget_sec < 300:
                raise RuntimeError("Budget cannot cover minimal task + cleanup reserve")
            # Replace the conservative initial watchdog deadline with the
            # budget-derived wall time at the actual live rate.
            from h3_session_watchdog import update_session_deadline

            new_deadline = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=budget_sec)
            update_session_deadline(session, new_deadline.isoformat())
            record({"event": "watchdog_deadline_updated", "session": session,
                    "budget_sec": budget_sec, "rate_cu_h": live["rate"],
                    "deadline_utc": new_deadline.isoformat()})
            # Remote signature probe: verify drive.mount accepts timeout_ms
            # before spending the mount window on a patched call the remote
            # runtime would reject with TypeError.
            sig = exec_script(session, ROOT / "h3_mount_signature_probe.py", timeout=120)
            if not check_mount_signature(sig):
                raise RuntimeError("Remote drive.mount does not accept timeout_ms; abort before drivemount")
            # Official interactive Drive mount (patched remote timeout 600s).
            mount_drive(session, deadline_sec=budget_sec, run_id=args.run_id,
                        timeline_path=run_dir / "mount_timeline.json")
            exec_script(session, ROOT / "h3_init_remote.py", timeout=60)
            # Upload poster, contract, assemble, manifest, workflow, submitted workflow.
            api = validate_comfy_workflow(args.workflow_api, variant)
            submitted_path = run_dir / f"workflow_{variant}_submitted.json"
            submitted_path.write_text(json.dumps(api, ensure_ascii=False, indent=2))
            for source, target in [
                (ROOT / "assets" / "original_poster.jpg", REMOTE + "/poster.jpg"),
                (ROOT / "h3_contract.py", REMOTE + "/h3_contract.py"),
                (ROOT / "assemble.py", REMOTE + "/assemble.py"),
                (Path(args.cache_manifest), REMOTE + "/model_manifest.json"),
                (Path(args.workflow_api), REMOTE + "/workflow_api.json"),
                (submitted_path, REMOTE + "/workflow_submitted.json"),
            ]:
                run_cli(["upload", "-s", session, str(source), target], timeout=300)
            mount_cfg = RUNS / f"mount_{session}.json"
            mount_cfg.write_text(json.dumps({"drive_cache_dir": args.drive_cache_dir}))
            run_cli(["upload", "-s", session, str(mount_cfg), REMOTE + "/drive_mount_config.json"], timeout=120)
            mount_probe = exec_script(session, ROOT / "h3_drive_probe.py", timeout=180)
            persist_exec_result(run_dir, f"drive_probe_{variant}", mount_probe)
            if "MUSE_DRIVE_MOUNT_MODELS_VISIBLE 4" not in mount_probe["stdout"]:
                raise RuntimeError("Mounted Drive model cache not visible; release GPU")
            # Worker files go up only after the mounted-cache probe passed:
            # the probe must run before the remote worker that does H3
            # inference (and the test in test_auth_and_transport enforces
            # this order). The launcher needs both files on the remote disk.
            for source, target in [
                (ROOT / "h3_job_state.py", REMOTE + "/h3_job_state.py"),
                (ROOT / "h3_remote_worker.py", REMOTE + "/h3_remote_worker.py"),
            ]:
                run_cli(["upload", "-s", session, str(source), target], timeout=300)
            # Single-variant remote worker.
            elapsed = time.monotonic() - start
            if elapsed > budget_sec:
                raise TimeoutError("Budget wall time reached before " + variant)
            task = {"variant": variant, "run_id": args.run_id, "drive_cache_dir": args.drive_cache_dir, "comfy_ref": args.comfy_ref,
                    "inference_timeout_sec": min(1800, max(100, int(budget_sec - elapsed - 120))),
                    "rate_cu_h": live["rate"],
                    "session_budget_cu": round(budget_sec * float(live["rate"]) / 3600.0, 3),
                    "comfyui_cache": _comfyui_cache_task()}
            task_path = RUNS / f"task_{session}.json"
            task_path.write_text(json.dumps(task))
            run_cli(["upload", "-s", session, str(task_path), REMOTE + "/task.json"], timeout=120)
            _manifest_update(args.run_id, task_state="WORKER_STARTED", heartbeat=True)
            # Recoverable control plane (TASK-005 §4.2): short-start the
            # detached worker, then poll its durable state file. The old
            # single long blocking exec is gone: a controller restart no
            # longer loses the worker's progress, and `resume` can pick
            # the run back up from the manifest + state file.
            launch = launch_worker(session)
            record({"event": "worker_launched", "variant": variant, "session": session, "launch": launch})
            try:
                worker_state = wait_for_worker(
                    session, args.run_id, variant,
                    deadline_monotonic=start + budget_sec,
                    poll_interval_sec=getattr(args, "poll_interval_sec", 60),
                    on_state=lambda s: _manifest_worker_state(args.run_id, s))
                done_info = check_job_done_info(worker_state.get("output"), variant)
            except Exception:
                # Pull the server log before the session stops: it holds
                # the /prompt rejection detail the worker output may lack.
                download_remote_comfyui_log(session, run_dir)
                raise
            record({"event": "worker_done", "variant": variant, "session": session, "done": done_info})
            _manifest_update(args.run_id, task_state="INFERENCE_COMPLETED",
                             last_verified_stage="INFERENCE_COMPLETED")
            record({"event": "delivery_state", "variant": variant, "run_id": args.run_id, "state": "INFERENCE_COMPLETED"})
            # Independent remote verification of the real MP4 BEFORE any
            # download: the file must exist and ffprobe must read exactly
            # 274 frames at 24 fps, 1080x1920. Only then is the download
            # allowed; the local ffprobe validation after download is a
            # second, independent check before any success report.
            output_check = exec_script(session, ROOT / "h3_output_check.py", timeout=180,
                                       env={"H3_VARIANT": variant})
            checked = check_remote_output(output_check, variant)
            record({"event": "remote_output_checked", "variant": variant, "session": session, "checked": checked})
            record({"event": "delivery_state", "variant": variant, "run_id": args.run_id, "state": "REMOTE_MP4_VALIDATED"})
            # Download the submitted workflow the worker validated, for audit.
            try:
                run_cli(["download", "-s", session, REMOTE + "/workflow_submitted.json", str(run_dir / f"workflow_{variant}_submitted_remote.json")], timeout=120)
            except Exception:
                pass
            info = download_and_validate_variant(session, variant, args.run_id)
            _manifest_update(args.run_id, task_state="VIDEO_VALIDATED",
                             last_verified_stage="VIDEO_VALIDATED",
                             artifacts={"local_sha256": info["sha256"],
                                        "local_frames": info["frame_count"]})
            # The local MP4 is now on Muse-controlled persistent storage:
            # the artifact is durable before the GPU stop (TASK-005 §5).
            _manifest_update(args.run_id, task_state="ARTIFACT_DURABLE")
            record({"event": "version_downloaded", "variant": variant, "session": session, "sha256": info["sha256"], "frames": info["frame_count"]})
            record({"event": "delivery_state", "variant": variant, "run_id": args.run_id,
                    "state": "LOCAL_MP4_DOWNLOADED"})
            record({"event": "delivery_state", "variant": variant, "run_id": args.run_id,
                    "state": "LOCAL_MP4_VALIDATED"})
            live = inspect()
            ledger = reconcile_budget(ledger, live["cu_balance"], LEDGER_PATH)
            # No return here: the GPU must be stopped (outer finally) BEFORE
            # any Drive upload. The result is stored and delivered after the
            # stop is verified; see the tail of this function.
            production_result = {
                "variant": variant, "run_id": args.run_id, "session": session, "gpu": gpu_info,
                "attempts": attempts, "video": info, "ledger": {k: v for k, v in ledger.items() if not k.startswith("_")},
                "live_balance_cu": live["cu_balance"], "live_rate_cu_h": live["rate"],
                "delivery": {"state": "LOCAL_MP4_VALIDATED", "variant": variant, "run_id": args.run_id,
                             "local_video_validated": True},
            }
        finally:
            if watchdog_proc is not None:
                try:
                    watchdog_proc.terminate()
                except Exception:
                    pass
            _EXEC_LOG_DIR = None
    finally:
        # Capture the propagating exception FIRST: sys.exc_info() must be
        # read before any call below (stop_and_verify_session catches its
        # own errors internally).
        _exc = sys.exc_info()
        if allocation_attempted and session is not None:
            stop_result = stop_and_verify_session(session)
            stop_ok = stop_result["stopped_confirmed"]
            from h3_session_watchdog import mark_session_stopped

            try:
                mark_session_stopped(session)
            except Exception:
                pass
            record({"event": "finished", "session": session, "variant": variant, "stop_verified": stop_ok,
                    "allocation_attempted": allocation_attempted, "elapsed_sec": round(time.monotonic() - start, 1)})
            # Durable manifest terminal update for the GPU phase (TASK-005
            # §4.1): on the success path this records GPU_STOP_CONFIRMED;
            # on any failure path it records FAILED with the real error.
            # The Drive tail below runs only after this finally.
            if _exc[0] is not None:
                _manifest_update(args.run_id, task_state="FAILED",
                                 session_state="STOPPED" if stop_ok else "UNKNOWN",
                                 last_error=f"{_exc[0].__name__}: {_exc[1]}"[:500])
            elif stop_ok:
                _manifest_update(args.run_id, task_state="GPU_STOP_CONFIRMED",
                                 session_state="STOPPED")
            else:
                _manifest_update(args.run_id, task_state="FAILED",
                                 session_state="UNKNOWN",
                                 last_error="Stop not verified; run FAILED regardless of generated files")
    if allocation_attempted and not created:
        raise RuntimeError("GPU allocation did not complete; repeated session checks found no allocation, but delayed assignment remains possible. Recheck sessions later.")
    if not stop_ok:
        raise RuntimeError("Stop not verified; run FAILED regardless of generated files")
    if production_result is None:
        raise RuntimeError("run_variant reached unreachable state")
    # Archive only when the stop is confirmed AND the local MP4 was
    # validated: production_result is built only after
    # download_and_validate_variant and the LOCAL_MP4_VALIDATED state.
    if production_result.get("delivery", {}).get("state") != "LOCAL_MP4_VALIDATED":
        raise RuntimeError("Local MP4 not validated; refusing Drive archive")
    # The GPU is stopped and verified at this point, so the Drive upload
    # burns no CU. Bounded retries inside deliver_variant_to_drive; on
    # failure the local MP4 is preserved and the error directs an
    # archive-only retry — never a re-run of the inference.
    try:
        delivery = deliver_variant_to_drive(variant, production_result["run_id"], production_result["video"])
    except Exception as e:
        # The video is durable locally; the terminal state is
        # COMPLETED_AWAITING_ARCHIVE (archive-only retry), not FAILED.
        _manifest_update(args.run_id, task_state="COMPLETED_AWAITING_ARCHIVE",
                         last_error=str(e)[:500])
        raise
    production_result["delivery"] = delivery
    _manifest_update(args.run_id, task_state="DRIVE_VERIFIED",
                     last_verified_stage="DRIVE_VERIFIED",
                     artifacts={"drive_file_id": delivery.get("drive_file_id"),
                                "drive_output_folder_id": delivery.get("drive_output_folder_id")})
    _manifest_update(args.run_id, task_state="TASK_COMPLETED")
    return production_result


def test_or_run(args):
    if args.action == "check":
        print(json.dumps(inspect(), ensure_ascii=False, indent=2))
        return
    if args.action == "plan":
        print(json.dumps({"goal": "A/B R2V", "gpu": "G4_THEN_A100_HIGHMEM", "poster_sha256": POSTER_SHA256,
                          "video_frames_per_variant": 274, "output": "1080x1920, 24fps",
                          "historic_remaining_cu": HIST_REMAIN, "historic_stop_balance": HARD_STOP,
                          "project_budget_cu": PROJECT_BUDGET_CU, "per_session_budget_cu": PER_SESSION_BUDGET_CU,
                          "no_gpu_created": True, "workflows": "Require API-format official R2V workflow and four-file server-verified manifest"}, ensure_ascii=False, indent=2))
        return
    if args.action == "preflight":
        result = preflight(args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.action == "archive":
        # TASK-003 §1: archive-only retry. No GPU, no session, no ledger:
        # re-validate the local MP4 (raises unless it is the validated
        # 274-frame final video) and run the deliver helper, which re-proves
        # the file and uploads with bounded retries. Use after a run whose
        # local MP4 survived but whose Drive upload failed.
        if not args.variant:
            raise ValueError("--variant A|B is required for archive")
        if not args.run_id:
            raise ValueError("--run-id is required for archive")
        delivery = _archive_local_mp4(args.variant, args.run_id)
        print(json.dumps(delivery, ensure_ascii=False, indent=2))
        return delivery
    if args.action == "status":
        # TASK-005 §4.1: read-only recovery view for a run. Never writes
        # (does not bump recovery_generation); resume is a separate
        # operation (PR-4).
        from h3_run_manifest import recovery_view

        if not args.run_id:
            raise ValueError("--run-id is required for status")
        print(json.dumps(recovery_view(ROOT, args.run_id), ensure_ascii=False, indent=2))
        return
    if args.action == "resume":
        # TASK-005 §4.3: reconcile the manifest, code version, ledger,
        # real session state, worker state, local outputs and Drive
        # evidence, then resume the run if it is safe. Refusals never
        # allocate a GPU, never stop a session blindly, never re-generate
        # a completed video, and never create a 30 CU ledger.
        if not args.run_id:
            raise ValueError("--run-id is required for resume")
        result = _resume_run(args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return result
    if args.action == "stop":
        # TASK-005 §4.3: stop a run's managed session via the structured
        # stop contract. Refuses non-managed sessions outright.
        result = _stop_run(args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return result
    if args.action == "reconcile":
        # TASK-005 §4.3: correct each manifest's session_state from a
        # definitive tristate query. Read-only for sessions: never stops
        # or allocates anything.
        result = _reconcile_task(args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return result
    if args.action != "run":
        raise ValueError("Unknown action")
    result = run_variant(args)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def main():
    global _COLAB_AUTH, PRODUCTION_TASK_ID, LEDGER_PATH, PRIOR_TASK_UNBILLED_RESERVE_CU
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["plan", "check", "run", "preflight", "archive", "status",
                                            "resume", "stop", "reconcile"])
    parser.add_argument("--colab-auth", choices=("oauth2", "adc"), default="adc")
    parser.add_argument("--workflow-api", default=str(ROOT / "workflow_api.json"))
    parser.add_argument("--cache-manifest", default=str(ROOT / "model_cache" / "verified_manifest.json"))
    parser.add_argument("--drive-cache-dir", default="/content/drive/MyDrive/Muse-H3-Model-Depot")
    parser.add_argument("--comfy-ref", default=COMFY_REF_DEFAULT)
    parser.add_argument("--budget-cu", type=float, default=0.0)
    parser.add_argument("--project-budget-cu", type=float, default=PROJECT_BUDGET_CU)
    parser.add_argument("--per-session-budget-cu", type=float, default=PER_SESSION_BUDGET_CU)
    parser.add_argument("--production-task-id", default="",
                        help="Required for run. The historical task h3-ab-20261111 is never "
                             "used implicitly; type it out to continue that task, or pass a "
                             "new id together with --authorize-new-task.")
    parser.add_argument("--authorize-new-task", action="store_true",
                        help="Explicit user authorization to create a NEW 30 CU task ledger. "
                             "Required when no ledger exists yet for --production-task-id.")
    parser.add_argument("--prior-unbilled-reserve-cu", type=float, default=PRIOR_TASK_UNBILLED_RESERVE_CU)
    parser.add_argument("--gpu-policy", default="G4_THEN_A100_HIGHMEM")
    parser.add_argument("--variant", choices=("A", "B"), default=None)
    parser.add_argument("--run-id", default="")
    parser.add_argument("--max-rate-cu-h", type=float, default=14.0)
    parser.add_argument("--max-wall-sec", type=int, default=MAX_SESSION_SECONDS)
    parser.add_argument("--poll-interval-sec", type=int, default=60,
                        help="Seconds between worker-state polls (TASK-005 §4.2).")
    parser.add_argument("--approve-interactive-drive-mount", action="store_true")
    parser.add_argument("--fresh-inference", action="store_true")
    for flag in ("execute", "confirm-gpu", "authorize-task-gpu", "allow-g4", "allow-a100-highmem"):
        parser.add_argument("--" + flag, action="store_true")
    a = parser.parse_args()
    _COLAB_AUTH = a.colab_auth
    if a.action == "run" and not a.production_task_id:
        parser.error("--production-task-id is required for run; the historical default is never reused silently")
    if a.production_task_id:
        PRODUCTION_TASK_ID = a.production_task_id
        LEDGER_PATH = ledger_path_for_task(ROOT, PRODUCTION_TASK_ID)
    PRIOR_TASK_UNBILLED_RESERVE_CU = float(a.prior_unbilled_reserve_cu)
    if a.action in ("run", "archive") and not a.variant:
        parser.error("--variant A|B is required for run/archive")
    if a.action in ("run", "archive") and not a.run_id:
        parser.error("--run-id is required for run/archive")
    if a.action in ("resume", "stop") and not a.run_id:
        parser.error("--run-id is required for resume/stop")
    if a.action == "reconcile" and not a.production_task_id:
        parser.error("--production-task-id is required for reconcile")
    if a.action == "preflight" and not a.run_id:
        a.run_id = create_run_id()
    try:
        test_or_run(a)
    except Exception as e:
        # Record the failure in the run dir so the stage and the error
        # survive the process exit (the GPU is stopped by run_variant's
        # finally block, not here).
        if getattr(a, "run_id", ""):
            try:
                import traceback

                (RUNS / a.run_id).mkdir(parents=True, exist_ok=True)
                (RUNS / a.run_id / "failure.json").write_text(
                    json.dumps({"action": a.action, "run_id": a.run_id,
                                "production_task_id": getattr(a, "production_task_id", PRODUCTION_TASK_ID),
                                "error_type": type(e).__name__, "error": str(e)[:2000],
                                "traceback": traceback.format_exc()[-4000:]},
                               ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                pass
            # Durable manifest: a run that failed before run_variant's own
            # finally (e.g. preflight/authorization) still gets its FAILED
            # terminal state, so a later `status` does not show it as
            # healthy-but-incomplete. Best-effort; never masks the error.
            if a.action == "run":
                try:
                    from h3_run_manifest import update_run

                    update_run(ROOT, a.run_id, task_state="FAILED",
                               last_error=f"{type(e).__name__}: {e}"[:500])
                except Exception:
                    pass
        print("ERROR:", str(e), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
