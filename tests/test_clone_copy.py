import errno
import os
from pathlib import Path
import shutil
import stat
from types import SimpleNamespace
import stat
import tempfile
import unittest
from unittest.mock import patch
from agent_transcribe.clone_copy import clone_copy2, atomic_clone_copy, _clonefile, _blocking_source_flags
from agent_transcribe.folder_intake import build_folder_plan
from agent_transcribe.conversion import convert_folder
from agent_transcribe.portable import verify_bundle

class CloneCopyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();self.source=self.root/'source';self.source.write_bytes(b'first bytes\n');self.target=self.root/'target'

    def test_real_clone_distinct_inode_and_independent_mutation(self):
        try:_clonefile(self.source,self.target)
        except OSError as exc:
            if exc.errno in (errno.ENOSYS,errno.ENOTSUP,errno.EXDEV):self.skipTest('native clone unavailable')
            raise
        self.assertNotEqual(self.source.stat().st_ino,self.target.stat().st_ino)
        self.target.write_bytes(b'target changed');self.assertEqual(self.source.read_bytes(),b'first bytes\n')
        self.source.write_bytes(b'source changed');self.assertEqual(self.target.read_bytes(),b'target changed')

    def test_copy_metadata_and_destination_directory(self):
        os.chmod(self.source,0o640);os.utime(self.source,ns=(1600000000000000000,1600000000123456789))
        directory=self.root/'directory';directory.mkdir();clone_copy2(self.source,directory);target=directory/self.source.name
        self.assertEqual(target.read_bytes(),self.source.read_bytes());self.assertEqual(target.stat().st_mtime_ns,self.source.stat().st_mtime_ns);self.assertEqual(target.stat().st_mode&0o777,0o640)

    def test_darwin_flag_mask_allows_only_tracked_and_hidden(self):
        for flags in (0, 0x40, 0x8000, 0x8040):
            with patch('agent_transcribe.clone_copy.sys.platform', 'darwin'), patch('agent_transcribe.clone_copy.os.stat', return_value=SimpleNamespace(st_flags=flags)):
                self.assertEqual(_blocking_source_flags('synthetic'), 0)
        # Local Darwin SDK definitions, plus unknown bits: none may opt in.
        for flag in (0x1, 0x2, 0x4, 0x8, 0x20, 0x80, 0x10000, 0x20000, 0x40000, 0x80000,
                     0x100000, 0x800000, 0x40000000, 0x80000000, 0x100):
            with patch('agent_transcribe.clone_copy.sys.platform', 'darwin'), patch('agent_transcribe.clone_copy.os.stat', return_value=SimpleNamespace(st_flags=flag | 0x8040)):
                self.assertEqual(_blocking_source_flags('synthetic'), flag)
        with patch('agent_transcribe.clone_copy.sys.platform', 'not-darwin'), patch('agent_transcribe.clone_copy.os.stat', return_value=SimpleNamespace(st_flags=0x40)):
            self.assertEqual(_blocking_source_flags('synthetic'), 0x40)

    def test_real_tracked_hidden_sources_clone_and_remain_independent(self):
        import agent_transcribe.clone_copy as implementation
        if not hasattr(os, 'chflags'):
            self.skipTest('BSD file flags unavailable')
        real = implementation._clonefile
        for flags in (0x40, 0x8000, 0x8040):
            source = self.root / f'flag-source-{flags}'
            source.write_bytes(b'source content must stay unchanged')
            os.chflags(source, flags)
            before = source.stat()
            with patch('agent_transcribe.clone_copy._clonefile', wraps=real) as native:
                target = self.root / f'flag-copy-{flags}'
                clone_copy2(source, target)
                atomic_target = self.root / f'flag-atomic-{flags}'
                atomic_clone_copy(atomic_target, source)
            self.assertEqual(native.call_count, 2)
            self.assertEqual(target.read_bytes(), source.read_bytes())
            self.assertEqual(target.stat().st_mtime_ns, before.st_mtime_ns)
            self.assertEqual(target.stat().st_flags, flags)
            self.assertEqual(atomic_target.stat().st_mode & 0o777, 0o600)
            self.assertEqual(len({p.stat().st_ino for p in (source, target, atomic_target)}), 3)
            target.write_bytes(b'only this clone changes')
            self.assertEqual(source.read_bytes(), b'source content must stay unchanged')
            self.assertEqual(atomic_target.read_bytes(), b'source content must stay unchanged')
            self.assertEqual((source.stat().st_mtime_ns, source.stat().st_flags), (before.st_mtime_ns, before.st_flags))

    def test_blocking_source_flags_keep_legacy_atomic_and_copy2_paths(self):
        for flags in (0x2, 0x4, 0x80, 0x20000, 0x40000, 0x80000,
                      0x100000, 0x800000, 0x40000000):
            with patch('agent_transcribe.clone_copy._blocking_source_flags', return_value=flags), patch('agent_transcribe.clone_copy._clonefile') as native, patch('agent_transcribe.common.atomic_copy') as legacy:
                atomic_clone_copy(self.target, self.source)
                legacy.assert_called_once_with(self.target, self.source)
                native.assert_not_called()
            with patch('agent_transcribe.clone_copy._blocking_source_flags', return_value=flags), patch('agent_transcribe.clone_copy._clonefile') as native, patch('agent_transcribe.clone_copy.shutil.copy2') as ordinary:
                clone_copy2(self.source, self.target)
                ordinary.assert_called_once()
                native.assert_not_called()

    def test_existing_destination_matches_copy2_semantics(self):
        self.target.write_bytes(b'old')
        with patch('agent_transcribe.clone_copy._clonefile') as native:clone_copy2(self.source,self.target);native.assert_not_called()
        self.assertEqual(self.target.read_bytes(),self.source.read_bytes())
        with self.assertRaises(shutil.SameFileError):clone_copy2(self.source,self.source)

    def test_cross_volume_and_unsupported_fallback(self):
        for code in (errno.EXDEV,errno.ENOTSUP,errno.ENOSYS):
            target=self.root/str(code)
            with patch('agent_transcribe.clone_copy._clonefile',side_effect=OSError(code,'unsupported')):clone_copy2(self.source,target)
            self.assertEqual(target.read_bytes(),self.source.read_bytes());self.assertNotEqual(target.stat().st_ino,self.source.stat().st_ino)

    def test_append_only_source_preserves_original_copy_contracts(self):
        if not hasattr(os, 'chflags'):
            self.skipTest('BSD file flags unavailable')
        os.chflags(self.source, stat.UF_APPEND)
        before = self.source.stat()
        with patch('agent_transcribe.clone_copy._clonefile') as native:
            atomic_clone_copy(self.target, self.source)
            other = self.root / 'append-copy2'
            clone_copy2(self.source, other)
            native.assert_not_called()
        self.assertEqual(self.target.stat().st_flags, 0)
        self.assertEqual(other.stat().st_flags, stat.UF_APPEND)
        self.assertEqual(self.target.read_bytes(), self.source.read_bytes())
        self.assertEqual(other.read_bytes(), self.source.read_bytes())
        self.assertEqual((self.source.stat().st_mtime_ns, self.source.stat().st_flags), (before.st_mtime_ns, before.st_flags))

    def test_space_io_permission_errors_are_not_suppressed(self):
        for code in (errno.ENOSPC,errno.EIO,errno.EACCES):
            with patch('agent_transcribe.clone_copy._clonefile',side_effect=OSError(code,'fatal')),patch('agent_transcribe.clone_copy.shutil.copy2') as fallback:
                with self.assertRaises(OSError) as got:clone_copy2(self.source,self.target)
                self.assertEqual(got.exception.errno,code);fallback.assert_not_called()

    def test_failed_clone_conflict_preserved(self):
        def conflict(*args):self.target.write_bytes(b'other writer');raise OSError(errno.EXDEV,'unsupported')
        with patch('agent_transcribe.clone_copy._clonefile',side_effect=conflict):
            with self.assertRaises(OSError):clone_copy2(self.source,self.target)
        self.assertEqual(self.target.read_bytes(),b'other writer')

    def test_symlink_semantics(self):
        link=self.root/'link';link.symlink_to(self.source)
        clone_copy2(link,self.target,follow_symlinks=False);self.assertTrue(self.target.is_symlink());self.assertEqual(os.readlink(self.target),str(self.source))

    def test_nested_copytree_and_relocation(self):
        tree=self.root/'tree';(tree/'nested').mkdir(parents=True);(tree/'nested'/'data').write_bytes(b'data')
        copied=self.root/'copied';shutil.copytree(tree,copied,copy_function=clone_copy2);shutil.move(copied,self.root/'moved')
        target=self.root/'moved'/'nested'/'data';self.assertEqual(target.read_bytes(),b'data');self.assertNotEqual(target.stat().st_ino,(tree/'nested'/'data').stat().st_ino)

    def test_exclusive_atomic_publication_and_metadata(self):
        os.chmod(self.source,0o644)
        atomic_clone_copy(self.target,self.source);before=self.target.read_bytes()
        self.assertEqual(self.target.stat().st_mode&0o777,0o600)
        self.assertEqual(self.target.stat().st_mtime_ns,self.source.stat().st_mtime_ns);self.assertNotEqual(self.target.stat().st_ino,self.source.stat().st_ino)
        with self.assertRaises(FileExistsError):atomic_clone_copy(self.target,self.source)
        self.assertEqual(self.target.read_bytes(),before);self.assertFalse(list(self.root.glob('.pending-copy-*')))

    @unittest.skipUnless(hasattr(os, 'chflags'), 'BSD file flags unavailable')
    def test_atomic_readable_immutable_source_uses_legacy_byte_copy(self):
        try:
            os.chflags(self.source, stat.UF_IMMUTABLE)
            with patch('agent_transcribe.clone_copy._clonefile') as native:
                atomic_clone_copy(self.target,self.source)
                native.assert_not_called()
            self.assertEqual(self.target.read_bytes(),self.source.read_bytes())
            self.assertEqual(self.target.stat().st_flags,0)
            self.assertEqual(self.target.stat().st_mode&0o777,0o600)
            flagged_copy=self.root/'flagged-copy2'
            with patch('agent_transcribe.clone_copy._clonefile') as native:
                clone_copy2(self.source,flagged_copy)
                native.assert_not_called()
            self.assertEqual(flagged_copy.stat().st_flags,stat.UF_IMMUTABLE)
            os.chflags(flagged_copy,0)
        finally:
            os.chflags(self.source,0)

    def test_synthetic_conversion_resume_and_portable_relocation(self):
        originals=self.root/'originals';originals.mkdir();(originals/'a.txt').write_text('Source text preserved.\n');bundle=self.root/'bundle';plan=build_folder_plan(originals,bundle);result=convert_folder(plan);self.assertTrue(result['publication_complete'])
        files=[p for tree in (bundle,bundle.with_name(bundle.name+'.conversion-work')) for p in tree.rglob('*') if p.is_file()]
        inodes=[(p.stat().st_dev,p.stat().st_ino) for p in files]
        self.assertEqual(len(inodes),len(set(inodes)), 'Every retained path must have an independent inode')
        result=convert_folder(plan);self.assertTrue(result['publication_complete']);shutil.move(bundle,self.root/'relocated');self.assertTrue(verify_bundle(self.root/'relocated')['valid'])
