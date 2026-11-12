"""Independent watchdog for managed Colab GPU sessions.

Fresh implementation from the user's direct 2026-11-11 instruction
(section 5.3). It runs in a separate process from h3_controller.py so a
controller crash / VM restart does not silently leave a GPU billing.

The watchdog only ever stops sessions recorded in its own managed-session
file. It never touches other Colab sessions.

State file: model_cache/h3_managed_sessions.json (mode 0600).
A VM restart cannot be guaranteed to keep this process alive; that limit
is recorded in the run report as a risk, never claimed as an absolute cap.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys
import tempfile
import time
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
                   production_task_id: str = "") -> dict:
    sessions = load_managed_sessions(path)
    sessions = [s for s in sessions if s.get("session_id") != session_id]
    rec = {
        "session_id": session_id,
        "gpu": gpu,
        "run_id": run_id,
        "production_task_id": production_task_id,
        "started_at_utc": _utc_now(),
        "deadline_utc": deadline_utc,
        "status": "active",
    }
    sessions.append(rec)
    save_managed_sessions(sessions, path)
    return rec


def mark_session_stopped(session_id: str, path: Path = STATE_PATH) -> None:
    sessions = load_managed_sessions(path)
    for s in sessions:
        if s.get("session_id") == session_id:
            s["status"] = "stopped"
            s["stopped_at_utc"] = _utc_now()
    save_managed_sessions(sessions, path)


def update_session_deadline(session_id: str, deadline_utc: str, path: Path = STATE_PATH) -> dict | None:
    """Replace a managed session's watchdog deadline (e.g. after the GPU
    probe, when the controller knows the actual live rate and the
    budget-derived wall time). The running watch process re-reads the
    record on every poll, so the new deadline takes effect without a
    restart. Returns the updated record, or None if not found."""
    sessions = load_managed_sessions(path)
    updated = None
    for s in sessions:
        if s.get("session_id") == session_id:
            s["deadline_utc"] = deadline_utc
            s["deadline_updated_at_utc"] = _utc_now()
            updated = s
    save_managed_sessions(sessions, path)
    return updated


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


def session_still_active(session_id: str, colab_auth: str = "adc") -> bool:
    p = _run_colab(["sessions"], colab_auth=colab_auth)
    text = p.stdout + "\n" + p.stderr
    return session_id in text and "No active sessions" not in text


def stop_session(session_id: str, colab_auth: str = "adc") -> bool:
    for _ in range(3):
        _run_colab(["stop", "-s", session_id], colab_auth=colab_auth, timeout=65)
        time.sleep(3)
        if not session_still_active(session_id, colab_auth=colab_auth):
            return True
    return False


def reconcile_on_startup(colab_auth: str = "adc", path: Path = STATE_PATH) -> dict:
    """Called before any new GPU allocation: report managed sessions that
    are still active. Never auto-stop a session that is not in our file."""
    sessions = [s for s in load_managed_sessions(path) if s.get("status") == "active"]
    still = []
    for s in sessions:
        if session_still_active(s["session_id"], colab_auth=colab_auth):
            still.append(s["session_id"])
    return {"managed_active_sessions": still, "checked_at_utc": _utc_now()}


def watch(session_id: str, colab_auth: str = "adc", poll_sec: int = 60) -> int:
    """Poll until the managed session's deadline / hard-stop, then stop it.
    Exits 0 when the session is confirmed gone, 1 otherwise."""
    from h3_budget import read_live_usage

    sessions = load_managed_sessions()
    rec = next((s for s in sessions if s.get("session_id") == session_id), None)
    if rec is None:
        print(f"watchdog: no managed record for {session_id}", file=sys.stderr)
        return 1
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
        if not session_still_active(session_id, colab_auth=colab_auth):
            mark_session_stopped(session_id)
            print(f"watchdog: {session_id} already gone")
            return 0
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
            ok = stop_session(session_id, colab_auth=colab_auth)
            if ok:
                mark_session_stopped(session_id)
                return 0
            print(f"watchdog: CRITICAL could not stop {session_id}", file=sys.stderr)
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
        return watch(a.session, poll_sec=a.poll_sec)
    if a.cmd == "reconcile":
        print(json.dumps(reconcile_on_startup(a.colab_auth), indent=2))
        return 0
    if a.cmd == "stop":
        ok = stop_session(a.session, a.colab_auth)
        if ok:
            mark_session_stopped(a.session)
        print(json.dumps({"session": a.session, "stopped_confirmed": ok}, indent=2))
        return 0 if ok else 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
