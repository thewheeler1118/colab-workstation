"""Muse -> official colab CLI -> H3 G4/A100 orchestration.

GPU DISABLED unless explicit flags. One TaskSpec `run` per invocation
(--task-spec <spec.json>; --variant A|B is only the built-in fixture
adapter that builds the equivalent spec). Tasks share one persistent
per-task 30 CU ledger
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
import functools
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
    parse_task_deadline,
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
    MODEL_HF_REVISION,
    POSTER_SHA256,
    POSTER_DRIVE_ID,
    STORY_DRIVE_ID,
)
from h3_task_spec import (
    TaskSpec,
    load_task_spec,
    task_spec_for_variant,
    validate_ref_image_file,
)
from auth_support import resolve_colab_executable, audit_user_adc_file
from h3_environment import verify_environment

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


def _comfyui_download_task():
    """ComfyUI source-cache descriptor for the NONINTERACTIVE download
    path (TASK-006 P0-B): Drive file ids from the same local manifest
    _comfyui_cache_task reads, so the worker downloads the tarball
    instead of reading it from a mount. None when the cache manifest
    is missing or has no Drive ids: the worker then falls back to git
    clone, exactly as on the mount path."""
    manifest_path = ROOT / "model_cache" / "comfyui_source_manifest.json"
    if not manifest_path.exists():
        return None
    manifest = json.loads(manifest_path.read_text())
    files = manifest.get("drive_files") or {}
    archive = manifest.get("archive_name")
    entry = files.get(archive) if archive else None
    if not entry or not entry.get("id"):
        return None
    return {
        "file_id": entry["id"],
        "size": int(entry["size"]),
        "sha256": entry["sha256"],
        "archive": archive,
        "commit": manifest["commit"],
    }


# ---------------------------------------------------------------------------
# TASK-007: the generic task engine. One TaskSpec drives a run; --variant
# A|B is only the built-in fixture adapter. These helpers resolve the
# spec for every action and materialise its reference images as local
# files (verified against their SHA-256s) before any GPU exists.
# ---------------------------------------------------------------------------

def resolve_run_spec(args) -> TaskSpec:
    """Resolve the one TaskSpec for this invocation.

    --task-spec wins and must agree with --production-task-id when both
    are given; --task-spec and --variant together is an error. With no
    --task-spec, --variant A|B maps through the built-in fixture
    adapter bound to the repo poster. Neither given is an error (the
    caller decides whether a spec is required for its action).
    A previously resolved spec stashed on args._task_spec is reused.
    """
    cached = getattr(args, "_task_spec", None)
    if isinstance(cached, TaskSpec):
        return cached
    spec_path = getattr(args, "task_spec", None)
    variant = getattr(args, "variant", None)
    if spec_path and variant:
        raise ValueError("--task-spec and --variant conflict; pass exactly one")
    if spec_path:
        spec = load_task_spec(spec_path)
        task_id = getattr(args, "production_task_id", "") or PRODUCTION_TASK_ID
        if getattr(args, "production_task_id", "") and spec.task_id != task_id:
            raise ValueError(
                f"--task-spec task_id {spec.task_id!r} does not match "
                f"--production-task-id {task_id!r}")
        args._task_spec = spec
        return spec
    if variant in ("A", "B"):
        task_id = getattr(args, "production_task_id", "") or PRODUCTION_TASK_ID
        spec = task_spec_for_variant(variant, task_id, ROOT / "assets" / "original_poster.jpg")
        args._task_spec = spec
        return spec
    raise ValueError("--task-spec <spec.json> or --variant A|B is required")


def resolve_optional_spec(args):
    """Resolve a spec when one was given; None when neither --task-spec
    nor --variant was passed (the caller then runs its legacy path)."""
    if getattr(args, "task_spec", None) or getattr(args, "variant", None):
        return resolve_run_spec(args)
    return getattr(args, "_task_spec", None)


def _spec_from_manifest(manifest: dict):
    """Rebuild the TaskSpec a run started with, from its durable
    manifest (artifacts.task_spec, recorded at authorisation). Older
    manifests without one fall back to the A/B fixture adapter when
    their variant is A/B; anything else is None (callers then use the
    frozen A/B delivery shape)."""
    stored = (manifest.get("artifacts") or {}).get("task_spec")
    if isinstance(stored, dict):
        try:
            return TaskSpec.from_dict(stored)
        except ValueError:
            return None
    variant = manifest.get("variant")
    if variant in ("A", "B"):
        try:
            return task_spec_for_variant(
                variant, manifest.get("production_task_id", "legacy"),
                ROOT / "assets" / "original_poster.jpg")
        except ValueError:
            return None
    return None


def _download_drive_file(file_id: str, dest: Path, drive=None) -> Path:
    """Download one Drive file by id to dest (metadata + media read
    only, no writes to Drive). The caller verifies the SHA-256.
    `drive` is a dependency-injection hook for tests."""
    from drive_depot import drive_service

    svc = drive or drive_service()
    data = svc.files().get_media(fileId=file_id).execute()
    if not isinstance(data, (bytes, bytearray)):
        raise RuntimeError(f"Drive download for {file_id} did not return bytes")
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(bytes(data))
    return dest


def _materialize_ref_images(spec: TaskSpec, dest_dir: Path, drive=None) -> dict:
    """Make both reference images available as local files, verified
    against the spec SHA-256s, BEFORE any GPU exists.

    Local refs are validated in place and returned as-is. Drive refs
    are downloaded to dest_dir/ref_image_1.jpg (or _2.jpg) and verified
    right after download. Returns {"ref_image_1": Path,
    "ref_image_2": Path}; ref_image_2 reuses ref_image_1's file when
    the spec reuses it. Raises on any missing file or hash mismatch.
    """
    dest_dir = Path(dest_dir)
    out = {}
    for key, ref, fname in (
            ("ref_image_1", spec.ref_image_1, "ref_image_1.jpg"),
            ("ref_image_2", spec.effective_ref_image_2, "ref_image_2.jpg")):
        if ref.local_path is not None:
            validate_ref_image_file(ref.local_path, ref.sha256)
            out[key] = Path(ref.local_path)
        else:
            dest = dest_dir / fname
            _download_drive_file(ref.drive_file_id, dest, drive=drive)
            validate_ref_image_file(dest, ref.sha256)
            out[key] = dest
    return out


def _check_task_spec_refs(spec: TaskSpec, drive=None) -> str:
    """Preflight reference-image gate (0 CU): local refs must exist with
    matching SHA-256; Drive refs must exist and not be trashed (their
    SHA-256 cannot be proven from Drive metadata — it is verified when
    the file is materialised, still before any GPU). Returns a summary.
    """
    from drive_depot import drive_service

    parts = []
    for key, ref in (("ref_image_1", spec.ref_image_1),
                     ("ref_image_2", spec.effective_ref_image_2)):
        if ref.local_path is not None:
            validate_ref_image_file(ref.local_path, ref.sha256)
            parts.append(f"{key}=local:{Path(ref.local_path).name} sha256 ok")
        else:
            svc = drive or drive_service()
            meta = svc.files().get(
                fileId=ref.drive_file_id,
                fields="id,name,trashed,size").execute()
            if not meta or meta.get("trashed"):
                raise RuntimeError(f"{key}: Drive file {ref.drive_file_id} missing or trashed")
            parts.append(f"{key}=drive:{meta.get('name') or ref.drive_file_id} "
                         f"size={meta.get('size')} (sha256 verified at download, pre-GPU)")
    return "; ".join(parts)


def _build_drive_download_block(cache_manifest):
    """Build the drive_download task block from the server-verified
    model manifest (+ the ComfyUI download entry when present).
    The block carries Drive file ids, pinned sizes and SHA-256s only —
    never a token value. Raises when the manifest does not name
    exactly the four pinned model files."""
    manifest = json.loads(Path(cache_manifest).read_text())
    entries = manifest.get("files", [])
    from h3_contract import MODEL_FILES

    if {x.get("path") for x in entries} != set(MODEL_FILES):
        raise RuntimeError("drive_download needs exactly the four pinned model files")
    files = []
    for x in entries:
        if not x.get("drive_file_id") or not x.get("sha256"):
            raise RuntimeError("drive_download entry missing drive id/sha256: " + str(x.get("path")))
        files.append({"file_id": x["drive_file_id"], "path": x["path"],
                      "size": int(x["size"]), "sha256": x["sha256"],
                      "md5": x.get("server_md5")})
    probe_file = min(files, key=lambda f: f["size"])
    return {
        "files": files,
        "comfyui": _comfyui_download_task(),
        "dest_dir": REMOTE + "/drive_downloads",
        "token_file": REMOTE + "/drive_token.json",
        "probe": {"file_id": probe_file["file_id"]},
    }


def verify_drive_download_preflight(args) -> dict:
    """Pre-GPU-allocation proof that the noninteractive download chain
    works: the download module imports, a read-only token mints from
    the user ADC, and that token can read Drive metadata for a pinned
    model file through the host's proven requests + certifi transport
    (TASK-007: the old stdlib-urllib metadata read can fail behind
    this VM's authenticated TLS-intercepting egress proxy; the remote
    worker keeps its urllib implementation). This proves the credential
    chain only; remote throughput is NOT proven here
    (TESTABLE_HYPOTHESIS) and is measured by the remote probe + the
    restore itself. Raises on any failure; nothing is allocated yet,
    so a refusal is clean."""
    import h3_drive_download
    import h3_drive_token
    from h3_drive_transport import build_token_drive_service

    if not hasattr(h3_drive_download, "DriveDownloader") \
            or not hasattr(h3_drive_download, "FileTokenProvider"):
        raise RuntimeError("h3_drive_download module is missing its download API")
    doc = h3_drive_token.mint_readonly_token()
    # Prove the minted token against the live Drive API with a metadata
    # GET only (no media bytes): a token that cannot read metadata
    # cannot download anything either.
    block = _build_drive_download_block(args.cache_manifest)
    service = build_token_drive_service(doc["access_token"])
    meta = service.files().get(
        fileId=block["files"][0]["file_id"],
        fields="id,name,size,md5Checksum",
        supportsAllDrives=True).execute()
    if str(meta.get("size", "")) != str(block["files"][0]["size"]):
        raise RuntimeError("preflight metadata size mismatch for probe file")
    record({"event": "drive_download_preflight_ok",
            "run_id": getattr(args, "run_id", ""),
            "token_id": doc["token_id"],
            "probe_file_id": block["files"][0]["file_id"]})
    return {"preflight": "ok", "token_id": doc["token_id"],
            "probe_file_id": block["files"][0]["file_id"]}


def verify_drive_file_token_archive() -> dict:
    """Pre-GPU-allocation proof for the worker's upload direction
    (TASK-007, TASK-006 P1-A): mint the least-privilege drive.file
    token and prove it can see the archive root the controller
    pre-creates with the full-scope service. Metadata GET only — zero
    writes, no throwaway files. A token that cannot see the folder
    cannot upload into it, which refutes the P1-A drive.file
    hypothesis; that must fail preflight (0 CU), never the run. When
    the archive root does not exist yet the probe is deferred to
    bootstrap (which creates it) instead of failing. Raises on any
    other failure. Returns token_id + folder evidence, never a token
    value."""
    import h3_drive_token
    from drive_depot import drive_service, list_named
    from h3_drive_transport import build_token_drive_service

    file_doc = h3_drive_token.mint_file_token()
    service = drive_service()
    found = list_named(service, "root", "Muse-H3-Video-Outputs")
    if not found:
        return {"token_id": file_doc["token_id"], "archive_folder": None,
                "note": "archive root not yet created; visibility probe "
                        "deferred to bootstrap (the folder is created "
                        "there with the full-scope service)"}
    folder_id = found[0]["id"]
    token_service = build_token_drive_service(file_doc["access_token"])
    meta = token_service.files().get(
        fileId=folder_id, fields="id,name",
        supportsAllDrives=True).execute()
    if meta.get("id") != folder_id:
        raise RuntimeError(
            "drive.file token cannot see the archive folder " + str(folder_id))
    record({"event": "drive_file_token_archive_ok",
            "token_id": file_doc["token_id"],
            "archive_folder": folder_id})
    return {"token_id": file_doc["token_id"], "archive_folder": folder_id,
            "archive_folder_name": meta.get("name")}


def _upload_drive_token(session: str, run_dir, token_doc: dict, remote_name: str) -> dict:
    """Write a token document to a local 0600 file and upload it to
    the remote workdir. Returns {"token_id", "remote_path"}. The token
    value is never logged or returned in the record (only token_id)."""
    import h3_drive_token

    local_path = h3_drive_token.write_token_file(run_dir / remote_name, token_doc)
    run_cli(["upload", "-s", session, str(local_path), REMOTE + "/" + remote_name], timeout=120)
    return {"token_id": token_doc["token_id"], "remote_path": REMOTE + "/" + remote_name}


def _mint_and_upload_drive_token(session: str, run_dir, remote_name: str, mint_fn=None) -> dict:
    """Mint a fresh token and upload it as a 0600 file.
    One helper so the token branches have a single boundary (the
    chain harnesses stub this, like mount_drive); the real mint +
    upload are covered by tests/test_drive_download.py. `mint_fn`
    selects the scope: read-only by default, `mint_file_token` for
    the P1-A worker upload token (TASK-006 P1-A)."""
    import h3_drive_token

    mint = mint_fn or h3_drive_token.mint_readonly_token
    return _upload_drive_token(session, run_dir, mint(), remote_name)


def _build_drive_upload_block(run_id: str, variant: str) -> dict:
    """Pre-create the run's Drive output folder and build the
    drive_upload task block (TASK-006 P1-A). The folder is created with
    the full-scope controller service so the worker's least-privilege
    drive.file token only ever writes into a folder this app created.
    Returns {folder_id, file_name, run_id, variant} — ids only, never
    a token value."""
    from drive_depot import drive_service, ensure_folder

    service = drive_service()
    out_root = ensure_folder(service, None, "Muse-H3-Video-Outputs", allow_write=True)
    run_folder = ensure_folder(service, out_root, run_id, allow_write=True)
    return {"folder_id": run_folder, "file_name": f"{variant}.mp4",
            "run_id": run_id, "variant": variant}


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


def check_remote_output(exec_result: dict, variant: str, spec=None) -> dict:
    """Fail-closed gate on the remote MP4 BEFORE any download: the remote
    output-check script must report OUTPUT_CHECK_JSON with ok=true, the
    file existing, and ffprobe reading the spec's total frames at the
    spec fps and output size (fps within a 0.5 tolerance for container
    rounding). The worker's MUSE_H3_DONE_JSON marker alone is a claim;
    this is the independent remote evidence the file is real and
    decodable. Raises with the check output tail otherwise. Only the
    spec-less legacy path falls back to the frozen A/B shape
    (274 / 24 / 1080x1920)."""
    expected_frames = spec.total_frames if spec is not None else 274
    expected_fps = spec.fps if spec is not None else 24
    expected_width = spec.output_width if spec is not None else 1080
    expected_height = spec.output_height if spec is not None else 1920
    text = (exec_result or {}).get("stdout", "") or ""
    for line in text.splitlines():
        if line.startswith("OUTPUT_CHECK_JSON:"):
            info = json.loads(line.split("OUTPUT_CHECK_JSON:", 1)[1].strip())
            fps = info.get("fps")
            fps_ok = isinstance(fps, (int, float)) and abs(fps - expected_fps) <= 0.5
            if info.get("ok") and info.get("exists") and info.get("frames") == expected_frames \
                    and fps_ok and info.get("width") == expected_width and info.get("height") == expected_height:
                return info
            raise RuntimeError(
                f"Remote output check failed for variant {variant}: expected "
                f"{expected_frames} frames at {expected_fps}fps "
                f"{expected_width}x{expected_height}, got {json.dumps(info, ensure_ascii=False)[:800]}")
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


def validate_local(api, proof, spec=None):
    """Static + contract validation of the API workflow.

    With a TaskSpec the workflow is patched once with that spec and the
    node values must equal the spec values. Without one (legacy callers)
    the old A/B loop runs: both variants must patch cleanly against
    the frozen constants.
    """
    manifest = json.loads(Path(proof).read_text())
    validate_model_cache_manifest(manifest)
    from h3_contract import patch_api_workflow, patch_api_workflow_for_spec

    raw = json.loads(Path(api).read_text())
    if "nodes" in raw:
        raise ValueError("Expected ComfyUI API format, not UI-format workflow (nodes/links)")
    if spec is None:
        validate_poster(ROOT / "assets" / "original_poster.jpg")
        for variant in ("A", "B"):
            patched = patch_api_workflow(raw, variant)
            node = patched["136"]["inputs"]
            assert node["width"] == 736 and node["height"] == 1312 and node["length"] == 226
            assert patched["124"]["inputs"]["steps"] == 25
            assert patched["124"]["inputs"]["scheduler"] == "beta"
            assert patched["146"]["inputs"]["value"] is False
        return manifest
    patched = patch_api_workflow_for_spec(raw, spec)
    node = patched["136"]["inputs"]
    assert node["width"] == spec.width and node["height"] == spec.height \
        and node["length"] == spec.animation_frames
    assert patched["124"]["inputs"]["steps"] == spec.steps
    assert patched["124"]["inputs"]["scheduler"] == "beta"
    assert patched["146"]["inputs"]["value"] is False
    assert node["prompt"] == spec.prompt
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
                              authorize_new_task: bool = False,
                              deadline_utc: str | None = None) -> dict:
    live = read_live_usage(_COLAB_AUTH)
    if live.get("balance") is None:
        raise RuntimeError("Cannot parse CU balance; abort")
    return load_or_create_budget_ledger(LEDGER_PATH, live["balance"], run_id,
                                        prior_unbilled_reserve_cu=prior_unbilled_reserve_cu,
                                        authorize_new_task=authorize_new_task,
                                        deadline_utc=deadline_utc)


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


def validate_comfy_workflow(api_path: Path, spec_or_variant) -> dict:
    """Static + contract validation of the API workflow for one task.

    Accepts a TaskSpec, or a legacy "A"/"B" string (mapped through the
    fixture adapter). The patched workflow's node values must equal the
    spec's values.
    """
    raw = json.loads(Path(api_path).read_text())
    if "nodes" in raw:
        raise ValueError("Expected ComfyUI API format, not UI-format workflow (nodes/links)")
    from h3_contract import patch_api_workflow_for_spec

    if isinstance(spec_or_variant, TaskSpec):
        spec = spec_or_variant
    else:
        spec = task_spec_for_variant(
            spec_or_variant, PRODUCTION_TASK_ID,
            ROOT / "assets" / "original_poster.jpg")
    api = patch_api_workflow_for_spec(raw, spec, "muse_ref_1.jpg", "muse_ref_2.jpg")
    node = api["136"]["inputs"]
    assert node["width"] == spec.width and node["height"] == spec.height \
        and node["length"] == spec.animation_frames
    assert node["ref_image_size"] == "match"
    assert node["prompt"] == spec.prompt
    assert api["124"]["inputs"]["steps"] == spec.steps and api["124"]["inputs"]["scheduler"] == "beta"
    assert api["146"]["inputs"]["value"] is False
    return api


def preflight(args) -> dict:
    """Fully read-only. Never creates a GPU. Returns a checks dict; raises on failure.

    With a --task-spec (or --variant) the TaskSpec is resolved first:
    its schema, reference images and workflow patch are all validated
    here, at 0 CU. Without one the legacy A/B checks run.
    """
    checks = {}
    spec = resolve_optional_spec(args)

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

    def _drive_download_preflight():
        # TASK-007: the dual-token gate. The readonly token is minted
        # and proven against live Drive metadata (requests transport)
        # before any GPU exists; an invalid_scope surfaces here, at
        # 0 CU, never after allocation.
        result = verify_drive_download_preflight(args)
        return {"token_id": result["token_id"],
                "probe_file_id": result["probe_file_id"]}

    def _drive_file_token_archive():
        # TASK-007: the drive.file token the worker will upload with
        # is minted and proven against the archive root (metadata
        # only, zero writes). See verify_drive_file_token_archive.
        return verify_drive_file_token_archive()

    if getattr(args, "drive_source", "download") == "download":
        check("drive_download_preflight", _drive_download_preflight)
        check("drive_file_token_archive", _drive_file_token_archive)

    if spec is None:
        check("poster_sha256", lambda: (validate_poster(ROOT / "assets" / "original_poster.jpg"), "sha256 ok")[1])
    else:
        # TASK-007 Task 3: the spec's reference images are proven here,
        # at 0 CU — local files by SHA-256, Drive files by metadata
        # existence (their SHA-256 is verified at materialisation,
        # still before any GPU).
        check("task_spec_ref_images", lambda: _check_task_spec_refs(spec))
    check("storyboard_drive_id", lambda: STORY_DRIVE_ID == "1TTv-xyLsSLSvTL1jy9ewOFM4qXUKBwpP" or (_ for _ in ()).throw(RuntimeError("Storyboard Drive ID mismatch")))

    def _workflow():
        if spec is None:
            validate_local(args.workflow_api, args.cache_manifest)
            return "API workflow A/B patch ok, 736x1312, 226 frames, 25 steps, non-Turbo"
        validate_local(args.workflow_api, args.cache_manifest, spec)
        validate_comfy_workflow(args.workflow_api, spec)
        return (f"task-spec patch ok, {spec.width}x{spec.height}, "
                f"{spec.animation_frames} frames + {spec.tail_card_frames} tail, "
                f"{spec.steps} steps, seed {spec.seed}, "
                f"total {spec.total_frames} frames {spec.duration_sec:.2f}s")

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

    def _environment_contract():
        # TASK-005 §6 (Issue #29): the full environment gate — CLI
        # version + client hash + both patches + ffmpeg/ffprobe + the
        # allowed Python list — against the checked-in manifest. The two
        # patch checks above stay as granular diagnostics.
        from h3_environment import verify_environment

        return verify_environment()

    check("environment_contract", _environment_contract)

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
    if spec is not None:
        result["task_spec"] = {
            "task_id": spec.task_id, "output_prefix": spec.output_prefix,
            "origin": spec.origin,
            "ref_image_1_sha256": spec.ref_image_1.sha256,
            "ref_image_2_sha256": spec.effective_ref_image_2.sha256,
            "prompt_chars": len(spec.prompt),
            "width": spec.width, "height": spec.height, "fps": spec.fps,
            "animation_frames": spec.animation_frames,
            "tail_card_frames": spec.tail_card_frames,
            "total_frames": spec.total_frames,
            "duration_sec": round(spec.duration_sec, 4),
            "steps": spec.steps, "seed": spec.seed,
            "output_width": spec.output_width, "output_height": spec.output_height,
        }
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


def download_and_validate_variant(session: str, variant: str, run_id: str, spec=None) -> dict:
    from h3_video_qa import validate_final_video

    out_dir = OUTPUT_ROOT / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    # Remote worker writes output/shao_nian_h3_<output_prefix>.mp4 (already assembled)
    target = out_dir / f"{variant}.mp4"
    run_cli(["download", "-s", session, REMOTE + f"/output/shao_nian_h3_{variant}.mp4", str(target)], timeout=600)
    info = validate_final_video(
        target,
        expected_frames=spec.total_frames if spec is not None else 274,
        expected_fps=spec.fps if spec is not None else 24,
        expected_width=spec.output_width if spec is not None else 1080,
        expected_height=spec.output_height if spec is not None else 1920)
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
        entry = upload_one(service, local_path, run_folder, name, name, sha, MODEL_HF_REVISION)
        meta = get_file_evidence(service, entry["drive_file_id"])
        if not check_archive_metadata(meta, run_folder):
            raise RuntimeError(f"Drive archive parent/trash check failed for {name}")
        uploaded[name] = {"drive_file_id": entry["drive_file_id"], "size": entry["size"],
                          "server_md5": entry["server_md5"], "sha256": sha, "parent": run_folder}
    return {"output_folder_id": run_folder, "files": uploaded}


def _validate_delivery_inputs(variant: str, run_id: str, video_info: dict, spec=None) -> Path:
    """Fail-closed pre-check shared by deliver_variant_to_drive and the
    idempotent wrapper: the local file must exist and be exactly the
    validated final video video_info describes, with the spec's total
    frames, output size and fps (fps within a 0.5 tolerance). When no
    spec is passed, the run's durable manifest is consulted for its
    recorded TaskSpec; only a manifest with no usable spec falls back
    to the frozen A/B delivery shape (274 / 24 / 1080x1920)."""
    from h3_contract import sha256_file

    if spec is None:
        try:
            from h3_run_manifest import load_run_manifest

            spec = _spec_from_manifest(load_run_manifest(ROOT, run_id) or {})
        except Exception:
            spec = None
    expected_frames = spec.total_frames if spec is not None else 274
    expected_fps = spec.fps if spec is not None else 24
    expected_width = spec.output_width if spec is not None else 1080
    expected_height = spec.output_height if spec is not None else 1920
    local_path = OUTPUT_ROOT / run_id / f"{variant}.mp4"
    if not local_path.is_file():
        raise RuntimeError(f"Delivery refused: local MP4 missing: {local_path}")
    for key in ("frame_count", "width", "height", "fps", "sha256"):
        if key not in video_info:
            raise RuntimeError(f"Delivery refused: video_info lacks {key}")
    if (video_info["frame_count"], video_info["width"], video_info["height"]) != (expected_frames, expected_width, expected_height):
        raise RuntimeError(
            f"Delivery refused: local video is {video_info['frame_count']} frames "
            f"{video_info['width']}x{video_info['height']}, expected {expected_frames} frames "
            f"{expected_width}x{expected_height}")
    fps = video_info.get("fps")
    if not isinstance(fps, (int, float)) or abs(fps - expected_fps) > 0.5:
        raise RuntimeError(
            f"Delivery refused: local video fps={fps!r}, expected {expected_fps}")
    if sha256_file(local_path) != video_info["sha256"]:
        raise RuntimeError(f"Delivery refused: local MP4 SHA-256 does not match validated video_info: {local_path}")
    return local_path


def _recorded_delivery(run_id: str) -> str | None:
    """Manifest-recorded Drive file id for this run, if a previous
    delivery already recorded one. Best-effort: no manifest (or no id)
    simply means 'nothing recorded'."""
    try:
        from h3_run_manifest import load_run_manifest

        artifacts = load_run_manifest(ROOT, run_id).get("artifacts") or {}
        return artifacts.get("drive_file_id") or None
    except Exception:
        return None


def _existing_delivery_evidence(variant: str, run_id: str, video_info: dict,
                                 drive_file_id: str, drive_output_folder_id: str | None,
                                 reused_via: str) -> dict:
    """Delivery evidence for a server-verified file that already exists:
    same shape as a fresh upload, plus reuse markers so callers and
    tests can tell no new upload happened."""
    return {
        "variant": variant, "run_id": run_id,
        "local_video_validated": True,
        "drive_file_id": drive_file_id,
        "drive_upload_verified": True,
        "frames": video_info["frame_count"], "width": video_info["width"],
        "height": video_info["height"], "fps": video_info["fps"],
        "sha256": video_info["sha256"],
        "drive_output_folder_id": drive_output_folder_id,
        "state": "DELIVERY_COMPLETED",
        "reused_existing": True,
        "reused_via": reused_via,
    }


def deliver_variant_to_drive_idempotent(variant: str, run_id: str, video_info: dict,
                                        archive_fn=None, max_attempts: int = 3, spec=None) -> dict:
    """Idempotent delivery (TASK-005 §5, Issue #28). Before uploading, a
    manifest-recorded drive_file_id is server-verified (size + MD5
    against the local file via drive_api.verify_uploaded_file) and reused
    when it matches — this covers the 'upload succeeded but the client
    never got the receipt' case, where the manifest already holds the id
    but the run was never marked delivered.

    A recorded id that does NOT verify is an explicit error, never a
    silent re-upload (the recorded file may be a different video, or the
    manifest may be stale). With no recorded id, the upload path runs:
    upload_one itself reuses a same-name/same-MD5 file in the run folder
    and refuses to overwrite different content, so a retry never creates
    a duplicate or clobbers a correct file."""
    from drive_api import verify_uploaded_file
    from drive_depot import drive_service

    local_path = _validate_delivery_inputs(variant, run_id, video_info, spec)
    recorded_id = _recorded_delivery(run_id)
    if recorded_id:
        service = drive_service()
        try:
            verify_uploaded_file(service, recorded_id, local_path)
        except Exception as e:  # noqa: BLE001 - explicit, never silent
            raise RuntimeError(
                f"Recorded Drive file {recorded_id} for run {run_id} does not "
                f"verify against the local MP4 ({e}); refusing to silently "
                f"re-upload. Inspect the Drive file before re-delivering.") from e
        manifest_artifacts = {}
        try:
            from h3_run_manifest import load_run_manifest

            manifest_artifacts = load_run_manifest(ROOT, run_id).get("artifacts") or {}
        except Exception:
            pass
        return _existing_delivery_evidence(
            variant, run_id, video_info, recorded_id,
            manifest_artifacts.get("drive_output_folder_id"), "recorded_id")
    return deliver_variant_to_drive(variant, run_id, video_info,
                                    archive_fn=archive_fn, max_attempts=max_attempts, spec=spec)


def deliver_variant_to_drive(variant: str, run_id: str, video_info: dict,
                             archive_fn=None, max_attempts: int = 3, spec=None) -> dict:
    """Archive a locally validated MP4 to Drive AFTER the GPU is stopped.

    Call this only after download_and_validate_variant() has already run
    validate_final_video() on the local file. This helper re-proves the
    file on disk is that validated file (it must exist and its SHA-256
    must match video_info), then uploads with bounded retries and returns
    the delivery evidence. The server-side parent/trash check inside
    archive_variant_to_drive() is what makes drive_upload_verified true —
    a bare upload return is never enough.

    Delivery states (TASK-002 §4.5, TASK-005 §5): INFERENCE_COMPLETED /
    REMOTE_MP4_VALIDATED / LOCAL_MP4_DOWNLOADED / LOCAL_MP4_VALIDATED are
    recorded by run_variant before this call; this helper covers
    DURABLE_UPLOAD_STARTED (first attempt) / DURABLE_UPLOAD_VERIFIED
    (server-confirmed) in the manifest, and the caller records DELIVERED
    when it marks DRIVE_VERIFIED.

    On failure after max_attempts it raises RuntimeError stating the
    local MP4 path: the GPU is already stopped, so the correct recovery
    is an archive-only retry — never a re-run of the inference.
    """
    archive_fn = archive_fn or archive_variant_to_drive
    local_path = _validate_delivery_inputs(variant, run_id, video_info, spec)
    _manifest_update(run_id, artifacts={"delivery_state": "DURABLE_UPLOAD_STARTED"})
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
            _manifest_update(run_id, artifacts={"delivery_state": "DURABLE_UPLOAD_VERIFIED"})
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
    best-effort and never decides the verdict.

    TASK-006 P0-A (Issue #42): this function is the single place that
    marks the managed record stopped, and only on a confirmed STOPPED
    verdict. A marking failure never overturns a confirmed stop — the
    record stays stale-active, which reconcile repairs from live
    evidence. On an unconfirmed stop the record is deliberately left
    `active` (session id, run id, task id, deadline all retained), so
    no later path can mistake the GPU for released."""
    from h3_session_watchdog import SessionState, mark_session_stopped, query_session_state

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
    record_marked = False
    if stop_ok:
        try:
            record_marked = mark_session_stopped(session, SessionState.STOPPED)
        except Exception:  # noqa: BLE001 - marking is best-effort, never overturns a confirmed stop
            record_marked = False
        _confirm_gpu_lease_stopped(session)
    if not stop_ok:
        record({"event": "CRITICAL_ORPHAN_RISK", "session": session})
    return {"session": session, "stopped_confirmed": stop_ok, "state": last_state.value,
            "record_marked": record_marked}


def _lease_registry():
    """Lease registry with the Drive mirror (TASK-006 P0-C). The Drive
    service is built lazily inside the mirror backend, so constructing
    the registry never needs google libs up front."""
    from h3_gpu_lease_registry import DriveMirrorBackend, LeaseRegistry

    return LeaseRegistry(mirror=DriveMirrorBackend())


def _register_gpu_lease(session: str, gpu_model: str, run_id: str,
                        production_task_id: str, stop_deadline_utc: str,
                        budget_cap_cu: float, max_wall_sec=None) -> dict:
    """Register the lease for a just-allocated session (TASK-006 P0-C).
    Raises on local registry failure: the caller lets the exception
    propagate so the outer finally stops the session — an unregistered
    lease is invisible to the backup executor, so the run must not
    proceed. The mirror write is best-effort inside register_lease.
    `max_wall_sec` (TASK-007) sets the lease's absolute physical cap
    `emergency_deadline_utc` (started + max_wall + 900 s), past which
    the executor may stop even an unfinalised lease."""
    from h3_gpu_lease_registry import current_owner_id

    registry = _lease_registry()
    return registry.register_lease(
        session, run_id, production_task_id, gpu_model,
        stop_deadline_utc, budget_cap_cu, current_owner_id(),
        max_wall_sec=max_wall_sec)


def _update_gpu_lease_deadline(session: str, stop_deadline_utc: str) -> None:
    """Fail-closed lease deadline finalisation, 3 attempts (TASK-007).
    A local registry write failure raises after the retries: the caller
    lets it propagate so the outer finally stops the session — an
    unfinalised lease is invisible to the backup executor except past
    its emergency cap. Only a Drive mirror failure degrades to a
    warning event (the registry records mirror_pending and retries on
    the next mutation). A lease that no longer exists is a warning,
    not a raise: there is nothing to finalise."""
    registry = _lease_registry()
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            updated = registry.update_deadline(session, stop_deadline_utc,
                                               final=True)
            if updated is None:
                record({"event": "lease_deadline_missing", "session": session,
                        "deadline_utc": stop_deadline_utc})
                return
            if registry.mirror_pending():
                record({"event": "lease_mirror_pending", "session": session,
                        "deadline_utc": stop_deadline_utc})
            return
        except Exception as e:  # noqa: BLE001 - retried, then raised
            last_error = e
            if attempt < 2:
                time.sleep(2)
    record({"event": "lease_deadline_sync_failed", "session": session,
            "deadline_utc": stop_deadline_utc,
            "error": type(last_error).__name__ if last_error else "unknown"})
    if last_error is not None:
        raise last_error


def _confirm_gpu_lease_stopped(session: str) -> None:
    """Best-effort: mark the lease stopped after a confirmed stop
    (TASK-006 P0-C). Never raises; a failure is an audit event only —
    the confirmed stop stands, and reconcile repairs the lease from
    live evidence."""
    try:
        _lease_registry().confirm_stopped(session, "STOPPED")
    except Exception as e:  # noqa: BLE001 - marking is best-effort only
        record({"event": "lease_confirm_failed", "session": session,
                "error": type(e).__name__})


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


def _current_source_commit() -> str:
    """The commit this controller code came from (TASK-007.1, fix 3).
    Delegates to h3_environment.resolve_source_commit: git HEAD in a
    checkout, else the sync's RELEASE_COMMIT file in a non-git
    production dir, else "". The old code ran `git rev-parse HEAD`
    inline here and in _resume_run, which returned "" in production
    (no .git) — run manifests recorded an empty code_commit and the
    resume version check silently skipped."""
    try:
        from h3_environment import resolve_source_commit

        return resolve_source_commit(ROOT)
    except Exception:
        return ""


def _manifest_create(args, ledger, spec=None) -> None:
    """Best-effort manifest creation at task authorization (TASK-005
    §4.1). Never raises into the run. The TaskSpec (when given) is
    recorded in artifacts.task_spec so resume can rebuild the exact
    task the run started with; the manifest's variant field carries
    the spec output_prefix."""
    try:
        import hashlib

        from h3_run_manifest import create_run_manifest, update_run

        code_commit = _current_source_commit()

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
                            variant=spec.output_prefix if spec is not None else args.variant, code_commit=code_commit,
                            workflow_sha256=_sha(args.workflow_api),
                            model_manifest_sha256=_sha(args.cache_manifest),
                            budget=budget)
        if spec is not None:
            update_run(ROOT, args.run_id, artifacts={"task_spec": spec.to_dict()})
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


def _upload_wall_seconds(evidence: dict) -> float | None:
    """Elapsed upload wall time in seconds from the worker evidence
    timestamps (ISO UTC, Z-suffixed). None when either timestamp is
    missing or unparsable — the evidence is still recorded; only the
    derived wall time is absent."""
    try:
        import datetime

        fmt = "%Y-%m-%dT%H:%M:%SZ"
        start = datetime.datetime.strptime(
            evidence["upload_started_at_utc"], fmt).replace(
                tzinfo=datetime.timezone.utc)
        finish = datetime.datetime.strptime(
            evidence["upload_finished_at_utc"], fmt).replace(
                tzinfo=datetime.timezone.utc)
        return round((finish - start).total_seconds(), 3)
    except Exception:
        return None


def _record_remote_upload(run_id: str, state: dict) -> dict | None:
    """Record worker-side remote-upload evidence in the durable manifest
    (TASK-006 P1-A). The worker's job state carries `remote_upload`
    (evidence) or `remote_upload_failed` (error type) as top-level keys.
    A verified worker upload means a Drive copy existed BEFORE the GPU
    stop, so drive_verified_before_stop is recorded honestly; the task
    axis still reaches DRIVE_VERIFIED only after the stop, when the
    tail re-verifies the recorded id server-side against the local MP4
    (deliver_variant_to_drive_idempotent reuses a recorded id only
    after that verify). Returns the evidence dict or None.
    Best-effort: never raises."""
    try:
        evidence = (state or {}).get("remote_upload")
        if isinstance(evidence, dict) and evidence.get("drive_file_id"):
            already_recorded = _recorded_delivery(run_id) == evidence["drive_file_id"]
            artifacts = {
                "delivery_state": "REMOTE_UPLOAD_VERIFIED",
                "remote_upload": evidence,
                "drive_file_id": evidence["drive_file_id"],
                "drive_verified_before_stop": True,
            }
            if evidence.get("drive_output_folder_id"):
                artifacts["drive_output_folder_id"] = evidence["drive_output_folder_id"]
            # GPU cost of the worker-side upload: the wall time is
            # recorded here from the evidence timestamps. The CU cost
            # is wall_seconds x live session rate / 3600; the rate
            # lives in the ledger/live inspect, so the CU attribution
            # is derived by the operator from these two numbers, not
            # computed in this helper.
            wall = _upload_wall_seconds(evidence)
            if wall is not None:
                artifacts["upload_wall_seconds"] = wall
            _manifest_update(run_id, artifacts=artifacts)
            if not already_recorded:
                record({"event": "remote_upload_verified", "run_id": run_id,
                        "drive_file_id": evidence["drive_file_id"],
                        "size": evidence.get("size"),
                        "sha256": (evidence.get("sha256") or "")[:16],
                        "reused": bool(evidence.get("reused"))})
            return evidence
        failed = (state or {}).get("remote_upload_failed")
        if isinstance(failed, dict):
            record({"event": "remote_upload_failed", "run_id": run_id,
                    "error_type": failed.get("error_type"),
                    "error": str(failed.get("error", ""))[:300]})
    except Exception:
        pass
    return None


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
    # TASK-006 P1-A: a poll that catches the worker's upload evidence
    # records it immediately, so a controller restart after VIDEO_READY
    # still finds the Drive id in the manifest.
    _record_remote_upload(run_id, state)


def _handle_token_refresh_request(session: str, run_id: str, run_dir,
                                  renewal_state: dict, state: dict) -> None:
    """Serve one worker token-renewal request (TASK-006 P0-B).

    The worker asks through its durable job state
    (``token_refresh_requested``: token id + timestamp) when its
    short-lived Drive token is inside the renewal margin. The
    controller mints a fresh read-only token and uploads it as a new
    0600 file; the worker's provider picks it up on its next
    get_token() call. Bounded by MAX_TOKEN_RENEWALS per run; a
    renewal failure is recorded but never raises here — the worker
    fails closed on token expiry, and a renewal failure must not kill
    an otherwise healthy poll loop. Only token ids are recorded,
    never token values."""
    from h3_drive_download import MAX_TOKEN_RENEWALS
    import h3_drive_token

    req = (state or {}).get("token_refresh_requested")
    if not isinstance(req, dict):
        return
    token_id = req.get("token_id")
    if not token_id or token_id == renewal_state.get("last_token_id"):
        return  # already served this token, or nothing to serve
    if renewal_state.get("count", 0) >= MAX_TOKEN_RENEWALS:
        record({"event": "token_renewal_refused", "session": session,
                "run_id": run_id, "token_id": token_id,
                "reason": f"max renewals reached ({MAX_TOKEN_RENEWALS})"})
        renewal_state["last_token_id"] = token_id
        return
    # TASK-006 P1-A: the scope tells the controller which token to mint
    # and which remote file to replace. drive.file renews the worker
    # upload token; anything else (including a missing scope, for
    # backward compatibility) renews the read-only download token.
    if req.get("scope") == "drive.file":
        remote_name = "drive_upload_token.json"
        mint_fn = h3_drive_token.mint_file_token
    else:
        remote_name = "drive_token.json"
        mint_fn = h3_drive_token.mint_readonly_token
    try:
        doc = mint_fn()
        _upload_drive_token(session, run_dir, doc, remote_name)
        renewal_state["count"] = renewal_state.get("count", 0) + 1
        renewal_state["last_token_id"] = token_id
        record({"event": "token_renewed", "session": session,
                "run_id": run_id, "token_id": doc["token_id"],
                "remote_name": remote_name,
                "renewals_used": renewal_state["count"]})
    except Exception as e:  # noqa: BLE001 - recorded, worker fails closed
        record({"event": "token_renewal_failed", "session": session,
                "run_id": run_id, "token_id": token_id,
                "error_type": type(e).__name__})


def _make_worker_state_handler(run_id: str, session: str, run_dir,
                               renewal_state: dict):
    """Combine the manifest heartbeat with token-renewal service into
    the single on_state callback wait_for_worker expects."""
    def _handler(state: dict) -> None:
        _manifest_worker_state(run_id, state)
        _handle_token_refresh_request(session, run_id, run_dir,
                                      renewal_state, state)

    return _handler


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


def check_job_done_info(done_info, variant: str, spec=None) -> dict:
    """Fail-closed validation of a VIDEO_READY output record, mirroring
    check_worker_done_marker's contract for the detached path: the record
    must name a final MP4 with the spec's total frames and a SHA-256
    (only the spec-less legacy path expects the frozen 274-frame shape).
    The remote ffprobe check and the local validation after download are
    still the real gates; this only proves the worker claims a complete
    output."""
    if not isinstance(done_info, dict):
        raise RuntimeError(
            f"Worker VIDEO_READY record is not an object for variant {variant}: {done_info!r}"[:300])
    expected_frames = spec.total_frames if spec is not None else 274
    if done_info.get("frames") != expected_frames:
        raise RuntimeError(
            f"Worker VIDEO_READY record has frames={done_info.get('frames')!r}, expected {expected_frames}")
    if not done_info.get("file") or not done_info.get("sha256"):
        raise RuntimeError(
            f"Worker VIDEO_READY record is missing file/sha256: {done_info!r}"[:300])
    return done_info


def _inspect_live(run_id: str | None = None, retries: int = 3) -> dict:
    """inspect() with bounded retries for transient CLI failures
    (TASK-006 P1-B). A transient failure is recorded on the manifest
    (never a task failure by itself); only a failure on every attempt
    raises. Pre-allocation callers do NOT use this: before any GPU
    exists, an unverifiable balance still refuses outright."""
    last = None
    for attempt in range(retries):
        try:
            return inspect()
        except Exception as e:  # noqa: BLE001 - retried, then raised
            last = e
            if run_id is not None:
                _manifest_update(
                    run_id,
                    transient_error=f"inspect() transient failure "
                                    f"(attempt {attempt + 1}/{retries}): {e}"[:500])
            if attempt < retries - 1:
                time.sleep(2)
    raise last


def wait_for_worker(session: str, run_id: str, variant: str,
                    deadline_monotonic: float, poll_interval_sec: int = 60,
                    degraded_after_failures: int = 3,
                    on_state=None) -> dict:
    """Poll the detached worker until VIDEO_READY. Returns the final
    worker state. Raises on worker FAILED, on the budget wall time, on
    an unknown stage, or on a state record that belongs to another run.
    A failed poll never means 'the worker is dead' (TASK-006 P1-B):
    after `degraded_after_failures` consecutive poll failures the run
    is marked degraded (audit event + manifest transient error) and
    polling CONTINUES until the budget wall time — a network timeout
    is not a task failure. The watchdog still enforces the deadline
    on its own poll loop while the channel is down, so the extended
    retry cannot burn past the deadline."""
    failures = 0
    degraded = False
    while True:
        if time.monotonic() > deadline_monotonic:
            raise TimeoutError(
                f"Worker for run {run_id} did not finish before the budget wall time")
        try:
            state = poll_worker(session)
        except WorkerPollError as e:
            failures += 1
            if failures >= degraded_after_failures and not degraded:
                degraded = True
                record({"event": "poll_degraded", "session": session,
                        "run_id": run_id,
                        "consecutive_failures": failures,
                        "error": str(e)[:300]})
                _manifest_update(
                    run_id,
                    transient_error=f"worker poll degraded after {failures} "
                                    f"consecutive failures: {e}"[:500])
            time.sleep(poll_interval_sec)
            continue
        failures = 0
        degraded = False
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


def _archive_local_mp4(variant: str, run_id: str, spec=None) -> dict:
    """Archive-only delivery (TASK-003 §1, shared by the `archive` action
    and by `resume`): re-validate the local MP4 and upload it with
    bounded retries. No GPU, no session, no ledger, never a re-run of
    the inference. The MP4 is validated against the spec when one is
    given, else against the frozen A/B delivery shape."""
    from h3_video_qa import validate_final_video

    info = validate_final_video(
        OUTPUT_ROOT / run_id / f"{variant}.mp4",
        expected_frames=spec.total_frames if spec is not None else 274,
        expected_fps=spec.fps if spec is not None else 24,
        expected_width=spec.output_width if spec is not None else 1080,
        expected_height=spec.output_height if spec is not None else 1920)
    return deliver_variant_to_drive_idempotent(variant, run_id, info, spec=spec)


def _resume_archive_only(args, manifest: dict) -> dict:
    """COMPLETED_AWAITING_ARCHIVE: the local MP4 is durable; only the
    Drive upload is missing. Archive it and complete the run."""
    run_id = manifest["run_id"]
    variant = manifest["variant"]
    spec = _spec_from_manifest(manifest)
    try:
        delivery = _archive_local_mp4(variant, run_id, spec)
    except Exception as e:  # noqa: BLE001 - stays awaiting archive
        return {"resumed": False, "run_id": run_id,
                "reason": f"archive-only retry failed, still awaiting archive: {e}",
                "manifest_task_state": "COMPLETED_AWAITING_ARCHIVE"}
    _manifest_update(run_id, task_state="DRIVE_VERIFIED",
                     last_verified_stage="DRIVE_VERIFIED",
                     session_state="STOPPED",
                     artifacts={"drive_file_id": delivery.get("drive_file_id"),
                                "drive_output_folder_id": delivery.get("drive_output_folder_id"),
                                "delivery_state": "DELIVERED"},
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
    spec = _spec_from_manifest(manifest)
    local_mp4 = OUTPUT_ROOT / run_id / f"{variant}.mp4"
    if local_mp4.is_file():
        try:
            delivery = _archive_local_mp4(variant, run_id, spec)
        except Exception as e:  # noqa: BLE001 - local copy unusable
            return {"resumed": False, "run_id": run_id,
                    "reason": f"session stopped and the local MP4 cannot be archived: {e}"}
        # TASK-006 P1-A: the session is known STOPPED here (the tristate
        # query said so); record it with the DRIVE_VERIFIED move so the
        # TASK_COMPLETED gate (which requires a STOPPED session) can pass.
        _manifest_update(run_id, task_state="DRIVE_VERIFIED",
                         last_verified_stage="DRIVE_VERIFIED",
                         session_state="STOPPED",
                         artifacts={"drive_file_id": delivery.get("drive_file_id"),
                                    "drive_output_folder_id": delivery.get("drive_output_folder_id"),
                                    "delivery_state": "DELIVERED"})
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
            # TASK-006 P1-A: when the worker's remote-upload evidence is
            # on record, the metadata check also compares size + MD5
            # against it — an id alone is not proof the right bytes are
            # there. Without that evidence the id + not-trashed check
            # keeps its previous meaning.
            remote = (manifest.get("artifacts") or {}).get("remote_upload") or {}
            ok = meta.get("id") == drive_file_id and not meta.get("trashed")
            if ok and remote.get("size") is not None:
                ok = int(meta.get("size", -1)) == int(remote["size"])
            if ok and remote.get("md5"):
                ok = (meta.get("md5Checksum") or "").lower() == str(remote["md5"]).lower()
            if ok:
                # TASK-006 P1-A: the session is known STOPPED here (the
                # tristate query said so); record it with the
                # DRIVE_VERIFIED move so the TASK_COMPLETED gate can pass.
                _manifest_update(run_id, task_state="DRIVE_VERIFIED",
                                 last_verified_stage="DRIVE_VERIFIED",
                                 session_state="STOPPED")
                _manifest_update(run_id, task_state="TASK_COMPLETED")
                return {"resumed": True, "run_id": run_id, "variant": variant,
                        "outcome": "session stopped; Drive file already verified server-side",
                        "drive_file_id": drive_file_id}
            # Recorded id does not verify: fall through to FAILED below
            # (no durable artifact). The mismatch is recorded in the
            # audit event above, not silently re-uploaded.
            record({"event": "recorded_drive_file_unverified", "run_id": run_id,
                    "drive_file_id": drive_file_id})
        except Exception as e:  # noqa: BLE001 - Drive check unavailable
            return {"resumed": False, "run_id": run_id,
                    "reason": f"session stopped; Drive verification unavailable: {e}"}
    # TASK-006 P1-A: the runtime is gone, so the remote temp disk is gone
    # with it. When a worker upload was in flight (or verified) this is
    # a lost artifact, recorded as such; the task FAILS and counts in
    # the P1-C denominator. Never re-infer silently.
    artifacts = manifest.get("artifacts") or {}
    upload_in_flight = bool(artifacts.get("remote_upload")) or \
        artifacts.get("delivery_state") in ("REMOTE_UPLOAD_STARTED",
                                            "REMOTE_UPLOAD_VERIFIED")
    update_run(ROOT, run_id, task_state="FAILED",
               artifacts={"artifact_lost": True} if upload_in_flight else None,
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
    spec = _spec_from_manifest(manifest)
    done_info = check_job_done_info(worker_state.get("output"), variant, spec)
    output_check = exec_script(session_id, ROOT / "h3_output_check.py", timeout=180,
                               env={"H3_VARIANT": variant,
                                    "H3_EXPECTED_FRAMES": str(spec.total_frames) if spec is not None else "274",
                                    "H3_EXPECTED_FPS": str(spec.fps) if spec is not None else "24",
                                    "H3_OUTPUT_WIDTH": str(spec.output_width) if spec is not None else "1080",
                                    "H3_OUTPUT_HEIGHT": str(spec.output_height) if spec is not None else "1920"})
    check_remote_output(output_check, variant, spec)
    record({"event": "resume_remote_output_checked", "variant": variant,
            "session": session_id, "done": done_info})
    from h3_video_qa import validate_final_video

    info = None
    local_mp4 = OUTPUT_ROOT / run_id / f"{variant}.mp4"
    if local_mp4.is_file():
        try:
            info = validate_final_video(
                local_mp4,
                expected_frames=spec.total_frames if spec is not None else 274,
                expected_fps=spec.fps if spec is not None else 24,
                expected_width=spec.output_width if spec is not None else 1080,
                expected_height=spec.output_height if spec is not None else 1920)
            record({"event": "resume_local_mp4_reused", "variant": variant,
                    "run_id": run_id, "sha256": info["sha256"]})
        except Exception:
            info = None  # re-download below; a stale local file is never trusted
    if info is None:
        info = download_and_validate_variant(session_id, variant, run_id, spec)
    _manifest_update(run_id, task_state="VIDEO_VALIDATED",
                     last_verified_stage="VIDEO_VALIDATED",
                     artifacts={"local_sha256": info["sha256"],
                                "local_frames": info["frame_count"],
                                "delivery_state": "COMPLETE_LOCAL"})
    _manifest_update(run_id, task_state="ARTIFACT_DURABLE")
    # TASK-006 P1-A: record any worker-side remote-upload evidence the
    # resumed worker state carries AFTER the COMPLETE_LOCAL update, so
    # a worker upload is not clobbered back to COMPLETE_LOCAL by it.
    # The tail below re-verifies the recorded id server-side before
    # marking DRIVE_VERIFIED.
    _record_remote_upload(run_id, worker_state)
    live = _inspect_live(run_id)
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
        delivery = deliver_variant_to_drive_idempotent(variant, run_id, info, spec=spec)
    except Exception as e:  # noqa: BLE001 - video is durable locally
        _manifest_update(run_id, task_state="COMPLETED_AWAITING_ARCHIVE",
                         last_error=str(e)[:500])
        raise
    production_result["delivery"] = delivery
    _manifest_update(run_id, task_state="DRIVE_VERIFIED",
                     last_verified_stage="DRIVE_VERIFIED",
                     artifacts={"drive_file_id": delivery.get("drive_file_id"),
                                "drive_output_folder_id": delivery.get("drive_output_folder_id"),
                                "delivery_state": "DELIVERED"})
    _manifest_update(run_id, task_state="TASK_COMPLETED")
    return production_result


def _with_task_lock(fn):
    """Serialise one controller per production task (TASK-006 P1-B,
    Issue #50). The task id comes from args.production_task_id
    (run / reconcile) or from the run's manifest (resume / stop). When
    no task id can be resolved, the function runs unlocked — its own
    validation raises. A second controller for the same task refuses
    immediately (TaskLockBusy) without allocating anything."""
    @functools.wraps(fn)
    def wrapper(args, *a, **kw):
        task_id = getattr(args, "production_task_id", "") or ""
        if not task_id and fn.__name__ in ("_resume_run", "_resume_run_inner", "_stop_run"):
            try:
                from h3_run_manifest import load_run_manifest

                manifest = load_run_manifest(ROOT, getattr(args, "run_id", ""))
                task_id = (manifest or {}).get("production_task_id") or ""
            except Exception:
                task_id = ""
        if not task_id:
            return fn(args, *a, **kw)
        from h3_task_lock import TaskLock

        with TaskLock(ROOT, task_id):
            return fn(args, *a, **kw)

    return wrapper


def _worker_launched(manifest: dict) -> bool:
    """True when the remote worker was (almost certainly) already
    launched for this run. The bootstrap step record is the primary
    signal; the task state is the fallback for manifests written before
    P1-B (no ``bootstrap`` field) — a run at or past WORKER_STARTED was
    launched, because the old controller recorded WORKER_STARTED only
    after the task was built and immediately before launching. The
    sub-second window between that record and the launch is resolved
    fail-closed (refuse, never double-launch)."""
    steps = ((manifest.get("bootstrap") or {}).get("steps") or {})
    if (steps.get("launch_worker") or {}).get("state") == "done":
        return True
    try:
        from h3_run_manifest import TASK_STAGES
        return (TASK_STAGES.index(manifest.get("task_state", "")) >=
                TASK_STAGES.index("WORKER_STARTED"))
    except ValueError:
        return False


def _resume_bootstrap(args, manifest: dict) -> dict:
    """Resume a run whose controller died mid-bootstrap (TASK-006 P1-B).

    Only entered when the worker was never launched (no `launch_worker`
    step in the manifest). Re-runs the bootstrap idempotently from the
    recorded step, launches the worker, and finishes the run the same
    way as run_variant. Never allocates a GPU, never re-runs inference
    (none ever ran). The remaining budget is recomputed from the live
    rate + ledger, with the same 300 s floor as the poll-loop resume."""
    run_id = manifest["run_id"]
    variant = manifest["variant"]
    session_id = manifest["session_id"]
    task_id = manifest["production_task_id"]
    ledger_path = ledger_path_for_task(ROOT, task_id)
    if not ledger_path.is_file():
        return {"resumed": False, "run_id": run_id,
                "reason": f"no budget ledger for task {task_id}; resume never "
                          f"creates a 30 CU ledger"}
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    live = _inspect_live(run_id)
    if live["rate"] is None or live["rate"] <= 0:
        return {"resumed": False, "run_id": run_id,
                "reason": "live CU/hr rate unreadable; cannot bound a resumed bootstrap"}
    budget_sec = allowed_runtime_seconds(live["rate"], live["cu_balance"], ledger)
    budget_sec = min(budget_sec, getattr(args, "max_wall_sec", MAX_SESSION_SECONDS))
    if budget_sec < 300:
        return {"resumed": False, "run_id": run_id,
                "reason": "remaining budget cannot cover a minimal resumed run + cleanup reserve"}
    run_dir = RUNS / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    # TASK-007: rebuild the run's TaskSpec from the manifest (recorded
    # at authorisation) so the resumed bootstrap uploads the same
    # reference images and task.json as the original run. The session
    # already exists, so this materialisation spends no new GPU.
    spec = _spec_from_manifest(manifest)
    if spec is not None:
        args._task_spec = spec
        args.variant = spec.output_prefix
        refs = _materialize_ref_images(spec, run_dir)
        args._ref_image_1_path = refs["ref_image_1"]
        args._ref_image_2_path = refs["ref_image_2"]
    start = time.monotonic()
    boot = _run_bootstrap(args, session_id, run_dir, budget_sec, live["rate"], start)
    launch = launch_worker(session_id)
    record({"event": "worker_launched", "variant": variant, "session": session_id, "launch": launch})
    _manifest_update(run_id, bootstrap={
        "steps": {"launch_worker": {"state": "done", "at_utc": _utc_now_iso()}}})
    try:
        worker_state = wait_for_worker(
            session_id, run_id, variant,
            deadline_monotonic=start + budget_sec,
            poll_interval_sec=getattr(args, "poll_interval_sec", 60),
            on_state=_make_worker_state_handler(
                run_id, session_id, run_dir, boot["renewal_state"]))
        done_info = check_job_done_info(worker_state.get("output"), variant, spec)
    except Exception:
        # Pull the server log before the session stops: it holds the
        # /prompt rejection detail the worker output may lack.
        download_remote_comfyui_log(session_id, run_dir)
        raise
    return _resume_finish(args, manifest, worker_state)


@_with_task_lock
def _resume_run(args) -> dict:
    """Locked entry point; the body is _resume_run_inner so the deliver
    loop (which already holds the task lock) can call it directly."""
    return _resume_run_inner(args)


def _resume_run_inner(args) -> dict:
    """Reconcile a run from its durable manifest and resume it if it is
    safe to do so. The checklist (TASK-005 §4.3), in order:
    terminal states, code version, task ledger, real session state,
    worker state, local outputs, Drive evidence.
    Refusals never allocate a GPU, never stop a session blindly, never
    re-generate a completed video, never re-upload into a conflicting
    deliverable, and never create a 30 CU ledger."""
    from h3_run_manifest import resume_run

    # Environment contract (TASK-005 §6): resume drives the same CLI
    # (queries, polls, and — for the stop — an empty POST that needs the
    # patched client), so the gate runs before any side effect, including
    # the recovery-generation bump.
    verify_environment()
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
    # of a half-finished run. The current commit resolves through
    # _current_source_commit(), so the check also works in a non-git
    # production dir (RELEASE_COMMIT), where the old inline git call
    # returned "" and the check silently skipped.
    current_commit = _current_source_commit()
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
        # TASK-006 P1-B: the worker may never have launched — the
        # controller died mid-bootstrap. The manifest decides: no launch
        # signal means resume the bootstrap; a launch signal with no
        # observable state means refuse (never double-launch a worker).
        if _worker_launched(manifest):
            return {"resumed": False, "run_id": run_id,
                    "reason": f"worker launched but state unobservable ({e}); session left "
                              f"running — stop it explicitly with the stop action "
                              f"if it should not continue",
                    "view": view}
        return _resume_bootstrap(args, manifest)
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
    live = _inspect_live(run_id)
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
        on_state=_make_worker_state_handler(
            run_id, session_id, RUNS / run_id,
            {"count": 0, "last_token_id": None}))
    return _resume_finish(args, manifest, worker_state)


@_with_task_lock
def _stop_run(args) -> dict:
    """Stop a run's managed session via the structured stop contract
    (PR-2). Refuses when the manifest has no session, or the session is
    not in the managed file — a non-project session is never stopped."""
    from h3_run_manifest import load_run_manifest, update_run
    from h3_session_watchdog import stop_managed_session

    if not args.run_id:
        raise ValueError("--run-id is required for stop")
    # Environment contract (TASK-005 §6): the stop path issues an empty
    # POST (unassign) that needs the patched CLI; gate before touching
    # any session.
    verify_environment()
    manifest = load_run_manifest(ROOT, args.run_id)
    session_id = manifest.get("session_id")
    if not session_id:
        raise ValueError(f"manifest for run {args.run_id} has no session id; nothing to stop")
    result = stop_managed_session(session_id, colab_auth=_COLAB_AUTH)
    if result.get("state") == "STOPPED":
        update_run(ROOT, args.run_id, session_state="STOPPED")
    return result


@_with_task_lock
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


def _utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _bootstrap_state(run_id: str) -> dict:
    """The durable bootstrap record for a run (empty when absent).
    Best-effort: a missing/unreadable manifest means 'nothing done'."""
    try:
        from h3_run_manifest import load_run_manifest

        manifest = load_run_manifest(ROOT, run_id)
        return (manifest or {}).get("bootstrap") or {"steps": {}, "current_step": None}
    except Exception:
        return {"steps": {}, "current_step": None}


def _run_bootstrap(args, session: str, run_dir: Path, budget_sec: int,
                   live_rate: float, start: float) -> dict:
    """Run (or re-run, idempotently) the bootstrap sequence between
    GPU_ALLOCATED and WORKER_STARTED (TASK-006 P1-B, Issue #50).

    Steps are recorded in the manifest's `bootstrap` field before they
    are attempted (`current_step`) and after they succeed (`done`). A
    controller restart mid-bootstrap leaves an exact pointer at the
    interrupted step; a resume re-runs from there, skipping steps
    already `done` (uploads overwrite the same remote paths, so
    re-running a step is idempotent). The drive download probe and the
    mount probe always re-run: they are cheap and re-prove the remote
    still holds the token files / the mount. The task upload always
    re-runs too: budget_sec / rate are fresh on a resume.

    Returns {"drive_download", "drive_upload", "task", "renewal_state"}.
    Raises on any hard failure (the caller's finally stops the GPU).
    The drive_upload_block step is fail-soft: it records `skipped` and
    the run falls back to controller-side delivery.
    """
    run_id = args.run_id
    # TASK-007: the bootstrap runs one TaskSpec. run_variant resolves
    # and materialises it before allocation; _resume_bootstrap rebuilds
    # it from the manifest. A direct caller falls back to resolving
    # from args (--task-spec / --variant) and materialising here.
    spec = getattr(args, "_task_spec", None) or resolve_optional_spec(args)
    if spec is None:
        raise ValueError("bootstrap needs a TaskSpec (--task-spec or --variant)")
    args._task_spec = spec
    variant = spec.output_prefix
    ref1_path = getattr(args, "_ref_image_1_path", None)
    ref2_path = getattr(args, "_ref_image_2_path", None)
    if ref1_path is None or ref2_path is None:
        refs = _materialize_ref_images(spec, run_dir)
        ref1_path, ref2_path = refs["ref_image_1"], refs["ref_image_2"]
        args._ref_image_1_path, args._ref_image_2_path = ref1_path, ref2_path
    drive_source = getattr(args, "drive_source", "download")
    bootstrap = _bootstrap_state(run_id)
    done_steps = dict(bootstrap.get("steps") or {})

    def _is_done(step: str) -> bool:
        return (done_steps.get(step) or {}).get("state") == "done"

    def _begin(step: str) -> None:
        _manifest_update(run_id, bootstrap={"current_step": step})

    def _finish(step: str, state: str = "done") -> None:
        _manifest_update(run_id, bootstrap={
            "current_step": None,
            "steps": {step: {"state": state, "at_utc": _utc_now_iso()}}})
        done_steps[step] = {"state": state}

    drive_download = None
    drive_upload = None
    renewal_state = {"count": 0, "last_token_id": None}
    if drive_source == "mount":
        if not _is_done("mount_drive"):
            _begin("mount_drive")
            # Remote signature probe: verify drive.mount accepts timeout_ms
            # before spending the mount window on a patched call the remote
            # runtime would reject with TypeError.
            sig = exec_script(session, ROOT / "h3_mount_signature_probe.py", timeout=120)
            if not check_mount_signature(sig):
                raise RuntimeError("Remote drive.mount does not accept timeout_ms; abort before drivemount")
            # Official interactive Drive mount (patched remote timeout 600s).
            mount_drive(session, deadline_sec=budget_sec, run_id=run_id,
                        timeline_path=run_dir / "mount_timeline.json")
            _finish("mount_drive")
    if not _is_done("init_remote"):
        _begin("init_remote")
        exec_script(session, ROOT / "h3_init_remote.py", timeout=60)
        _finish("init_remote")
    if not _is_done("upload_contract_files"):
        _begin("upload_contract_files")
        # Upload the spec's reference images, the contract, assemble,
        # manifest, workflow, submitted workflow. ref_image_1.jpg /
        # ref_image_2.jpg are what the worker verifies and injects;
        # poster.jpg carries ref_image_1 under the legacy name so a
        # legacy worker fallback (task.json without task_spec) still
        # finds its poster.
        api = validate_comfy_workflow(args.workflow_api, spec)
        submitted_path = run_dir / f"workflow_{variant}_submitted.json"
        submitted_path.write_text(json.dumps(api, ensure_ascii=False, indent=2))
        for source, target in [
            (Path(ref1_path), REMOTE + "/poster.jpg"),
            (Path(ref1_path), REMOTE + "/ref_image_1.jpg"),
            (Path(ref2_path), REMOTE + "/ref_image_2.jpg"),
            (ROOT / "h3_contract.py", REMOTE + "/h3_contract.py"),
            (ROOT / "h3_task_spec.py", REMOTE + "/h3_task_spec.py"),
            (ROOT / "assemble.py", REMOTE + "/assemble.py"),
            (Path(args.cache_manifest), REMOTE + "/model_manifest.json"),
            (Path(args.workflow_api), REMOTE + "/workflow_api.json"),
            (submitted_path, REMOTE + "/workflow_submitted.json"),
        ]:
            run_cli(["upload", "-s", session, str(source), target], timeout=300)
        _finish("upload_contract_files")
    if drive_source == "mount":
        # The mount probe always re-runs: cheap, and it re-proves the
        # mount is still live. mount_drive itself is skipped when done
        # (it needs a TTY and must never be re-entered blindly).
        _begin("mount_probe")
        mount_cfg = RUNS / f"mount_{session}.json"
        mount_cfg.write_text(json.dumps({"drive_cache_dir": args.drive_cache_dir}))
        run_cli(["upload", "-s", session, str(mount_cfg), REMOTE + "/drive_mount_config.json"], timeout=120)
        mount_probe = exec_script(session, ROOT / "h3_drive_probe.py", timeout=180)
        persist_exec_result(run_dir, f"drive_probe_{variant}", mount_probe)
        if "MUSE_DRIVE_MOUNT_MODELS_VISIBLE 4" not in mount_probe["stdout"]:
            raise RuntimeError("Mounted Drive model cache not visible; release GPU")
        _finish("mount_probe")
    else:
        # The download probe always re-runs: fresh tokens are minted
        # each time, and the probe re-proves the remote transport with
        # a 1 MiB Range probe before the worker starts. The worker
        # token file is deleted by the worker on first read.
        _begin("drive_download_probe")
        drive_download = _build_drive_download_block(args.cache_manifest)
        _mint_and_upload_drive_token(session, run_dir, "drive_token.json")
        _mint_and_upload_drive_token(session, run_dir, "drive_token_probe.json")
        probe_spec_path = run_dir / "drive_download_probe.json"
        probe_spec_path.write_text(json.dumps(drive_download["probe"]))
        run_cli(["upload", "-s", session, str(probe_spec_path),
                 REMOTE + "/drive_download_probe.json"], timeout=120)
        run_cli(["upload", "-s", session, str(ROOT / "h3_drive_download.py"),
                 REMOTE + "/h3_drive_download.py"], timeout=120)
        run_cli(["upload", "-s", session, str(ROOT / "h3_drive_download_probe.py"),
                 REMOTE + "/h3_drive_download_probe.py"], timeout=120)
        download_probe = exec_script(session, ROOT / "h3_drive_download_probe.py", timeout=180)
        persist_exec_result(run_dir, f"drive_download_probe_{variant}", download_probe)
        if "MUSE_DRIVE_DOWNLOAD_PROBE_OK" not in download_probe["stdout"]:
            raise RuntimeError("Remote Drive download probe failed; release GPU")
        record({"event": "drive_download_probe_ok", "session": session,
                "run_id": run_id,
                "probe_file_id": drive_download["probe"]["file_id"]})
        _finish("drive_download_probe")
        if not _is_done("drive_upload_block"):
            _begin("drive_upload_block")
            # TASK-006 P1-A: worker-side durable upload. Pre-create
            # the run folder, mint the least-privilege drive.file
            # token, and upload the stdlib uploader module. Any
            # failure here is fail-soft: the worker simply gets no
            # drive_upload block and the run falls back to the
            # controller-side download + idempotent delivery.
            try:
                drive_upload = _build_drive_upload_block(run_id, variant)
                import h3_drive_token

                _mint_and_upload_drive_token(
                    session, run_dir, "drive_upload_token.json",
                    mint_fn=h3_drive_token.mint_file_token)
                run_cli(["upload", "-s", session, str(ROOT / "h3_drive_upload.py"),
                         REMOTE + "/h3_drive_upload.py"], timeout=120)
                record({"event": "drive_upload_block_ready", "session": session,
                        "run_id": run_id,
                        "folder_id": drive_upload["folder_id"]})
                _finish("drive_upload_block")
            except Exception as e:  # noqa: BLE001 - fail-soft to controller delivery
                drive_upload = None
                record({"event": "drive_upload_block_failed", "session": session,
                        "run_id": run_id, "error_type": type(e).__name__,
                        "error": str(e)[:300]})
                _finish("drive_upload_block", state="skipped")
    if not _is_done("upload_worker_files"):
        _begin("upload_worker_files")
        # Worker files go up only after the cache probe passed:
        # the probe must run before the remote worker that does H3
        # inference (and the test in test_auth_and_transport enforces
        # this order). The launcher needs both files on the remote disk.
        worker_uploads = [
            (ROOT / "h3_job_state.py", REMOTE + "/h3_job_state.py"),
            (ROOT / "h3_remote_worker.py", REMOTE + "/h3_remote_worker.py"),
        ]
        if drive_source == "download":
            worker_uploads.append(
                (ROOT / "h3_drive_download.py", REMOTE + "/h3_drive_download.py"))
        for source, target in worker_uploads:
            run_cli(["upload", "-s", session, str(source), target], timeout=300)
        _finish("upload_worker_files")
    # Single-variant remote worker. The task is always rebuilt: the
    # budget wall time and rate are fresh on a resume.
    _begin("upload_task")
    elapsed = time.monotonic() - start
    if elapsed > budget_sec:
        raise TimeoutError("Budget wall time reached before " + variant)
    task = {"variant": variant, "run_id": run_id, "drive_cache_dir": args.drive_cache_dir, "comfy_ref": args.comfy_ref,
            "production_task_id": getattr(args, "production_task_id", PRODUCTION_TASK_ID),
            "task_spec": spec.to_dict(),
            "inference_timeout_sec": min(1800, max(100, int(budget_sec - elapsed - 120))),
            "rate_cu_h": live_rate,
            "session_budget_cu": round(budget_sec * float(live_rate) / 3600.0, 3),
            "comfyui_cache": _comfyui_cache_task(),
            "drive_source": drive_source}
    if drive_source == "download":
        # The worker downloads from Drive with the delivered
        # short-lived token; drive_cache_dir is kept as the
        # download staging dir for the worker's path handling.
        task["drive_cache_dir"] = drive_download["dest_dir"]
        task["drive_download"] = drive_download
        # TASK-006 P1-A: the worker uploads the finished MP4
        # itself when this block is present; absent, the run
        # falls back to controller-side delivery.
        if drive_upload is not None:
            task["drive_upload"] = drive_upload
    task_path = RUNS / f"task_{session}.json"
    task_path.write_text(json.dumps(task))
    run_cli(["upload", "-s", session, str(task_path), REMOTE + "/task.json"], timeout=120)
    _finish("upload_task")
    _manifest_update(run_id, task_state="WORKER_STARTED", heartbeat=True)
    return {"drive_download": drive_download, "drive_upload": drive_upload,
            "task": task, "renewal_state": renewal_state}


@_with_task_lock
def run_variant(args) -> dict:
    """Locked entry point; the body is _run_variant_inner so the deliver
    loop (which already holds the task lock) can call it directly."""
    return _run_variant_inner(args)


# ---------------------------------------------------------------------------
# TASK-008: task-level automatic recovery loop (`deliver` action and
# `run --auto-recover`).
#
# A single `run` is one GPU attempt. `deliver` supervises a whole
# production task inside one TaskLock, one 30 CU task budget and an
# optional task_deadline_utc: it reuses an already-delivered run (0 CU),
# recovers an archive-only run (0 GPU / 0 CU), resumes an in-flight
# run, and — only behind the gates below — starts a controlled new
# attempt with a derived run_id. It never re-infers a completed video,
# never opens a second GPU while the previous session may be alive,
# and never retries a deterministic error. Every outcome is a result
# dict (like `resume`); a refusal is a verdict, not a crash.
# ---------------------------------------------------------------------------

DEFAULT_MAX_TASK_ATTEMPTS = 2
MAX_TASK_ATTEMPTS_LIMIT = 3
# Minimum viable wall time for a fresh attempt (40 min): model restore
# plus ~22 min inference plus download/stop margin. The actual wall is
# whatever allowed_runtime_seconds() grants from the live rate and the
# remaining budget; a grant below this floor refuses the attempt.
MIN_ATTEMPT_WALL_SEC = 2400
DEADLINE_MARGIN_SEC = 300
SESSION_STOP_WAIT_ATTEMPTS = 3
SESSION_STOP_WAIT_INTERVAL_SEC = 10

# Substrings that mark an error as deterministic — retrying it would
# only burn CU: account/permission problems, reference/model SHA-256
# mismatches, a missing reference image, a bad model depot, an
# exhausted budget, or a task that was never authorised. ValueError is
# deterministic by construction (bad input/config, never transient).
DETERMINISTIC_ERROR_MARKERS = (
    "ACCOUNT_MISMATCH",
    "Colab account != Drive account",
    "NEW_TASK_REQUIRES_AUTHORIZATION",
    "invalid_scope",
    "HTTP 403",
    "Forbidden",
    "INSUFFICIENT_CU",
    "Not enough paid time",
    "SHA-256 mismatch",
    "SHA256 mismatch",
    "SHA-256 does not match",
    "Reference image missing",
    "Wrong depot folder",
    "Four model files required",
    "No server-verified READY document",
    "Missing/invalid verified manifest",
    "READY document size mismatch",
)

# Resume-refusal reasons that mean the previous session may still be
# alive: opening a second GPU under any of them is forbidden.
_RESUME_LIVE_SESSION_MARKERS = (
    "UNKNOWN",
    "unobservable",
    "left running",
    "belongs to another run",
)


def _is_deterministic_error(exc: BaseException) -> bool:
    """True when retrying this error cannot help (see
    DETERMINISTIC_ERROR_MARKERS)."""
    if isinstance(exc, ValueError):
        return True
    text = str(exc)
    return any(marker in text for marker in DETERMINISTIC_ERROR_MARKERS)


def _resume_refusal_blocks_new_attempt(reason: str) -> bool:
    """True when a resume refusal means the previous session may still
    be alive (see _RESUME_LIVE_SESSION_MARKERS)."""
    return any(marker in (reason or "") for marker in _RESUME_LIVE_SESSION_MARKERS)


def _task_runs(root: Path, production_task_id: str) -> list[dict]:
    """Durable manifests for one production task, oldest first."""
    from h3_run_manifest import list_runs

    return [m for m in list_runs(root)
            if m.get("production_task_id") == production_task_id]


def _attempts_used(task_id: str) -> int:
    """GPU attempts already spent on this task: runs that allocated a
    session. A run that failed before allocation spends no attempt."""
    return sum(1 for m in _task_runs(ROOT, task_id) if m.get("session_id"))


def _read_task_ledger(task_id: str) -> dict | None:
    """The task's budget ledger, or None when no ledger exists yet."""
    path = ledger_path_for_task(ROOT, task_id)
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _provisional_ledger(live_balance_cu: float) -> dict:
    """Ledger-shaped dict for a task with no ledger file yet: the
    creation-time baseline is the live balance right now, exactly as
    load_or_create_budget_ledger would record it. Used only for the
    deliver budget gate; never written to disk."""
    return {
        "project_budget_cu": PROJECT_BUDGET_CU,
        "max_observed_spent_cu": 0.0,
        "hard_stop_balance_cu": round(float(live_balance_cu) - PROJECT_BUDGET_CU, 2),
        "prior_task_unbilled_reserve_cu": PRIOR_TASK_UNBILLED_RESERVE_CU,
    }


def _verify_completed_delivery(manifest: dict) -> tuple[str, dict | str | None]:
    """Re-prove a TASK_COMPLETED run's recorded delivery (0 CU).
    Returns ("verified", evidence), ("lost", reason), or
    ("unverifiable", reason). The Drive file must still exist and not
    be trashed; when the local MP4 is still present it must verify
    against the recorded file (size + MD5)."""
    artifacts = manifest.get("artifacts") or {}
    drive_file_id = artifacts.get("drive_file_id")
    run_id = manifest.get("run_id") or ""
    if not drive_file_id:
        return "lost", f"run {run_id} is TASK_COMPLETED but records no drive_file_id"
    try:
        from drive_depot import drive_service, get_file_evidence

        service = drive_service()
        meta = get_file_evidence(service, drive_file_id)
    except Exception as e:  # noqa: BLE001 - classified below
        text = str(e)
        if "404" in text or "notFound" in text:
            return "lost", f"Drive file {drive_file_id} for run {run_id} is gone ({text[:200]})"
        return "unverifiable", f"Drive evidence lookup failed for run {run_id}: {type(e).__name__}: {text[:200]}"
    if not meta or meta.get("trashed"):
        return "lost", f"Drive file {drive_file_id} for run {run_id} is missing or trashed"
    local_path = OUTPUT_ROOT / run_id / f"{manifest.get('variant') or ''}.mp4"
    if local_path.is_file():
        try:
            from drive_api import verify_uploaded_file

            verify_uploaded_file(service, drive_file_id, local_path)
        except Exception as e:  # noqa: BLE001 - a mismatch is definitive
            return "lost", (f"recorded Drive file {drive_file_id} does not verify "
                           f"against the local MP4 for run {run_id}: {e}")
    return "verified", {
        "variant": manifest.get("variant"), "run_id": run_id,
        "local_video_validated": True,
        "drive_file_id": drive_file_id,
        "drive_upload_verified": True,
        "frames": artifacts.get("local_frames"),
        "sha256": artifacts.get("local_sha256"),
        "drive_output_folder_id": artifacts.get("drive_output_folder_id"),
        "state": "DELIVERY_COMPLETED",
        "reused_existing": True,
        "reused_via": "task_completed_manifest",
    }


def _wait_for_session_stopped(session_id: str,
                              attempts: int = SESSION_STOP_WAIT_ATTEMPTS,
                              interval_sec: float = SESSION_STOP_WAIT_INTERVAL_SEC):
    """Bounded re-check for a previous attempt's session: returns the
    last observed SessionState (STOPPED / ACTIVE / UNKNOWN). UNKNOWN is
    never treated as STOPPED."""
    from h3_session_watchdog import SessionState, query_session_state

    last = SessionState.UNKNOWN
    for i in range(attempts):
        last = query_session_state(session_id, colab_auth=_COLAB_AUTH)
        if last == SessionState.STOPPED:
            return last
        if i < attempts - 1:
            time.sleep(interval_sec)
    return last


def _fresh_run_id() -> str:
    """A derived run_id in the established timestamped format."""
    import secrets

    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"h3-{stamp}-{secrets.token_hex(3)}"


def _deliver_resume_args(task_id: str, run_id: str) -> argparse.Namespace:
    """A minimal resume invocation for one run. Built fresh every time:
    _resume_bootstrap stashes the manifest's spec on args._task_spec,
    which must never leak into the deliver args' own spec cache."""
    return argparse.Namespace(
        run_id=run_id, production_task_id=task_id,
        max_wall_sec=MAX_SESSION_SECONDS, poll_interval_sec=60,
        variant=None, task_spec=None)


def _deliver_attempt_args(args, run_id: str) -> argparse.Namespace:
    """A run_variant invocation for one controlled attempt: a copy of
    the deliver args with the derived run_id. The copy keeps the
    resolved TaskSpec cache and the live-run confirmation flags."""
    run_args = argparse.Namespace(**vars(args))
    run_args.action = "run"
    run_args.run_id = run_id
    return run_args


def _deliver_result(delivered: bool, outcome: str, task_id: str, run_id: str | None,
                    delivery: dict | None, attempts_used: int, max_attempts: int,
                    steps: list, reason: str = "") -> dict:
    return {
        "delivered": delivered,
        "production_task_id": task_id,
        "outcome": outcome,
        "reason": reason,
        "run_id": run_id,
        "delivery": delivery,
        "attempts_used": attempts_used,
        "max_task_attempts": max_attempts,
        "steps": steps,
    }


def _resume_one(task_id: str, run_id: str):
    """One resume attempt; returns (result_dict_or_None, error_or_None).
    The caller re-reads the manifest to decide what actually happened —
    the return shape alone does not say whether the run progressed."""
    try:
        return _resume_run_inner(_deliver_resume_args(task_id, run_id)), None
    except Exception as e:  # noqa: BLE001 - the manifest decides what happened
        return None, e


@_with_task_lock
def deliver_task(args) -> dict:
    """Task-level supervised delivery (TASK-008). See the section header
    above. Never allocates a second GPU while the previous session may
    be alive; never re-infers a completed video; never retries a
    deterministic error. Returns a result dict; refusals are verdicts."""
    from h3_run_manifest import TERMINAL_STATES, load_run_manifest
    from h3_session_watchdog import SessionState

    task_id = getattr(args, "production_task_id", "") or ""
    if not task_id:
        raise RuntimeError("--production-task-id is required for deliver")
    try:
        max_attempts = int(getattr(args, "max_task_attempts", DEFAULT_MAX_TASK_ATTEMPTS))
    except (TypeError, ValueError):
        raise RuntimeError("--max-task-attempts must be an integer") from None
    if not 1 <= max_attempts <= MAX_TASK_ATTEMPTS_LIMIT:
        raise RuntimeError(
            f"--max-task-attempts must be 1..{MAX_TASK_ATTEMPTS_LIMIT}, got {max_attempts}")
    deadline = parse_task_deadline(getattr(args, "task_deadline_utc", "") or "")
    steps: list[dict] = []
    guard = 0
    # TASK-008.1: the caller's --run-id is attempt 1 only. Once a new
    # attempt has been started in this deliver call, later attempts must
    # derive a fresh run_id — reusing the first id would collide with
    # the manifest the first attempt already created.
    initial_run_id_used = False
    while True:
        guard += 1
        if guard > max_attempts + 10:
            return _deliver_result(False, "loop_guard", task_id, None, None,
                                   _attempts_used(task_id), max_attempts, steps,
                                   "deliver loop exceeded its iteration guard; refusing to continue")
        runs = _task_runs(ROOT, task_id)
        # 1. Reuse an already-delivered run (0 CU).
        completed = [m for m in runs if m.get("task_state") == "TASK_COMPLETED"]
        for manifest in sorted(completed, key=lambda m: m.get("created_at_utc", ""), reverse=True):
            status, payload = _verify_completed_delivery(manifest)
            if status == "verified":
                steps.append({"step": "reused_completed", "run_id": manifest.get("run_id")})
                return _deliver_result(True, "reused_completed", task_id, manifest.get("run_id"),
                                       payload, _attempts_used(task_id), max_attempts, steps)
            if status == "lost":
                return _deliver_result(False, "delivery_evidence_lost", task_id,
                                       manifest.get("run_id"), None,
                                       _attempts_used(task_id), max_attempts, steps, str(payload))
            steps.append({"step": "delivery_unverifiable", "run_id": manifest.get("run_id"),
                          "detail": str(payload)})
            return _deliver_result(False, "delivery_unverifiable", task_id,
                                   manifest.get("run_id"), None,
                                   _attempts_used(task_id), max_attempts, steps, str(payload))
        # 2. Archive-only recovery for COMPLETED_AWAITING_ARCHIVE (0 GPU / 0 CU).
        awaiting = [m for m in runs if m.get("task_state") == "COMPLETED_AWAITING_ARCHIVE"]
        if awaiting:
            manifest = sorted(awaiting, key=lambda m: m.get("created_at_utc", ""), reverse=True)[0]
            run_id = manifest.get("run_id") or ""
            result, error = _resume_one(task_id, run_id)
            steps.append({"step": "archive_recovery", "run_id": run_id,
                          "detail": str(error or result)[:300]})
            after = (load_run_manifest(ROOT, run_id) or {}).get("task_state")
            if after == "TASK_COMPLETED":
                continue  # re-read: step 1 picks the recovered run up
            return _deliver_result(False, "archive_failed", task_id, run_id, None,
                                   _attempts_used(task_id), max_attempts, steps,
                                   f"archive-only recovery did not complete run {run_id} "
                                   f"(state {after}): {str(error or result)[:300]}")
        # 3. Resume a non-terminal in-flight run.
        inflight = [m for m in runs if m.get("task_state") not in TERMINAL_STATES]
        if inflight:
            manifest = sorted(inflight, key=lambda m: m.get("created_at_utc", ""))[0]
            run_id = manifest.get("run_id") or ""
            before = manifest.get("task_state")
            result, error = _resume_one(task_id, run_id)
            steps.append({"step": "resume_inflight", "run_id": run_id,
                          "detail": str(error or result)[:300]})
            after = (load_run_manifest(ROOT, run_id) or {}).get("task_state")
            if after == "TASK_COMPLETED" or after in TERMINAL_STATES or after != before:
                continue  # progressed (completed / failed / archived): re-read
            if error is not None:
                # A resume that raised leaves the session state unknown:
                # never open a second GPU on top of it.
                return _deliver_result(False, "blocked_live_session", task_id, run_id, None,
                                       _attempts_used(task_id), max_attempts, steps,
                                       f"resume raised {type(error).__name__}: {str(error)[:300]}; "
                                       f"session state unknown, refusing a second GPU")
            reason = (result or {}).get("reason") or ""
            if _resume_refusal_blocks_new_attempt(reason):
                return _deliver_result(False, "blocked_live_session", task_id, run_id, None,
                                       _attempts_used(task_id), max_attempts, steps,
                                       f"in-flight run {run_id} may still hold a live session; "
                                       f"refusing a second GPU ({reason[:300]})")
            return _deliver_result(False, "resume_refused", task_id, run_id, None,
                                   _attempts_used(task_id), max_attempts, steps,
                                   f"in-flight run {run_id} refused resume without progressing "
                                   f"({reason[:300]})")
        # 4. Controlled new attempt behind the gates.
        attempts_used = _attempts_used(task_id)
        if attempts_used >= max_attempts:
            return _deliver_result(False, "attempts_exhausted", task_id,
                                   runs[-1].get("run_id") if runs else None, None,
                                   attempts_used, max_attempts, steps,
                                   f"task already used {attempts_used}/{max_attempts} GPU attempts")
        latest = runs[-1] if runs else None
        if latest is not None and latest.get("task_state") != "FAILED":
            return _deliver_result(False, "blocked_unexpected_state", task_id,
                                   latest.get("run_id"), None, attempts_used, max_attempts, steps,
                                   f"latest run {latest.get('run_id')} is {latest.get('task_state')}, "
                                   f"not FAILED; refusing a new attempt")
        if latest is not None and latest.get("session_id"):
            state = _wait_for_session_stopped(latest["session_id"])
            if state != SessionState.STOPPED:
                return _deliver_result(False, "blocked_live_session", task_id,
                                       latest.get("run_id"), None, attempts_used, max_attempts, steps,
                                       f"previous run's session {latest['session_id']} is {state.name}; "
                                       f"refusing a second GPU")
            steps.append({"step": "previous_session_stopped", "run_id": latest.get("run_id")})
        try:
            live = inspect()
        except Exception as e:  # noqa: BLE001 - live data is the gate input
            return _deliver_result(False, "live_unreadable", task_id,
                                   latest.get("run_id") if latest else None, None,
                                   attempts_used, max_attempts, steps,
                                   f"live usage/sessions unreadable; cannot bound a new attempt: {e}")
        if live.get("cu_balance") is None:
            return _deliver_result(False, "live_unreadable", task_id,
                                   latest.get("run_id") if latest else None, None,
                                   attempts_used, max_attempts, steps,
                                   "live CU balance unreadable; cannot bound a new attempt")
        # TASK-008.1: an idle machine reports rate 0 (or None) with no
        # active sessions — that is the normal idle state, not unreadable
        # data (production preflight observes exactly this: 0 sessions,
        # rate 0). Estimate the wall time at the conservative rate
        # ceiling --max-rate-cu-h (default 14.0 CU/h, the same worst-case
        # rate the pre-registration deadline uses), so the gate stays
        # conservative while the true burn rate is unknown. A positive
        # live rate is used as-is.
        rate = live.get("rate")
        if rate is None or float(rate or 0) <= 0:
            rate = float(getattr(args, "max_rate_cu_h", 14.0))
        ledger = _read_task_ledger(task_id)
        if ledger is None:
            ledger = _provisional_ledger(live["cu_balance"])
        try:
            wall = allowed_runtime_seconds(rate, live["cu_balance"], ledger)
        except Exception as e:  # noqa: BLE001 - BLOCKED / insufficient CU
            return _deliver_result(False, "budget_insufficient", task_id,
                                   latest.get("run_id") if latest else None, None,
                                   attempts_used, max_attempts, steps, str(e)[:300])
        if wall < MIN_ATTEMPT_WALL_SEC:
            return _deliver_result(False, "budget_insufficient", task_id,
                                   latest.get("run_id") if latest else None, None,
                                   attempts_used, max_attempts, steps,
                                   f"remaining budget grants only {wall}s of wall time; "
                                   f"a minimal attempt needs {MIN_ATTEMPT_WALL_SEC}s")
        effective_deadline = deadline or (ledger.get("deadline_utc") if isinstance(ledger, dict) else None)
        if effective_deadline:
            deadline_dt = datetime.datetime.strptime(
                effective_deadline, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
            now_dt = datetime.datetime.now(datetime.timezone.utc)
            if (deadline_dt - now_dt).total_seconds() < MIN_ATTEMPT_WALL_SEC + DEADLINE_MARGIN_SEC:
                return _deliver_result(False, "deadline_passed", task_id,
                                       latest.get("run_id") if latest else None, None,
                                       attempts_used, max_attempts, steps,
                                       f"less than {MIN_ATTEMPT_WALL_SEC + DEADLINE_MARGIN_SEC}s remain "
                                       f"before the task deadline {effective_deadline}")
        # Spec for the new attempt (resolved here, not upfront: reuse /
        # archive / resume paths need no spec at all).
        try:
            spec = resolve_run_spec(args)
        except Exception as e:  # noqa: BLE001 - a bad spec is deterministic
            return _deliver_result(False, "deterministic_error", task_id,
                                   latest.get("run_id") if latest else None, None,
                                   attempts_used, max_attempts, steps,
                                   f"cannot resolve a TaskSpec for a new attempt: {e}")
        initial_run_id = getattr(args, "run_id", "") or ""
        if initial_run_id and not initial_run_id_used:
            # First new attempt of this deliver call: the caller's
            # --run-id is used. If it already exists before the first
            # attempt starts, refuse — the earlier reuse / archive /
            # resume steps own an existing manifest, and silently
            # reusing the id would collide with it.
            run_id = initial_run_id
            from h3_run_manifest import manifest_exists

            if manifest_exists(ROOT, run_id):
                return _deliver_result(False, "deterministic_error", task_id, run_id, None,
                                       attempts_used, max_attempts, steps,
                                       f"--run-id {run_id} already exists; refusing a duplicate run")
            initial_run_id_used = True
        else:
            # Second and later attempts always derive a fresh id.
            run_id = _fresh_run_id()
        steps.append({"step": "new_attempt", "run_id": run_id,
                      "detail": f"attempt {attempts_used + 1}/{max_attempts}, wall {wall}s "
                                f"at {rate} CU/h"})
        try:
            result = _run_variant_inner(_deliver_attempt_args(args, run_id))
        except Exception as e:  # noqa: BLE001 - deterministic vs transient split
            if _is_deterministic_error(e):
                return _deliver_result(False, "deterministic_error", task_id, run_id, None,
                                       attempts_used + 1, max_attempts, steps,
                                       f"deterministic failure, refusing to retry: {e}")
            steps.append({"step": "attempt_failed_transient", "run_id": run_id,
                          "detail": f"{type(e).__name__}: {str(e)[:300]}"})
            continue
        steps.append({"step": "new_attempt_succeeded", "run_id": run_id})
        return _deliver_result(True, "new_attempt_succeeded", task_id, run_id,
                               (result or {}).get("delivery"), attempts_used + 1,
                               max_attempts, steps)


def _run_variant_inner(args) -> dict:
    global _EXEC_LOG_DIR, LEDGER_PATH, PRODUCTION_TASK_ID
    # TASK-007: one run is one TaskSpec. --task-spec loads it; --variant
    # A|B maps through the fixture adapter. The naming token for every
    # output file is spec.output_prefix (the old "variant" label).
    spec = resolve_run_spec(args)
    variant = spec.output_prefix
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
    # TASK-006 P0-B: the noninteractive download path is the default.
    # The mount path (interactive drivemount) remains only as an
    # explicit manual debug path; it keeps its old gates.
    drive_source = getattr(args, "drive_source", "download")
    if drive_source not in ("download", "mount"):
        raise ValueError("drive-source must be download or mount")
    if drive_source == "mount":
        if not args.drive_cache_dir.startswith("/content/drive/"):
            raise RuntimeError("Expected explicitly mounted Drive cache path under /content/drive")
        if not args.approve_interactive_drive_mount:
            raise RuntimeError("G4 blocked: --approve-interactive-drive-mount required")
    if not args.fresh_inference:
        raise RuntimeError("--fresh-inference required for a from-zero production run")
    # TASK-005 §8 (Issue #31): a run_id is the run's identity. A second
    # `run` with the same run_id would allocate a second paid GPU, re-run
    # inference, and collide with the first run's manifest stages (the
    # manifest is forward-only and terminal states refuse moves). Refuse
    # BEFORE any external call or ledger mutation; `status`/`resume` are
    # the paths to inspect or recover the existing run, and a retry uses
    # a fresh run_id. (A run that failed before allocation also needs a
    # fresh run_id — by design, since run_ids are cheap timestamps.)
    from h3_run_manifest import load_run_manifest, manifest_exists

    if manifest_exists(ROOT, args.run_id):
        try:
            _state = load_run_manifest(ROOT, args.run_id).get("task_state")
        except Exception:
            _state = "unknown"
        raise RuntimeError(
            f"run_id {args.run_id} already exists (state {_state}); refusing to start a second run "
            f"with the same run id — a duplicate run would allocate a second paid GPU and re-run "
            f"inference. Use a new run id, or 'status'/'resume' to inspect or recover the existing run.")
    validate_local(args.workflow_api, args.cache_manifest, spec)
    validate_comfy_workflow(args.workflow_api, spec)
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
                                       authorize_new_task=getattr(args, "authorize_new_task", False),
                                       deadline_utc=parse_task_deadline(
                                           getattr(args, "task_deadline_utc", "")))
    # Durable run manifest (TASK-005 §4.1): the task is authorised here,
    # so the persistent record starts now and survives a controller restart.
    _manifest_create(args, ledger, spec)
    # TASK-007: materialise the spec's reference images BEFORE any GPU
    # exists — a missing file or a SHA-256 mismatch refuses here, at
    # 0 CU, never after allocation. The bootstrap uploads these files.
    _ref_dir = RUNS / args.run_id
    _ref_dir.mkdir(parents=True, exist_ok=True)
    _refs = _materialize_ref_images(spec, _ref_dir)
    args._ref_image_1_path = _refs["ref_image_1"]
    args._ref_image_2_path = _refs["ref_image_2"]
    record({"event": "ref_images_materialised", "run_id": args.run_id,
            "ref_image_1": str(_refs["ref_image_1"]),
            "ref_image_2": str(_refs["ref_image_2"])})
    # Environment contract (TASK-005 §6, Issue #29): the pinned CLI
    # (0.7.5 + both production patches), ffmpeg/ffprobe and the allowed
    # Python versions must match the checked-in manifest BEFORE any live
    # session interaction or allocation. Nothing is allocated yet, so a
    # mismatch here is a clean refusal, not an orphan.
    verify_environment()
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
            # TASK-006 P1-B: name an allocating record explicitly — its
            # controller died mid-allocation, so the operator stops it
            # or resumes its run; it is never silently adopted.
            allocating = " [ALLOCATING — controller died mid-allocation]" \
                if d.get("allocating") else ""
            lines.append(
                f"{d.get('session_id')} (run {d.get('run_id')}, task {d.get('production_task_id')}, "
                f"deadline {d.get('deadline_utc')}, unbilled estimate "
                f"{est if est is not None else 'unconfirmable'} CU){allocating}")
        raise RuntimeError(
            "CRITICAL_ORPHAN_GPU_SESSION: managed GPU session(s) still active; refusing to allocate "
            "another GPU in parallel. Stop them first (never stop non-project sessions): "
            + "; ".join(lines))
    if drive_source == "mount":
        probe = run_cli(['drivemount', '--help'], timeout=20, check=False)
        if probe["returncode"] != 0:
            raise RuntimeError("Official Colab drivemount unavailable")
        if not sys.stdin.isatty():
            raise RuntimeError("Official drivemount requires interactive terminal; no TTY, abort BEFORE GPU allocation")
    else:
        # Noninteractive path: prove the download chain (mint + metadata
        # read with the minted token) BEFORE any paid GPU exists.
        verify_drive_download_preflight(args)

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
    watchdog_proc = None
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
        # TASK-006 P1-B: the pre-registration deadline uses the highest
        # max rate across GPU choices (most conservative = shortest wall
        # time). The session name is known locally before the CLI `new`
        # call, so the managed record and the lease are written BEFORE
        # the call — a controller death in the allocation window leaves
        # a record reconcile can resolve, not an invisible billing GPU.
        # The post-probe update replaces this deadline with the
        # budget-derived value at the actual live rate.
        pre_max_rate = max(c["max_rate_cu_h"] for c in GPU_CHOICES.values())
        pre_deadline = (datetime.datetime.now(datetime.timezone.utc)
                        + datetime.timedelta(
                            seconds=initial_session_wall_seconds(pre_max_rate)))
        for attempt in range(3):
            choice = choose_gpu_candidate(attempt)
            chosen_key = choice["key"]
            session = allocate_managed_session(choice, args.run_id)
            allocation_attempted = True
            record({"event": "allocation_attempt", "session": session, "gpu": chosen_key, "variant": variant, "run_id": args.run_id})
            # TASK-006 P1-B: pre-register the session as `allocating`
            # and the lease as non-final BEFORE the CLI `new` call.
            # A lease-registration failure here raises before any GPU
            # exists; the outer finally stops nothing (the session was
            # never created) and the run fails closed.
            from h3_session_watchdog import record_session

            record_session(session, chosen_key, args.run_id, pre_deadline.isoformat(),
                           production_task_id=args.production_task_id, status="allocating")
            _register_gpu_lease(session, chosen_key, args.run_id,
                                args.production_task_id, pre_deadline.isoformat(),
                                args.per_session_budget_cu,
                                max_wall_sec=args.max_wall_sec)
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
        # TASK-006 P1-B: confirm the pre-registered record. The CLI
        # returned, so the session is live and billable from here; the
        # record moves allocating -> active. A missing record means the
        # managed file lost the session — fail closed instead of
        # proceeding with an unregistered GPU.
        from h3_session_watchdog import confirm_session_active

        session_rec = confirm_session_active(session)
        if session_rec is None:
            raise RuntimeError(
                f"pre-registered session record for {session} vanished; "
                "refusing to proceed with an unregistered GPU")
        # Watchdog record + background watcher. The pre-registration
        # deadline (most conservative, from the highest max rate) stands
        # until the GPU probe below, when it is replaced with the
        # budget-derived value from allowed_runtime_seconds() using the
        # actual live rate. No fixed 3600 s cutoff anymore.
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
            live = _inspect_live(args.run_id)
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
            # TASK-006 P0-C: finalise the lease deadline for the backup
            # executor (fail-closed since TASK-007: a local write failure
            # raises into the outer finally, which stops the session;
            # only a Drive mirror failure degrades to a warning).
            _update_gpu_lease_deadline(session, new_deadline.isoformat())
            record({"event": "watchdog_deadline_updated", "session": session,
                    "budget_sec": budget_sec, "rate_cu_h": live["rate"],
                    "deadline_utc": new_deadline.isoformat()})
            # TASK-006 P1-B: the bootstrap sequence lives in
            # _run_bootstrap (durable per-step state, resumable).
            boot = _run_bootstrap(args, session, run_dir, budget_sec,
                                live["rate"], start)
            renewal_state = boot["renewal_state"]
            # Recoverable control plane (TASK-005 §4.2): short-start the
            # detached worker, then poll its durable state file. The old
            # single long blocking exec is gone: a controller restart no
            # longer loses the worker's progress, and `resume` can pick
            # the run back up from the manifest + state file.
            launch = launch_worker(session)
            record({"event": "worker_launched", "variant": variant, "session": session, "launch": launch})
            # TASK-006 P1-B: record the launch durably. Resume uses it
            # to tell "worker launched but unobservable" (refuse, never
            # double-launch) from "worker never launched" (resume the
            # bootstrap).
            _manifest_update(args.run_id, bootstrap={
                "steps": {"launch_worker": {"state": "done", "at_utc": _utc_now_iso()}}})
            try:
                worker_state = wait_for_worker(
                    session, args.run_id, variant,
                    deadline_monotonic=start + budget_sec,
                    poll_interval_sec=getattr(args, "poll_interval_sec", 60),
                    on_state=_make_worker_state_handler(
                        args.run_id, session, run_dir, renewal_state))
                done_info = check_job_done_info(worker_state.get("output"), variant, spec)
            except Exception:
                # Pull the server log before the session stops: it holds
                # the /prompt rejection detail the worker output may lack.
                download_remote_comfyui_log(session, run_dir)
                raise
            record({"event": "worker_done", "variant": variant, "session": session, "done": done_info})
            _manifest_update(args.run_id, task_state="INFERENCE_COMPLETED",
                             last_verified_stage="INFERENCE_COMPLETED",
                             artifacts={"delivery_state": "TEMPORARY"})
            # TASK-006 P1-A: record AFTER the TEMPORARY update, so a
            # worker upload is not clobbered back to TEMPORARY by it.
            # The tail re-verifies the recorded Drive id server-side
            # against the local MP4 before marking DRIVE_VERIFIED, so
            # a worker upload never skips the controller's independent
            # verification. Absent evidence means the controller-side
            # fallback delivers instead.
            _record_remote_upload(args.run_id, worker_state)
            record({"event": "delivery_state", "variant": variant, "run_id": args.run_id, "state": "INFERENCE_COMPLETED"})
            # Independent remote verification of the real MP4 BEFORE any
            # download: the file must exist and ffprobe must read the
            # spec's total frames at the spec fps and output size. Only
            # then is the download allowed; the local ffprobe validation
            # after download is a second, independent check before any
            # success report.
            output_check = exec_script(session, ROOT / "h3_output_check.py", timeout=180,
                                       env={"H3_VARIANT": variant,
                                            "H3_EXPECTED_FRAMES": str(spec.total_frames),
                                            "H3_EXPECTED_FPS": str(spec.fps),
                                            "H3_OUTPUT_WIDTH": str(spec.output_width),
                                            "H3_OUTPUT_HEIGHT": str(spec.output_height)})
            checked = check_remote_output(output_check, variant, spec)
            record({"event": "remote_output_checked", "variant": variant, "session": session, "checked": checked})
            record({"event": "delivery_state", "variant": variant, "run_id": args.run_id, "state": "REMOTE_MP4_VALIDATED"})
            # Download the submitted workflow the worker validated, for audit.
            try:
                run_cli(["download", "-s", session, REMOTE + "/workflow_submitted.json", str(run_dir / f"workflow_{variant}_submitted_remote.json")], timeout=120)
            except Exception:
                pass
            info = download_and_validate_variant(session, variant, args.run_id, spec)
            _manifest_update(args.run_id, task_state="VIDEO_VALIDATED",
                             last_verified_stage="VIDEO_VALIDATED",
                             artifacts={"local_sha256": info["sha256"],
                                        "local_frames": info["frame_count"],
                                        "delivery_state": "COMPLETE_LOCAL"})
            # The local MP4 is now on Muse-controlled persistent storage:
            # the artifact is durable before the GPU stop (TASK-005 §5).
            _manifest_update(args.run_id, task_state="ARTIFACT_DURABLE")
            record({"event": "version_downloaded", "variant": variant, "session": session, "sha256": info["sha256"], "frames": info["frame_count"]})
            record({"event": "delivery_state", "variant": variant, "run_id": args.run_id,
                    "state": "LOCAL_MP4_DOWNLOADED"})
            record({"event": "delivery_state", "variant": variant, "run_id": args.run_id,
                    "state": "LOCAL_MP4_VALIDATED"})
            live = _inspect_live(args.run_id)
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
            # TASK-006 P0-A: the watchdog is deliberately NOT terminated
            # here. It is terminated only after a confirmed stop (outer
            # finally); on an unconfirmed stop it keeps running so it can
            # enforce the deadline/hard-stop. Terminating it first would
            # remove the only protection while the session is still live.
            _EXEC_LOG_DIR = None
    finally:
        # Capture the propagating exception FIRST: sys.exc_info() must be
        # read before any call below (stop_and_verify_session catches its
        # own errors internally).
        _exc = sys.exc_info()
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
                    # Leave the watchdog alive: it still enforces the
                    # deadline/hard-stop for a session whose stop could
                    # not be confirmed. The managed record stays `active`
                    # (stop_and_verify_session marks it only on a
                    # confirmed STOPPED), so reconcile/resume keep seeing
                    # this session as a live, billable GPU.
                    record({"event": "watchdog_left_running",
                            "session": session, "run_id": args.run_id,
                            "production_task_id": args.production_task_id,
                            "stop_state": stop_result.get("state"),
                            "reason": "stop not confirmed; watchdog keeps deadline protection"})
            record({"event": "finished", "session": session, "variant": variant, "stop_verified": stop_ok,
                    "run_id": args.run_id, "production_task_id": args.production_task_id,
                    "stop_state": stop_result.get("state"),
                    "record_marked": stop_result.get("record_marked", False),
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
    # burns no CU. Bounded retries inside the idempotent deliver; on
    # failure the local MP4 is preserved and the error directs an
    # archive-only retry — never a re-run of the inference.
    try:
        delivery = deliver_variant_to_drive_idempotent(variant, production_result["run_id"], production_result["video"], spec=spec)
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
                                "drive_output_folder_id": delivery.get("drive_output_folder_id"),
                                "delivery_state": "DELIVERED"})
    _manifest_update(args.run_id, task_state="TASK_COMPLETED")
    return production_result


def test_or_run(args):
    if args.action == "check":
        print(json.dumps(inspect(), ensure_ascii=False, indent=2))
        return
    if args.action == "plan":
        spec = resolve_optional_spec(args)
        if spec is None:
            print(json.dumps({"goal": "A/B R2V", "gpu": "G4_THEN_A100_HIGHMEM", "poster_sha256": POSTER_SHA256,
                              "video_frames_per_variant": 274, "output": "1080x1920, 24fps",
                              "historic_remaining_cu": HIST_REMAIN, "historic_stop_balance": HARD_STOP,
                              "project_budget_cu": PROJECT_BUDGET_CU, "per_session_budget_cu": PER_SESSION_BUDGET_CU,
                              "no_gpu_created": True, "workflows": "Require API-format official R2V workflow and four-file server-verified manifest"}, ensure_ascii=False, indent=2))
            return
        print(json.dumps({"goal": "MiniMax-H3 generic task", "gpu": "G4_THEN_A100_HIGHMEM",
                          "task_id": spec.task_id, "output_prefix": spec.output_prefix,
                          "origin": spec.origin,
                          "ref_image_1_sha256": spec.ref_image_1.sha256,
                          "ref_image_2_sha256": spec.effective_ref_image_2.sha256,
                          "prompt_chars": len(spec.prompt),
                          "width": spec.width, "height": spec.height, "fps": spec.fps,
                          "animation_frames": spec.animation_frames,
                          "tail_card_frames": spec.tail_card_frames,
                          "total_frames": spec.total_frames,
                          "duration_sec": round(spec.duration_sec, 4),
                          "steps": spec.steps, "seed": spec.seed,
                          "output": f"{spec.output_width}x{spec.output_height}, {spec.fps}fps",
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
        # final video) and run the deliver helper, which re-proves the
        # file and uploads with bounded retries. Use after a run whose
        # local MP4 survived but whose Drive upload failed. The MP4 is
        # validated against the run's TaskSpec (from --task-spec, or the
        # A/B fixture adapter).
        spec = resolve_run_spec(args)
        if not args.run_id:
            raise ValueError("--run-id is required for archive")
        delivery = _archive_local_mp4(spec.output_prefix, args.run_id, spec)
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
    if args.action == "deliver":
        # TASK-008: task-level supervised delivery — reuse a delivered
        # run, recover an archive-only run, resume an in-flight run,
        # and only behind the gates start a controlled new attempt.
        # TASK-008.1: a failed delivery must not exit 0 — the caller
        # (script / operator) must see the failure. SystemExit is not
        # caught by main()'s `except Exception`, so no manifest is
        # rewritten on this path.
        result = deliver_task(args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if not result.get("delivered"):
            sys.exit(1)
        return result
    if args.action != "run":
        raise ValueError("Unknown action")
    if getattr(args, "auto_recover", False):
        # TASK-008: `run --auto-recover` is the deliver loop with the
        # same args (the --run-id, when given and fresh, is attempt 1).
        # TASK-008.1: same non-zero exit on a failed delivery as the
        # deliver action above.
        result = deliver_task(args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if not result.get("delivered"):
            sys.exit(1)
        return result
    result = run_variant(args)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def main():
    global _COLAB_AUTH, PRODUCTION_TASK_ID, LEDGER_PATH, PRIOR_TASK_UNBILLED_RESERVE_CU
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["plan", "check", "run", "preflight", "archive", "status",
                                            "resume", "stop", "reconcile", "deliver"])
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
    parser.add_argument("--task-deadline-utc", default="",
                        help="TASK-006 P1-C: the task deadline (UTC, YYYY-MM-DDTHH:MM:SSZ) "
                             "for the deadline-bound end-to-end metric. Recorded on the "
                             "task ledger at authorisation; a task without one is not in "
                             "that metric's denominator. A recorded deadline never moves.")
    parser.add_argument("--prior-unbilled-reserve-cu", type=float, default=PRIOR_TASK_UNBILLED_RESERVE_CU)
    parser.add_argument("--gpu-policy", default="G4_THEN_A100_HIGHMEM")
    parser.add_argument("--variant", choices=("A", "B"), default=None,
                        help="Legacy A/B fixture only: builds the equivalent "
                             "TaskSpec from the frozen poster contract. New "
                             "tasks must use --task-spec instead.")
    parser.add_argument("--task-spec", default=None,
                        help="Path to a generic task-spec JSON file "
                             "(see h3_task_spec.py). When given, --variant "
                             "must not be passed; the run is fully driven "
                             "by the spec and --production-task-id must "
                             "match the spec's task_id.")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--auto-recover", action="store_true",
                        help="TASK-008: run as the deliver supervisor instead of a "
                             "single attempt (same loop as the deliver action).")
    parser.add_argument("--max-task-attempts", type=int, default=DEFAULT_MAX_TASK_ATTEMPTS,
                        help="TASK-008: deliver / run --auto-recover: maximum GPU "
                             "attempts for the whole task (1..3, default 2).")
    parser.add_argument("--max-rate-cu-h", type=float, default=14.0)
    parser.add_argument("--max-wall-sec", type=int, default=MAX_SESSION_SECONDS)
    parser.add_argument("--poll-interval-sec", type=int, default=60,
                        help="Seconds between worker-state polls (TASK-005 §4.2).")
    parser.add_argument("--approve-interactive-drive-mount", action="store_true")
    parser.add_argument("--drive-source", choices=("download", "mount"), default="download",
                        help="TASK-006 P0-B: how the worker gets the models. "
                             "'download' (default) uses a short-lived read-only "
                             "Drive token + Range download, no drivemount. "
                             "'mount' is the legacy interactive drivemount path "
                             "(manual debug only; requires --approve-interactive-drive-mount and a TTY).")
    parser.add_argument("--fresh-inference", action="store_true")
    for flag in ("execute", "confirm-gpu", "authorize-task-gpu", "allow-g4", "allow-a100-highmem"):
        parser.add_argument("--" + flag, action="store_true")
    a = parser.parse_args()
    _COLAB_AUTH = a.colab_auth
    if a.action == "run" and not a.production_task_id:
        parser.error("--production-task-id is required for run; the historical default is never reused silently")
    # TASK-007: preflight/plan with --task-spec and no --production-task-id
    # take the task id from the spec file itself (the user authored it
    # there). run/archive still require --production-task-id on the CLI.
    if a.task_spec and not a.production_task_id and a.action in ("preflight", "plan"):
        a.production_task_id = load_task_spec(a.task_spec).task_id
    if a.production_task_id:
        PRODUCTION_TASK_ID = a.production_task_id
        LEDGER_PATH = ledger_path_for_task(ROOT, PRODUCTION_TASK_ID)
    PRIOR_TASK_UNBILLED_RESERVE_CU = float(a.prior_unbilled_reserve_cu)
    if a.action in ("run", "archive") and not a.variant and not a.task_spec:
        parser.error("--task-spec <spec.json> or --variant A|B is required for run/archive")
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
            # TASK-008: a run that already reached a durable terminal
            # state (COMPLETED_AWAITING_ARCHIVE / TASK_COMPLETED) is
            # never rewritten to FAILED by a late exception in the
            # archive tail — that rewrite produced a spurious ANOMALY
            # audit alert for a run whose video was already durable.
            # The same guard applies to `deliver`, whose run_id is the
            # base id of the attempt that just ran.
            if a.action in ("run", "deliver"):
                try:
                    from h3_run_manifest import load_run_manifest, update_run

                    current = (load_run_manifest(ROOT, a.run_id) or {}).get("task_state")
                    if current not in ("COMPLETED_AWAITING_ARCHIVE", "TASK_COMPLETED"):
                        update_run(ROOT, a.run_id, task_state="FAILED",
                                   last_error=f"{type(e).__name__}: {e}"[:500])
                except Exception:
                    pass
        print("ERROR:", str(e), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
