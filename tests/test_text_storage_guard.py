import copy
import errno
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from agent_transcribe import conversion as c
from agent_transcribe.docling_backend import BackendOptions,inspect_source
from agent_transcribe.folder_intake import build_folder_plan

class TextStorageGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name).resolve()
    def profile(self,raw,name='source.txt'):
        p=self.root/name;p.write_bytes(raw);source=dict(kind='txt',absolute_path=str(p),relative_path=name,size=len(raw),mtime_ns=p.stat().st_mtime_ns,sha256=hashlib.sha256(raw).hexdigest());inv=inspect_source(p,'txt');return source,inv,c._storage_profile(source,inv,BackendOptions())
    def test_short_blank_empty_utf8_control_long_and_bom_width(self):
        values=[b'word\n'*10,b'\n'*10,b'',('Unicode é 中文 🗃\n'*3).encode(),b'\x07\t"\\\x00\f\r\n',b'x'*32768+b'\n',b'\xef\xbb\xbfBOM\n',('UTF16\n').encode('utf-16')]
        for i,raw in enumerate(values):
            source,inv,p=self.profile(raw,str(i)+'.txt');d=p['text_storage'];self.assertEqual(d['schema'],1);self.assertEqual(d['line_count'],inv['expected_count']);self.assertGreaterEqual(p['metadata_bytes'],max(1048576,len(raw)*8));self.assertEqual(d['metadata_reserve_bytes'],1048576+d['line_count']*131072+64*max(len(raw),d['json_escaped_text_bytes']))
        _,_,control=self.profile(b'\x07\x00\n','escaped.txt');self.assertGreater(control['text_storage']['json_escaped_text_bytes'],3)
    def test_failed_bom_and_no_bom_cp1252_keep_unknown_no_promotion(self):
        for i,raw in enumerate([b'\xff\xfeA',b'caf\xe9\n']):
            source,inv,p=self.profile(raw,'bad'+str(i)+'.txt');d=p['text_storage'];self.assertIsNone(inv['expected_count']);self.assertIsNone(d['line_count']);self.assertIsNone(d['encoding']);self.assertIsNone(d['json_escaped_text_bytes']);self.assertEqual(d['line_count_upper_allowance'],len(raw)+1);self.assertTrue(p['unknown_count']);self.assertGreaterEqual(d['escaped_text_byte_allowance'],len(raw)*6)
    def test_failed_decode_still_publishes_existing_partial_gap(self):
        source=self.root/'originals';source.mkdir();(source/'bad.txt').write_bytes(b'caf\xe9\n');output=self.root/'bundle';result=c.convert_folder(build_folder_plan(source,output));self.assertFalse(result['conversion_complete']);self.assertFalse(result['coverage_accounted']);self.assertTrue(result['publication_complete']);self.assertEqual((output/'Originals/bad.txt').read_bytes(),b'caf\xe9\n')
    def test_line_inventory_and_hash_drift_refused(self):
        source,inv,_=self.profile(b'one\ntwo\n');bad=copy.deepcopy(inv);bad['expected_count']=1
        with self.assertRaises(c.IntegrityError):c._storage_profile(source,bad,BackendOptions())
        Path(source['absolute_path']).write_bytes(b'one\nTHREE\n')
        with self.assertRaises(c.IntegrityError):c._storage_profile(source,inv,BackendOptions())
    def test_old_profile_monotonic_upgrade_history_complete_untouched(self):
        source,inv,new=self.profile(b'one\ntwo\n');old={k:v for k,v in new.items() if k!='text_storage'};old['metadata_bytes']=1024**3;entry=dict(state='pending',source=source,inventory=inv,storage_profile=copy.deepcopy(old));done=dict(state='complete',source=copy.deepcopy(source),storage_profile=copy.deepcopy(old));receipt={'sources':[entry,done]}
        with patch.object(c,'_save'),patch.object(c.shutil,'disk_usage',return_value=type('Space',(),{'free':10**15})()):c._reserve_storage(self.root,receipt,BackendOptions())
        self.assertEqual(entry['storage_profile_history'],[old]);self.assertEqual(entry['storage_profile']['metadata_bytes'],1024**3);self.assertIn('text_storage',entry['storage_profile']);self.assertEqual(done['storage_profile'],old);self.assertNotIn('storage_profile_history',done)
    def test_storage_failure_precedes_copy_render_worker_and_publication(self):
        source=self.root/'originals';source.mkdir();(source/'short.txt').write_bytes(b'x\n'*100);output=self.root/'bundle'
        with patch.object(c.shutil,'disk_usage',return_value=type('Space',(),{'free':2*1048576})()),patch.object(c,'atomic_copy',side_effect=AssertionError('copy before gate')),patch.object(c,'render_units',side_effect=AssertionError('render before gate')),patch.object(c,'_Worker',side_effect=AssertionError('worker before gate')),patch.object(c,'_publish',side_effect=AssertionError('publish before gate')):
            with self.assertRaises(OSError) as got:c.convert_folder(build_folder_plan(source,output))
        self.assertEqual(got.exception.errno,errno.ENOSPC);self.assertFalse((output/'Originals').exists());self.assertFalse((output/'Items').exists());receipt=json.loads((output/'conversion.json').read_text());self.assertFalse(receipt['storage_estimate']['sufficient'])
    def test_complete_profile_unchanged_but_pending_root_views_reserved(self):
        source,inv,new=self.profile(b'x\n'*10);old={k:v for k,v in new.items() if k!='text_storage'};old['metadata_bytes']=1024
        entry=dict(state='complete',source=source,inventory=inv,storage_profile=copy.deepcopy(old));receipt={'sources':[entry],'view_signature':'old','views_intent':{'signature':'new','files':[{'size':8*1048576}],'previous':[{'size':1048576}]}}
        with patch.object(c,'_save'),patch.object(c.shutil,'disk_usage',return_value=type('Space',(),{'free':2*1048576})()):
            with self.assertRaises(OSError) as got:c._reserve_storage(self.root,receipt,BackendOptions())
        self.assertEqual(got.exception.errno,errno.ENOSPC);self.assertEqual(entry['storage_profile'],old);self.assertNotIn('storage_profile_history',entry);self.assertEqual(receipt['storage_estimate']['pending_view_publication_bytes'],9*1048576);self.assertGreater(receipt['storage_estimate']['retained_text_view_reserve_bytes'],0)
        # Also cover the earlier interruption before a root intent was saved.
        receipt.pop('views_intent');receipt.pop('view_signature')
        with patch.object(c,'_save'),patch.object(c.shutil,'disk_usage',return_value=type('Space',(),{'free':2*1048576})()):
            with self.assertRaises(OSError):c._reserve_storage(self.root,receipt,BackendOptions())
        self.assertEqual(entry['storage_profile'],old)

    def test_profile_formula_covers_measured_fixture_costs(self):
        # Frozen measured diagnostic results; no dependency on private/archive fixtures.
        for n,logical,blocks in [(100,3921925,6393856),(1000,38634217,62468096)]:
            _,_,p=self.profile((b'Synthetic retained text '+b'x'*7+b'\n')*n,str(n)+'.txt');self.assertGreater(p['metadata_bytes'],logical);self.assertGreater(p['metadata_bytes'],blocks)
