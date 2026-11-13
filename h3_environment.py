"""TASK-005 §6 (Issue #29): environment contract.

The production runs failed twice on environment drift: the only CLI on
the VM is the UNPATCHED official 0.7.5 (both production patches absent —
verified live 2026-11-13), and no CLI exists in the production PATH at
all. This module is the single source of truth for what environment the
controller expects, how to record it, and how to verify it before any
GPU is allocated.

Two parts:
- generate_environment_manifest(): records the LIVE local environment
  (CLI version + client hash + both patch markers, ffmpeg/ffprobe,
  Python) plus the remote expectations (ComfyUI commit, model HF
  revision, workflow SHA-256) into environment_manifest.json.
- verify_environment(): compares the LIVE environment against the
  checked-in manifest and raises EnvironmentMismatch naming the exact
  field, expected and actual values. Gated fields fail closed; the
  Drive transport is RECORDED, not gated (its fix is PR-7, and gating
  on it today would block runs the A run proved can succeed).

The CLI resolver is shared by construction: controller and watchdog
both import resolve_colab_executable from auth_support (see the
test in tests/test_environment_contract.py).
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENVIRONMENT_MANIFEST_PATH = ROOT / "environment_manifest.json"
GENERATOR_VERSION = 1

# Fields the gate enforces. Anything else in the manifest is recorded
# evidence, not a gate.
GATED_FIELDS = ("cli_version", "cli_client_sha256", "cli_post_body_patch",
                "cli_drivemount_timeout_patch",
                "ffmpeg_present", "ffprobe_present")
# Python is gated by MEMBERSHIP in an allowed list, not by exact match:
# the repo's CI matrix runs 3.11 and 3.12, and the manifest is generated
# on one interpreter. The allowed list is a deliberate policy decision,
# edited in a PR — the generator never narrows it to the current version.
PYTHON_ALLOWED_DEFAULT = ["3.11", "3.12"]


class EnvironmentMismatch(RuntimeError):
    """One gated environment field does not match the manifest. The
    message names the field, the expected value and the actual value —
    never a bare 'environment wrong'."""


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(4 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _tool_version(executable: str) -> str | None:
    """First line of `<tool> --version`, or None when the tool cannot run."""
    if not executable or not shutil.which(executable):
        return None
    try:
        out = subprocess.run([executable, "--version"], capture_output=True,
                             text=True, timeout=20)
    except Exception:
        return None
    text = (out.stdout or "") + (out.stderr or "")
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return None


def _cli_version(cli_exe: str) -> str | None:
    """`colab version` prints 'Version: 0.7.5'. Returns the version
    string, or None when the CLI cannot run."""
    try:
        out = subprocess.run([cli_exe, "version"], capture_output=True,
                             text=True, timeout=20)
    except Exception:
        return None
    text = (out.stdout or "") + (out.stderr or "")
    m = re.search(r"Version:\s*([0-9]+\.[0-9]+\.[0-9]+)", text)
    return m.group(1) if m else None


def _cli_site_packages(cli_exe: str) -> list[str]:
    """site-packages dirs of the interpreter that owns this CLI
    executable (the venv's bin/python next to bin/colab)."""
    exe = Path(cli_exe)
    python = exe.parent / "python"
    if not python.is_file():
        python = exe.parent / "python3"
    if not python.is_file():
        return []
    try:
        out = subprocess.run(
            [str(python), "-c", "import site, json; print(json.dumps(site.getsitepackages()))"],
            capture_output=True, text=True, timeout=20)
        return json.loads(out.stdout.strip() or "[]")
    except Exception:
        return []


def _patch_present(check_name: str, search_roots) -> bool:
    """One production patch marker, via the controller's own check
    (lazy import: the controller imports this module at module top)."""
    from h3_controller import cli_drivemount_timeout_patch_present
    from h3_controller import cli_post_body_patch_present

    checks = {"cli_post_body_patch": cli_post_body_patch_present,
              "cli_drivemount_timeout_patch": cli_drivemount_timeout_patch_present}
    return bool(checks[check_name](search_roots))


def generate_environment_manifest(cli_exe: str | None = None,
                                  search_roots=None) -> dict:
    """Record the live local environment. Never invents a value: a check
    that cannot run records None (or False for the patch markers) and the
    gate fails on it. The manifest is written by
    scripts/generate_environment_manifest.py. `search_roots` is a
    dependency-injection hook for tests; production always derives the
    roots from the CLI's own interpreter."""
    from auth_support import resolve_colab_executable

    if cli_exe is None:
        try:
            cli_exe = resolve_colab_executable()
        except FileNotFoundError:
            cli_exe = None
    # The allowed Python list is policy, not a measurement: preserve the
    # checked-in list when it exists, otherwise default to the CI matrix.
    allowed_python = list(PYTHON_ALLOWED_DEFAULT)
    try:
        existing = json.loads(ENVIRONMENT_MANIFEST_PATH.read_text(encoding="utf-8"))
        if isinstance(existing.get("python_major_minor_allowed"), list) \
                and existing["python_major_minor_allowed"]:
            allowed_python = existing["python_major_minor_allowed"]
    except Exception:
        pass
    manifest: dict = {
        "generator_version": GENERATOR_VERSION,
        "cli_version": None,
        "cli_executable": cli_exe,
        "cli_client_sha256": None,
        "cli_pristine_client_sha256": None,
        "cli_wheel_sha256": None,
        "cli_wheel_note": ("No wheel file is kept on this VM; the pin is "
                           "enforced by version + installed client hash + "
                           "both patch markers, not by a wheel hash."),
        "cli_post_body_patch": False,
        "cli_drivemount_timeout_patch": False,
        "python_major_minor": f"{sys.version_info.major}.{sys.version_info.minor}",
        "python_major_minor_allowed": allowed_python,
        "python_version": sys.version.split()[0],
        "ffmpeg_present": False,
        "ffmpeg_version": None,
        "ffprobe_present": False,
        "ffprobe_version": None,
        "drive_transport": _drive_transport_record(),
        "comfyui_commit": None,
        "model_hf_revision": None,
        "workflow_sha256": None,
    }
    pristine = ROOT / "tests/fixtures/pristine-colab-cli-0.7.5/colab_cli/client.py"
    if pristine.is_file():
        manifest["cli_pristine_client_sha256"] = _sha256_file(pristine)
    if cli_exe:
        manifest["cli_version"] = _cli_version(cli_exe)
        roots = search_roots if search_roots is not None else _cli_site_packages(cli_exe)
        client = None
        for base in roots:
            candidate = Path(base) / "colab_cli" / "client.py"
            if candidate.is_file():
                client = candidate
                break
        if client is not None:
            manifest["cli_client_sha256"] = _sha256_file(client)
        manifest["cli_post_body_patch"] = _patch_present("cli_post_body_patch", roots or None)
        manifest["cli_drivemount_timeout_patch"] = _patch_present(
            "cli_drivemount_timeout_patch", roots or None)
    ffmpeg_v = _tool_version("ffmpeg")
    manifest["ffmpeg_present"] = ffmpeg_v is not None
    manifest["ffmpeg_version"] = ffmpeg_v
    ffprobe_v = _tool_version("ffprobe")
    manifest["ffprobe_present"] = ffprobe_v is not None
    manifest["ffprobe_version"] = ffprobe_v
    try:
        from h3_controller import COMFY_REF_DEFAULT

        manifest["comfyui_commit"] = COMFY_REF_DEFAULT
    except Exception:
        pass
    try:
        from h3_contract import MODEL_HF_REVISION

        manifest["model_hf_revision"] = MODEL_HF_REVISION
    except Exception:
        ready = ROOT / "model_cache/verified_manifest.json"
        try:
            manifest["model_hf_revision"] = json.loads(
                ready.read_text(encoding="utf-8")).get("hf_revision")
        except Exception:
            pass
    workflow = ROOT / "workflow_api.json"
    if workflow.is_file():
        manifest["workflow_sha256"] = _sha256_file(workflow)
    return manifest


def _drive_transport_record() -> dict:
    """The Drive transport the code actually uses, read from the module
    that implements it — never a hardcoded string. PR-7 replaced the
    httplib2 transport (KNOWN_BROKEN_ON_THIS_VM) with requests; the gate
    enforces the recorded transport value, so a silent revert to
    httplib2 fails closed instead of failing at the first Drive call."""
    try:
        from h3_drive_transport import DRIVE_TRANSPORT

        transport = DRIVE_TRANSPORT
    except Exception:
        transport = None
    return {"transport": transport,
            "status": ("requests (httplib2 replaced in PR-7, Issue #30; "
                       "live read-only verification 2026-11-13)")
            if transport == "requests" else "UNKNOWN — transport module missing",
            "fix": ("PR-7 (Issue #30): drive_service() now builds on "
                    "h3_drive_transport.RequestsHttp")}


def _live_environment(cli_exe: str | None = None, search_roots=None) -> dict:
    """The same fields generate_environment_manifest records, re-read
    live for the gate. Reuses the generator so the two can never drift
    apart in what they measure."""
    return generate_environment_manifest(cli_exe=cli_exe, search_roots=search_roots)


def verify_environment(manifest: dict | None = None,
                       manifest_path: Path | None = None,
                       cli_exe: str | None = None,
                       search_roots=None) -> dict:
    """Compare the live environment against the checked-in manifest.
    Raises EnvironmentMismatch on the FIRST gated mismatch (field,
    expected, actual all named). Returns a report dict on success.
    Python is gated by membership in the manifest's allowed list; the
    Drive transport is gated on its recorded value (PR-7 made it a real
    gate — the value is read from the transport module itself, so a
    revert fails closed here instead of at the first Drive call)."""
    if manifest is None:
        path = Path(manifest_path) if manifest_path else ENVIRONMENT_MANIFEST_PATH
        if not path.is_file():
            raise EnvironmentMismatch(
                f"Environment manifest missing: {path}; generate it with "
                f"scripts/generate_environment_manifest.py")
        manifest = json.loads(path.read_text(encoding="utf-8"))
    live = _live_environment(cli_exe=cli_exe, search_roots=search_roots)
    report = {"checked": [], "ok": True}
    for field in GATED_FIELDS:
        expected = manifest.get(field)
        actual = live.get(field)
        entry = {"field": field, "expected": expected, "actual": actual,
                 "match": expected == actual and expected is not None}
        report["checked"].append(entry)
        if not entry["match"]:
            raise EnvironmentMismatch(
                f"Environment mismatch on {field}: expected {expected!r}, "
                f"got {actual!r}. Reinstall the pinned environment with "
                f"scripts/install_colab_cli.sh, or regenerate the manifest "
                f"deliberately in a PR if the pinned environment changed.")
    allowed = manifest.get("python_major_minor_allowed") or []
    live_py = live.get("python_major_minor")
    entry = {"field": "python_major_minor", "expected": allowed,
             "actual": live_py, "match": live_py in allowed}
    report["checked"].append(entry)
    if not entry["match"]:
        raise EnvironmentMismatch(
            f"Environment mismatch on python_major_minor: live {live_py!r} "
            f"is not in the manifest's allowed list {allowed!r}. Run the "
            f"controller on an allowed Python, or widen the list "
            f"deliberately in a PR.")
    report["cli_executable"] = live.get("cli_executable")
    expected_transport = (manifest.get("drive_transport") or {}).get("transport")
    live_transport = (live.get("drive_transport") or {}).get("transport")
    entry = {"field": "drive_transport.transport",
             "expected": expected_transport, "actual": live_transport,
             "match": expected_transport == live_transport
             and expected_transport is not None}
    report["checked"].append(entry)
    if not entry["match"]:
        raise EnvironmentMismatch(
            f"Environment mismatch on drive_transport.transport: expected "
            f"{expected_transport!r}, got {live_transport!r}. The Drive "
            f"transport is pinned to requests (PR-7); a revert to httplib2 "
            f"fails on this VM. Regenerate the manifest deliberately in a "
            f"PR if the transport changes.")
    report["drive_transport"] = manifest.get("drive_transport")
    return report
