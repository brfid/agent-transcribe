"""Supported reading corrections on disposable filed documents, never archives."""
import fcntl
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_transcribe import filing
from agent_transcribe.common import IntegrityError, checksum


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.originals = self.root / "originals"
        self.texts = self.root / "raw"
        self.destination = self.root / "organized"
        self.originals.mkdir()
        self.texts.mkdir()
        assignments = []
        for index in (1, 2):
            name = f"IMG_{index:04d}.jpg"
            source = self.originals / name
            source.write_bytes(b"Original embedded image and metadata " + name.encode())
            os.utime(source, ns=(1234567890123456789 + index, 1234567890123456789 + index))
            extract = self.texts / (name + ".txt")
            extract.write_text(f"OLD READING {index}\n")
            assignments.append({"source": name, "document": "Filed packet", "description": "Packet",
                "disposition": "document", "source_sha256": checksum(source), "capture_order": index,
                "reading_order": 3 - index, "reading_order_evidence": "Visible page numbers inspected",
                "preferred_text": {"path": extract.name, "sha256": checksum(extract)},
                "gap_reason": "Earlier OCR missed words" if index == 1 else None})
        recipe = self.root / "assignments.json"
        recipe.write_text(json.dumps({"schema": 1, "source_root": "originals", "text_root": "raw", "assignments": assignments}))
        filing.file_assignments(recipe, self.destination)
        self.folder = self.destination / "Filed packet"
        readme = self.folder / "README.md"
        readme.write_text("Human document title and dated review evidence.\n\n" + readme.read_text() + "\nAn unresolved human question.\n")
        self.before = self.state()
        self.receipt_bytes = (self.folder / filing.RECEIPT).read_bytes()
        self.corrected = self.texts / "corrected.txt"
        self.corrected.write_text("CORRECTED READING ONE\n")

    def state(self):
        return json.loads((self.folder / filing.RECEIPT).read_text())

    def recipe(self, changes=None, **extra):
        value = {"schema": 1, "expected_receipt_sha256": checksum(self.folder / filing.RECEIPT),
                 "reason": "Replace incomplete recognition with a retained correction",
                 "evidence": "Source image passage and corrected extraction inspected",
                 "text_root": "raw", "changes": changes if changes is not None else [{"source": "IMG_0001.jpg",
                     "preferred_text": {"path": "corrected.txt", "sha256": checksum(self.corrected)},
                     "gap_reason": None, "gap_clear_reason": "Corrected passage covers the previously omitted words"}]}
        value.update(extra)
        path = self.root / "changes.json"
        path.write_text(json.dumps(value))
        return path

    def apply(self, recipe=None, **kwargs):
        return filing.reconcile_document(self.folder, recipe or self.recipe(), **kwargs)

    def snapshot(self, paths):
        return {str(path): (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}

    def test_corrected_reading_and_gap_preserve_sources_copies_raw_order_prose_and_history(self):
        paths = list(self.originals.iterdir()) + list(self.texts.iterdir()) + list((self.folder / "Scans").iterdir())
        preserved = self.snapshot(paths)
        old_export = (self.folder / self.before["sources"][0]["text"]).read_bytes()
        old_combined = (self.folder / filing.COMBINED).read_bytes()
        recipe = self.recipe()
        recipe_bytes = recipe.read_bytes()
        result = self.apply(recipe)
        self.assertTrue(result["reconciliation_complete"])
        self.assertEqual(result["reconciled_sources"], 1)
        self.assertEqual(result["text_gaps"], 0)
        self.assertEqual(self.snapshot(paths), preserved)
        after = self.state()
        mutable = {"preferred_text", "complementary_text", "gap_reason", "text_sha256"}
        self.assertEqual({k: v for k, v in self.before["sources"][0].items() if k not in mutable},
                         {k: v for k, v in after["sources"][0].items() if k not in mutable})
        self.assertEqual(after["sources"][1], self.before["sources"][1])
        combined = (self.folder / filing.COMBINED).read_text()
        self.assertLess(combined.index("OLD READING 2"), combined.index("CORRECTED READING ONE"))
        self.assertNotIn("OLD READING 1", combined)
        readme = (self.folder / "README.md").read_text()
        self.assertIn("Human document title", readme)
        self.assertIn("unresolved human question", readme)
        history = self.folder / ".filing-history"
        self.assertIn(old_export, [p.read_bytes() for p in history.rglob("*.txt")])
        self.assertIn(old_combined, [p.read_bytes() for p in history.rglob("*.txt")])
        self.assertIn(self.receipt_bytes, [p.read_bytes() for p in history.rglob(filing.RECEIPT)])
        self.assertTrue(any("Human document title" in p.read_text() for p in history.rglob("README-*.md")))
        event = after["reconciliations"][-1]
        self.assertEqual((self.folder / event["recipe"]).read_bytes(), recipe_bytes)
        self.assertIn("corrected extraction", event["evidence"])
        self.assertTrue(filing.validate_document(self.folder, verify_sources=True)["valid"])
        self.assertTrue(self.apply(recipe)["repeated"])

    def test_gap_only_update_retains_preferred_and_complementary_reading(self):
        alternate = self.texts / "alternate.txt"
        alternate.write_text("ALTERNATE RETAINED READING")
        self.apply(self.recipe([{"source": "IMG_0001.jpg", "complementary_text": [{"path": alternate.name, "sha256": checksum(alternate)}]}]))
        accepted = self.state()["sources"][0]
        self.apply(self.recipe([{"source": "IMG_0001.jpg", "gap_reason": "Footnote remains illegible"}]))
        row = self.state()["sources"][0]
        self.assertEqual(row["preferred_text"], accepted["preferred_text"])
        self.assertEqual(row["complementary_text"], accepted["complementary_text"])
        text = (self.folder / row["text"]).read_text()
        self.assertIn("OLD READING 1", text)
        self.assertIn("ALTERNATE RETAINED READING", text)
        self.assertIn("Footnote remains illegible", text)

    def test_missing_and_duplicate_and_out_of_scope_edits_are_refused(self):
        cases = [[], [{"source": "IMG_0001.jpg"}],
            [{"source": "IMG_0001.jpg", "gap_reason": "One"}] * 2,
            [{"source": "not-filed.jpg", "gap_reason": "Missing"}],
            [{"source": "../outside.jpg", "gap_reason": "Missing"}],
            [{"gap_reason": "No source"}],
            [{"source": "IMG_0001.jpg", "reading_order": 1, "gap_reason": "No"}],
            [{"source": "IMG_0001.jpg", "source_sha256": "0" * 64, "gap_reason": "No"}],
            [{"source": "IMG_0001.jpg", "disposition": "context", "gap_reason": "No"}],
            [{"source": "IMG_0001.jpg", "copy": "Other.jpg", "gap_reason": "No"}]]
        for changes in cases:
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    self.apply(self.recipe(changes))
                self.assertEqual((self.folder / filing.RECEIPT).read_bytes(), self.receipt_bytes)
                self.assertFalse((self.folder / ".filing.pending.json").exists())

    def test_required_recipe_pins_reason_evidence_and_strict_readings(self):
        for field, value in [("schema", 2), ("expected_receipt_sha256", None), ("reason", " "),
                             ("evidence", []), ("text_root", "/absolute/root"), ("unknown", True)]:
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    self.apply(self.recipe(**{field: value}))
        for choice in ({"path": "corrected.txt"}, {"path": "corrected.txt", "sha256": "bad"},
                       {"path": "corrected.txt", "sha256": checksum(self.corrected), "unknown": True}, {}):
            with self.subTest(choice=choice):
                with self.assertRaises(ValueError):
                    self.apply(self.recipe([{"source": "IMG_0001.jpg", "preferred_text": choice}]))

    def test_gap_clearing_and_missing_or_blank_readings_need_explicit_support(self):
        blank = self.texts / "blank.txt"
        blank.write_text(" \n")
        cases = [{"source": "IMG_0001.jpg", "gap_reason": None},
                 {"source": "IMG_0002.jpg", "preferred_text": None},
                 {"source": "IMG_0001.jpg", "gap_reason": "", "gap_clear_reason": "Guess"},
                 {"source": "IMG_0001.jpg", "preferred_text": {"path": "blank.txt", "sha256": checksum(blank)},
                  "gap_reason": None, "gap_clear_reason": "Unsupported claim"},
                 {"source": "IMG_0001.jpg", "gap_reason": "Still partial", "gap_clear_reason": "Unrelated"}]
        for change in cases:
            with self.subTest(change=change):
                with self.assertRaises(ValueError):
                    self.apply(self.recipe([change]))
                self.assertEqual((self.folder / filing.RECEIPT).read_bytes(), self.receipt_bytes)
        result = self.apply(self.recipe([{"source": "IMG_0001.jpg", "preferred_text": None,
                                        "gap_reason": "Recognition withdrawn after image review"}]))
        self.assertEqual(result["text_gaps"], 1)
        self.assertIn("NO PREFERRED EXTRACTED TEXT", (self.folder / self.state()["sources"][0]["text"]).read_text())

    def test_duplicate_readings_managed_output_alias_and_symlink_refused(self):
        choice = {"path": "corrected.txt", "sha256": checksum(self.corrected)}
        with self.assertRaisesRegex(ValueError, "Repeated"):
            self.apply(self.recipe([{"source": "IMG_0001.jpg", "preferred_text": choice, "complementary_text": [choice]}]))
        export = self.folder / self.before["sources"][0]["text"]
        with self.assertRaisesRegex(ValueError, "managed"):
            self.apply(self.recipe([{"source": "IMG_0001.jpg", "preferred_text": {"path": self.before["sources"][0]["text"], "sha256": checksum(export)}}],
                                   text_root="organized/Filed packet"))
        linked = self.texts / "link.txt"
        linked.symlink_to(self.corrected)
        with self.assertRaises(IntegrityError):
            self.apply(self.recipe([{"source": "IMG_0001.jpg", "preferred_text": {"path": linked.name, "sha256": checksum(self.corrected)}}]))

    def test_stale_selection_and_completed_receipt_edit_refused(self):
        recipe = self.recipe(expected_receipt_sha256="0" * 64)
        with self.assertRaisesRegex(IntegrityError, "Stale"):
            self.apply(recipe)
        recipe = self.recipe()
        self.apply(recipe)
        current = self.state()
        current["unrelated_edit"] = "Do not silently accept a changed receipt on repeat"
        (self.folder / filing.RECEIPT).write_text(json.dumps(current))
        before = self.snapshot([self.folder / row["text"] for row in current["sources"]])
        with self.assertRaisesRegex(IntegrityError, "receipt changed"):
            self.apply(recipe)
        self.assertEqual(self.snapshot([self.folder / row["text"] for row in current["sources"]]), before)

    def test_changed_original_new_reading_and_any_existing_output_stop_before_publication(self):
        recipe = self.recipe()
        paths = [self.originals / "IMG_0001.jpg", self.corrected,
                 self.folder / self.before["sources"][0]["text"], self.folder / self.before["sources"][1]["text"],
                 self.folder / filing.COMBINED, self.folder / self.before["sources"][1]["copy"]]
        for path in paths:
            with self.subTest(path=path):
                original, timestamp = path.read_bytes(), path.stat().st_mtime_ns
                path.write_bytes(b"External edit preserved")
                with patch.object(filing, "_publish_file", side_effect=AssertionError("Publication must not begin")):
                    with self.assertRaises(IntegrityError):
                        self.apply(recipe)
                self.assertEqual(path.read_bytes(), b"External edit preserved")
                self.assertFalse((self.folder / ".filing.pending.json").exists())
                path.write_bytes(original)
                os.utime(path, ns=(timestamp, timestamp))

    def test_exact_fetch_scope_and_control_before_read(self):
        recipe = self.recipe()
        fetched = []
        actual_read = Path.read_bytes
        def read_bytes(path):
            if path == recipe:
                self.assertIn(recipe, fetched)
            return actual_read(path)
        def intake(paths):
            self.assertTrue(all(path.is_file() for path in paths))
            fetched.extend(paths)
            return {"success": True, "paths": [str(path) for path in paths]}, True
        with patch.object(filing, "fetch", side_effect=intake), patch.object(Path, "read_bytes", read_bytes):
            self.apply(recipe, fetch_inputs=True)
        self.assertEqual(fetched[0], recipe)
        self.assertIn(self.folder / filing.RECEIPT, fetched)
        self.assertIn(self.folder / "README.md", fetched)
        self.assertIn(self.originals / "IMG_0001.jpg", fetched)
        self.assertNotIn(self.originals / "IMG_0002.jpg", fetched)
        self.assertNotIn(self.texts / "IMG_0002.jpg.txt", fetched)
        self.assertTrue(all(path != self.folder and path != self.root for path in fetched))

    def interrupt(self, recipe, target_name=filing.COMBINED):
        actual = filing._publish_file
        def stop(target, *args, **kwargs):
            if target.name == target_name:
                raise OSError("Simulated interruption")
            return actual(target, *args, **kwargs)
        with patch.object(filing, "_publish_file", side_effect=stop):
            with self.assertRaisesRegex(OSError, "interruption"):
                self.apply(recipe)
        self.assertTrue((self.folder / ".filing.pending.json").exists())

    def test_interrupted_reconciliation_resumes_prepared_bytes_and_late_prose(self):
        recipe = self.recipe()
        self.interrupt(recipe)
        readme = self.folder / "README.md"
        readme.write_text(readme.read_text() + "\nHuman note added during interruption.\n")
        with patch.object(filing, "_text_export", side_effect=AssertionError("Must not rederive prepared readings")):
            result = self.apply(recipe)
        self.assertTrue(result["resumed"])
        self.assertIn("note added during interruption", readme.read_text())
        self.assertFalse((self.folder / ".filing.pending.json").exists())
        self.assertTrue(filing.validate_document(self.folder)["valid"])

    def test_pending_recipe_change_unrelated_recipe_and_changed_input_refused(self):
        recipe = self.recipe()
        recipe_bytes = recipe.read_bytes()
        self.interrupt(recipe)
        pending_bytes = (self.folder / ".filing.pending.json").read_bytes()
        recipe.write_bytes(recipe_bytes + b"\n")
        with self.assertRaisesRegex(IntegrityError, "same identified"):
            self.apply(recipe)
        recipe.write_bytes(recipe_bytes)
        reading_bytes = self.corrected.read_bytes()
        self.corrected.write_text("Changed after preparation")
        with self.assertRaises(IntegrityError):
            self.apply(recipe)
        self.assertEqual((self.folder / ".filing.pending.json").read_bytes(), pending_bytes)
        self.corrected.write_bytes(reading_bytes)
        self.assertTrue(self.apply(recipe)["resumed"])

    def test_pending_stale_receipt_and_later_output_conflict_preflight_all_targets(self):
        recipe = self.recipe()
        with patch.object(filing, "_publish_file", side_effect=OSError("Before publication")):
            with self.assertRaises(OSError):
                self.apply(recipe)
        pending_path = self.folder / ".filing.pending.json"
        pending = json.loads(pending_path.read_text())
        targets = [self.folder / record["target"] for record in pending["outputs"]]
        original = (self.folder / filing.RECEIPT).read_bytes()
        (self.folder / filing.RECEIPT).write_bytes(original + b"\n")
        with patch.object(filing, "_publish_file", side_effect=AssertionError("Must preflight receipt")):
            with self.assertRaises(IntegrityError):
                self.apply(recipe)
        (self.folder / filing.RECEIPT).write_bytes(original)
        combined = self.folder / filing.COMBINED
        combined.write_text("Manual combined correction")
        with patch.object(filing, "_publish_file", side_effect=AssertionError("Must preflight later target")):
            with self.assertRaises(IntegrityError):
                self.apply(recipe)
        self.assertFalse(targets[0].exists())
        self.assertEqual(combined.read_text(), "Manual combined correction")

    def test_pending_unchanged_output_and_copy_metadata_changes_block_resume(self):
        recipe = self.recipe()
        self.interrupt(recipe)
        untouched = self.folder / self.before["sources"][1]["text"]
        original = untouched.read_bytes()
        untouched.write_text("External untouched export edit")
        with patch.object(filing, "_publish_file", side_effect=AssertionError("Retained guard must run first")):
            with self.assertRaises(IntegrityError):
                self.apply(recipe)
        untouched.write_bytes(original)
        copied = self.folder / self.before["sources"][1]["copy"]
        stamp = copied.stat().st_mtime_ns
        os.utime(copied, ns=(stamp + 100, stamp + 100))
        with self.assertRaisesRegex(IntegrityError, "modification time"):
            self.apply(recipe)
        os.utime(copied, ns=(stamp, stamp))
        self.assertTrue(self.apply(recipe)["resumed"])

    def test_concurrent_writer_lock_refuses_without_publication(self):
        recipe = self.recipe()
        with (self.folder / ".filing.lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(IntegrityError, "Another filing writer"):
                self.apply(recipe)
        self.assertEqual((self.folder / filing.RECEIPT).read_bytes(), self.receipt_bytes)
        self.assertFalse((self.folder / ".filing.pending.json").exists())

    def test_fully_committed_receipt_with_pending_checkpoint_resumes_safely(self):
        recipe = self.recipe()
        actual_unlink = Path.unlink
        pending_path = self.folder / ".filing.pending.json"
        def unlink(path, *args, **kwargs):
            if path == pending_path:
                raise OSError("Interrupted after receipt commit")
            return actual_unlink(path, *args, **kwargs)
        with patch.object(Path, "unlink", unlink):
            with self.assertRaisesRegex(OSError, "receipt commit"):
                self.apply(recipe)
        self.assertTrue(pending_path.exists())
        self.assertNotEqual((self.folder / filing.RECEIPT).read_bytes(), self.receipt_bytes)
        with patch.object(filing, "_text_export", side_effect=AssertionError("Prepared bytes only")):
            self.assertTrue(self.apply(recipe)["resumed"])
        self.assertTrue(filing.validate_document(self.folder)["valid"])

    def test_corrupt_prepared_bytes_and_changed_managed_map_stop_resume(self):
        recipe = self.recipe()
        self.interrupt(recipe)
        pending = json.loads((self.folder / ".filing.pending.json").read_text())
        prepared = self.folder / pending["stage"] / pending["outputs"][-1]["staged"]
        original = prepared.read_bytes()
        prepared.write_text("Altered prepared result pin")
        with patch.object(filing, "_publish_file", side_effect=AssertionError("All staged files checked first")):
            with self.assertRaises(IntegrityError):
                self.apply(recipe)
        prepared.write_bytes(original)
        readme = self.folder / "README.md"
        prose = readme.read_text()
        readme.write_text(prose.replace("| 0001", "| 9999"))
        with self.assertRaisesRegex(IntegrityError, "membership"):
            self.apply(recipe)
        readme.write_text(prose)
        self.assertTrue(self.apply(recipe)["resumed"])

    def test_cloud_recipe_without_fetch_is_refused_before_read(self):
        cloud = self.root / "com~apple~CloudDocs"
        cloud.mkdir()
        recipe = cloud / "changes.json"
        recipe.write_text("Deliberately invalid unread intake")
        with self.assertRaisesRegex(ValueError, "fetch_inputs"):
            self.apply(recipe)

    def append_recipe(self):
        source = self.originals / "IMG_0003.jpg"
        source.write_bytes(b"Third original")
        extract = self.texts / "third.txt"
        extract.write_text("THIRD READING")
        recipe = self.root / "append.json"
        recipe.write_text(json.dumps({"schema": 1, "source_root": "originals", "text_root": "raw", "assignments": [
            {"source": source.name, "document": "Filed packet", "description": "Packet", "disposition": "document",
             "source_sha256": checksum(source), "preferred_text": {"path": extract.name, "sha256": checksum(extract)}}]}))
        return recipe, source

    def test_append_fetches_existing_controls_before_read_and_outputs_before_validation(self):
        recipe, source = self.append_recipe()
        fetched = []
        actual_regular = filing._regular
        guarded = {self.folder / filing.RECEIPT, self.folder / "README.md", self.folder / filing.COMBINED}
        guarded.update(self.folder / row[key] for row in self.before["sources"] for key in ("text", "copy"))
        def regular(path):
            if path in guarded and path not in fetched:
                raise IntegrityError("Simulated evicted file was read before exact intake")
            return actual_regular(path)
        def intake(paths):
            self.assertTrue(all(path.is_file() for path in paths))
            fetched.extend(paths)
            return {"success": True, "paths": [str(path) for path in paths]}, True
        with patch.object(filing, "fetch", side_effect=intake), patch.object(filing, "_regular", side_effect=regular):
            result = filing.file_assignments(recipe, self.destination, fetch_inputs=True)
        self.assertTrue(result["filing_complete"])
        self.assertTrue(guarded <= set(fetched))
        self.assertIn(source, fetched)
        self.assertNotIn(self.originals / "IMG_0001.jpg", fetched)
        self.assertNotIn(self.originals / "IMG_0002.jpg", fetched)
        self.assertTrue(list((self.folder / ".filing-history").rglob("existing-controls-intake-*.json")))
        self.assertTrue(list((self.folder / ".filing-history").rglob("existing-outputs-intake-*.json")))

    def test_append_control_intake_failure_is_retained_without_publication(self):
        recipe, source = self.append_recipe()
        def intake(paths):
            if paths == [recipe]:
                return {"success": True}, True
            return {"success": False, "failures": ["Synthetic exact control read failure"]}, False
        with patch.object(filing, "fetch", side_effect=intake), patch.object(filing, "_publish_file", side_effect=AssertionError("No publication")):
            with self.assertRaisesRegex(IntegrityError, "controls intake failed"):
                filing.file_assignments(recipe, self.destination, fetch_inputs=True)
        self.assertEqual((self.folder / filing.RECEIPT).read_bytes(), self.receipt_bytes)
        evidence = list((self.folder / ".filing-history").rglob("existing-controls-intake-*.json"))
        self.assertEqual(len(evidence), 1)
        self.assertFalse(json.loads(evidence[0].read_text())["success"])

    def test_unsupported_receipt_and_source_formats_are_rejected_without_mutation(self):
        recipe = self.recipe()
        receipt = self.folder / filing.RECEIPT
        current = receipt.read_bytes()
        receipt.rename(self.folder / ".filing.json")
        with self.assertRaisesRegex(ValueError, "current filing receipt"):
            self.apply(recipe)
        self.assertEqual((self.folder / ".filing.json").read_bytes(), current)
        (self.folder / ".filing.json").rename(receipt)
        adapted = self.state()
        adapted["sources"][0]["legacy"] = True
        receipt.write_text(json.dumps(adapted))
        recipe = self.recipe()
        with self.assertRaisesRegex(ValueError, "Unsupported filing source format"):
            self.apply(recipe)
        self.assertEqual(self.state(), adapted)


if __name__ == "__main__":
    unittest.main()
