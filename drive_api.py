"""Google Drive v3 service-side verification and resumable upload helper.
Needs google-api-python-client + google-auth and user-supplied authorized credentials.
No OAuth token is read from the official Colab CLI keyring. Never log session URIs.
"""
from __future__ import annotations
import hashlib,json,time
from pathlib import Path

def file_digests(path:Path):
    sha=hashlib.sha256();md5=hashlib.md5(usedforsecurity=False)
    with path.open('rb') as fh:
        for chunk in iter(lambda:fh.read(4*1024*1024),b''):
            sha.update(chunk);md5.update(chunk)
    return {'size':path.stat().st_size,'sha256':sha.hexdigest(),'md5':md5.hexdigest()}

def get_file_evidence(service,file_id:str):
    """Returns SERVER metadata only. A 404 may indicate permissions rather than absence."""
    return service.files().get(fileId=file_id,fields='id,name,size,md5Checksum,sha256Checksum,mimeType,parents,trashed',supportsAllDrives=True).execute()

def verify_uploaded_file(service, file_id, local_file:Path):
    actual=file_digests(local_file)
    meta=get_file_evidence(service,file_id)
    if meta.get('trashed'):raise RuntimeError('File is trashed')
    if int(meta.get('size',-1))!=actual['size']:raise RuntimeError('Server size mismatch')
    if not meta.get('md5Checksum') or meta['md5Checksum'].lower()!=actual['md5']:
        raise RuntimeError('Drive server MD5 missing/mismatch')
    if meta.get('sha256Checksum') and meta['sha256Checksum'].lower()!=actual['sha256']:
        raise RuntimeError('Drive server SHA256 mismatch')
    return {'drive_file_id':meta['id'],'size':actual['size'],
            'sha256':actual['sha256'],'server_md5':meta['md5Checksum'],
            'server_sha256':meta.get('sha256Checksum'),'server_verified':True}

def resumable_upload(service, source:Path, parent_id:str, mime_type='application/octet-stream'):
    """Resumable-in-process upload; interruption between processes requires retry/recovery.
    The method DOES NOT claim cross-VM resume. Verify all files before publishing READY.
    """
    from googleapiclient.http import MediaFileUpload
    if not source.is_file() or source.stat().st_size==0:raise ValueError('Missing source file')
    if not parent_id:raise ValueError('Target Drive folder ID must be explicitly provided')
    media=MediaFileUpload(str(source),mimetype=mime_type,chunksize=8*1024*1024,resumable=True)
    req=service.files().create(body={'name':source.name,'parents':[parent_id]},media_body=media,
                               fields='id,name,size,md5Checksum,sha256Checksum',supportsAllDrives=True)
    response=None;retries=0
    while response is None:
        try:
            _,response=req.next_chunk(num_retries=3)
        except Exception:
            retries+=1
            if retries>4:raise
            time.sleep(min(2**retries,20))
    return verify_uploaded_file(service,response['id'],source)

def build_drive_service_from_adc():
    """ADC must be authorized separately by the user with appropriate Drive scopes.
    Caller MUST compare the effective Drive account with Colab OAuth account before use.

    Transport (TASK-005 PR-7): requests via h3_drive_transport. The previous
    build(credentials=...) form routed through httplib2, which fails on this
    VM with SSL WRONG_VERSION_NUMBER (see drive_depot.drive_service).
    """
    import google.auth
    from h3_drive_transport import build_requests_drive_service
    creds,_=google.auth.default(scopes=['https://www.googleapis.com/auth/drive.file'])
    return build_requests_drive_service(creds)
