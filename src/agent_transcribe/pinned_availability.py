"""Opt-in exact pinned availability; never invent an identity or hydrate a tree."""
import contextlib,contextvars,hashlib,json,os,stat,shutil,errno,uuid
from pathlib import Path
from .common import IntegrityError,ICMAT,json_bytes,run as run_command
_POLICY=contextvars.ContextVar('pinned_availability',default=None)
DEADLINE=120

def _path(value, *, strict=False):
    p=Path(value)
    if not p.is_absolute() or '..' in p.parts:raise IntegrityError('Availability path must be absolute without parent traversal')
    if not strict and _POLICY.get() is None:
        if p.is_symlink():raise IntegrityError('Symlink in availability path: '+str(p))
        p=p.resolve()  # No fetch policy: normalize system aliases after rejecting leaf links.
    for node in (p,*p.parents):
        if node.is_symlink():raise IntegrityError('Symlink in availability path: '+str(node))
    return p

def _identity(info):return info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns

def _read(path):
    digest=hashlib.sha256();count=0
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):
            count+=len(block);digest.update(block)
    return count,digest.hexdigest()

def _sync_dir(path):
    fd=os.open(path,os.O_RDONLY)
    try:os.fsync(fd)
    finally:os.close(fd)

def _write(path,value):
    with path.open('xb') as stream:
        stream.write(json_bytes(value));stream.flush();os.fsync(stream.fileno())
    _sync_dir(path.parent)

@contextlib.contextmanager
def availability_scope(policy):
    token=_POLICY.set(policy)
    try:yield
    finally:_POLICY.reset(token)

def current_policy():return _POLICY.get()
def set_worker_policy(policy):_POLICY.set(policy)

def policy_for(output,work,inputs,enabled,reserve_bytes=None):
    return dict(enabled=bool(enabled),output=str(_path(Path(output).absolute(),strict=enabled)),work=str(_path(Path(work).absolute(),strict=enabled)),inputs=[str(_path(Path(p).absolute(),strict=enabled)) for p in inputs],run_id=uuid.uuid4().hex,reserve_bytes=reserve_bytes)

def verify_pinned(path,records):
    """Return exact actual size/hash; canonical IntegrityError always stays fatal."""
    path=_path(Path(path).absolute());records=records if isinstance(records,list) else [records]
    if not records or any(not isinstance(r,dict) or type(r.get('size')) is not int or r['size']<0 or not isinstance(r.get('sha256'),str) or len(r['sha256'])!=64 or any(c not in '0123456789abcdef' for c in r['sha256']) for r in records):raise IntegrityError('Invalid exact pinned identity')
    policy=current_policy()
    if policy is not None:
        output,work=_path(policy['output']),_path(policy['work'])
        if not (path.is_relative_to(output) or path.is_relative_to(work) or str(path) in policy['inputs']):raise IntegrityError('Pinned read outside authorized roots/inputs')
    before=path.lstat()
    if not stat.S_ISREG(before.st_mode):raise IntegrityError('Pinned file is not regular: '+str(path))
    candidates=[r for r in records if r['size']==before.st_size and ('mtime_ns' not in r or r['mtime_ns']==before.st_mtime_ns)]
    if not candidates:raise IntegrityError('Pinned file metadata changed: '+str(path))
    dataless=lambda s:bool(getattr(s,'st_flags',0)&0x40000000)
    trigger=None
    if dataless(before):trigger='dataless before pinned read'
    else:
        count,digest=_read(path);after=_path(path).lstat()
        if _identity(before)!=_identity(after):raise IntegrityError('Pinned file changed during read: '+str(path))
        if count==before.st_size:
            if not any(digest==r['sha256'] for r in candidates):raise IntegrityError('Pinned file content changed: '+str(path))
            if not dataless(after):return dict(size=count,sha256=digest)
            trigger='dataless after matching pinned read'
        elif count<before.st_size:trigger='short pinned read with stable metadata'
        else:raise IntegrityError('Pinned file oversized read: '+str(path))
    policy=current_policy()
    if not policy or not policy['enabled']:raise IntegrityError('Pinned file unavailable; exact fetch disabled: '+str(path))
    output,work=_path(policy['output']),_path(policy['work'])
    if not (path.is_relative_to(output) or path.is_relative_to(work) or str(path) in policy['inputs']):raise IntegrityError('Pinned fetch outside authorized roots/inputs')
    reservation=policy.get('reserve_bytes')
    if type(reservation) is not int or reservation<=0:raise IntegrityError('Pinned fetch requires positive canonical reservation')
    capacity_before=shutil.disk_usage(work).free
    run=policy['run_id']
    if not isinstance(run,str) or len(run)!=32 or any(c not in '0123456789abcdef' for c in run):raise IntegrityError('Unsafe availability run identity')
    evidence=work/'availability-intake'/run;_path(evidence);evidence.mkdir(parents=True,exist_ok=True);_path(evidence)
    for directory in (work.parent,work,work/'availability-intake',evidence):_sync_dir(directory)
    key=hashlib.sha256(str(path).encode()).hexdigest();claim=evidence/(key+'.intent.json');terminal=evidence/(key+'.result.json');verification=evidence/(key+'.verification.json')
    try:_write(claim,dict(path=str(path),records=records,trigger=trigger,identity=_identity(before),deadline_seconds=DEADLINE,canonical_reservation_bytes=reservation,available_free_before=capacity_before))
    except FileExistsError as exc:raise IntegrityError('Exact path fetch already consumed this run: '+str(path)) from exc
    if capacity_before<reservation+before.st_size:
        _write(terminal,dict(refused_before_fetch=True,canonical_reservation_bytes=reservation,available_free_before=capacity_before,file_bytes=before.st_size))
        raise OSError(errno.ENOSPC,'Insufficient capacity for exact pinned fetch plus canonical reservation')
    command=[ICMAT,'fetch','--json','--download-ahead','1','--',str(path)];receipt=None;error=None;code=None
    try:
        result=run_command(command,timeout=DEADLINE,check=False);code=result.returncode
        receipt=json.loads(result.stdout)
        error=result.stderr.decode(errors='replace') if isinstance(result.stderr,bytes) else result.stderr
    except Exception as exc:error=repr(exc)
    capacity_after=shutil.disk_usage(work).free
    _write(terminal,dict(command=command,exit_code=code,receipt=receipt,error=error,canonical_reservation_bytes=reservation,available_free_before=capacity_before,available_free_after=capacity_after))
    if capacity_after<reservation:
        _write(verification,dict(verified=False,error='canonical reservation crossed after exact fetch',available_free_after=capacity_after))
        raise OSError(errno.ENOSPC,'Canonical reservation crossed after exact pinned fetch')
    counts=receipt.get('counts',{}) if isinstance(receipt,dict) else {}
    if code!=0 or not isinstance(receipt,dict) or not receipt.get('success') or not receipt.get('enumerationComplete') or receipt.get('failures') or counts.get('regularFiles')!=1 or counts.get('filesVerified')!=1 or counts.get('datalessFiles')!=0:raise IntegrityError('Exact pinned availability failed; work evidence retained: '+str(path))
    actual_count=actual_digest=None
    try:
        refreshed=_path(path).lstat()
        if _identity(before)!=_identity(refreshed) or dataless(refreshed):raise IntegrityError('Pinned file changed/unavailable across exact fetch: '+str(path))
        actual_count,actual_digest=_read(path);after=_path(path).lstat()
        if _identity(refreshed)!=_identity(after) or dataless(after) or actual_count!=before.st_size or not any(actual_digest==r['sha256'] for r in candidates):raise IntegrityError('Pinned reread failed; no further fetch: '+str(path))
    except Exception as exc:
        _write(verification,dict(verified=False,actual_count=actual_count,actual_sha256=actual_digest,error=repr(exc)))
        raise
    _write(verification,dict(verified=True,actual_count=actual_count,actual_sha256=actual_digest,identity=_identity(after)))
    return dict(size=actual_count,sha256=actual_digest)
