"""Independent watchdog for managed Colab GPU sessions.

Fresh implementation from the user's direct 2026-11-11 instruction
(section 5.3). It runs in a separate process from h3_controller.py so a
controller crash / VM restart does not silently leave a GPU billing.

The watchdog only ever stops sessions recorded in its own managed-session
file. It never touches other Colab sessions.

State file: model_cache/h3_managed_sessions.json (mode 0600).
A VM restart cannot be guaranteed to keep this process alive; that limit
is recorded in the run report as a risk, never claimed as an absolute cap.

Independent shutdown: this watchdog is a local Muse VM process. No
independent shutdown executor is deployed or verified (see
docs/watchdog_independence.md). Status: INDEPENDENT_SHUTDOWN_NOT_GUARANTEED.
Never substitute a remote `kill -9` for a real GPU session stop.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from enum import Enum

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX platforms have no flock
    fcntl = None
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "model_cache" / "h3_managed_sessions.json"
# Per-task ledger (user budget rule 2026-11-11 09:12). The watch subcommand
# rebinds this from --production-task-id; the default is the current task.
from h3_budget import DEFAULT_PRODUCTION_TASK_ID, ledger_path_for_task, effective_hard_stop_cu

LEDGER_PATH = ledger_path_for_task(ROOT, DEFAULT_PRODUCTION_TASK_ID)


def _utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _atomic_write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".h3_watch_", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            json.dump(data, fp, sort_keys=True, indent=2)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp_name, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


# TASK-006 P0-A (Issue #42): the controller and the watchdog both mutate
# the same managed-session file. An atomic file replace alone does not
# make load-modify-save atomic across processes — two writers can load
# the same content and the last save silently drops the other's update.
# Every mutation below therefore holds this exclusive lock across the
# whole load-modify-save, on a `<path>.lock` sidecar.
_STATE_LOCK_TIMEOUT_SEC = 10.0


@contextlib.contextmanager
def _state_lock(path: Path):
    """Exclusive lock for one managed-session file. Bounded wait; raises
    TimeoutError instead of hanging forever. On platforms without fcntl
    the lock is a no-op (documented; the production VM and CI are POSIX)."""
    if fcntl is None:  # pragma: no cover - non-POSIX
        yield
        return
    lock_path = Path(str(path) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    deadline = time.monotonic() + _STATE_LOCK_TIMEOUT_SEC
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"timed out waiting for the managed-session lock {lock_path}")
                time.sleep(0.05)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _mutate_session(session_id: str, path: Path, mutate) -> tuple[dict | None, bool]:
    """Load-modify-save one managed record under the state lock. `mutate`
    receives the record dict and returns True when it changed it. A change
    bumps the record's `version` by exactly one and refreshes
    `last_updated_at_utc`. Returns (record, changed): record is None when
    no such record exists; changed is False when the record was found but
    `mutate` declined to change it."""
    with _state_lock(path):
        sessions = load_managed_sessions(path)
        for s in sessions:
            if s.get("session_id") == session_id:
                if not mutate(s):
                    return s, False
                s["version"] = int(s.get("version") or 0) + 1
                s["last_updated_at_utc"] = _utc_now()
                save_managed_sessions(sessions, path)
                return s, True
    return None, False


def load_managed_sessions(path: Path = STATE_PATH) -> list:
    if not Path(path).exists():
        return []
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        return data.get("sessions", [])
    return data


def save_managed_sessions(sessions: list, path: Path = STATE_PATH) -> None:
    _atomic_write_json(Path(path), {"sessions": sessions, "updated_at_utc": _utc_now()})


def record_session(session_id: str, gpu: str, run_id: str, deadline_utc: str, path: Path = STATE_PATH,
                   production_task_id: str = "", status: str = "active") -> dict:
    # Under the state lock: re-recording an existing id replaces the old
    # record but carries its version forward (+1), so versions stay
    # monotonic across re-records.
    # TASK-006 P1-B: `status` is "allocating" when the record is written
    # BEFORE the CLI `new` call (the session name is known locally, the
    # live session may not exist yet); confirm_session_active moves it
    # to "active" after the CLI returns. An "allocating" record is never
    # treated as stopped and never silently adopted.
    if status not in ("active", "allocating"):
        raise ValueError(f"record_session status must be 'active' or 'allocating', got {status!r}")
    with _state_lock(path):
        sessions = load_managed_sessions(path)
        old = next((s for s in sessions if s.get("session_id") == session_id), None)
        sessions = [s for s in sessions if s.get("session_id") != session_id]
        rec = {
            "session_id": session_id,
            "gpu": gpu,
            "run_id": run_id,
            "production_task_id": production_task_id,
            "started_at_utc": _utc_now(),
            "deadline_utc": deadline_utc,
            "status": status,
            "version": (int(old.get("version") or 0) + 1) if old else 1,
            "last_updated_at_utc": _utc_now(),
        }
        sessions.append(rec)
        save_managed_sessions(sessions, path)
    return rec


def confirm_session_active(session_id: str, path: Path = STATE_PATH) -> dict | None:
    """Move an `allocating` record to `active` after the CLI `new` call
    returned (TASK-006 P1-B). Returns the record, or None when not found
    or not in `allocating` state — a record that is already `active` is
    left alone (confirming twice is a no-op, not an error)."""
    def _confirm(s: dict) -> bool:
        if s.get("status") != "allocating":
            return False
        s["status"] = "active"
        return True

    rec, _changed = _mutate_session(session_id, path, _confirm)
    return rec


def mark_session_stopped(session_id: str, confirmed_state, path: Path = STATE_PATH) -> bool:
    """Mark a managed record stopped — ONLY on a confirmed STOPPED verdict
    from a real session query (TASK-006 P0-A, Issue #42).
    `confirmed_state` must be SessionState.STOPPED or the string
    "STOPPED"; anything else raises ValueError, so no caller can mark a
    record stopped on an ACTIVE / UNKNOWN / unverified verdict. The old
    unconditional call (mark after any stop attempt) is what let a still
    billing GPU disappear from the managed file.
    Returns True when the record was found (and is now stopped), False
    when no such record exists. Marking an already-stopped record is a
    no-op that returns True."""
    state_value = confirmed_state.value if isinstance(confirmed_state, SessionState) else str(confirmed_state)
    if state_value != SessionState.STOPPED.value:
        raise ValueError(
            f"mark_session_stopped requires a confirmed STOPPED state, got {state_value!r}; "
            "refusing to mark the session stopped")

    def _mark(s: dict) -> bool:
        if s.get("status") == "stopped":
            return False
        s["status"] = "stopped"
        s["stopped_at_utc"] = _utc_now()
        return True

    rec, _changed = _mutate_session(session_id, path, _mark)
    return rec is not None


def update_session_deadline(session_id: str, deadline_utc: str, path: Path = STATE_PATH) -> dict | None:
    """Replace a managed session's watchdog deadline (e.g. after the GPU
    probe, when the controller knows the actual live rate and the
    budget-derived wall time). The running watch process re-reads the
    record on every poll, so the new deadline takes effect without a
    restart. Returns the updated record, or None if not found or no
    longer active — a stopped record's deadline is never rewritten."""
    def _update(s: dict) -> bool:
        if s.get("status") != "active":
            return False
        s["deadline_utc"] = deadline_utc
        s["deadline_updated_at_utc"] = _utc_now()
        return True

    rec, changed = _mutate_session(session_id, path, _update)
    return rec if changed else None


def should_stop(record: dict, live_balance_cu, ledger: dict | None, now: datetime.datetime | None = None,
                rate_cu_h: float | None = None) -> tuple[bool, str]:
    """Stop decision for a managed session. With `rate_cu_h` given, the
    posted balance is reduced by the unbilled-session estimate
    (estimate_unbilled_cu) before the hard-stop comparison, because Colab
    billing lags the real burn (audit 2026-11-11). Without a rate the old
    posted-balance-only behaviour applies."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    try:
        deadline = datetime.datetime.fromisoformat(str(record.get("deadline_utc", "")).replace("Z", "+00:00"))
    except ValueError:
        return True, "unparseable deadline"
    if now >= deadline:
        return True, "deadline reached"
    if ledger is not None and live_balance_cu is not None:
        balance = float(live_balance_cu)
        if rate_cu_h is not None:
            from h3_budget import estimate_unbilled_cu, effective_balance_cu

            balance = effective_balance_cu(balance, estimate_unbilled_cu(record.get("started_at_utc"), rate_cu_h, now=now))
        if balance <= effective_hard_stop_cu(ledger) + 0.0:
            return True, "hard-stop balance reached"
    return False, ""


def _run_colab(args: list, colab_auth: str = "adc", timeout: int = 65):
    from auth_support import resolve_colab_executable

    exe = resolve_colab_executable()
    p = subprocess.run(
        [exe, f"--auth={colab_auth}", *args],
        capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
    )
    return p


class SessionState(Enum):
    """Three-state answer to 'is this managed GPU session still there?'.

    TASK-005 §3 (Issue #24): the old boolean `session_still_active()`
    used substring matching and never checked the CLI exit code, so a
    failed `colab sessions` call (empty output) was misread as 'session
    gone' — a false STOPPED that could leave a GPU billing while the
    controller reported a confirmed stop. Every caller must handle
    UNKNOWN explicitly and fail closed.
    """

    ACTIVE = "ACTIVE"
    STOPPED = "STOPPED"
    UNKNOWN = "UNKNOWN"


class SessionQueryError(RuntimeError):
    """Raised when a managed session's state cannot be confirmed and the
    caller must fail closed (e.g. reconcile before a new allocation)."""


# Canonical `colab sessions` (official CLI 0.7.5) line, e.g.
#   [muse-h3-e76302ae4b] gpu-a100-hm-euw4a0-wnsvovi8whof | Hardware: A100 | Shape: High-RAM | Variant: GPU
# Kept local (not imported from reference_v2.colab_manager) so this
# watchdog stays self-contained as an independent safety process.
_SESSION_LINE_RE = re.compile(
    r"^\[(?P<name>[^\]]+)\]\s+(?P<endpoint>\S+)\s*\|\s*Hardware:\s*(?P<hw>[^|\s]+)"
)
_NO_SESSIONS_MARKERS = ("no active sessions", "no sessions found", "0 active sessions")


def _strip_cli_notice(text: str) -> str:
    """Drop the Colab CLI's intermittent update-check notice (same filter
    as h3_controller._strip_cli_notice; duplicated so the watchdog does
    not import the controller)."""
    return "\n".join(
        line for line in (text or "").splitlines()
        if "new version of Colab CLI" not in line
        and "colab update" not in line
        and "enable_update_check" not in line
    )


def _strict_session_names(text: str) -> list | None:
    """Parse `colab sessions` output strictly. Returns the session names
    from a JSON list or 0.7.5-format lines, or None when the output
    contains content lines in no recognised form (format change -> the
    caller must treat the state as UNKNOWN, never guess). An empty list
    means the server explicitly reported no active sessions."""
    cleaned = _strip_cli_notice(text).strip()
    if not cleaned:
        return None
    if cleaned.startswith("[") and cleaned.endswith("]"):
        try:
            items = json.loads(cleaned)
        except (ValueError, TypeError):
            return None
        if isinstance(items, list):
            names: list = []
            for item in items:
                if isinstance(item, dict):
                    sid = str(item.get("name") or item.get("session") or item.get("id") or "").strip()
                    if sid:
                        names.append(sid)
            return names
        return None
    names = []
    recognised = False
    for line in cleaned.splitlines():
        s = line.strip()
        if not s:
            continue
        low = s.lower()
        if any(m in low for m in _NO_SESSIONS_MARKERS):
            recognised = True
            continue
        if s.startswith("[colab]"):
            # Informational CLI lines, not sessions.
            recognised = True
            continue
        m = _SESSION_LINE_RE.match(s)
        if m:
            recognised = True
            names.append(m.group("name").strip())
            continue
        # Any other content line is an unrecognised format: do not guess.
        return None
    return names if recognised else None


def query_session_state(session_id: str, colab_auth: str = "adc") -> SessionState:
    """Three-state session query (TASK-005 §3, Issue #24).

    ACTIVE: a reliably parsed server response explicitly lists this exact
    session id (exact match, never substring).
    STOPPED: a reliably parsed response explicitly says there are no
    active sessions, or lists sessions without this id.
    UNKNOWN: CLI nonzero exit, auth failure, timeout, empty output,
    unparseable output, or an incompatible output format. Callers must
    fail closed on UNKNOWN — never treat it as STOPPED.
    """
    try:
        p = _run_colab(["sessions"], colab_auth=colab_auth)
    except Exception:  # noqa: BLE001 - timeout / missing CLI / spawn failure
        return SessionState.UNKNOWN
    if p.returncode != 0:
        return SessionState.UNKNOWN
    text = (p.stdout or "") + "\n" + (p.stderr or "")
    names = _strict_session_names(text)
    if names is None:
        return SessionState.UNKNOWN
    return SessionState.ACTIVE if session_id in names else SessionState.STOPPED


def stop_session(session_id: str, colab_auth: str = "adc", max_attempts: int = 3) -> dict:
    """Stop a managed session with a confirmed verdict (TASK-005 §3,
    Issue #25). Returns a structured result, never a bare bool:

    {"session", "state", "stop_cli_ok", "attempts", "queries", "error"}

    Contract:
    1. Call the official stop CLI and record its exit code.
    2. Re-query the real session state (three-state, Issue #24).
    3. Report success ONLY on explicit STOPPED.
    4. UNKNOWN -> bounded re-confirmation, then escalate as UNKNOWN
       (never as STOPPED).
    5. This function stops exactly the id it is given. Callers that must
       never touch a non-project session use stop_managed_session(),
       which refuses ids absent from the managed file.
    6. The caller must not clear the managed record unless state is
       STOPPED.
    """
    attempts = 0
    queries = 0
    stop_cli_ok = None
    last_error: str | None = None
    last_state = SessionState.UNKNOWN
    while attempts < max_attempts:
        attempts += 1
        try:
            p = _run_colab(["stop", "-s", session_id], colab_auth=colab_auth, timeout=65)
            stop_cli_ok = (p.returncode == 0)
            if not stop_cli_ok:
                last_error = (f"stop CLI exit {p.returncode}: "
                              f"{(p.stderr or p.stdout or '')[-300:]}")
        except Exception as e:  # noqa: BLE001 - timeout / missing CLI
            stop_cli_ok = False
            last_error = f"stop CLI failed: {e}"
        time.sleep(3)
        last_state = query_session_state(session_id, colab_auth=colab_auth)
        queries += 1
        if last_state is SessionState.STOPPED:
            return {"session": session_id, "state": "STOPPED", "stop_cli_ok": stop_cli_ok,
                    "attempts": attempts, "queries": queries, "error": None}
        if last_state is SessionState.UNKNOWN:
            last_error = "session state UNKNOWN after stop; cannot confirm"
            continue
        last_error = "session still ACTIVE after stop"
    return {"session": session_id, "state": last_state.value, "stop_cli_ok": stop_cli_ok,
            "attempts": attempts, "queries": queries, "error": last_error}


def stop_managed_session(session_id: str, colab_auth: str = "adc",
                         path: Path = STATE_PATH) -> dict:
    """Stop a session only if it is in our managed file. An id absent
    from the file (or already marked stopped) is refused without calling
    the CLI, so a non-project session is never stopped (TASK-005 §3)."""
    sessions = load_managed_sessions(path)
    rec = next((s for s in sessions if s.get("session_id") == session_id), None)
    if rec is None:
        return {"session": session_id, "state": "REFUSED_NOT_MANAGED", "stop_cli_ok": None,
                "attempts": 0, "queries": 0,
                "error": "session id is not in the managed-session file"}
    # TASK-006 P1-B: an `allocating` record may be stopped too — that is
    # an explicit operator decision (the `stop` action), not a silent
    # ALREADY_STOPPED refusal. A record already `stopped` (or failed)
    # still refuses: there is nothing live left to stop.
    if rec.get("status") not in ("active", "allocating"):
        return {"session": session_id, "state": "ALREADY_STOPPED", "stop_cli_ok": None,
                "attempts": 0, "queries": 0, "error": None}
    return stop_session(session_id, colab_auth=colab_auth)


def reconcile_on_startup(colab_auth: str = "adc", path: Path = STATE_PATH) -> dict:
    """Called before any new GPU allocation: verify EVERY managed record
    against the real session state. Never auto-stop a session that is not
    in our file. Each active session is reported with its run/task/deadline
    and a best-effort unbilled-cost estimate (None when the live rate cannot
    be read — the cost is then unconfirmable, but the session still blocks).
    A session whose state cannot be confirmed (UNKNOWN) raises
    SessionQueryError: an unconfirmable session state must fail closed,
    never silently allow a parallel GPU.

    TASK-006 P0-A (Issue #42): the managed file is checked against live
    reality in both directions, because the old unconditional mark could
    leave either mismatch behind:
    - record `stopped` but session still ACTIVE: a contradiction. Raises
      SessionQueryError and blocks allocation — the GPU may still be
      billing and must be stopped explicitly, never ignored.
    - record `active` but session already STOPPED: repaired by marking
      stopped, the live query being the proof; reported in
      `repaired_stale_active` (this is the mismatch the old resume paths
      left behind by never marking at all)."""
    sessions = load_managed_sessions(path)
    rate = None
    try:
        from h3_budget import read_live_usage

        live = read_live_usage(colab_auth)
        rate = live.get("rate")
    except Exception:  # noqa: BLE001 - rate is best-effort only
        rate = None
    still = []
    detail = []
    repaired = []
    failed_allocations = []
    allocating_live = []
    for s in sessions:
        state = query_session_state(s["session_id"], colab_auth=colab_auth)
        if state is SessionState.UNKNOWN:
            raise SessionQueryError(
                f"cannot confirm state of managed session {s['session_id']} "
                f"(run {s.get('run_id')}); failing closed: refusing to allocate a new GPU")
        if s.get("status") == "stopped":
            if state is SessionState.ACTIVE:
                raise SessionQueryError(
                    f"managed record for {s['session_id']} (run {s.get('run_id')}) says stopped "
                    "but the session is still ACTIVE; failing closed: refusing to allocate a "
                    "new GPU — stop that session explicitly first")
            continue
        # TASK-006 P1-B: an `allocating` record is a session whose CLI
        # `new` call may or may not have completed (the controller died
        # in the allocation window). Resolve it by live query, never by
        # assumption: live STOPPED means the allocation never happened
        # (mark it failed, it no longer blocks); live ACTIVE means a
        # billing GPU exists that no run owns yet (it blocks a new
        # allocation and is reported separately so the operator can
        # stop it explicitly or resume the run).
        if s.get("status") == "allocating":
            if state is SessionState.STOPPED:
                def _fail(rec):
                    if rec.get("status") != "allocating":
                        return False
                    rec["status"] = "allocation_failed"
                    return True

                _mutate_session(s["session_id"], path, _fail)
                failed_allocations.append(s["session_id"])
                continue
            allocating_live.append(s["session_id"])
            still.append(s["session_id"])
            detail.append({
                "session_id": s["session_id"],
                "run_id": s.get("run_id"),
                "production_task_id": s.get("production_task_id"),
                "deadline_utc": s.get("deadline_utc"),
                "started_at_utc": s.get("started_at_utc"),
                "unbilled_estimate_cu": None,
                "allocating": True,
            })
            continue
        if state is SessionState.STOPPED:
            mark_session_stopped(s["session_id"], SessionState.STOPPED, path=path)
            repaired.append(s["session_id"])
            continue
        still.append(s["session_id"])
        est = None
        if rate is not None:
            try:
                from h3_budget import estimate_unbilled_cu

                est = estimate_unbilled_cu(s.get("started_at_utc"), rate)
            except Exception:  # noqa: BLE001 - estimate is best-effort only
                est = None
        detail.append({
            "session_id": s["session_id"],
            "run_id": s.get("run_id"),
            "production_task_id": s.get("production_task_id"),
            "deadline_utc": s.get("deadline_utc"),
            "started_at_utc": s.get("started_at_utc"),
            "unbilled_estimate_cu": est,
        })
    return {"managed_active_sessions": still, "managed_active_detail": detail,
            "repaired_stale_active": repaired,
            "failed_allocations": failed_allocations,
            "allocating_live_sessions": allocating_live,
            "checked_at_utc": _utc_now()}


def watch(session_id: str, colab_auth: str = "adc", poll_sec: int = 60,
          production_task_id: str = "", ready_path: Path | None = None) -> int:
    """Poll until the managed session's deadline / hard-stop, then stop it.
    Exits 0 when the session is confirmed gone, 1 otherwise.

    Before polling, the watchdog proves it is ready and writes a
    readiness file (when `ready_path` is given): it has read the managed
    record, confirmed the deadline parses, is bound to the same
    production task id the controller asked for, and can resolve the
    Colab CLI that executes the stop. Without that proof the controller
    refuses to start inference (WATCHDOG_NOT_RUNNING)."""
    from h3_budget import read_live_usage

    sessions = load_managed_sessions()
    rec = next((s for s in sessions if s.get("session_id") == session_id), None)
    if rec is None:
        print(f"watchdog: no managed record for {session_id}", file=sys.stderr)
        return 1
    bound_task = production_task_id or rec.get("production_task_id", "")
    if rec.get("production_task_id") and production_task_id and rec["production_task_id"] != production_task_id:
        print(f"watchdog: task mismatch for {session_id}: record {rec['production_task_id']} != {production_task_id}",
              file=sys.stderr)
        return 1
    try:
        deadline = datetime.datetime.fromisoformat(str(rec.get("deadline_utc", "")).replace("Z", "+00:00"))
    except ValueError:
        print(f"watchdog: unparseable deadline for {session_id}: {rec.get('deadline_utc')!r}", file=sys.stderr)
        return 1
    try:
        from auth_support import resolve_colab_executable

        resolve_colab_executable()
    except Exception as e:  # noqa: BLE001 - no CLI means no stop authority
        print(f"watchdog: cannot resolve Colab CLI (no stop authority): {e}", file=sys.stderr)
        return 1
    if ready_path is not None:
        _atomic_write_json(Path(ready_path), {
            "session_id": session_id,
            "production_task_id": bound_task,
            "run_id": rec.get("run_id"),
            "deadline_utc": rec.get("deadline_utc"),
            "ready_at_utc": _utc_now(),
            "ledger_path": str(LEDGER_PATH),
        })
    ledger = None
    if LEDGER_PATH.exists():
        from h3_budget import _validate_ledger

        ledger = _validate_ledger(json.loads(LEDGER_PATH.read_text(encoding="utf-8")))
    while True:
        # Re-read the record on every poll: the controller may replace the
        # deadline after the GPU probe (budget-derived wall time at the
        # actual live rate), and the new deadline must take effect here.
        sessions = load_managed_sessions()
        rec = next((s for s in sessions if s.get("session_id") == session_id), None)
        if rec is None:
            print(f"watchdog: managed record for {session_id} disappeared", file=sys.stderr)
            return 1
        if rec.get("status") != "active":
            print(f"watchdog: {session_id} already {rec.get('status')}")
            return 0
        state = query_session_state(session_id, colab_auth=colab_auth)
        if state is SessionState.STOPPED:
            mark_session_stopped(session_id, SessionState.STOPPED)
            print(f"watchdog: {session_id} already gone")
            return 0
        if state is SessionState.UNKNOWN:
            # Transient CLI/network failure: do NOT mark the session
            # stopped. Fall through to the deadline / hard-stop check
            # below (record-local, needs no CLI); the deadline still
            # bounds this watch, and an unconfirmed stop escalates.
            print(f"watchdog: cannot confirm state of {session_id} (UNKNOWN); "
                  "continuing to poll; the deadline still bounds this watch", flush=True)
        try:
            live = read_live_usage(colab_auth)
            balance = live.get("balance")
            rate = live.get("rate")
        except Exception:
            balance = None
            rate = None
        stop, reason = should_stop(rec, balance, ledger, rate_cu_h=rate)
        if stop:
            print(f"watchdog: stopping {session_id}: {reason}", flush=True)
            result = stop_managed_session(session_id, colab_auth=colab_auth)
            if result["state"] == "STOPPED":
                mark_session_stopped(session_id, SessionState.STOPPED)
                return 0
            print(f"watchdog: CRITICAL could not confirm stop of {session_id}: "
                  f"{json.dumps(result, ensure_ascii=False)}", file=sys.stderr)
            return 1
        time.sleep(poll_sec)


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("record")
    p.add_argument("--session", required=True)
    p.add_argument("--gpu", required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument("--deadline-utc", required=True)
    p.add_argument("--production-task-id", default="")
    p = sub.add_parser("watch")
    p.add_argument("--session", required=True)
    p.add_argument("--poll-sec", type=int, default=60)
    p.add_argument("--production-task-id", default=DEFAULT_PRODUCTION_TASK_ID)
    p.add_argument("--ready-file", default=None,
                   help="Path the watchdog writes its readiness proof to once startup is confirmed.")
    p = sub.add_parser("reconcile")
    p.add_argument("--colab-auth", default="adc")
    p = sub.add_parser("stop")
    p.add_argument("--session", required=True)
    p.add_argument("--colab-auth", default="adc")
    a = parser.parse_args()
    if a.cmd == "record":
        print(json.dumps(record_session(a.session, a.gpu, a.run_id, a.deadline_utc,
                                        production_task_id=a.production_task_id), indent=2))
        return 0
    if a.cmd == "watch":
        global LEDGER_PATH
        LEDGER_PATH = ledger_path_for_task(ROOT, a.production_task_id)
        return watch(a.session, poll_sec=a.poll_sec, production_task_id=a.production_task_id,
                     ready_path=Path(a.ready_file) if a.ready_file else None)
    if a.cmd == "reconcile":
        print(json.dumps(reconcile_on_startup(a.colab_auth), indent=2))
        return 0
    if a.cmd == "stop":
        result = stop_managed_session(a.session, a.colab_auth)
        if result["state"] == "STOPPED":
            mark_session_stopped(a.session, SessionState.STOPPED)
        print(json.dumps(result, indent=2))
        return 0 if result["state"] == "STOPPED" else 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
