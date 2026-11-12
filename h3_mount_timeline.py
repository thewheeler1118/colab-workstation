"""Structured mount timeline for the official `colab drivemount` step.

Pure functions only: no subprocess, no GPU, no network. The controller feeds
in (a) timestamped stdout lines forwarded from the drivemount subprocess and
(b) the CLI's own history events (~/.config/colab-cli/history/<session>.jsonl),
and gets back a phase-by-phase timeline with durations, outcome, and error
cause. Used by h3_controller.mount_drive_interactive and by Mock tests.

Phase model (all timestamps UTC ISO, nullable when unobserved):
  cell_started            automation event / "Mounting Google Drive..." line
  kernel_requested_auth   colab_request (dfs_ephemeral) — kernel asked for auth
  oauth_link_shown        drive_auth_needed / "REQUIRED: ... Authorization needed"
  auth_granted            "Authorizing VM..." line (operator pressed Enter after
                          granting access in the browser)
  credentials_propagated  drive_auth_success / "Credentials propagated..."
  mount_finished          automation_result event / subprocess exit

The remote drive.mount() timer runs from cell_started; the authorization wait
(kernel_requested_auth -> credentials_propagated) is the interval the
WebSocket credential hook spends blocked. Under the old 120s remote timeout
any auth wait over 120s failed the mount even when authorization itself
succeeded; the 600s patch (V3_DRIVEMOUNT_TIMEOUT_PATCH.md) is what makes a
slow ceremony survivable. The timeline records `would_have_failed_at_120s`
so a patched run carries its own evidence for the fix.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

REMOTE_TIMEOUT_MS_DEFAULT = 600000
OLD_REMOTE_TIMEOUT_MS = 120000


def history_path(session: str) -> Path:
    return Path(os.path.expanduser("~/.config/colab-cli/history")) / f"{session}.jsonl"


def read_history_events(session: str) -> list:
    """Read the CLI history file for a session. Missing/corrupt lines are skipped."""
    path = history_path(session)
    if not path.exists():
        return []
    events = []
    try:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        return []
    return events


def _parse_ts(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat() if dt else None


def _secs(a: datetime | None, b: datetime | None) -> float | None:
    if a is None or b is None:
        return None
    return round((b - a).total_seconds(), 1)


def parse_stdout_markers(lines: list) -> dict:
    """Extract phase timestamps from timestamped stdout lines.

    `lines` is a list of (iso_timestamp, text) pairs in arrival order.
    Authorization URIs are never stored; only the fact that one was shown.
    """
    marks: dict = {}
    for ts, text in lines:
        t = _parse_ts(ts)
        if t is None:
            continue
        if "Mounting Google Drive to" in text and "cell_started" not in marks:
            marks["cell_started"] = t
        if "REQUIRED: Google Drive Authorization needed" in text and "oauth_link_shown" not in marks:
            marks["oauth_link_shown"] = t
        if "Authorizing VM..." in text and "auth_granted" not in marks:
            marks["auth_granted"] = t
        if "Credentials propagated" in text and "credentials_propagated" not in marks:
            marks["credentials_propagated"] = t
        if ("ValueError: mount failed" in text or "mount failed" in text.lower()) and "mount_error" not in marks:
            marks["mount_error"] = t
            # Keep the error line short and secret-free (no URIs).
            marks["mount_error_text"] = re.sub(r"https?://\S+", "<redacted-url>", text.strip())[:300]
    return marks


def parse_history_events(events: list) -> dict:
    """Extract phase timestamps from CLI history JSONL events."""
    marks: dict = {}
    for ev in events:
        if not isinstance(ev, dict):
            continue
        t = _parse_ts(ev.get("timestamp"))
        if t is None:
            continue
        etype = ev.get("event_type")
        if etype == "automation" and ev.get("op") == "drivemount" and "cell_started" not in marks:
            marks["cell_started"] = t
        elif etype == "colab_request" and ev.get("type") == "dfs_ephemeral" and "kernel_requested_auth" not in marks:
            marks["kernel_requested_auth"] = t
        elif etype == "drive_auth_needed" and "oauth_link_shown" not in marks:
            marks["oauth_link_shown"] = t
        elif etype == "drive_auth_success" and "credentials_propagated" not in marks:
            marks["credentials_propagated"] = t
        elif etype == "automation_result" and ev.get("op") == "drivemount" and "mount_finished" not in marks:
            marks["mount_finished"] = t
            outputs = ev.get("outputs") or []
            for out in outputs:
                if isinstance(out, dict) and out.get("output_type") == "error":
                    marks["mount_error"] = t
                    marks["mount_error_text"] = f"{out.get('ename', 'Error')}: {out.get('evalue', '')}"[:300]
    return marks


def build_mount_timeline(
    session: str,
    run_id: str,
    stdout_lines: list,
    history_events: list,
    returncode: int | None,
    started_at: str | None,
    finished_at: str | None,
    remote_timeout_ms: int = REMOTE_TIMEOUT_MS_DEFAULT,
    timed_out: bool = False,
) -> dict:
    """Merge stdout markers (live) and history events (authoritative) into one timeline."""
    marks = parse_stdout_markers(stdout_lines)
    for key, value in parse_history_events(history_events).items():
        # History timestamps are authoritative; stdout fills gaps only.
        if key in ("mount_error_text",):
            marks.setdefault(key, value)
        else:
            marks[key] = value
    start = _parse_ts(started_at)
    finish = _parse_ts(finished_at)
    cell_started = marks.get("cell_started") or start
    mount_finished = marks.get("mount_finished") or finish
    auth_wait = _secs(marks.get("kernel_requested_auth") or marks.get("oauth_link_shown"),
                      marks.get("credentials_propagated"))
    total_wait = _secs(cell_started, marks.get("credentials_propagated"))
    if timed_out:
        outcome = "timeout"
        error = "Local drivemount subprocess timeout expired before the mount returned"
    elif returncode == 0 and "mount_error" not in marks:
        outcome = "mounted"
        error = None
    elif "mount_error" in marks:
        outcome = "mount_failed"
        error = marks.get("mount_error_text") or "Remote drive.mount() reported an error"
    elif returncode not in (None, 0):
        outcome = "mount_failed"
        error = f"drivemount subprocess exited with returncode {returncode}"
    else:
        outcome = "unknown"
        error = "Mount finished without a clear success or error signal"
    remote_timeout_sec = remote_timeout_ms / 1000.0
    return {
        "session": session,
        "run_id": run_id,
        "remote_timeout_ms": remote_timeout_ms,
        "cell_started_utc": _iso(cell_started),
        "kernel_requested_auth_utc": _iso(marks.get("kernel_requested_auth")),
        "oauth_link_shown_utc": _iso(marks.get("oauth_link_shown")),
        "auth_granted_utc": _iso(marks.get("auth_granted")),
        "credentials_propagated_utc": _iso(marks.get("credentials_propagated")),
        "mount_finished_utc": _iso(mount_finished),
        "phase_durations_sec": {
            "cell_to_link": _secs(cell_started, marks.get("oauth_link_shown")),
            "link_to_auth": _secs(marks.get("oauth_link_shown"), marks.get("auth_granted")),
            "auth_to_propagated": _secs(marks.get("auth_granted"), marks.get("credentials_propagated")),
            "propagated_to_finish": _secs(marks.get("credentials_propagated"), mount_finished),
            "auth_wait_total": total_wait,
            "ws_blocked_sec": auth_wait,
            "total": _secs(cell_started, mount_finished),
        },
        "auth_wait_within_remote_timeout": (total_wait is not None and total_wait <= remote_timeout_sec),
        "would_have_failed_at_120s": (total_wait is not None and total_wait > OLD_REMOTE_TIMEOUT_MS / 1000.0),
        "outcome": outcome,
        "error": error,
    }


def summarize_timeline(timeline: dict) -> str:
    """One-line human summary for the run report."""
    d = timeline.get("phase_durations_sec", {})
    total = d.get("total")
    wait = d.get("auth_wait_total")
    parts = [f"outcome={timeline.get('outcome')}"]
    if total is not None:
        parts.append(f"total={total}s")
    if wait is not None:
        parts.append(f"auth_wait={wait}s")
        parts.append("within_600s" if timeline.get("auth_wait_within_remote_timeout") else "OVER_600s")
        if timeline.get("would_have_failed_at_120s"):
            parts.append("would_have_failed_at_120s")
    if timeline.get("error"):
        parts.append(f"error={timeline['error'][:120]}")
    return "mount_timeline: " + ", ".join(parts)
