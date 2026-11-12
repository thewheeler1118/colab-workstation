"""Remote Colab single-variant H3 execution. Invoked by `colab exec -f`.
Requires a previously authorized/mounted Drive and a server-verified cache manifest.
Must NOT be considered production-verified until ComfyUI API workflow is exported/validated.
"""
from __future__ import annotations
import hashlib,json,os,re,shutil,subprocess,sys,tarfile,tempfile,time,urllib.request,urllib.error,uuid
from pathlib import Path

ROOT=Path('/content/muse_h3')
sys.path.insert(0,str(ROOT))
from h3_contract import MODEL_FILES,validate_model_cache_manifest,sha256_file,patch_api_workflow,WIDTH,HEIGHT,ANIMATION_FRAMES,FPS
from assemble import run as assemble_video,ffprobe

# ---- Worker stage tracking (user requirement 2026-11-11 09:12) ----
# Every phase prints a WORKER_STAGE marker, so the controller's persisted
# exec output always shows the last successful stage. On any failure the
# worker prints one structured WORKER_FAILED_JSON line with the last stage
# and the exception, then re-raises (the controller stops the GPU in its
# finally block). This is observability only: it does not claim the
# 2026-11-11 worker failure itself is fixed.
_LAST_STAGE = "init"


def set_stage(name: str) -> None:
    global _LAST_STAGE
    _LAST_STAGE = name
    print("WORKER_STAGE", name, flush=True)


def _sanitize_text(text):
    """Redact any embedded user:password in a URL before it reaches a log.
    Git commands here use a public URL and carry no credentials, but the
    rule is applied uniformly so a secret can never leak via a failure
    record."""
    if not text:
        return text
    return re.sub(r'(://[^/\s:]+:)[^/\s@]+@', r'\1***@', text)


# Detail of the most recent run_cmd failure, if any. Cleared at the start
# of every run_cmd call, so it only ever describes the failure that is
# currently propagating. worker_failure_info merges it into the structured
# WORKER_FAILED_JSON line, so a Git failure is recorded with its sanitized
# stdout/stderr, exit code, command stage and exception type in one place.
_LAST_CMD_FAILURE = None


def worker_failure_info(stage: str, exc: BaseException) -> dict:
    info = {"stage": stage, "error_type": type(exc).__name__, "error": str(exc)[:1500]}
    if _LAST_CMD_FAILURE:
        info["cmd_failure"] = _LAST_CMD_FAILURE
    return info

def require_inside(path:Path, root:Path):
    if not path.resolve().is_relative_to(root.resolve()):raise ValueError('unsafe path')

# 2026-11-11 (item 8): Drive -> Colab 40GB restore capacity detection.
# The four weights total 40,073,842,159 bytes. The restore copies them from
# the mounted Drive cache to local NVMe before inference, so a short disk or
# a slow copy can burn the whole session budget. Two gates, both fail-closed:
#  (a) disk gate before any copy (free >= total + 5 GiB reserve);
#  (b) throughput/cost projection after the first 256MB copied, using the
#      measured MB/s and the controller-supplied live rate + session budget.
# No throughput has ever been measured (the mount never succeeded), so the
# first real run measures it here; the local preflight only reports the
# requirement at an assumed 50-150 MB/s range, labelled as an assumption.
RESTORE_RESERVE_BYTES = 5 * 1024 ** 3

def estimate_restore(total_bytes, free_bytes=None, measured_mbps=None, rate_cu_h=None, budget_cu=None, reserve_bytes=RESTORE_RESERVE_BYTES):
    """Pure estimator. free_bytes=None skips the disk gate; measured_mbps=None
    skips the time/cost projection. Returns a dict with ok + reason."""
    total = int(total_bytes)
    required = total + int(reserve_bytes)
    result = {"total_bytes": total, "required_bytes": required, "free_bytes": free_bytes,
              "measured_mbps": measured_mbps, "eta_sec": None, "estimated_cost_cu": None,
              "ok": True, "reason": None}
    if free_bytes is not None and int(free_bytes) < required:
        result["ok"] = False
        result["reason"] = (f"insufficient disk for 40GB restore: need {required} bytes "
                            f"(models {total} + reserve {reserve_bytes}), have {int(free_bytes)} "
                            f"(shortfall {required - int(free_bytes)})")
        return result
    if measured_mbps:
        eta = total / (float(measured_mbps) * 1_000_000)
        result["eta_sec"] = round(eta, 1)
        if rate_cu_h:
            cost = eta / 3600.0 * float(rate_cu_h)
            result["estimated_cost_cu"] = round(cost, 3)
            if budget_cu is not None and cost > float(budget_cu):
                result["ok"] = False
                result["reason"] = (f"restore cost {cost:.2f} CU exceeds session budget "
                                    f"{float(budget_cu):.2f} CU at {float(measured_mbps):.1f} MB/s")
    return result

def check_restore_capacity(comfy, total_bytes, rate_cu_h=None, budget_cu=None):
    """Remote disk gate, run before the first copy. Raises on failure."""
    models = Path(comfy) / "models"
    models.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(models).free
    est = estimate_restore(total_bytes, free, rate_cu_h=rate_cu_h, budget_cu=budget_cu)
    print("RESTORE_CAPACITY", json.dumps(est), flush=True)
    if not est["ok"]:
        raise RuntimeError("Restore capacity check failed: " + str(est["reason"]))
    return est

def project_restore_throughput(total_bytes, copied_bytes, elapsed_sec, rate_cu_h=None, budget_cu=None):
    """Throughput/cost projection from the first measured chunk. Raises on failure."""
    mbps = (int(copied_bytes) / 1_000_000) / elapsed_sec if elapsed_sec and elapsed_sec > 0 else None
    est = estimate_restore(total_bytes, None, measured_mbps=mbps, rate_cu_h=rate_cu_h, budget_cu=budget_cu)
    print("RESTORE_PROJECTION", json.dumps(est), flush=True)
    if not est["ok"]:
        raise RuntimeError("Restore projection blocked: " + str(est["reason"]))
    return est

def run_cmd(args,timeout=1200,cwd=None):
    """Run a shell command, capturing its output. On failure the captured
    stdout/stderr are printed BEFORE re-raising: 2026-11-12 the git clone
    of ComfyUI failed with exit 128 and git's own fatal message was lost
    (inherited stdio was not captured by the exec channel), leaving only
    the exit code to diagnose from. The failure detail is also stashed in
    _LAST_CMD_FAILURE so worker_failure_info carries it in the structured
    WORKER_FAILED_JSON line (sanitized, truncated)."""
    global _LAST_CMD_FAILURE
    _LAST_CMD_FAILURE = None
    print('STAGE_CMD',args[0],flush=True)
    try:
        proc = subprocess.run(args,cwd=cwd,check=True,timeout=timeout,
                              stdin=subprocess.DEVNULL,capture_output=True,text=True)
        if proc.stdout:
            print(proc.stdout,flush=True)
        if proc.stderr:
            print(proc.stderr,flush=True)
        return proc
    except subprocess.CalledProcessError as e:
        _LAST_CMD_FAILURE = {
            "cmd": _sanitize_text(" ".join(str(a) for a in args)),
            "returncode": e.returncode,
            "stdout": _sanitize_text(e.stdout or "")[-2000:],
            "stderr": _sanitize_text(e.stderr or "")[-2000:],
        }
        print('CMD_FAILED',args[0],'exit',e.returncode,flush=True)
        if e.stdout:
            print(_sanitize_text(e.stdout),flush=True)
        if e.stderr:
            print(_sanitize_text(e.stderr),flush=True)
        raise


def run_cmd_with_retry(args,timeout=1200,cwd=None,attempts=2,delay_sec=20):
    """Bounded retry for one idempotent shell command (git clone/fetch).
    A single transient network failure must not kill a whole GPU session.
    Raises the last CalledProcessError after the final attempt."""
    last = None
    for attempt in range(1, attempts + 1):
        try:
            return run_cmd(args,timeout=timeout,cwd=cwd)
        except subprocess.CalledProcessError as e:
            last = e
            if attempt < attempts:
                print('CMD_RETRY',args[0],'attempt',attempt,'failed; retrying in',delay_sec,'s',flush=True)
                time.sleep(delay_sec)
    raise last

def _spec_satisfied(version: str, spec: str) -> bool:
    """Minimal PEP 440 subset (==, >=, <=, >, <, ~=, comma-separated).
    Returns False on anything unparsable: the caller treats that as
    'not satisfied' and pip runs, which is the previous behavior."""
    def _parts(v):
        out = []
        for piece in str(v).strip().split('.'):
            digits = re.match(r'^(\d+)', piece)
            if digits is None:
                return None
            out.append(int(digits.group(1)))
        return out
    ver = _parts(version)
    if ver is None:
        return False
    for clause in spec.split(','):
        clause = clause.strip()
        m = re.match(r'^(==|>=|<=|>|<|~=)\s*([0-9][0-9A-Za-z.]*)$', clause)
        if not m:
            return False
        op, want = m.group(1), _parts(m.group(2))
        if want is None:
            return False
        want_raw = list(want)
        want = want + [0] * (len(ver) - len(want)) if len(want) < len(ver) else want
        ver_p = ver + [0] * (len(want) - len(ver)) if len(ver) < len(want) else ver
        if op == '==' and not ver_p == want:
            return False
        if op == '>=' and not ver_p >= want:
            return False
        if op == '<=' and not ver_p <= want:
            return False
        if op == '>' and not ver_p > want:
            return False
        if op == '<' and not ver_p < want:
            return False
        if op == '~=':
            # ~= X.Y means >= X.Y and == X.* — the prefix is all but the
            # last component of the ORIGINAL spec, before padding.
            if not ver_p >= want:
                return False
            prefix = want_raw[:-1]
            if ver_p[:len(prefix)] != prefix:
                return False
    return True


def log_requirements_status(requirements_path):
    """Best-effort, no-network diagnostic: which requirements of the pinned
    ComfyUI tree are already satisfied in this runtime. Never blocks the
    install; on any doubt the entry is 'unknown' and pip runs as before.
    Returns a dict {satisfied, missing, unknown} (lists of requirement lines)."""
    from importlib import metadata as _md
    satisfied, missing, unknown = [], [], []
    try:
        for line in Path(requirements_path).read_text().splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            m = re.match(r'^([A-Za-z0-9_.\-]+)\s*(.*)$', line)
            if not m:
                unknown.append(line)
                continue
            name, spec = m.group(1), m.group(2).strip()
            try:
                installed = _md.version(name)
            except Exception:
                missing.append(line)
                continue
            if not spec or _spec_satisfied(installed, spec):
                satisfied.append(line)
            else:
                missing.append(line + ' (installed ' + installed + ')')
    except Exception as e:
        print('REQUIREMENTS_CHECK_UNAVAILABLE', type(e).__name__, str(e)[:200], flush=True)
        return {"satisfied": [], "missing": [], "unknown": ["check failed"]}
    status = {"satisfied": satisfied, "missing": missing, "unknown": unknown}
    print('REQUIREMENTS_STATUS', json.dumps(status, ensure_ascii=False), flush=True)
    return status


def pip_install_needed(requirements_path):
    """True unless the requirements check ran cleanly (no unknowns) and
    nothing is missing. Skipping pip then is equivalent to running it:
    pip would install nothing new. Any doubt -> run pip as before."""
    status = log_requirements_status(requirements_path)
    return bool(status["missing"] or status["unknown"])


def verify_comfyui_commit(comfy: Path, git_ref: str) -> bool:
    """True iff <comfy> is a git checkout whose HEAD is exactly git_ref."""
    try:
        proc = subprocess.run(['git', '-C', str(comfy), 'rev-parse', 'HEAD'],
                              capture_output=True, text=True, timeout=60)
    except Exception:
        return False
    return proc.returncode == 0 and proc.stdout.strip() == git_ref


def comfyui_commit(comfy: Path):
    """Commit id of an installed ComfyUI tree: from .git when present,
    else from the COMFYUI_COMMIT.txt marker written into a cache-restored
    tree (git archive carries no .git). None when neither is available."""
    if (comfy / '.git').exists():
        proc = subprocess.run(['git', '-C', str(comfy), 'rev-parse', 'HEAD'],
                              capture_output=True, text=True, timeout=60)
        if proc.returncode == 0:
            return proc.stdout.strip()
    marker = comfy / 'COMFYUI_COMMIT.txt'
    if marker.is_file():
        return marker.read_text().strip()
    return None


def clone_comfyui_atomic(comfy: Path, git_ref: str, repo_url='https://github.com/Comfy-Org/ComfyUI.git',
                         attempts=2, delay_sec=20):
    """Clone the pinned ComfyUI commit with an atomic install.

    2026-11-12 root-cause follow-up: the previous retry cloned straight
    into /content/ComfyUI, but git leaves a partial target directory
    behind when a clone fails, and a second `git clone` into a non-empty
    directory fails at once (exit 128) — so the retry could never work.
    Each attempt now clones into a fresh temp sibling directory, verifies
    the commit, and only then renames it into place (same filesystem, so
    the rename is atomic). A failed attempt's temp dir is removed.
    Returns 'present' when the target is already the pinned commit,
    'cloned' on a fresh install."""
    if (comfy / '.git').exists() and verify_comfyui_commit(comfy, git_ref):
        print('COMFYUI_ALREADY_PRESENT', git_ref, flush=True)
        return 'present'
    if not git_ref:
        raise RuntimeError('No pinned ComfyUI commit/ref in manifest; refuse moving main')
    parent = comfy.parent
    last_exc = None
    for attempt in range(1, attempts + 1):
        tmp = Path(tempfile.mkdtemp(prefix='.comfyui-install-', dir=str(parent)))
        try:
            run_cmd(['git', 'clone', repo_url, str(tmp)], timeout=600)
            run_cmd(['git', '-C', str(tmp), 'fetch', '--depth', '1', 'origin', git_ref], timeout=600)
            run_cmd(['git', '-C', str(tmp), 'checkout', '--detach', 'FETCH_HEAD'], timeout=120)
            if not verify_comfyui_commit(tmp, git_ref):
                raise RuntimeError('cloned ComfyUI HEAD does not match pinned commit ' + git_ref)
            if comfy.exists():
                shutil.rmtree(comfy, ignore_errors=True)  # partial/stale install
            os.rename(tmp, comfy)
            print('COMFYUI_CLONED', git_ref, flush=True)
            return 'cloned'
        except Exception as e:
            last_exc = e
            shutil.rmtree(tmp, ignore_errors=True)
            if attempt < attempts:
                print('COMFYUI_CLONE_RETRY', attempt, 'failed; retrying in', delay_sec, 's', flush=True)
                time.sleep(delay_sec)
    raise last_exc


def restore_comfyui_from_cache(cache_dir, comfy: Path, expected_sha256: str,
                               expected_commit: str, archive_name=None):
    """Restore the pinned ComfyUI source from the Drive source cache.
    Verifies the archive SHA-256, then the embedded commit id, then that
    main.py/requirements.txt exist; installs atomically. Raises
    RuntimeError with the reason on any mismatch — the caller logs
    COMFYUI_CACHE_FAILED and falls back to git clone. Returns 'restored'."""
    if not cache_dir:
        raise RuntimeError('no cache dir configured')
    archive_name = archive_name or ('comfyui-' + expected_commit + '.tar.gz')
    archive = Path(cache_dir) / archive_name
    if not archive.is_file():
        raise RuntimeError('cache archive missing: ' + archive_name)
    digest = hashlib.sha256()
    with archive.open('rb') as fh:
        for chunk in iter(lambda: fh.read(4 * 1024 * 1024), b''):
            digest.update(chunk)
    if digest.hexdigest() != expected_sha256:
        raise RuntimeError('cache archive SHA-256 mismatch')
    parent = comfy.parent
    tmp = Path(tempfile.mkdtemp(prefix='.comfyui-cache-', dir=str(parent)))
    try:
        with tarfile.open(archive, 'r:gz') as tf:
            tf.extractall(tmp, filter='data')
        extracted = tmp / 'comfyui'
        marker = extracted / 'COMFYUI_COMMIT.txt'
        if not marker.is_file():
            raise RuntimeError('cache archive has no COMFYUI_COMMIT.txt')
        if marker.read_text().strip() != expected_commit:
            raise RuntimeError('cache archive commit mismatch')
        if not (extracted / 'main.py').is_file() or not (extracted / 'requirements.txt').is_file():
            raise RuntimeError('cache archive missing main.py/requirements.txt')
        if comfy.exists():
            shutil.rmtree(comfy, ignore_errors=True)  # partial/stale install
        os.rename(extracted, comfy)
        print('COMFYUI_CACHE_RESTORED', expected_commit, flush=True)
        return 'restored'
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def get_json(url,timeout=10):
    with urllib.request.urlopen(url,timeout=timeout) as r:return json.loads(r.read().decode())

def post_json(url,data,timeout=30):
    req=urllib.request.Request(url,data=json.dumps(data).encode(),headers={'Content-Type':'application/json'})
    try:
        with urllib.request.urlopen(req,timeout=timeout) as r:return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        # The server's rejection reason lives in the response body (e.g.
        # /prompt 400 validation errors). Without it the failure is
        # undiagnosable: 2026-11-12 a bare "HTTP Error 400: Bad Request"
        # cost a GPU session to chase. Surface the body.
        try:body=e.read().decode(errors='replace')
        except Exception:body=''
        raise RuntimeError(f'POST {url} failed HTTP {e.code}: {body[:2000]}') from e

def ensure_comfy(comfy:Path,git_ref:str,cache=None):
    """Install the pinned ComfyUI source, then its Python dependencies.

    Order: (1) already-installed pinned commit -> reuse; (2) Drive source
    cache (archive SHA-256 + embedded commit id verified, atomic install)
    -> restore; (3) GitHub clone (atomic, bounded retry) -> clone. A
    cache failure is logged as COMFYUI_CACHE_FAILED with its reason and
    falls through to the clone; the original error stays on the record.
    Dependency install: a no-network requirements check first — when
    every requirement is already satisfied, pip is skipped (it would
    install nothing new); otherwise pip runs exactly as before. No
    Python/Torch/CUDA version is changed and the pinned commit is never
    moved."""
    if not git_ref:
        raise RuntimeError('No pinned ComfyUI commit/ref in manifest; refuse moving main')
    if (comfy/'.git').exists() and verify_comfyui_commit(comfy, git_ref):
        print('COMFYUI_ALREADY_PRESENT', git_ref, flush=True)
    else:
        restored = False
        if cache and cache.get('dir') and cache.get('sha256') and cache.get('commit'):
            try:
                restore_comfyui_from_cache(Path(cache['dir']), comfy,
                                           cache['sha256'], cache['commit'],
                                           cache.get('archive'))
                restored = True
            except Exception as e:
                # Keep the original error on the record; fall through to git.
                print('COMFYUI_CACHE_FAILED', type(e).__name__, str(e)[:500], flush=True)
        if not restored:
            clone_comfyui_atomic(comfy, git_ref)
    installed = comfyui_commit(comfy)
    if installed != git_ref:
        raise RuntimeError('installed ComfyUI commit ' + str(installed) + ' != pinned ' + git_ref)
    print('COMFY_REF', installed, flush=True)
    requirements = comfy / 'requirements.txt'
    if pip_install_needed(requirements):
        run_cmd([sys.executable,'-m','pip','install','-r',str(requirements)],timeout=1200)
    else:
        print('PIP_INSTALL_SKIPPED', 'all requirements already satisfied', flush=True)

def restore_weights(comfy:Path,cache:Path,manifest:dict,rate_cu_h=None,budget_cu=None):
    validate_model_cache_manifest(manifest)
    if not cache.is_dir():raise RuntimeError('Drive cache not mounted. OAuth/mount MUST be handled interactively before worker.')
    total=int(sum(int(x['size']) for x in manifest['files']))
    check_restore_capacity(comfy,total,rate_cu_h=rate_cu_h,budget_cu=budget_cu)
    projected=False
    for item in manifest['files']:
        relative=Path(item['path'])
        src=cache/relative
        require_inside(src,cache)
        if not src.is_file() or src.stat().st_size!=item['size']:
            raise RuntimeError('Drive cache absent/incomplete: '+str(src))
        dst=comfy/'models'/relative
        dst.parent.mkdir(parents=True,exist_ok=True)
        if dst.exists() and dst.stat().st_size==item['size'] and sha256_file(dst)==item['sha256']:
            print('CACHE_REUSE',relative,flush=True);continue
        tmp=dst.with_name(dst.name+'.part')
        try:
            with src.open('rb') as fi,tmp.open('wb') as fo:
                copied=0;copy_start=time.monotonic()
                while True:
                    chunk=fi.read(16*1024*1024)
                    if not chunk:break
                    fo.write(chunk);copied+=len(chunk)
                    if copied%(256*1024*1024)==0:
                        print('MODEL_COPY_PROGRESS',relative,copied,flush=True)
                        if not projected:
                            projected=True
                            project_restore_throughput(total,copied,time.monotonic()-copy_start,rate_cu_h=rate_cu_h,budget_cu=budget_cu)
            if tmp.stat().st_size!=item['size'] or sha256_file(tmp)!=item['sha256']:
                raise RuntimeError('Model checksum mismatch: '+str(relative))
            tmp.replace(dst)
        finally:
            if tmp.exists():tmp.unlink()
        print('CACHE_VERIFIED_NVME',relative,flush=True)

def wait_health(server, proc,deadline):
    while time.monotonic()<deadline:
        if proc.poll() is not None:raise RuntimeError('ComfyUI server exited '+str(proc.returncode))
        try:
            info=get_json(server+'/object_info',timeout=5)
            for typ in ('MiniMaxH3ReferenceToVideo','SaveVideo','UNETLoader','CLIPLoader'):
                if typ not in info:raise RuntimeError('Required Comfy node missing '+typ)
            return info
        except (urllib.error.URLError,TimeoutError,ConnectionError):pass
        time.sleep(4)
    raise TimeoutError('ComfyUI /object_info start timeout')

# Control-after-generate modes, serialized by the frontend as extra input
# keys on primitive nodes (e.g. PrimitiveInt {"value": 20, "fixed": "fixed"}).
# The live /object_info does NOT list them, and the server ignores unknown
# prompt keys (execution.py: validate_inputs only iterates schema inputs;
# get_input_data drops keys whose input_category is None), so the validator
# must tolerate them. Closed set from the pinned ComfyUI enum
# ControlAfterGenerate (comfy_api/latest/_io.py). 2026-11-12: the old
# check rejected the official template's nodes 143/144 for exactly this.
CONTROL_AFTER_GENERATE_MODES = {"fixed", "increment", "decrement", "randomize"}


def validate_api_against_object_info(api:dict,info:dict)->None:
    """Real /object_info schema check. Reject unknown class_types, missing
    required inputs, and a sampler chain not connected to SaveVideo.
    Static node-name presence alone is not acceptance.

    Autogrow (dynamic-group) inputs: the live schema lists the group name
    (e.g. "values", "ref_images") while the prompt addresses entries with
    dotted keys ("values.a", "ref_images.ref_image_0") — the server nests
    them into the group via dynamic_paths before execution. A required
    group is therefore satisfied by any dotted entry under it, and dotted
    keys whose root is a schema input are allowed. 2026-11-12: the old
    literal check rejected the official template's node 131
    (ComfyMathExpression, "values.a") and would have rejected node 136
    (MiniMaxH3ReferenceToVideo, "ref_images.ref_image_*"); the workflow
    was correct, the validator was not. The server remains the final
    authority on prompt validity."""
    if 'nodes' in api:
        raise ValueError('Expected ComfyUI API format, not UI-format workflow (nodes/links)')
    for key,node in api.items():
        typ=node.get('class_type') if isinstance(node,dict) else None
        if typ not in info:
            raise RuntimeError(f'ComfyUI /object_info has no node type {typ!r} for workflow node {key}')
        schema=(info[typ].get('input') or {})
        required=schema.get('required') or {}
        inputs=node.get('inputs') or {}
        for field in required:
            if field not in inputs and not any(
                    f.startswith(field + '.') for f in inputs):
                raise RuntimeError(f'Workflow node {key} ({typ}) missing required input {field!r}')
        # Unknown input fields are rejected: they indicate a stale template.
        # Tolerated: dotted autogrow entries (root is a schema input) and
        # the frontend-serialized control-after-generate mode keys.
        allowed=set(required) | set(schema.get('optional') or {})
        for field in inputs:
            if (field not in allowed and field.split('.', 1)[0] not in allowed
                    and field not in CONTROL_AFTER_GENERATE_MODES):
                raise RuntimeError(f'Workflow node {key} ({typ}) has unknown input {field!r} not in /object_info schema')
    # SaveVideo must be reachable: at least one node in the graph must feed it.
    # The API format links inputs by [node_id, slot]; check SaveVideo inputs reference other nodes.
    save_nodes=[n for n in api.values() if isinstance(n,dict) and n.get('class_type')=='SaveVideo']
    if not save_nodes:
        raise RuntimeError('No SaveVideo node in submitted workflow')
    for node in save_nodes:
        refs=[v for v in (node.get('inputs') or {}).values() if isinstance(v,list) and v and isinstance(v[0],(str,int))]
        if not refs:
            raise RuntimeError('SaveVideo node has no connected sampler/video input')

def execute_prompt(server,api:dict,timeout_sec:float):
    result=post_json(server+'/prompt',{'prompt':api,'client_id':'muse-h3-'+uuid.uuid4().hex})
    if result.get('node_errors') or not result.get('prompt_id'):
        raise RuntimeError('ComfyUI rejected workflow: '+json.dumps(result,ensure_ascii=False)[:1800])
    pid=result['prompt_id'];end=time.monotonic()+timeout_sec
    print('INFERENCE_SUBMITTED',pid,flush=True)
    while time.monotonic()<end:
        data=get_json(server+'/history/'+pid,timeout=10)
        if pid in data:
            event=data[pid]
            if event.get('status',{}).get('status_str')=='error':
                raise RuntimeError('ComfyUI inference error: '+str(event.get('status'))[:1500])
            if event.get('status',{}).get('messages'):
                for msg in event['status']['messages']:
                    if msg and msg[0] in ('execution_error','execution_interrupted'):
                        raise RuntimeError('ComfyUI execution error: '+str(msg)[:1500])
            outputs=event.get('outputs',{})
            # Only a completed (success) history entry with outputs counts.
            # HTTP 200 / non-empty history alone is not success.
            if outputs and event.get('status',{}).get('status_str')=='success':
                return outputs
        print('H3_INFERENCE_HEARTBEAT',int(end-time.monotonic()),flush=True)
        time.sleep(8)
    raise TimeoutError('H3 inference deadline exceeded')

VIDEO_SUFFIXES = ('.mp4', '.webm', '.mov')

# Raw animation the worker must locate: pinned production constants.
# The ffprobe gate below requires the container to decode fully
# (-count_frames), a video stream, exactly ANIMATION_FRAMES frames at FPS
# and WIDTH x HEIGHT. Those are the contract values assemble.py already
# enforces downstream; the gate fails fast here, with the observed value
# in the error, instead of letting a wrong file reach assembly. If a
# future live run shows ComfyUI writing a different container fps, the
# gate fails closed naming it — correct the constant with that evidence,
# never by weakening the gate to suffix+size.


def probe_animation(path):
    """ffprobe decode gate for a raw animation file (full decode via
    assemble.ffprobe -count_frames). Returns probe evidence or raises
    RuntimeError naming the mismatch. A non-video blob fails here: ffprobe
    cannot parse it, so there is no video stream to measure."""
    try:
        data = ffprobe(Path(path))
    except Exception as e:  # noqa: BLE001 - ffprobe errors are the verdict
        raise RuntimeError(f"ffprobe failed on {Path(path).name}: {type(e).__name__}: {str(e)[:200]}")
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise RuntimeError(f"No video stream in {Path(path).name}")
    frames = int(video.get("nb_read_frames") or video.get("nb_frames") or 0)
    rate = video.get("r_frame_rate", "0/1")
    num, den = (rate.split("/") + ["1"])[:2]
    fps = float(num) / float(den) if float(den) else 0.0
    info = {"path": str(path), "frames": frames, "fps": round(fps, 3),
            "width": int(video.get("width", 0)), "height": int(video.get("height", 0)),
            "codec": video.get("codec_name")}
    problems = []
    if frames != ANIMATION_FRAMES:
        problems.append(f"frames={frames}, expected {ANIMATION_FRAMES}")
    if abs(fps - FPS) > 0.5:
        problems.append(f"fps={fps}, expected {FPS}")
    if info["width"] != WIDTH or info["height"] != HEIGHT:
        problems.append(f"size={info['width']}x{info['height']}, expected {WIDTH}x{HEIGHT}")
    if problems:
        raise RuntimeError(f"Animation probe failed for {Path(path).name}: " + "; ".join(problems))
    return info


def _history_candidates(comfy, outputs, run_id="", variant=""):
    """Filenames named by /history outputs, in history order, de-duplicated.
    Entries without a filename can never name a file. When run_id/variant
    are given, only files whose name contains both are this run's — a
    previous run's file in the same output dir must never be picked up."""
    candidates = []
    for output in outputs.values():
        # SaveVideo's UI output in the pinned ComfyUI (0.44.0) is
        # PreviewVideo, whose as_dict() is {"images": [...], "animated":
        # (True,)} — NOT {"videos": [...]}. The old 'videos'/'gifs' keys
        # are kept for older formats; 'images' is read only when that
        # output entry has a truthy 'animated' value.
        keys = ['videos', 'gifs']
        if output.get('animated'):
            keys.append('images')
        for key in keys:
            for x in output.get(key, []) or []:
                name = x.get('filename')
                if not name:
                    continue
                if run_id and run_id not in name:
                    continue
                if variant and variant not in name:
                    continue
                p = comfy / 'output' / (x.get('subfolder') or '') / name
                if p not in candidates:
                    candidates.append(p)
    return candidates


def _validate_candidate(p, comfy, probe):
    """Suffix + size + ffprobe gates for one history candidate.
    Returns (probe_info, None) on pass or (None, reason) on failure."""
    try:
        require_inside(p, comfy / 'output')
    except ValueError:
        return None, 'path escapes ComfyUI/output'
    if not p.is_file():
        return None, 'not on disk'
    if p.suffix.lower() not in VIDEO_SUFFIXES:
        return None, f'unsupported suffix {p.suffix}'
    if p.stat().st_size <= 10000:
        return None, 'too small'
    try:
        return probe(p), None
    except RuntimeError as e:
        return None, str(e)[:300]


def find_rendered_video(comfy, outputs, run_id="", variant="", probe=None):
    """Locate this run's rendered animation from /history outputs.

    Priority: history file records (videos/gifs, plus images when animated
    is truthy). Every candidate must pass suffix, size and the ffprobe
    decode gate (probe_animation by default; inject a fake probe in tests
    to exercise selection logic without encoding video). Exactly one valid
    candidate wins; several valid candidates is AMBIGUITY, never "take the
    first" — a stale file from another run must not be silently used.
    Raises RuntimeError when nothing valid is found; the caller then runs
    rescue_rendered_video() before giving up.
    """
    probe = probe or probe_animation
    candidates = _history_candidates(comfy, outputs, run_id, variant)
    valid = []
    rejected = []
    for p in candidates:
        info, reason = _validate_candidate(p, comfy, probe)
        if info is None:
            rejected.append(f"{p.name}: {reason}")
        else:
            valid.append(p)
    if len(valid) == 1:
        return valid[0]
    if len(valid) > 1:
        raise RuntimeError('Ambiguous rendered video: multiple valid candidates for run '
                           + (run_id or '?') + ': ' + ', '.join(str(p) for p in valid))
    raise RuntimeError('ComfyUI history did not contain verifiable saved video; DO NOT assume successful render. '
                       'History output keys: ' + json.dumps({k: sorted(v.keys()) for k, v in outputs.items()}, ensure_ascii=False)[:800]
                       + ('; rejected: ' + '; '.join(rejected) if rejected else ''))


def rescue_rendered_video(comfy, run_id, variant, remaining_budget_seconds, probe=None):
    """Bounded rescue when history cannot locate the video (TASK-002 §4.4).

    Read-only scan of ComfyUI/output/ for files whose name contains this
    run's RUN_ID (and variant). Each name match still passes suffix, size
    and the ffprobe decode gate, and its SHA-256/path/size are recorded.
    Returns evidence dict for the single valid file. Raises when there is
    no valid file, when several valid files make ownership ambiguous, or
    when remaining_budget_seconds runs out — never to keep the GPU alive
    past the session budget for a rescue. The caller's outer exec timeout
    remains the hard bound; this is the inner one.
    """
    probe = probe or probe_animation
    if not run_id:
        raise RuntimeError('Rescue needs a run_id; refusing to guess which output belongs to this run')
    if remaining_budget_seconds is None or float(remaining_budget_seconds) <= 0:
        raise RuntimeError('Rescue time budget exhausted before scan')
    deadline = time.monotonic() + float(remaining_budget_seconds)
    out_root = comfy / 'output'
    if not out_root.is_dir():
        raise RuntimeError('Rescue: ComfyUI/output missing')
    matches = []
    for p in sorted(out_root.rglob('*'), key=lambda q: q.stat().st_mtime if q.is_file() else 0, reverse=True):
        if time.monotonic() > deadline:
            raise RuntimeError('Rescue time budget exhausted during scan')
        if not p.is_file() or p.suffix.lower() not in VIDEO_SUFFIXES:
            continue
        if run_id not in p.name or (variant and variant not in p.name):
            continue
        if p.stat().st_size <= 10000:
            continue
        matches.append(p)
        if len(matches) > 20:
            break
    valid = []
    for p in matches:
        if time.monotonic() > deadline:
            raise RuntimeError('Rescue time budget exhausted during probe')
        try:
            info = probe(p)
        except RuntimeError:
            continue
        valid.append((p, info))
    if len(valid) == 1:
        p, info = valid[0]
        return {"path": str(p), "size_bytes": p.stat().st_size, "sha256": sha256_file(p),
                "frames": info["frames"], "fps": info["fps"],
                "width": info["width"], "height": info["height"], "codec": info.get("codec")}
    if len(valid) > 1:
        raise RuntimeError('Rescue ambiguous: multiple valid videos for run ' + run_id + ': '
                           + ', '.join(str(p) for p, _ in valid))
    raise RuntimeError(f'Rescue found no verifiable video for run {run_id} variant {variant} '
                       f'(scanned {len(matches)} name-matching files)')

def main():
    task=json.loads((ROOT/'task.json').read_text())
    version=task['variant']
    run_id=task.get('run_id','')
    if version not in ('A','B'):raise ValueError('bad variant')
    manifest=json.loads((ROOT/'model_manifest.json').read_text())
    cache=Path(task['drive_cache_dir']);comfy=Path('/content/ComfyUI')
    if not (ROOT/'poster.jpg').is_file():raise FileNotFoundError('poster missing')
    server='http://127.0.0.1:8188'
    logfile=(ROOT/'comfyui.log').open('a')
    proc=None
    object_info=None
    api=None
    try:
        # The whole body is inside the try now: 2026-11-12 the worker died
        # in ensure_comfy (git clone exit 128) BEFORE the old try block,
        # so no WORKER_FAILED_JSON was printed and the stage had to be
        # inferred from the persisted traceback instead.
        set_stage('ensure_comfy')
        ensure_comfy(comfy,task['comfy_ref'],task.get('comfyui_cache'))
        set_stage('restore_weights')
        restore_weights(comfy,cache,manifest,rate_cu_h=task.get('rate_cu_h'),budget_cu=task.get('session_budget_cu'))
        (comfy/'input').mkdir(exist_ok=True)
        shutil.copy2(ROOT/'poster.jpg',comfy/'input'/'muse_original_poster.jpg')
        set_stage('comfyui_start')
        proc=subprocess.Popen([sys.executable,'main.py','--listen','127.0.0.1','--port','8188'],cwd=comfy,stdout=logfile,stderr=subprocess.STDOUT,start_new_session=True)
        set_stage('object_info_validation')
        object_info=wait_health(server,proc,time.monotonic()+180)
        info=object_info
        print('COMFYUI_HEALTH_OK',flush=True)
        raw=json.loads((ROOT/'workflow_api.json').read_text())
        api=patch_api_workflow(raw,version,'muse_original_poster.jpg',seed=42 if version=='A' else 43,run_id=run_id)
        validate_api_against_object_info(api,info)
        (ROOT/'workflow_submitted.json').write_text(json.dumps(api,ensure_ascii=False,indent=2))
        print('COMFY_OBJECT_INFO_VALIDATED',flush=True)
        set_stage('inference_submit')
        outputs=execute_prompt(server,api,timeout_sec=task['inference_timeout_sec'])
        set_stage('find_rendered_video')
        try:
            source = find_rendered_video(comfy, outputs, run_id, version)
        except RuntimeError:
            # History could not locate a verifiable video (wrong shape,
            # stale name, or failed probe). Bounded rescue scans
            # ComfyUI/output/ for this RUN_ID before the GPU is released;
            # it never uses another run's file and never extends the
            # session budget — the controller's exec timeout still bounds
            # the whole worker run.
            set_stage('rescue_rendered_video')
            rescued = rescue_rendered_video(comfy, run_id, version, remaining_budget_seconds=300)
            source = Path(rescued['path'])
            print('VIDEO_RESCUED_JSON:' + json.dumps(rescued, ensure_ascii=False), flush=True)
        set_stage('assemble_mp4')
        final=ROOT/'output'/f'shao_nian_h3_{version}.mp4'
        info=assemble_video(source,ROOT/'poster.jpg',final)
        print('MP4_WRITTEN',str(final),final.stat().st_size,info.get('frame_count'),flush=True)
        (ROOT/'output'/f'{version}.json').write_text(json.dumps(info,ensure_ascii=False,indent=2))
        set_stage('done')
        print('MUSE_H3_DONE_JSON:'+json.dumps(info,ensure_ascii=False),flush=True)
    except Exception as e:
        print('WORKER_FAILED_JSON:'+json.dumps(worker_failure_info(_LAST_STAGE,e),ensure_ascii=False),flush=True)
        # On a validation failure, persist the live schemas for the node
        # types in this workflow (compact, filtered): the next diagnosis
        # then runs offline instead of costing another GPU session.
        if _LAST_STAGE=='object_info_validation' and object_info and api:
            try:
                types={n.get('class_type') for n in api.values() if isinstance(n,dict)}
                filtered={t:object_info[t] for t in sorted(types) if t in object_info}
                print('OBJECT_INFO_WORKFLOW_JSON:'+json.dumps(filtered,ensure_ascii=False),flush=True)
            except Exception:
                pass
        raise
    finally:
        if proc is not None:
            proc.terminate()
            try:proc.wait(timeout=15)
            except subprocess.TimeoutExpired:proc.kill()
        logfile.close()
if __name__=='__main__':main()
