"""Persistent per-task 30 CU project budget for Muse MiniMax-H3 Ref2VA A/B production.

Fresh implementation from the user's direct 2026-11-11 instruction (section 5).
No code is copied from any flagged attachment. Historical constants
HIST_REMAIN=11.45 / HARD_STOP=153.82 are audit history only and are never
used as a gate here.

Budget rule (user correction 2026-11-11 09:12, supersedes the 08:25
"append 30 CU" reading): EVERY new A/B production task the user explicitly
starts gets its OWN independent 30.00 CU budget — never 60 CU, never a
cumulative sum across tasks. Each task has its own `production_task_id`
and its own ledger file
`model_cache/h3_production_budget_<production_task_id>.json`. A/B variants
and all failed retries inside one task share that task's 30 CU; only
another explicit user instruction starts the next task's ledger.

The pre-08:25 ledger (`model_cache/h3_production_budget_30cu.json`, spent
2.50 CU, 11 run_ids, including a now-void 30->60 CU amendment) is archived
read-only at `model_cache/archive/` and is never written again.

A new task's ledger is baselined on the live `colab usage` balance just
before that task's first GPU allocation. Costs of the PREVIOUS task that
have not posted yet do not count as new-task consumption, but they ARE
reserved: the ledger stores `prior_task_unbilled_reserve_cu` (estimated
from the previous task's last session lifetime vs what has posted), and
every gate uses the EFFECTIVE hard stop
(`hard_stop + prior reserve`), so the account floor is preserved even
when the old postings land late.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

PROJECT_BUDGET_CU = 30.0
PER_SESSION_BUDGET_CU = 25.0
CLEANUP_RESERVE_CU = 0.75
# Absolute safety ceiling for one session's wall time. The binding limit is
# the dynamic budget-derived value from allowed_runtime_seconds() (usable CU
# / live rate); this ceiling only prevents an unbounded run if the live rate
# is implausibly low. At the maximum allowed rate (14 CU/h) the
# budget-derived limit is (25 - 0.75) / 14 * 3600 = 6235 s; at 8.88 CU/h it
# is 9831 s. The old fixed 3600 s cap was removed by the user's 2026-11-11
# amendment: a session must not be cut off by a fixed clock while its 25 CU
# session budget still covers the work.
MAX_SESSION_SECONDS = 14400

# The A/B production task explicitly authorized by the user on 2026-11-11
# 08:25 UTC. This is a HISTORICAL task id, not a default for new runs:
# the production entry points must receive --production-task-id explicitly,
# and a later task gets a new id (and a new ledger) only when the user
# explicitly starts another production task (TASK-002 §5.2).
DEFAULT_PRODUCTION_TASK_ID = "h3-ab-20261111"


def ledger_path_for_task(root: Path, production_task_id: str) -> Path:
    """Ledger file for one production task. Never shared across tasks."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,60}", production_task_id or ""):
        raise ValueError(f"Invalid production_task_id: {production_task_id!r}")
    return Path(root) / "model_cache" / f"h3_production_budget_{production_task_id}.json"


def effective_hard_stop_cu(ledger: dict) -> float:
    """The balance line the gates actually enforce: the task hard stop
    (baseline minus the 30 CU task budget) PLUS the prior task's unbilled
    reserve, so late postings of the previous task cannot push the
    account below the floor the new task was baselined on."""
    return float(ledger["hard_stop_balance_cu"]) + max(0.0, float(ledger.get("prior_task_unbilled_reserve_cu", 0.0)))

GPU_CHOICES = {
    "G4": {
        "command": ["new", "-s", "{session}", "--gpu", "G4"],
        "min_vram_mib": 92160,
        "name_tokens": ("RTX PRO 6000", "BLACKWELL"),
        "max_rate_cu_h": 14.0,
    },
    "A100_HIGH_MEM": {
        "command": ["new", "-s", "{session}", "--gpu", "A100", "--high-mem"],
        "min_vram_mib": 76800,
        "name_tokens": ("A100",),
        "max_rate_cu_h": 14.0,
    },
}

# G4: at most 2 allocation attempts (attempt 0, retry 1), then A100 once.
GPU_ATTEMPT_PLAN = ("G4", "G4", "A100_HIGH_MEM")


def choose_gpu_candidate(attempt: int) -> dict:
    """Return the GPU choice for a 0-based allocation attempt.

    attempt 0 -> G4, 1 -> G4 retry, 2 -> A100_HIGH_MEM, >=3 -> no candidate.
    """
    if attempt < 0 or attempt >= len(GPU_ATTEMPT_PLAN):
        raise RuntimeError("GPU allocation attempts exhausted; stop, do not try another GPU")
    key = GPU_ATTEMPT_PLAN[attempt]
    return {"key": key, **GPU_CHOICES[key]}


def allocation_command(session: str, choice_key: str, *, allow_g4: bool, allow_a100_highmem: bool) -> list[str]:
    if choice_key not in GPU_CHOICES:
        raise RuntimeError(f"GPU_NOT_AUTHORIZED: {choice_key!r}")
    if choice_key == "G4" and not allow_g4:
        raise RuntimeError("G4 requires --allow-g4")
    if choice_key == "A100_HIGH_MEM" and not allow_a100_highmem:
        raise RuntimeError("A100 High-Mem requires --allow-a100-highmem")
    if not re.fullmatch(r"muse-h3-[a-z0-9-]{4,50}", session):
        raise ValueError("Unmanaged or invalid session identifier")
    return [part.format(session=session) for part in GPU_CHOICES[choice_key]["command"]]


def is_capacity_failure(message: str) -> bool:
    """Capacity-only classification. 401/403, budget, account, workflow or
    model errors must never trigger a GPU fallback."""
    text = (message or "").lower()
    if any(x in text for x in ("401", "403", "invalid credentials", "unauthorized", "permission denied", "forbidden")):
        return False
    return any(
        x in text
        for x in (
            "http 503", "status 503", "service unavailable",
            "http 412", "status 412", "allocation refused",
            "precondition failed", "capacity", "resource exhausted",
            "no available", "not available", "too many assignments",
        )
    )


def is_transient_transport_error(message: str) -> bool:
    """Egress-proxy transport failures, verified live 2026-11-11: the proxy
    intermittently drops/truncates the assign response (~1025 bytes of a
    ~1564-byte body, or closes the connection with no response). These are
    retryable for the SAME GPU only, never a reason to fall back to another
    GPU. 411 Length Required is included: with the POST-body patch it must
    not occur, so a 411 means the patch is not in effect."""
    text = (message or "").lower()
    if any(x in text for x in ("401", "403", "invalid credentials", "unauthorized", "permission denied", "forbidden")):
        return False
    return any(
        x in text
        for x in (
            "proxyerror", "remotedisconnected", "incompleteread",
            "chunkedencodingerror", "connection broken", "connection aborted",
            "max retries exceeded", "ssl eof", "ssleof", "length required",
            "http 411", "status 411",
        )
    )


def initial_session_wall_seconds(max_rate_cu_h: float) -> int:
    """Conservative wall-time ceiling at allocation time, when the live rate
    is not yet known: the per-session cap minus the cleanup reserve, at the
    GPU's maximum allowed rate. After the GPU probe the controller replaces
    the watchdog deadline with the budget-derived value from
    allowed_runtime_seconds() using the actual live rate."""
    if max_rate_cu_h is None or float(max_rate_cu_h) <= 0:
        raise RuntimeError("GPU max rate missing; cannot set session wall time")
    seconds = int((PER_SESSION_BUDGET_CU - CLEANUP_RESERVE_CU) / float(max_rate_cu_h) * 3600)
    return max(300, min(seconds, MAX_SESSION_SECONDS))


def estimate_unbilled_cu(started_at_utc, rate_cu_h, now: datetime.datetime | None = None) -> float:
    """Estimate CU accrued by a running session that the posted balance has
    not caught up with yet. 2026-11-11 audit: billing lags — after the first
    two GPU allocations the balance did not move, and the deduction only
    appeared after the third run. The estimate is elapsed_hours * live rate;
    it is a planning figure only and NEVER overwrites max_observed_spent_cu,
    which stays derived from the posted balance in reconcile_budget."""
    if started_at_utc is None or rate_cu_h is None:
        return 0.0
    try:
        started = datetime.datetime.fromisoformat(str(started_at_utc).replace("Z", "+00:00"))
        rate = float(rate_cu_h)
    except (ValueError, TypeError):
        return 0.0
    if rate <= 0:
        return 0.0
    now = now or datetime.datetime.now(datetime.timezone.utc)
    elapsed_h = max(0.0, (now - started).total_seconds() / 3600.0)
    return round(elapsed_h * rate, 4)


def effective_balance_cu(live_balance_cu: float, unbilled_cu: float = 0.0) -> float:
    """Posted balance minus the unbilled-session estimate. Gates must use
    this, not the raw posted balance, while a session is running."""
    return float(live_balance_cu) - max(0.0, float(unbilled_cu or 0.0))


def parse_usage_text(text: str) -> dict:
    """Parse `colab usage` text output. Returns balance / rate / sessions."""
    balance = None
    rate = None
    active = None
    m = re.search(r"Current balance:\s*([0-9]+(?:\.[0-9]+)?)", text or "")
    if m:
        balance = float(m.group(1))
    m = re.search(r"Usage rate:\s*([0-9]+(?:\.[0-9]+)?)", text or "")
    if m:
        rate = float(m.group(1))
    m = re.search(r"Active assignments:\s*([0-9]+)", text or "")
    if m:
        active = int(m.group(1))
    if balance is None:
        raise RuntimeError("Cannot parse CU balance from colab usage output")
    return {"balance": balance, "rate": rate, "active_assignments": active}


def _colab_exe() -> str:
    from auth_support import resolve_colab_executable

    return resolve_colab_executable()


def read_live_usage(colab_auth: str = "adc") -> dict:
    """Query the real live balance/rate via the official CLI. No historical price."""
    exe = _colab_exe()
    p = subprocess.run(
        [exe, f"--auth={colab_auth}", "usage"],
        capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
    )
    if p.returncode != 0:
        raise RuntimeError("colab usage failed: " + (p.stderr or p.stdout)[-500:])
    return parse_usage_text(p.stdout + "\n" + p.stderr)


def read_live_sessions(colab_auth: str = "adc") -> list:
    exe = _colab_exe()
    p = subprocess.run(
        [exe, f"--auth={colab_auth}", "sessions"],
        capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
    )
    text = p.stdout + "\n" + p.stderr
    # `colab sessions` prints "No active sessions" when empty; otherwise
    # session names look like muse-h3-... . Keep parsing conservative.
    if "No active sessions" in text:
        return []
    names = sorted(set(re.findall(r"muse-h3-[a-z0-9-]{4,50}", text)))
    # If the output format lists other names, fall back to non-empty lines
    # that are not headers. Callers treat a non-empty list as "sessions exist".
    return names


def _utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".h3_budget_", dir=str(path.parent))
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


def _validate_ledger(data: dict) -> dict:
    if data.get("schema_version") != 1:
        raise RuntimeError("Invalid budget ledger schema_version")
    if float(data.get("project_budget_cu", -1)) != PROJECT_BUDGET_CU:
        raise RuntimeError(f"Budget ledger project cap is not {PROJECT_BUDGET_CU:.2f} CU")
    if float(data.get("per_session_budget_cu", -1)) != PER_SESSION_BUDGET_CU:
        raise RuntimeError(f"Budget ledger per-session cap is not {PER_SESSION_BUDGET_CU:.2f} CU")
    if data.get("initial_balance_cu") is None or data.get("hard_stop_balance_cu") is None:
        raise RuntimeError("Budget ledger missing baseline/hard-stop")
    if abs(float(data["initial_balance_cu"]) - float(data["hard_stop_balance_cu"]) - PROJECT_BUDGET_CU) > 0.001:
        raise RuntimeError(f"Budget ledger hard-stop is not baseline minus {PROJECT_BUDGET_CU:.0f} CU")
    reserve = float(data.get("prior_task_unbilled_reserve_cu", 0.0))
    if reserve < 0:
        raise RuntimeError("Budget ledger prior-task unbilled reserve is negative")
    if "effective_hard_stop_balance_cu" in data:
        if abs(float(data["effective_hard_stop_balance_cu"]) - effective_hard_stop_cu(data)) > 0.001:
            raise RuntimeError("Budget ledger effective hard-stop is not hard-stop plus prior reserve")
    if float(data.get("max_observed_spent_cu", 0)) < 0 or float(data["max_observed_spent_cu"]) > PROJECT_BUDGET_CU + 0.001:
        raise RuntimeError("Budget ledger max_observed_spent_cu out of range")
    if data.get("deadline_utc") is not None:
        # TASK-006 P1-C: the task deadline (metric F) must be a
        # well-formed UTC timestamp when recorded.
        try:
            datetime.datetime.strptime(str(data["deadline_utc"]), "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            raise RuntimeError("Budget ledger deadline_utc is not a UTC timestamp (YYYY-MM-DDTHH:MM:SSZ)") from None
    return data


def parse_task_deadline(value: str | None) -> str | None:
    """Validate a --task-deadline-utc value (UTC, YYYY-MM-DDTHH:MM:SSZ).
    Returns the value unchanged, or None when empty. Raises ValueError
    on a malformed value — a deadline must never be silently repaired."""
    if value is None or str(value).strip() == "":
        return None
    text = str(value).strip()
    try:
        datetime.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        raise ValueError(
            f"task deadline {text!r} is not a UTC timestamp "
            "(expected YYYY-MM-DDTHH:MM:SSZ)") from None
    return text


def load_or_create_budget_ledger(path: Path, live_balance_cu: float, run_id: str,
                                 prior_unbilled_reserve_cu: float = 0.0,
                                 authorize_new_task: bool = False,
                                 deadline_utc: str | None = None) -> dict:
    """Establish the baseline exactly once per production task. An existing
    ledger is never reset, even for a new run_id or a new variant.
    `prior_unbilled_reserve_cu` is the previous task's unbilled estimate,
    fixed at creation time; it is stored on the ledger and enforced via
    the effective hard stop (see effective_hard_stop_cu).

    A missing ledger is created ONLY when authorize_new_task is true:
    a new task id on its own never grants a fresh 30 CU budget (TASK-002
    §5.2). The authorization is recorded on the created ledger.
    """
    path = Path(path)
    if not path.exists() and not authorize_new_task:
        task_id = path.stem.replace("h3_production_budget_", "", 1)
        raise RuntimeError(
            f"NEW_TASK_REQUIRES_AUTHORIZATION: no ledger for task {task_id!r}; "
            "a new 30 CU task needs explicit user authorization (--authorize-new-task). "
            "Refusing to grant a fresh budget silently.")
    if path.exists():
        data = _validate_ledger(json.loads(path.read_text(encoding="utf-8")))
        # Balance must not have increased (top-up / meter reset needs
        # independent reconciliation, never a silent re-baseline).
        if float(live_balance_cu) > float(data["initial_balance_cu"]) + 0.02:
            raise RuntimeError("CU balance increased above ledger baseline; reconcile before GPU")
        if abs(float(prior_unbilled_reserve_cu) - float(data.get("prior_task_unbilled_reserve_cu", 0.0))) > 0.001:
            raise RuntimeError("Prior-task unbilled reserve does not match the ledger; reconcile before GPU")
        # TASK-006 P1-C: a recorded deadline never moves silently. A
        # ledger with no deadline adopts the first one passed (that is
        # the first authorisation of a deadline, not a move); after
        # that, a different value refuses.
        if deadline_utc is not None:
            recorded = data.get("deadline_utc")
            if recorded is not None and recorded != deadline_utc:
                raise RuntimeError(
                    f"Task deadline mismatch: ledger records {recorded}, "
                    f"got {deadline_utc}; deadlines never move silently")
            if recorded is None:
                data["deadline_utc"] = deadline_utc
        if run_id not in data.get("run_ids", []):
            data.setdefault("run_ids", []).append(run_id)
        data["updated_at_utc"] = _utc_now()
        data["_ledger_path"] = str(path)
        _atomic_write_json(path, {k: v for k, v in data.items() if not k.startswith("_")})
        return data
    hard_stop = round(float(live_balance_cu) - PROJECT_BUDGET_CU, 2)
    data = {
        "schema_version": 1,
        "production_task_id": Path(path).stem.replace("h3_production_budget_", "", 1),
        "project_budget_cu": PROJECT_BUDGET_CU,
        "per_session_budget_cu": PER_SESSION_BUDGET_CU,
        "authorized": True,
        "authorization": "user-explicit --authorize-new-task",
        "authorized_at_utc": _utc_now(),
        # TASK-006 P1-C: the task deadline for the deadline-bound E2E
        # metric. None when the task was authorised without one; such a
        # task is not in the metric-F denominator.
        "deadline_utc": deadline_utc,
        "initial_balance_cu": float(live_balance_cu),
        "hard_stop_balance_cu": hard_stop,
        "prior_task_unbilled_reserve_cu": float(prior_unbilled_reserve_cu),
        "effective_hard_stop_balance_cu": round(hard_stop + float(prior_unbilled_reserve_cu), 2),
        "max_observed_spent_cu": 0.0,
        "run_ids": [run_id],
        "sessions": [],
        "updated_at_utc": _utc_now(),
    }
    _atomic_write_json(path, data)
    data["_ledger_path"] = str(path)
    return data


def reconcile_budget(ledger: dict, live_balance_cu: float, path: Path | None = None) -> dict:
    """Update max_observed_spent_cu from the live balance and persist it.
    The hard-stop gate uses the EFFECTIVE hard stop (hard stop + prior
    task's unbilled reserve), so late postings of the previous task cannot
    push the account below the floor. Note: max_observed_spent_cu is
    baseline minus live balance, so it may include up to the reserve
    amount of prior-task postings — it is a conservative upper bound on
    this task's spend, never an understatement."""
    target = Path(path) if path is not None else Path(ledger.get("_ledger_path", ""))
    if not target or not str(target):
        raise RuntimeError("Budget ledger path unknown; cannot persist reconciliation")
    spent = max(0.0, float(ledger["initial_balance_cu"]) - float(live_balance_cu))
    if float(live_balance_cu) < effective_hard_stop_cu(ledger) - 0.001:
        raise RuntimeError("Project CU effective hard-stop balance already crossed")
    ledger["max_observed_spent_cu"] = max(float(ledger.get("max_observed_spent_cu", 0.0)), spent)
    ledger["updated_at_utc"] = _utc_now()
    _atomic_write_json(target, {k: v for k, v in ledger.items() if not k.startswith("_")})
    return ledger


def remaining_project_cu(ledger: dict) -> float:
    return max(0.0, PROJECT_BUDGET_CU - float(ledger.get("max_observed_spent_cu", 0.0)))


def allowed_runtime_seconds(
    live_rate_cu_h: float,
    live_balance_cu: float,
    ledger: dict,
    session_spent_cu: float = 0.0,
    unbilled_cu: float = 0.0,
) -> int:
    """Conservative wall time for one session, including setup/mount/copy/
    inference/download/cleanup. Raises BLOCKED-style RuntimeError if unsafe.

    The value is derived dynamically from the live rate, the 25 CU
    per-session cap and the cleanup reserve (usable CU / rate), bounded
    above only by the explicit MAX_SESSION_SECONDS safety ceiling — there
    is no fixed 3600 s cutoff anymore (user amendment 2026-11-11).

    `unbilled_cu` is the estimated spend of the already-running session that
    the posted balance has not caught up with (see estimate_unbilled_cu).
    It tightens the live headroom (balance minus hard stop) the same way
    `session_spent_cu` tightens the per-session cap: both are separate
    constraints on the same unbilled spend, so the min() takes the binding
    one. The ledger baseline and hard stop are never rewritten here."""
    if live_rate_cu_h is None or float(live_rate_cu_h) <= 0:
        raise RuntimeError("Live GPU burn rate missing or not positive; release GPU")
    if live_balance_cu is None:
        raise RuntimeError("Live CU balance missing; release GPU")
    remaining_project = remaining_project_cu(ledger)
    remaining_session = max(0.0, PER_SESSION_BUDGET_CU - float(session_spent_cu))
    live_headroom = effective_balance_cu(live_balance_cu, unbilled_cu) - effective_hard_stop_cu(ledger)
    available = min(remaining_project, remaining_session, live_headroom)
    usable = available - CLEANUP_RESERVE_CU
    if usable <= 0:
        raise RuntimeError("BLOCKED: INSUFFICIENT_CU")
    seconds = int(usable / float(live_rate_cu_h) * 3600)
    seconds = min(seconds, MAX_SESSION_SECONDS)
    if seconds < 300:
        raise RuntimeError("Not enough paid time for safe GPU run")
    return seconds


def parse_gpu_probe_output(text: str, choice_key: str) -> dict:
    """Parse nvidia-smi CSV output from the remote probe.

    Accepts either a plain `name, memory_mib` line or a
    `H3_GPU_EVIDENCE: {"name": ..., "memory_total_mib": ...}` line.
    A100 40GB is rejected even with --high-mem.
    """
    if choice_key not in GPU_CHOICES:
        raise RuntimeError(f"GPU_NOT_AUTHORIZED: {choice_key!r}")
    choice = GPU_CHOICES[choice_key]
    name = None
    mem = None
    for line in (text or "").splitlines():
        if "H3_GPU_EVIDENCE:" in line:
            info = json.loads(line.split("H3_GPU_EVIDENCE:", 1)[1].strip())
            name, mem = info.get("name"), info.get("memory_total_mib")
            break
    if name is None:
        # Plain nvidia-smi csv: "RTX PRO 6000 Blackwell, 97808"
        for line in (text or "").splitlines():
            line = line.strip()
            if not line or "," not in line:
                continue
            left, right = line.rsplit(",", 1)
            try:
                mem = int(right.strip())
            except ValueError:
                continue
            name = left.strip()
            break
    if not isinstance(name, str) or not isinstance(mem, int):
        raise RuntimeError("Incomplete GPU hardware evidence")
    if not any(tok in name.upper() for tok in choice["name_tokens"]):
        raise RuntimeError(f"GPU hardware mismatch for {choice_key}: {name!r}")
    if mem < int(choice["min_vram_mib"]):
        raise RuntimeError(f"{choice_key} VRAM insufficient: {mem} MiB (minimum {choice['min_vram_mib']})")
    return {"gpu": choice_key, "name": name, "memory_total_mib": mem}
