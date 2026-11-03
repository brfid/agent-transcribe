"""Synthetic orchestration, preservation and restart contract tests."""
import copy
import errno
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from agent_transcribe import conversion as c
from agent_transcribe.common import IntegrityError, checksum
from agent_transcribe.docling_backend import DoclingBackend
from agent_transcribe.folder_intake import build_archive_plan, build_folder_plan
from agent_transcribe.portable import export_part, verify_bundle


class InlineWorker:
    calls = []
    fail = set()

    def __init__(self, options):
        self.backend = DoclingBackend(c.BackendOptions(**options))

    def request(self, task):
        type(self).calls.append((task['source']['relative_path'], list(task['ids'])))
        result = self.backend.convert_range(task['path'], task['kind'], task['start'], task['end'], scratch=task['scratch'])
        result['units'] = [r for r in result['units'] if r['unit_id'] in task['ids']]
        for row in result['units']:
            if row['unit_id'] in type(self).fail:
                row.update(conversion_status='failed', error='synthetic content gap')
        manifest = export_part(result, task['directory'], source=task['source'], rendered=task['rendered'])
        return {'ok': True, 'manifest': manifest}

    def close(self):
        pass


class NumericWorker(InlineWorker):
    def request(self, task):
        type(self).calls.append((task['source']['relative_path'], list(task['ids'])))
        result = {'document': None, 'units': [
            {'unit_id': uid, 'text': f'unit {uid}', 'decode_status': 'not_applicable',
             'conversion_status': 'failed' if uid in type(self).fail else 'ok', 'attempts': [], 'recognition_warnings': []}
            for uid in task['ids']]}
        return {'ok': True, 'manifest': export_part(result, task['directory'], source=task['source'], rendered=task['rendered'])}


def crash_worker(connection, options):
    os._exit(42)


def sleep_worker(connection, options):
    time.sleep(20)


def ready(options, **kwargs):
    return {'ready': True, 'versions': {'synthetic': '1'}, 'models': []}


class ConversionResumeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='convert synthetic ')
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / 'source'
        self.root.mkdir()
        self.output = self.base / 'converted'
        InlineWorker.calls, InlineWorker.fail = [], set()
        NumericWorker.calls, NumericWorker.fail = [], set()
        self.worker = patch.object(c, '_Worker', InlineWorker)
        self.worker.start()

    def tearDown(self):
        self.worker.stop()
        self.temp.cleanup()

    def plan(self, name='text # %.txt', text='alpha\nbeta\n'):
        (self.root / name).write_text(text)
        return build_folder_plan(self.root, self.output)

    def receipt(self):
        return json.loads((self.output / 'conversion.json').read_text())

    def interrupt(self, event):
        hit = []
        def callback(observed, receipt):
            if observed == event and not hit:
                hit.append(True)
                raise KeyboardInterrupt()
        return patch.object(c, '_checkpoint_hook', callback)

    def test_text_preservation_complete_resume_no_repeated_inference(self):
        plan = self.plan()
        first = c.convert_folder(plan)
        self.assertEqual(first['exit_code'], 0)
        source = first['sources'][0]
        original = self.output / source['original_file']['path']
        self.assertEqual(original.read_bytes(), (self.root / 'text # %.txt').read_bytes())
        self.assertEqual(original.stat().st_mtime_ns, (self.root / 'text # %.txt').stat().st_mtime_ns)
        pins = {r['path']: checksum(self.output / r['path']) for r in c._accepted_files(first)}
        second = c.convert_folder(plan)
        self.assertEqual(second['exit_code'], 0)
        self.assertEqual(len(InlineWorker.calls), 1)
        self.assertEqual(pins, {p: checksum(self.output / p) for p in pins})

    def test_render_checkpoint_survives_inference_interrupt(self):
        plan = self.plan()
        with self.interrupt('inference'), self.assertRaises(KeyboardInterrupt):
            c.convert_folder(plan)
        chunk = self.receipt()['sources'][0]['ranges'][0]
        self.assertIn('render', chunk)
        self.assertEqual(len(InlineWorker.calls), 0)
        with patch.object(c, 'render_units', side_effect=AssertionError('must reuse render')):
            self.assertEqual(c.convert_folder(plan)['exit_code'], 0)

    def test_prepared_replay_avoids_inference(self):
        plan = self.plan()
        with self.interrupt('prepared'), self.assertRaises(KeyboardInterrupt):
            c.convert_folder(plan)
        self.assertEqual(verify_bundle(self.output)['exit_code'], 2)
        self.assertEqual(c.convert_folder(plan)['exit_code'], 0)
        self.assertEqual(len(InlineWorker.calls), 1)

    def test_publication_file_interrupt_replays_identical_bytes(self):
        plan = self.plan()
        with self.interrupt('publication_file'), self.assertRaises(KeyboardInterrupt):
            c.convert_folder(plan)
        self.assertEqual(c.convert_folder(plan)['exit_code'], 0)
        self.assertEqual(len(InlineWorker.calls), 1)

    def test_prepared_tamper_stops(self):
        plan = self.plan()
        with self.interrupt('prepared'), self.assertRaises(KeyboardInterrupt):
            c.convert_folder(plan)
        intent = self.receipt()['sources'][0]['ranges'][0]['intent']
        staged = self.output.with_name(self.output.name + '.conversion-work') / intent['prepared'] / intent['files'][0]['path']
        staged.write_text('tampered')
        with self.assertRaises(IntegrityError):
            c.convert_folder(plan)

    def test_source_options_and_accepted_output_drift_stop(self):
        plan = self.plan()
        result = c.convert_folder(plan)
        with self.assertRaises(IntegrityError):
            c.convert_folder(plan, options={'threads': 2})
        reading = self.output / result['sources'][0]['item_relative'] / 'reading.md'
        saved = reading.read_bytes()
        reading.write_text('edited')
        with self.assertRaises(IntegrityError):
            c.convert_folder(plan)
        reading.write_bytes(saved)
        (self.root / 'text # %.txt').write_text('changed')
        with self.assertRaises(IntegrityError):
            c.convert_folder(plan)

    def test_untracked_output_and_symlink_stop(self):
        plan = self.plan()
        c.convert_folder(plan)
        (self.output / 'untracked.txt').write_text('unexpected')
        with self.assertRaises(IntegrityError):
            c.convert_folder(plan)
        (self.output / 'untracked.txt').unlink()
        link = self.output / 'alias'
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(IntegrityError):
            c.convert_folder(plan)

    def test_two_writers_refused(self):
        plan = self.plan()
        with c._lock(self.output):
            with self.assertRaises(IntegrityError):
                c.convert_folder(plan)

    def test_diskfull_is_fatal_not_content_gap(self):
        plan = self.plan()
        with patch.object(c, 'atomic_copy', side_effect=OSError(errno.ENOSPC, 'synthetic disk full')):
            with self.assertRaises(OSError) as caught:
                c.convert_folder(plan)
        self.assertEqual(caught.exception.errno, errno.ENOSPC)
        self.assertEqual(self.receipt()['sources'][0]['state'], 'pending')
        self.assertEqual(c.convert_folder(plan)['exit_code'], 0)

    def test_content_gap_continues_other_source_and_ordinary_resume_no_retry(self):
        plan = self.plan()
        (self.root / 'z.txt').write_text('gamma\n')
        plan = build_folder_plan(self.root, self.output)
        InlineWorker.fail = {'line:2'}
        result = c.convert_folder(plan)
        self.assertEqual(result['exit_code'], 2)
        self.assertEqual([s['state'] for s in result['sources']], ['partial', 'complete'])
        self.assertEqual(len(InlineWorker.calls), 2)
        c.convert_folder(plan)
        self.assertEqual(len(InlineWorker.calls), 2)

    def test_retry_failed_preserves_prior_bytes(self):
        plan = self.plan()
        InlineWorker.fail = {'line:2'}
        result = c.convert_folder(plan)
        original_entry = result['sources'][0]
        old = self.output / original_entry['item_relative']
        old_bytes = {r['path']: (old / r['path']).read_bytes() for r in original_entry['files']}
        InlineWorker.fail = set()
        # TXT sentinel emits all original lines; range selection is structural.
        result = c.convert_folder(plan, retry_failed=True)
        self.assertEqual(result['exit_code'], 0)
        self.assertEqual(old_bytes, {p: (old / p).read_bytes() for p in old_bytes})
        self.assertNotEqual(result['sources'][0]['item_relative'], original_entry['item_relative'])

    def test_frozen_added_files_do_not_expand_plan(self):
        plan = self.plan()
        c.convert_folder(plan)
        (self.root / 'new.txt').write_text('new\n')
        result = c.convert_folder(plan)
        self.assertEqual(len(result['sources']), 1)
        self.assertFalse((self.output / 'Originals/new.txt').exists())

    def test_archive_jobs_resume_frozen_scope_and_defer_authorization(self):
        for name in ('a', 'b', 'deferred'):
            folder = self.root / name
            folder.mkdir()
            (folder / 'file.txt').write_text(name + '\n')
        plan = build_archive_plan(self.root, bindings=[
            {'name': n, 'source_root': n, 'destination': str(self.base / ('out-' + n)), 'scope_state': 'defer' if n == 'deferred' else 'ready',
             'prerequisites': ['job-0001-01'] if n == 'b' else []}
            for n in ('a', 'b', 'deferred')])
        with self.interrupt('prepared'), self.assertRaises(KeyboardInterrupt):
            c.run_archive_plan(plan)
        result = c.run_archive_plan(plan)
        self.assertEqual([r['state'] for r in result['jobs']], ['complete', 'complete', 'deferred'])
        self.assertEqual(len(InlineWorker.calls), 2)
        (self.root / 'a/new.txt').write_text('late')
        c.run_archive_plan(plan)
        self.assertEqual(len(InlineWorker.calls), 2)

    def test_relocation_needs_neither_source_nor_work_directory(self):
        plan = self.plan()
        c.convert_folder(plan)
        moved = self.base / 'relocated'
        self.output.rename(moved)
        self.root.rename(self.base / 'hidden-source')
        previous_directory = Path.cwd()
        os.chdir(self.base)
        try:
            self.assertEqual(verify_bundle(moved)['exit_code'], 0)
        finally:
            os.chdir(previous_directory)

    def test_root_views_publication_interrupt_replays_pinned_names(self):
        plan = self.plan()
        hit = []
        def callback(event, receipt):
            if event == "publication_file" and receipt.get("views_intent") and not hit:
                hit.append(True)
                raise KeyboardInterrupt()
        with patch.object(c, "_checkpoint_hook", callback), self.assertRaises(KeyboardInterrupt):
            c.convert_folder(plan)
        self.assertEqual(verify_bundle(self.output)["exit_code"], 2)
        self.assertEqual(c.convert_folder(plan)["exit_code"], 0)
        self.assertEqual(len(InlineWorker.calls), 1)

    def test_root_views_refresh_interrupt_replays_prior_or_prepared_hash(self):
        plan = self.plan()
        InlineWorker.fail = {"line:2"}
        first = c.convert_folder(plan)
        old = self.output / first["sources"][0]["item_relative"] / "reading.md"
        old_bytes = old.read_bytes()
        InlineWorker.fail = set()
        hit = []
        def callback(event, receipt):
            intent = receipt.get("views_intent")
            if event == "publication_file" and intent and receipt.get("view_signature") != intent["signature"] and not hit:
                hit.append(True)
                raise KeyboardInterrupt()
        with patch.object(c, "_checkpoint_hook", callback), self.assertRaises(KeyboardInterrupt):
            c.convert_folder(plan, retry_failed=True)
        self.assertEqual(verify_bundle(self.output)["exit_code"], 2)
        self.assertEqual(c.convert_folder(plan)["exit_code"], 0)
        self.assertEqual(old.read_bytes(), old_bytes)
        self.assertEqual(len(InlineWorker.calls), 2)

    def test_initial_receipt_hash_pin_unavailable_is_gap(self):
        plan = self.plan()
        plan["sources"][0]["sha256"] = checksum(self.root / "text # %.txt")
        (self.root / "text # %.txt").unlink()
        result = c.convert_folder(plan)
        self.assertEqual(result["exit_code"], 2)
        self.assertEqual(result["sources"][0]["state"], "unavailable")
        self.assertFalse(InlineWorker.calls)

    def test_unavailable_image_recovers_without_placeholder_runtime(self):
        from PIL import Image
        Image.new("RGB", (40, 30), "white").save(self.root / "page.png")
        plan = build_folder_plan(self.root, self.output)
        with patch.object(c, "prepare_source", side_effect=RuntimeError("synthetic unavailable")):
            first = c.convert_folder(plan)
        self.assertEqual(first["exit_code"], 2)
        self.assertNotIn("runtime", first)
        with patch.object(c, "preflight", side_effect=ready), patch.object(c, "_Worker", NumericWorker):
            result = c.convert_folder(plan, retry_failed=True)
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["runtime_requirement"], "ocr")

    def test_legacy_placeholder_with_pinned_pending_image_recovers(self):
        from PIL import Image
        Image.new("RGB", (40, 30), "white").save(self.root / "page.png")
        plan = build_folder_plan(self.root, self.output)
        with patch.object(c, "prepare_source", side_effect=RuntimeError("synthetic unavailable")):
            c.convert_folder(plan)
        receipt = self.receipt()
        receipt["runtime"] = copy.deepcopy(c._TEXT_RUNTIME)
        receipt["sources"][0].update(source=c.prepare_source(self.root, plan["sources"][0]),
                                     state="pending", intake_pinned=True)
        (self.output / "conversion.json").write_text(json.dumps(receipt))
        with patch.object(c, "preflight", side_effect=ready), patch.object(c, "_Worker", NumericWorker):
            result = c.convert_folder(plan, retry_failed=True)
            pins = {r["path"]: checksum(self.output / r["path"]) for r in c._accepted_files(result)}
            self.assertEqual(c.convert_folder(plan)["exit_code"], 0)
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["runtime_history"][0]["identity"], c._TEXT_RUNTIME)
        self.assertEqual(len(NumericWorker.calls), 1)
        self.assertEqual(pins, {p: checksum(self.output / p) for p in pins})

    def test_completed_text_survives_recovered_image_runtime_upgrade(self):
        from PIL import Image
        plan = self.plan()
        Image.new("RGB", (40, 30), "white").save(self.root / "page.png")
        plan = build_folder_plan(self.root, self.output)
        real_prepare = c.prepare_source
        def available_text(root, row, **kw):
            if row["relative_path"] == "page.png":
                raise RuntimeError("synthetic unavailable image")
            return real_prepare(root, row, **kw)
        with patch.object(c, "prepare_source", side_effect=available_text):
            first = c.convert_folder(plan)
        text = next(e for e in first["sources"] if e["source"]["kind"] == "txt")
        pins = {r["path"]: (self.output / text["item_relative"] / r["path"]).read_bytes() for r in text["files"]}
        with patch.object(c, "preflight", side_effect=ready), patch.object(c, "_Worker", NumericWorker):
            second = c.convert_folder(plan, retry_failed=True)
        self.assertEqual(second["exit_code"], 0)
        self.assertEqual(pins, {p: (self.output / text["item_relative"] / p).read_bytes() for p in pins})
        self.assertEqual([name for name, _ in NumericWorker.calls], ["page.png"])

    def test_runtime_requirement_upgrade_checks_old_identity_and_never_downgrades(self):
        package = {"ready": True, "versions": {"synthetic": "1"}, "models": []}
        ocr = {**package, "models": [{"sha256": "a" * 64}]}
        entry = {"source": {"kind": "image"}, "state": "pending", "parts": []}
        receipt = {"runtime": package, "runtime_requirement": "package", "sources": [entry]}
        def identities(opts, require_models):
            return copy.deepcopy(ocr if require_models else package)
        with patch.object(c, "preflight", side_effect=identities):
            c._pin_runtime(receipt, c.BackendOptions(), [entry])
            self.assertEqual(receipt["runtime"], ocr)
            c._pin_runtime(receipt, c.BackendOptions(), [])
            self.assertEqual(receipt["runtime_requirement"], "ocr")
        changed = {**package, "versions": {"synthetic": "2"}}
        with patch.object(c, "preflight", return_value=changed), self.assertRaises(IntegrityError):
            c._pin_runtime({"runtime": package, "runtime_requirement": "package", "sources": [entry]}, c.BackendOptions(), [entry])
        entry["ranges"] = [{"render": {"asset": "already-rendered.png"}}]
        with self.assertRaises(IntegrityError):
            c._pin_runtime({"runtime": copy.deepcopy(c._TEXT_RUNTIME), "sources": [entry]}, c.BackendOptions(), [entry])

    def test_unknown_context_is_filed_gap_without_models_or_inference(self):
        plan = self.plan(text="text")
        source = self.root / "text # %.txt"
        source.write_bytes(b"\x00UNKNOWN\x00")
        plan = build_folder_plan(self.root, self.output)
        with patch.object(c, "preflight", side_effect=AssertionError("context needs no models")):
            result = c.convert_folder(plan)
        self.assertEqual(result["exit_code"], 2)
        self.assertEqual(result["sources"][0]["source"]["kind"], "unknown")
        self.assertFalse(InlineWorker.calls)
        self.assertEqual((self.output / "Originals" / source.name).read_bytes(), source.read_bytes())
        self.assertIn("Unsupported/context", (self.output / "issues.md").read_text())


    def test_ranges_sixteen_and_retry_only_failed_physical_units(self):
        # Keep actual TXT line count consistent with the synthetic scheduler
        # inventory so storage accounting remains independently checkable.
        plan = self.plan(text="synthetic unit\n" * 33)
        inventory = {"expected_count": 33, "unit_kind": "package", "units": [{"unit_id": n} for n in range(1, 34)], "count_method": "synthetic independent numbered fixture", "count_warnings": []}
        # Synthetic numeric package inventory avoids introducing image fixtures
        # into a test of the scheduling and immutable acceptance contract.
        with patch.object(c, "_Worker", NumericWorker), patch.object(c, "inspect_source", return_value=inventory), patch.object(c, "_ranges", return_value=[(1, 16, list(range(1, 17))), (17, 32, list(range(17, 33))), (33, 33, [33])]):
            NumericWorker.fail = {18}
            first = c.convert_folder(plan)
            self.assertEqual([ids for _, ids in NumericWorker.calls], [list(range(1, 17)), list(range(17, 33)), [33]])
            old = self.output / first["sources"][0]["item_relative"]
            pins = {r["path"]: (old / r["path"]).read_bytes() for r in first["sources"][0]["files"]}
            NumericWorker.fail = set()
            second = c.convert_folder(plan, retry_failed=True)
            self.assertEqual(NumericWorker.calls[-1][1], [18])
            self.assertEqual(second["exit_code"], 0)
            self.assertEqual(pins, {path: (old / path).read_bytes() for path in pins})
        self.assertEqual(c._ranges({"units": [{"unit_id": n} for n in range(1, 34)]}, "pdf"), [(1, 16, list(range(1, 17))), (17, 32, list(range(17, 33))), (33, 33, [33])])

    def test_runtime_model_identity_drift_refused_before_inference(self):
        plan = self.plan()
        # A metadata-only context option triggers injectable runtime preflight.
        real_prepare = c.prepare_source
        def package_prepare(*args, **kwargs):
            value = real_prepare(*args, **kwargs)
            value["kind"] = "docx"
            return value
        with patch.object(c, "prepare_source", side_effect=package_prepare), patch.object(c, "preflight", side_effect=lambda opts, **kw: {"ready": True, "versions": {"synthetic": "1"}, "models": [{"sha256": "a" * 64}]}), self.interrupt("inference"), self.assertRaises(KeyboardInterrupt):
            c.convert_folder(plan)
        with patch.object(c, "prepare_source", side_effect=package_prepare), patch.object(c, "preflight", return_value={"ready": True, "versions": {"synthetic": "1"}, "models": [{"sha256": "b" * 64}]}):
            with self.assertRaises(IntegrityError):
                c.convert_folder(plan)
        self.assertFalse(InlineWorker.calls)

    def test_diskfull_mid_publication_replays_prepared_without_inference(self):
        plan = self.plan()
        real_copy = c.atomic_copy
        hit = []
        def limited(path, source):
            if "Items" in Path(path).parts and not hit:
                hit.append(True)
                raise OSError(errno.ENOSPC, "synthetic publication disk full")
            return real_copy(path, source)
        with patch.object(c, "atomic_copy", side_effect=limited), self.assertRaises(OSError):
            c.convert_folder(plan)
        self.assertEqual(len(InlineWorker.calls), 1)
        self.assertEqual(c.convert_folder(plan)["exit_code"], 0)
        self.assertEqual(len(InlineWorker.calls), 1)


    def test_retained_accepted_reading_pin_and_exact_bytes(self):
        plan = self.plan()
        original = self.root / "text # %.txt"
        accepted = self.base / "accepted.txt"
        accepted.write_bytes(b"ACCEPTED EXACT BYTES\n")
        prior = self.base / "prior-receipt.json"
        prior.write_text("{}")
        plan["sources"][0]["sha256"] = checksum(original)
        plan["sources"][0]["reuse"] = {"source_sha256": checksum(original), "text_path": str(accepted), "text_sha256": checksum(accepted), "receipt_path": str(prior), "receipt_sha256": checksum(prior), "gaps": [], "provenance": {}}
        result = c.convert_folder(plan)
        item = self.output / result["sources"][0]["item_relative"]
        self.assertEqual((item / "accepted.txt").read_bytes(), accepted.read_bytes())
        self.assertIn("ACCEPTED EXACT BYTES", (item / "reading.md").read_text())
        self.assertTrue((item / "pages.md").is_file())
        c.convert_folder(plan)
        self.assertEqual(len(InlineWorker.calls), 1)
        accepted.write_text("changed accepted reading")
        with self.assertRaises(IntegrityError):
            c.convert_folder(plan)

    def test_retry_accepted_part_interrupt_reuses_both_old_and_new_bytes(self):
        plan = self.plan()
        InlineWorker.fail = {"line:2"}
        c.convert_folder(plan)
        InlineWorker.fail = set()
        with self.interrupt("accepted"), self.assertRaises(KeyboardInterrupt):
            c.convert_folder(plan, retry_failed=True)
        self.assertEqual(c.convert_folder(plan)["exit_code"], 0)
        self.assertEqual(len(InlineWorker.calls), 2)

    def test_archive_dependencies_run_even_when_prerequisite_is_later(self):
        for name in ("a", "b"):
            folder = self.root / name
            folder.mkdir()
            (folder / "file.txt").write_text(name + "\n")
        plan = build_archive_plan(self.root, bindings=[
            {"name": "a", "source_root": "a", "destination": str(self.base / "out-a"), "scope_state": "ready", "prerequisites": ["job-0002-01"]},
            {"name": "b", "source_root": "b", "destination": str(self.base / "out-b"), "scope_state": "ready"}])
        result = c.run_archive_plan(plan)
        self.assertEqual([r["id"] for r in result["jobs"]], ["job-0002-01", "job-0001-01"])
        self.assertEqual(result["exit_code"], 0)


    def test_asset_budget_stops_before_original_copy_or_inference(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow is needed for synthetic image-header budgeting")
        Image.new("RGB", (1000, 700), "white").save(self.root / "page.png")
        plan = build_folder_plan(self.root, self.output)
        # Enough bytes for the compressed original; insufficient for images.
        free = (self.root / "page.png").stat().st_size * 10 + 2 * 1024 * 1024
        with patch.object(c, "preflight", side_effect=ready), patch.object(c.shutil, "disk_usage", return_value=type("Space", (), {"free": free})()), patch.object(c, "atomic_copy", side_effect=AssertionError("must budget before copies")), patch.object(c, "render_units", side_effect=AssertionError("must budget before rendering")):
            with self.assertRaises(OSError) as caught:
                c.convert_folder(plan)
        self.assertEqual(caught.exception.errno, errno.ENOSPC)
        estimate = self.receipt()["storage_estimate"]
        self.assertFalse(estimate["sufficient"])
        self.assertGreater(estimate["remaining_page_asset_bytes"], free)
        self.assertGreater(estimate["repair_reserve_bytes"], 0)
        self.assertFalse((self.output / "Originals").exists())
        self.assertFalse(InlineWorker.calls)

    def test_repair_reserve_is_required_even_when_assets_fit(self):
        plan = self.plan()
        unit_bytes = 4 * 1024 * 1024
        profile = {"source_sha256": None, "images_scale": 3, "units": [{"unit_id": "line:1", "bytes": unit_bytes}], "metadata_bytes": 1024, "unknown_count": False, "unknown_dimensions": False, "unknown_range_unit_bytes": unit_bytes, "warnings": []}
        def metadata(source, inventory, options):
            return {**profile, "source_sha256": source["sha256"]}
        free = unit_bytes * (c._STORAGE_ASSET_COPIES + 2) + 2 * 1024 * 1024
        with patch.object(c, "_storage_profile", side_effect=metadata), patch.object(c.shutil, "disk_usage", return_value=type("Space", (), {"free": free})()), patch.object(c, "atomic_copy", side_effect=AssertionError("repair reserve must precede copies")):
            with self.assertRaises(OSError) as caught:
                c.convert_folder(plan)
        self.assertEqual(caught.exception.errno, errno.ENOSPC)
        estimate = self.receipt()["storage_estimate"]
        self.assertGreaterEqual(free, estimate["remaining_original_bytes"] + estimate["remaining_page_asset_bytes"] + estimate["in_flight_range_bytes"] + estimate["metadata_and_views_bytes"])
        self.assertLess(free, estimate["required_free_bytes"])
        self.assertFalse(InlineWorker.calls)

    def test_low_storage_retry_preserves_all_accepted_bytes(self):
        plan = self.plan()
        InlineWorker.fail = {"line:2"}
        result = c.convert_folder(plan)
        pins = {row["path"]: (self.output / row["path"]).read_bytes() for row in c._accepted_files(result)}
        with patch.object(c.shutil, "disk_usage", return_value=type("Space", (), {"free": 1})()):
            with self.assertRaises(OSError) as caught:
                c.convert_folder(plan, retry_failed=True)
        self.assertEqual(caught.exception.errno, errno.ENOSPC)
        self.assertEqual(pins, {name: (self.output / name).read_bytes() for name in pins})
        self.assertEqual(len(InlineWorker.calls), 1)
        self.assertEqual(self.receipt()["sources"][0]["state"], "partial")

    def test_unknown_count_budget_records_only_bounded_range_reserve(self):
        plan = self.plan()
        inventory = {"expected_count": None, "unit_kind": "line", "units": [], "count_method": "synthetic unavailable count", "count_warnings": ["unknown total"]}
        with patch.object(c, "inspect_source", return_value=inventory), self.interrupt("inference"), self.assertRaises(KeyboardInterrupt):
            c.convert_folder(plan)
        receipt = self.receipt()
        estimate = receipt["storage_estimate"]
        self.assertEqual(estimate["unknown_count_or_dimensions"], ["text # %.txt"])
        self.assertGreater(estimate["in_flight_range_bytes"], 0)
        self.assertIn("not an unseen suffix", estimate["limitation"])
        self.assertIsNone(receipt["sources"][0]["inventory"]["expected_count"])
        self.assertFalse(InlineWorker.calls)


    def test_frozen_intake_lower_bound_stops_before_prepare_or_fetch(self):
        plan = self.plan()
        source = plan["sources"][0]
        source["locally_available"] = False  # Synthetic recorded data-less flag.
        size = source["size"]
        free = size * 2 + c._STORAGE_METADATA_BYTES - 1
        with patch.object(c.shutil, "disk_usage", return_value=type("Space", (), {"free": free})()), patch.object(c, "prepare_source", side_effect=AssertionError("must check frozen budget before intake/fetch")), patch.object(c, "inspect_source", side_effect=AssertionError("no new input probes before lower-bound check")):
            with self.assertRaises(OSError) as caught:
                c.convert_folder(plan, fetch_inputs=True)
        self.assertEqual(caught.exception.errno, errno.ENOSPC)
        bound = self.receipt()["intake_storage_lower_bound"]
        self.assertEqual(bound["remaining_original_bytes"], size)
        self.assertEqual(bound["recorded_dataless_materialization_bytes"], size)
        self.assertEqual(bound["required_free_bytes"], free + 1)
        self.assertFalse(bound["sufficient"])
        self.assertNotIn("storage_estimate", self.receipt())
        self.assertFalse((self.output / "Originals").exists())
        self.assertFalse(InlineWorker.calls)



class IsolatedWorkerTests(unittest.TestCase):
    def test_crash_restarts_once_then_finite_failure(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(c, '_worker_main', crash_worker):
            directory = Path(temp) / 'attempt'
            directory.mkdir()
            worker = c._Worker({})
            try:
                response = worker.request({'directory': str(directory)})
                self.assertFalse(response['ok'])
                self.assertIn('budget exhausted', response['error'])
                self.assertEqual(worker.restarts, 1)
            finally:
                worker.close()

    def test_watchdog_terminates_worker(self):
        with patch.object(c, '_worker_main', sleep_worker), patch.object(c, 'WORKER_WATCHDOG', 0.1):
            worker = c._Worker({})
            start = time.monotonic()
            try:
                response = worker.request({})
                self.assertFalse(response['ok'])
                self.assertIn('watchdog', response['error'])
                self.assertLess(time.monotonic() - start, 5)
                self.assertIsNone(worker.process)
            finally:
                worker.close()


@unittest.skipUnless(os.environ.get('AGENT_TRANSCRIBE_TEST_MODELS'), 'set AGENT_TRANSCRIBE_TEST_MODELS for real isolated offline conversion')
class RealFolderConversionTests(unittest.TestCase):
    def test_real_isolated_pdf_tiff_docx_text_bundle_and_resume(self):
        from PIL import Image, ImageDraw, ImageFont
        from docx import Document
        from reportlab.pdfgen.canvas import Canvas
        with tempfile.TemporaryDirectory(prefix='real-folder-') as temporary:
            base = Path(temporary).resolve()
            root, output = base / 'originals', base / 'portable'
            root.mkdir()
            canvas = Canvas(str(root / 'pages.pdf'), pagesize=(450, 600))
            for number in (1, 2):
                canvas.drawString(40, 500, f'SYNTHETIC ORIGINAL PAGE {number}')
                canvas.showPage()
            canvas.save()
            frames = []
            font = ImageFont.truetype('/System/Library/Fonts/Supplemental/Arial.ttf', 40)
            for label in ('FRAME ONE ALPHA', '', 'FRAME THREE GAMMA'):
                image = Image.new('RGB', (900, 400), 'white')
                ImageDraw.Draw(image).text((40, 100), label, font=font, fill='black')
                frames.append(image)
            frames[0].save(root / 'frames.tiff', save_all=True, append_images=frames[1:], dpi=(144, 144))
            doc = Document()
            doc.add_heading('SYNTHETIC DOCX HEADING', 1)
            doc.add_paragraph('Structural package text')
            doc.save(root / 'package.docx')
            (root / 'lines.txt').write_text('FIRST LINE\nSECOND LINE\n')
            plan = build_folder_plan(root, output)
            original_hashes = {p.name: checksum(p) for p in root.iterdir()}
            result = c.convert_folder(plan, options={'artifacts_path': os.environ['AGENT_TRANSCRIBE_TEST_MODELS']})
            self.assertEqual(result['exit_code'], 0, result['verification'])
            self.assertEqual(len(result['sources']), 4)
            tiff = next(e for e in result['sources'] if e['source']['kind'] == 'tiff')
            self.assertEqual([r['unit_id'] for r in tiff['map']['units']], [1, 2, 3])
            self.assertIn('ALPHA', tiff['map']['units'][0]['text'])
            self.assertIn('GAMMA', tiff['map']['units'][2]['text'])
            self.assertTrue(tiff['map']['units'][1]['render']['asset'])
            self.assertTrue(list(output.rglob('document.json')))
            pins = {row['path']: checksum(output / row['path']) for row in c._accepted_files(result)}
            with patch.object(c._Worker, 'request', side_effect=AssertionError('accepted range must not infer again')):
                self.assertEqual(c.convert_folder(plan, options={'artifacts_path': os.environ['AGENT_TRANSCRIBE_TEST_MODELS']})['exit_code'], 0)
            self.assertEqual(pins, {name: checksum(output / name) for name in pins})
            self.assertEqual(original_hashes, {p.name: checksum(p) for p in root.iterdir()})
            moved = base / 'relocated'
            output.rename(moved)
            root.rename(base / 'hidden originals')
            self.assertEqual(verify_bundle(moved)['exit_code'], 0)


if __name__ == '__main__':
    unittest.main()
