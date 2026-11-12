"""Cross-process resumable Google Drive v3 upload with private sidecar state.

Derived from official resumable-upload HTTP protocol. Never print the session
URI: it is a bearer-like capability. A VM replacement can resume so long as
its HOME, source file, ADC and unexpired session URI survive. 404/410 indicates
expiration; the caller must reconcile any already-completed server-side file.
"""
from __future__ import annotations
import hashlib,json,os,re,time
from pathlib import Path

UPLOAD_ENDPOINT='https://www.googleapis.com/upload/drive/v3/files'
CHUNK_SIZE=8*1024*1024  # multiple of 256 KiB
TRANSIENT={429,500,502,503,504}


def atomic_secret_json(path:Path,doc:dict):
    path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    os.chmod(path.parent,0o700)
    tmp=path.with_name('.'+path.name+'.tmp')
    fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f:
            json.dump(doc,f);f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        try:tmp.unlink()
        except FileNotFoundError:pass


def progress_offset(response,total:int):
    if response.status_code!=308:raise ValueError('expected 308')
    rng=response.headers.get('Range','')
    if not rng:return 0
    m=re.fullmatch(r'bytes=0-(\d+)',rng)
    if not m:raise RuntimeError('Invalid Drive resumable Range header')
    offset=int(m.group(1))+1
    if offset>total:raise RuntimeError('Drive offset beyond source length')
    return offset


def public_status(response):
    # Never echo URL or response text; both may contain sensitive material.
    return 'HTTP '+str(response.status_code)


def upload_with_persistent_session(auth_session,source:Path,name:str,parent_id:str,
                                   logical_rel:str,revision:str,sidecar:Path,sha256:str):
    """Returns Google Drive file metadata; after complete, caller verifies MD5/size.

    auth_session must be google.auth.transport.requests.AuthorizedSession with
    Drive OAuth. Use a stable sidecar path under $HOME across Muse VM restarts.
    """
    if not source.is_file():raise FileNotFoundError('Missing local HF source')
    total=source.stat().st_size
    if total<=0:raise ValueError('Empty HF source')
    if not re.fullmatch('[a-f0-9]{64}',sha256,re.I):raise ValueError('Missing source hash')
    expected={'source_size':total,'source_sha256':sha256,'name':name,
              'parent_id':parent_id,'logical_rel':logical_rel,'revision':revision}
    if sidecar.exists():
        saved=json.loads(sidecar.read_text(encoding='utf-8'))
        if any(saved.get(k)!=v for k,v in expected.items()):
            raise RuntimeError('Saved Drive upload session belongs to different source. Refusing reuse.')
        uri=saved.get('session_uri','')
        if not uri.startswith('https://www.googleapis.com/'):
            raise RuntimeError('Invalid saved Drive session URI')
    else:
        body={'name':name,'parents':[parent_id],
              'appProperties':{'logical_path':logical_rel,'hf_revision':revision}}
        r=auth_session.post(UPLOAD_ENDPOINT,
            params={'uploadType':'resumable','fields':'id,name,size,md5Checksum,parents'},
            headers={'X-Upload-Content-Type':'application/octet-stream',
                     'X-Upload-Content-Length':str(total),'Content-Type':'application/json; charset=UTF-8'},
            json=body,timeout=90,allow_redirects=False)
        if r.status_code not in (200,201):
            raise RuntimeError('Drive upload-session initiation failed: '+public_status(r))
        uri=r.headers.get('Location','')
        if not uri.startswith('https://www.googleapis.com/'):
            raise RuntimeError('Drive did not return a valid resumable session URI')
        atomic_secret_json(sidecar,{**expected,'session_uri':uri})

    def query():
        # Muse VM egress note (2026-11-10, verified live): the TLS-intercepting
        # egress proxy strips Content-Length: 0 from this empty status-query
        # PUT, so Google answers 411 for requests, httplib2 and curl alike.
        # The query is therefore advisory only: on any non-state answer (411,
        # other unexpected status, network error) fall back to offset 0 and
        # let the next chunk PUT's 308 Range correct the offset. Re-PUTting
        # an already-stored range is idempotent (verified live: 308 with the
        # stored Range, or 200 with the same file id when already complete).
        # 404/410 still mean the session itself is gone; keep that raise.
        try:
            r=auth_session.put(uri,headers={'Content-Range':f'bytes */{total}','Content-Length':'0'},
                               data=b'',timeout=90,allow_redirects=False)
        except Exception:
            return ('resume',0)
        if r.status_code in (200,201):return ('complete',r.json())
        if r.status_code==308:return ('resume',progress_offset(r,total))
        if r.status_code in (404,410):
            # A later retry can use a newly opened session after checking that
            # a committed file was not already created; don't auto-duplicate.
            sidecar.unlink(missing_ok=True)
            raise RuntimeError('Drive resumable session expired; re-run seed after server file reconciliation')
        return ('resume',0)

    kind,data=query()
    if kind=='complete':
        sidecar.unlink(missing_ok=True)
        return data
    offset=data
    attempts=0
    with source.open('rb') as f:
        while offset<total:
            f.seek(offset)
            chunk=f.read(min(CHUNK_SIZE,total-offset))
            if not chunk:raise RuntimeError('Premature source EOF')
            end=offset+len(chunk)-1
            try:
                r=auth_session.put(uri,headers={
                    'Content-Type':'application/octet-stream',
                    'Content-Length':str(len(chunk)),
                    'Content-Range':f'bytes {offset}-{end}/{total}'},
                    data=chunk,timeout=180,allow_redirects=False)
            except Exception:
                # Query the server because the chunk may have been received.
                attempts+=1
                if attempts>5:raise RuntimeError('Drive transport retries exhausted; resumable state saved') from None
                time.sleep(min(attempts*2,12))
                kind,data=query()
                if kind=='complete':
                    sidecar.unlink(missing_ok=True);return data
                offset=data
                continue
            if r.status_code in (200,201):
                sidecar.unlink(missing_ok=True)
                return r.json()
            if r.status_code==308:
                offset=progress_offset(r,total)
                attempts=0
                continue
            if r.status_code in TRANSIENT:
                attempts+=1
                if attempts>5:raise RuntimeError('Drive repeated transient errors; resumable state saved')
                time.sleep(min(attempts*2,12))
                kind,data=query()
                if kind=='complete':
                    sidecar.unlink(missing_ok=True);return data
                offset=data
                continue
            raise RuntimeError('Drive upload failed; resumable state saved: '+public_status(r))
    # If all bytes are acknowledged but a final response was lost, ask server.
    # The empty status query cannot confirm completion from this VM (see the
    # egress note in query()), so re-PUT the final chunk instead: the server
    # returns 200 with the file resource once the range is complete (verified
    # live: re-PUT after completion returns 200 with the same file id).
    final_start=(total-1)//CHUNK_SIZE*CHUNK_SIZE
    for _ in range(3):
        with source.open('rb') as f:
            f.seek(final_start)
            chunk=f.read(total-final_start)
        r=auth_session.put(uri,headers={'Content-Type':'application/octet-stream',
                                        'Content-Length':str(len(chunk)),
                                        'Content-Range':f'bytes {final_start}-{total-1}/{total}'},
                          data=chunk,timeout=180,allow_redirects=False)
        if r.status_code in (200,201):
            sidecar.unlink(missing_ok=True)
            return r.json()
        if r.status_code in (308,)+tuple(TRANSIENT):
            time.sleep(2)
            continue
        raise RuntimeError('Drive final upload completion not confirmed: '+public_status(r))
    raise RuntimeError('Drive final upload completion not confirmed')
