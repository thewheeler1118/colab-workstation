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

# Control-plane modules the controller needs in the interpreter that
# runs it (TASK-007.1): Drive service + token minting (google.*),
# the requests transport (requests/certifi), and the spec/config YAML
# loader (yaml). Recorded by the generator, gated by the verifier.
CONTROL_PLANE_MODULES = ("google.auth", "googleapiclient", "requests",
                         "certifi", "yaml")

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


def _probe_python(cli_exe: str | None) -> str | None:
    """The interpreter that owns the CLI (its venv's bin/python), else
    the interpreter running this code. The control-plane modules are
    checked in the interpreter that will actually run the controller —
    TASK-007.1: the system python3 has no google libs and the bare CLI
    venv had no Drive client or yaml, so probing the wrong interpreter
    hides exactly the drift the gate exists to catch."""
    if cli_exe:
        exe = Path(cli_exe)
        for name in ("python", "python3"):
            candidate = exe.parent / name
            if candidate.is_file():
                return str(candidate)
    return sys.executable or None


_CONTROL_PLANE_PROBE = (
    "import importlib.util, json, sys\n"
    "def _ok(m):\n"
    "    try:\n"
    "        return importlib.util.find_spec(m) is not None\n"
    "    except Exception:\n"
    "        return False\n"
    "print(json.dumps({m: _ok(m) for m in sys.argv[1:]}))\n")


def _control_plane_modules(python_exe: str | None) -> dict:
    """One subprocess: which control-plane modules the probe
    interpreter can import. A probe that cannot run records all False
    and the gate fails on it — never invented, never skipped."""
    if not python_exe:
        return {m: False for m in CONTROL_PLANE_MODULES}
    try:
        out = subprocess.run([python_exe, "-c", _CONTROL_PLANE_PROBE,
                              *CONTROL_PLANE_MODULES],
                             capture_output=True, text=True, timeout=60)
        result = json.loads((out.stdout or "").strip() or "{}")
        if isinstance(result, dict):
            return {m: bool(result.get(m, False))
                    for m in CONTROL_PLANE_MODULES}
    except Exception:
        pass
    return {m: False for m in CONTROL_PLANE_MODULES}


def resolve_source_commit(root=None) -> str:
    """The commit this code came from, without ever guessing
    (TASK-007.1, fix 3). Git checkout: the live HEAD is the truth.
    Synced production dir (no .git): the RELEASE_COMMIT file the sync
    wrote. Anything else: "" — callers treat "" as 'unknown', never
    as a version. The manifest itself is deliberately NOT a source:
    trusting it here would let a stale manifest prove itself."""
    base = Path(root) if root is not None else ROOT
    # Git first, unconditionally (like the old inline rev-parse): in a
    # non-repo dir it fails fast with empty stdout and we fall through.
    # Guarding on .git would skip the call entirely and break callers
    # that stub subprocess.run (see test_recoverable_controller._git).
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(base),
                             capture_output=True, text=True, timeout=10)
        head = (out.stdout or "").strip()
        if head:
            return head
    except Exception:
        pass
    try:
        release = base / "RELEASE_COMMIT"
        if release.is_file():
            text = release.read_text(encoding="utf-8").strip()
            if text:
                return text.split()[0]
    except Exception:
        pass
    return ""


def generate_environment_manifest(cli_exe: str | None = None,
                                  search_roots=None,
                                  source_commit: str | None = None) -> dict:
    """Record the live local environment. Never invents a value: a check
    that cannot run records None (or False for the patch markers) and the
    gate fails on it. The manifest is written by
    scripts/generate_environment_manifest.py. `search_roots` is a
    dependency-injection hook for tests; production always derives the
    roots from the CLI's own interpreter. `source_commit` records the
    source commit the manifest was generated against: pass it
    explicitly (--source-commit) or let the generator read the sync's
    RELEASE_COMMIT file. Git is deliberately NOT auto-read here — the
    manifest records the sync's claim, and in a git checkout the
    history is the record, so a pre-commit generation would otherwise
    bake a stale HEAD into the file."""
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
    recorded_commit = (source_commit or "").strip() or None
    if recorded_commit is None:
        try:
            release = ROOT / "RELEASE_COMMIT"
            if release.is_file():
                text = release.read_text(encoding="utf-8").strip()
                recorded_commit = text.split()[0] if text else None
        except Exception:
            recorded_commit = None
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
        "source_commit": recorded_commit,
        "control_plane_modules": _control_plane_modules(_probe_python(cli_exe)),
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
    # TASK-007.1: control-plane modules, probed in the interpreter that
    # will run the controller. A module recorded importable must stay
    # importable; a hand-removed package fails here, never at the first
    # Drive call after a GPU is allocated.
    expected_modules = manifest.get("control_plane_modules") or {}
    live_modules = live.get("control_plane_modules") or {}
    for module in CONTROL_PLANE_MODULES:
        expected = expected_modules.get(module)
        actual = live_modules.get(module)
        entry = {"field": f"control_plane_module.{module}",
                 "expected": expected, "actual": actual,
                 "match": expected == actual and expected is not None}
        report["checked"].append(entry)
        if not entry["match"]:
            raise EnvironmentMismatch(
                f"Environment mismatch on control_plane_module.{module}: "
                f"expected {expected!r}, got {actual!r}. Install the "
                f"control-plane dependencies with "
                f"scripts/install_colab_cli.sh, or regenerate the manifest "
                f"deliberately in a PR if the pinned environment changed.")
    # TASK-007.1: source commit. A manifest that records no commit
    # (legacy, or generated in a git checkout) passes vacuously; a
    # manifest that records one requires the live code to resolve to
    # the same commit, and fails closed when the live code cannot be
    # proven at all.
    expected_commit = manifest.get("source_commit")
    live_commit = resolve_source_commit()
    entry = {"field": "source_commit", "expected": expected_commit,
             "actual": live_commit,
             "match": expected_commit is None
             or (bool(live_commit) and expected_commit == live_commit)}
    report["checked"].append(entry)
    if not entry["match"]:
        raise EnvironmentMismatch(
            f"Environment mismatch on source_commit: manifest was "
            f"generated from {expected_commit!r} but the live code is "
            f"{live_commit!r}. Regenerate the manifest with the synced "
            f"commit (scripts/generate_environment_manifest.py "
            f"--source-commit <SHA>).")
    report["drive_transport"] = manifest.get("drive_transport")
    return report
