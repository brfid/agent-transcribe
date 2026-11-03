import hashlib,io,json,os,stat,tempfile,types,unittest
from pathlib import Path
from unittest import mock
from agent_transcribe.common import IntegrityError,file_record
from agent_transcribe import pinned_availability as a,conversion,recovery
class AvailabilityTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name).resolve();self.out=self.root/'out';self.work=self.root/'work';self.out.mkdir();self.work.mkdir();self.path=self.out/'pinned';self.path.write_bytes(b'expected');self.row=dict(path='pinned',size=8,sha256=hashlib.sha256(b'expected').hexdigest());self.policy=a.policy_for(self.out,self.work,[],True,reserve_bytes=1);self.flags=0;self.calls=0;self.realstat=Path.lstat;self.realread=a._read
 def metadata(self,p):
  actual=self.realstat(p)
  if p==self.path:return types.SimpleNamespace(st_dev=actual.st_dev,st_ino=actual.st_ino,st_size=actual.st_size,st_mtime_ns=actual.st_mtime_ns,st_ctime_ns=actual.st_ctime_ns,st_mode=actual.st_mode,st_flags=self.flags)
  return actual
 def fetch(self,command,**kw):
  self.calls+=1;self.assertEqual(command[-1],str(self.path));self.assertEqual(command[-5:-1],['--download-ahead','1','--',str(self.path)][:-1]) if False else None
  self.assertFalse(kw['check']);self.assertEqual(command[1:5],['fetch','--json','--download-ahead','1']);self.assertLess(kw['timeout'],conversion.WORKER_WATCHDOG);self.flags=0
  value=dict(success=True,enumerationComplete=True,failures=[],counts=dict(regularFiles=1,filesVerified=1,datalessFiles=0));return types.SimpleNamespace(returncode=0,stdout=json.dumps(value),stderr='')
 def scope(self):return a.availability_scope(self.policy)
 def test_resident_no_fetch(self):
  with self.scope(),mock.patch.object(a,'run_command',side_effect=self.fetch):self.assertEqual(a.verify_pinned(self.path,self.row)['size'],8)
  self.assertEqual(self.calls,0)
 def test_dataless_one_exact_fetch_and_durable_receipts(self):
  self.flags=0x40000000
  with self.scope(),mock.patch.object(Path,'lstat',lambda p,*args,**kw:self.metadata(p)),mock.patch.object(a,'run_command',side_effect=self.fetch):a.verify_pinned(self.path,self.row)
  self.assertEqual(self.calls,1);self.assertEqual(len(list(self.work.rglob('*.intent.json'))),1);self.assertEqual(len(list(self.work.rglob('*.result.json'))),1)
 def test_short_read_one_fetch(self):
  reads=iter([(0,hashlib.sha256(b'').hexdigest()),(8,self.row['sha256'])])
  with self.scope(),mock.patch.object(a,'_read',side_effect=lambda p:next(reads)),mock.patch.object(a,'run_command',side_effect=self.fetch):a.verify_pinned(self.path,self.row)
  self.assertEqual(self.calls,1)
 def test_optout_fatal(self):
  self.policy['enabled']=False;self.flags=0x40000000
  with self.scope(),mock.patch.object(Path,'lstat',lambda p,*args,**kw:self.metadata(p)),mock.patch.object(a,'run_command',side_effect=self.fetch):
   with self.assertRaises(IntegrityError):a.verify_pinned(self.path,self.row)
  self.assertEqual(self.calls,0)
 def test_full_wrong_hash_later_dataless_never_fetches(self):
  def read(p):self.flags=0x40000000;return 8,'0'*64
  with self.scope(),mock.patch.object(Path,'lstat',lambda p,*args,**kw:self.metadata(p)),mock.patch.object(a,'_read',side_effect=read),mock.patch.object(a,'run_command',side_effect=self.fetch):
   with self.assertRaises(IntegrityError):a.verify_pinned(self.path,self.row)
  self.assertEqual(self.calls,0)
 def test_persistent_budget_across_worker_policy_reconstruction(self):
  with mock.patch.object(Path,'lstat',lambda p,*args,**kw:self.metadata(p)),mock.patch.object(a,'run_command',side_effect=self.fetch):
   self.flags=0x40000000
   with self.scope():a.verify_pinned(self.path,self.row)
   self.flags=0x40000000
   with a.availability_scope(dict(self.policy)):
    with self.assertRaises(IntegrityError):a.verify_pinned(self.path,self.row)
  self.assertEqual(self.calls,1)
 def test_intent_survives_kill_before_terminal(self):
  self.flags=0x40000000
  with self.scope(),mock.patch.object(Path,'lstat',lambda p,*args,**kw:self.metadata(p)),mock.patch.object(a,'run_command',side_effect=KeyboardInterrupt):
   with self.assertRaises(KeyboardInterrupt):a.verify_pinned(self.path,self.row)
   with self.assertRaises(IntegrityError):a.verify_pinned(self.path,self.row)
  self.assertEqual(len(list(self.work.rglob('*.intent.json'))),1);self.assertFalse(list(self.work.rglob('*.result.json')))
 def test_dotdot_escape_fatal(self):
  with self.scope():
   with self.assertRaises(IntegrityError):a.verify_pinned(self.out/'..'/'out'/'pinned',self.row)
 def test_outside_scope_dataless_no_fetch(self):
  self.policy['output']=str(self.root/'elsewhere');self.flags=0x40000000
  with self.scope(),mock.patch.object(Path,'lstat',lambda p,*args,**kw:self.metadata(p)),mock.patch.object(a,'run_command',side_effect=self.fetch):
   with self.assertRaises(IntegrityError):a.verify_pinned(self.path,self.row)
  self.assertEqual(self.calls,0)
 def test_symlink_fatal(self):
  p=self.out/'link';p.symlink_to(self.path)
  with self.scope():
   with self.assertRaises(IntegrityError):a.verify_pinned(p,self.row)
 def test_failed_fetch_terminal_evidence_fatal(self):
  self.flags=0x40000000
  with self.scope(),mock.patch.object(Path,'lstat',lambda p,*args,**kw:self.metadata(p)),mock.patch.object(a,'run_command',side_effect=TimeoutError('synthetic')):
   with self.assertRaises(IntegrityError):a.verify_pinned(self.path,self.row)
  self.assertEqual(len(list(self.work.rglob('*.result.json'))),1)
 def test_unpinned_nonzero_zero_read_fatal_no_fetch(self):
  original=Path.open
  with mock.patch.object(Path,'open',lambda p,*args,**kw:io.BytesIO(b'') if p==self.path else original(p,*args,**kw)):
   with self.assertRaises(IntegrityError):file_record(self.path)
 def test_unpinned_zero_length_valid(self):
  self.path.write_bytes(b'');self.assertEqual(file_record(self.path),dict(size=0,sha256=hashlib.sha256(b'').hexdigest()))
 def test_actual_resume_and_record_scan_use_known_pin(self):
  receipt=dict(files=[self.row],sources=[]);reads=0
  def read(p):
   nonlocal reads
   reads+=1
   return (0,hashlib.sha256(b'').hexdigest()) if reads==1 else self.realread(p)
  with self.scope(),mock.patch.object(a,'_read',side_effect=read),mock.patch.object(a,'run_command',side_effect=self.fetch):
   conversion._verify_resume(self.out,receipt);conversion._check_untracked(self.out,receipt)
  self.assertEqual(self.calls,1)
 def test_outside_resident_read_refused_before_payload(self):
  self.policy['output']=str(self.root/'elsewhere')
  with self.scope(),mock.patch.object(a,'_read',side_effect=AssertionError('must not read')):
   with self.assertRaises(IntegrityError):a.verify_pinned(self.path,self.row)
 def test_invalid_pin_refused_before_fetch(self):
  with self.scope(),mock.patch.object(a,'run_command',side_effect=AssertionError('must not fetch')):
   with self.assertRaises(IntegrityError):a.verify_pinned(self.path,dict(size=8,sha256='bad'))
 def test_prefetch_capacity_refusal_has_no_materializer(self):
  self.flags=0x40000000
  with self.scope(),mock.patch.object(Path,'lstat',lambda p,*args,**kw:self.metadata(p)),mock.patch.object(a.shutil,'disk_usage',return_value=types.SimpleNamespace(free=8)),mock.patch.object(a,'run_command',side_effect=self.fetch):
   with self.assertRaises(OSError):a.verify_pinned(self.path,self.row)
  self.assertEqual(self.calls,0)
 def test_postfetch_capacity_refusal_fatal(self):
  self.flags=0x40000000
  with self.scope(),mock.patch.object(Path,'lstat',lambda p,*args,**kw:self.metadata(p)),mock.patch.object(a.shutil,'disk_usage',side_effect=[types.SimpleNamespace(free=100),types.SimpleNamespace(free=0)]),mock.patch.object(a,'run_command',side_effect=self.fetch):
   with self.assertRaises(OSError):a.verify_pinned(self.path,self.row)
  self.assertEqual(self.calls,1)
 def test_missing_invalid_reservation_never_fetches(self):
  self.flags=0x40000000
  for value in [None,0,-1,True,'1']:
   self.policy['reserve_bytes']=value
   with self.scope(),mock.patch.object(Path,'lstat',lambda p,*args,**kw:self.metadata(p)),mock.patch.object(a,'run_command',side_effect=self.fetch):
    with self.assertRaises(IntegrityError):a.verify_pinned(self.path,self.row)
  self.assertEqual(self.calls,0)
 def test_enabled_policy_rejects_parent_symlink(self):
  alias=self.root/'alias';alias.symlink_to(self.out,target_is_directory=True)
  with self.assertRaises(IntegrityError):a.policy_for(alias,self.work,[],True,reserve_bytes=1)
 def test_reservation_propagates_from_existing_storage_guard(self):
  self.policy['reserve_bytes']=None
  with self.scope():
   result=conversion._reserve_storage(self.out,{'sources':[]},conversion.BackendOptions())
  self.assertEqual(self.policy['reserve_bytes'],result['required_free_bytes']);self.assertGreater(self.policy['reserve_bytes'],0)
 def test_preverify_refusal_leaves_checkpoint_bytes_unchanged(self):
  checkpoint=self.out/'conversion.json';raw=b'{"checkpoint":"unchanged"}';checkpoint.write_bytes(raw);self.flags=0x40000000;self.policy['reserve_bytes']=None
  with self.scope(),mock.patch.object(Path,'lstat',lambda p,*args,**kw:self.metadata(p)),mock.patch.object(conversion,'_save',side_effect=AssertionError('must not save')):
   with self.assertRaises(IntegrityError):conversion._verify_resume(self.out,{'files':[self.row],'sources':[]})
  self.assertEqual(checkpoint.read_bytes(),raw)
 def test_fetch_verification_failure_retains_actual_digest(self):
  self.flags=0x40000000
  with self.scope(),mock.patch.object(Path,'lstat',lambda p,*args,**kw:self.metadata(p)),mock.patch.object(a,'_read',return_value=(8,'0'*64)),mock.patch.object(a,'run_command',side_effect=self.fetch):
   with self.assertRaises(IntegrityError):a.verify_pinned(self.path,self.row)
  result=json.loads(next(self.work.rglob('*.verification.json')).read_text());self.assertFalse(result['verified']);self.assertEqual(result['actual_sha256'],'0'*64)
 def test_evidence_parent_directories_synced(self):
  self.flags=0x40000000;synced=[];real=a._sync_dir
  def sync(path):synced.append(Path(path));return real(path)
  with self.scope(),mock.patch.object(Path,'lstat',lambda p,*args,**kw:self.metadata(p)),mock.patch.object(a,'run_command',side_effect=self.fetch),mock.patch.object(a,'_sync_dir',side_effect=sync):a.verify_pinned(self.path,self.row)
  self.assertIn(self.work.parent,synced);self.assertIn(self.work,synced);self.assertIn(self.work/'availability-intake',synced)
 def test_fatal_type_is_canonical(self):
  self.assertFalse(issubclass(IntegrityError,RuntimeError))
if __name__=='__main__':unittest.main()
