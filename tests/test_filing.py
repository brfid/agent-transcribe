"""Filing contracts using disposable originals and text; no archive processing."""
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


class FilingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.originals = self.root / "originals"
        self.texts = self.root / "extracts"
        self.destination = self.root / "organized"
        self.originals.mkdir()
        self.texts.mkdir()

    def assignment(self, name="IMG_0001.jpg", document="Recognizable report", text="FIRST BODY", **changes):
        source = self.originals / name
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"unaltered scan bytes " + name.encode())
        os.utime(source, ns=(1234567890123456789, 1234567890123456789))
        extract = self.texts / (name + ".txt")
        extract.parent.mkdir(parents=True, exist_ok=True)
        extract.write_text(text)
        row = {"source": name, "document": document, "description": "Report",
               "disposition": "document", "source_sha256": checksum(source),
               "preferred_text": {"path": name + ".txt", "sha256": checksum(extract)}}
        row.update(changes)
        return row

    def recipe(self, assignments, name="recipe.json"):
        path = self.root / name
        path.write_text(json.dumps({"schema": 1, "source_root": "originals", "text_root": "extracts", "assignments": assignments}))
        return path

    def file(self, assignments, **kwargs):
        return filing.file_assignments(self.recipe(assignments), self.destination, **kwargs)

    def state(self, folder="Recognizable report"):
        return json.loads((self.destination / folder / filing.RECEIPT).read_text())

    def test_complete_filing_preserves_bytes_mtime_order_and_search_scope(self):
        rows = [self.assignment(), self.assignment("IMG_0002.jpg", text="SECOND BODY")]
        result = self.file(rows)
        self.assertTrue(result["filing_complete"])
        folder = self.destination / "Recognizable report"
        state = self.state()
        for expected, row in zip(rows, state["sources"]):
            self.assertTrue(row["copy"].startswith("Scans/"))
            self.assertTrue(row["text"].startswith("Source text/"))
            self.assertTrue(row["copy"].endswith(Path(expected["source"]).name))
            self.assertEqual((folder / row["copy"]).read_bytes(), (self.originals / expected["source"]).read_bytes())
            self.assertEqual((folder / row["copy"]).stat().st_mtime_ns, (self.originals / expected["source"]).stat().st_mtime_ns)
        combined = (folder / filing.COMBINED).read_text()
        self.assertLess(combined.index("FIRST BODY"), combined.index("SECOND BODY"))
        self.assertTrue(filing.validate_document(folder, verify_sources=True)["valid"])
        selected = filing.preferred_search_paths([folder])
        self.assertEqual(len(selected["files"]), 3)
        self.assertNotIn(str(folder / filing.COMBINED), selected["files"])
        self.assertEqual(selected["files"][0], str(folder / "README.md"))

    def test_append_and_repeat_keep_names_and_human_prose(self):
        first = self.assignment()
        self.file([first])
        folder = self.destination / "Recognizable report"
        old = self.state()["sources"][0]
        readme = folder / "README.md"
        readme.write_text("Human title and interpretive note.\n\n" + readme.read_text() + "\nMore human notes.\n")
        second = self.assignment("IMG_0002.jpg", text="SECOND BODY")
        self.file([second])
        self.file([second])
        self.assertEqual(self.state()["sources"][0], old)
        self.assertEqual(len(self.state()["sources"]), 2)
        self.assertIn("Human title", readme.read_text())
        self.assertIn("More human notes", readme.read_text())
        self.assertTrue(list((folder / ".filing-history").rglob("OCR discovery text.txt")))

    def test_named_identical_basenames_preserve_distinct_original_paths(self):
        self.file([self.assignment("a/IMG_0001.jpg", text="FIRST"), self.assignment("b/IMG_0001.jpg", text="SECOND")])
        rows = self.state()["sources"]
        self.assertNotEqual(rows[0]["copy"], rows[1]["copy"])
        self.assertEqual([r["source"] for r in rows], ["a/IMG_0001.jpg", "b/IMG_0001.jpg"])

    def test_unavailable_and_blank_recognition_are_explicit_and_not_dropped(self):
        blank = self.assignment(text=" \n")
        missing = {"source": "not-local.jpg", "document": "Recognizable report", "description": "Unavailable scan",
                   "disposition": "unavailable", "reason": "Exact-file read failed", "gap_reason": "Original unavailable"}
        result = self.file([blank, missing])
        self.assertEqual(result["unavailable"], 1)
        report = filing.validate_document(self.destination / "Recognizable report")
        self.assertEqual(report["selected"], 2)
        self.assertEqual(report["text_gaps"], 2)
        self.assertEqual(report["copies"], 1)
        self.assertIn("Original unavailable", (self.destination / "Recognizable report" / filing.COMBINED).read_text())

    def test_complementary_text_keeps_separate_provenance(self):
        row = self.assignment()
        extra = self.texts / "alternate.txt"
        extra.write_text("ALTERNATE BODY")
        row["complementary_text"] = [{"path": "alternate.txt", "sha256": checksum(extra)}]
        self.file([row])
        text = (self.destination / "Recognizable report" / self.state()["sources"][0]["text"]).read_text()
        self.assertIn("FIRST BODY", text)
        self.assertIn("ALTERNATE BODY", text)
        self.assertIn("Complementary extraction", text)
        self.assertIn(checksum(self.texts / "IMG_0001.jpg.txt"), text)

    def test_changed_original_or_retained_text_stops_before_publication(self):
        row = self.assignment()
        (self.originals / row["source"]).write_bytes(b"changed")
        with self.assertRaises(IntegrityError):
            self.file([row])
        self.assertFalse((self.destination / "Recognizable report" / filing.RECEIPT).exists())
        row = self.assignment()
        (self.texts / row["preferred_text"]["path"]).write_text("changed reading")
        with self.assertRaises(IntegrityError):
            self.file([row])

    def test_corrupt_output_and_membership_changes_are_reported_without_overwrite(self):
        self.file([self.assignment()])
        folder = self.destination / "Recognizable report"
        readme = folder / "README.md"
        original = readme.read_text()
        readme.write_text(original.replace("| 0001", "| 9001"))
        with self.assertRaisesRegex(IntegrityError, "membership"):
            filing.preferred_search_paths([folder])
        partial = filing.preferred_search_paths([folder], allow_discrepancies=True)
        self.assertFalse(partial["complete"])
        self.assertEqual(partial["files"], [])
        readme.write_text(original)
        export = folder / self.state()["sources"][0]["text"]
        export.write_text("Human correction")
        with self.assertRaises(IntegrityError):
            self.file([self.assignment("IMG_0002.jpg")])
        self.assertEqual(export.read_text(), "Human correction")

    def test_interruption_resumes_exact_prepared_bytes_and_keeps_late_human_notes(self):
        recipe = self.recipe([self.assignment(), self.assignment("IMG_0002.jpg", text="SECOND BODY")])
        actual, calls = filing._publish_file, []
        def interrupt(target, *args, **kwargs):
            calls.append(target)
            if len(calls) == 2:
                raise OSError("synthetic publication interruption")
            return actual(target, *args, **kwargs)
        with patch("agent_transcribe.filing._publish_file", side_effect=interrupt):
            with self.assertRaises(OSError):
                filing.file_assignments(recipe, self.destination)
        folder = self.destination / "Recognizable report"
        self.assertTrue((folder / ".filing.pending.json").exists())
        (folder / "README.md").write_text("A user note during the interruption.\n")
        with patch("agent_transcribe.filing._text_export", side_effect=AssertionError("Must reuse prepared bytes")):
            filing.file_assignments(recipe, self.destination)
        self.assertIn("user note", (folder / "README.md").read_text())
        self.assertFalse((folder / ".filing.pending.json").exists())
        self.assertTrue(filing.validate_document(folder)["valid"])

    def test_corrupt_prepared_bytes_stop_resume(self):
        recipe = self.recipe([self.assignment()])
        with patch("agent_transcribe.filing._publish_file", side_effect=OSError("stop")):
            with self.assertRaises(OSError):
                filing.file_assignments(recipe, self.destination)
        folder = self.destination / "Recognizable report"
        pending = json.loads((folder / ".filing.pending.json").read_text())
        (folder / pending["stage"] / pending["outputs"][0]["staged"]).write_bytes(b"corrupt")
        with self.assertRaises(IntegrityError):
            filing.file_assignments(recipe, self.destination)

    def test_recipe_escapes_duplicate_sources_and_symlinks_rejected(self):
        row = self.assignment()
        with self.assertRaises(ValueError):
            self.file([row, row])
        for key in ["source", "document"]:
            with self.subTest(key=key):
                value = dict(row, **{key: "../escape"})
                with self.assertRaises(ValueError):
                    self.file([value])
        source = self.originals / row["source"]
        source.unlink()
        source.symlink_to(self.texts / "IMG_0001.jpg.txt")
        with self.assertRaises(IntegrityError):
            self.file([row])

    def test_receipts_relocate_with_archive_tree(self):
        self.file([self.assignment()])
        moved = self.root / "moved"
        moved.mkdir()
        for name in ["originals", "extracts", "organized"]:
            (self.root / name).rename(moved / name)
        folder = moved / "organized/Recognizable report"
        self.assertTrue(filing.validate_document(folder, verify_sources=True)["valid"])
        self.assertEqual(len(filing.preferred_search_paths([folder])["files"]), 2)

    def unsupported_receipt(self):
        self.file([self.assignment()])
        folder = self.destination / "Recognizable report"
        (folder / filing.RECEIPT).rename(folder / ".filing.json")
        return folder

    def test_unsupported_receipt_rejects_check_and_reading_without_mutation(self):
        folder = self.unsupported_receipt()
        before = (folder / ".filing.json").read_bytes()
        for operation in (lambda: filing.validate_document(folder),
                          lambda: filing.preferred_search_paths([folder])):
            with self.assertRaisesRegex(ValueError, "Unsupported filing receipt format"):
                operation()
        self.assertEqual((folder / ".filing.json").read_bytes(), before)
        self.assertFalse((folder / filing.RECEIPT).exists())

    def test_unsupported_receipt_rejects_extension_without_publishing(self):
        folder = self.unsupported_receipt()
        before = (folder / ".filing.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "Unsupported filing receipt format"):
            self.file([self.assignment("IMG_0002.jpg", text="SECOND BODY")])
        self.assertEqual((folder / ".filing.json").read_bytes(), before)
        self.assertFalse((folder / filing.RECEIPT).exists())
        self.assertEqual(len(list((folder / "Scans").iterdir())), 1)

    def test_noncurrent_snapshot_and_managed_markers_are_rejected(self):
        self.file([self.assignment()])
        folder = self.destination / "Recognizable report"
        receipt = folder / filing.RECEIPT
        state = self.state()
        for change in ({"assembly_version": 2}, {"legacy_receipt": {}}, {"assembly_version": None}):
            with self.subTest(change=change):
                changed = dict(state, **change)
                receipt.write_text(json.dumps(changed))
                before = receipt.read_bytes()
                with self.assertRaisesRegex(ValueError, "Unsupported filing receipt format"):
                    filing.validate_document(folder)
                self.assertEqual(receipt.read_bytes(), before)
        receipt.write_text(json.dumps(state))
        readme = folder / "README.md"
        changed = readme.read_text().replace(filing.START, "<!-- unsupported:source-map:start -->").replace(
            filing.END, "<!-- unsupported:source-map:end -->")
        readme.write_text(changed)
        with self.assertRaisesRegex(IntegrityError, "membership"):
            filing.validate_document(folder)
        self.assertEqual(readme.read_text(), changed)

    def test_late_earlier_source_preserves_filename_and_separates_orders(self):
        first = self.assignment("IMG_0002.jpg", text="LATER CAPTURE", capture_order=2,
                                reading_order=2, reading_order_evidence="Printed page 2 inspected")
        self.file([first])
        original_name = self.state()["sources"][0]["copy"]
        late = self.assignment("IMG_0001.jpg", text="EARLIER CAPTURE", capture_order=1,
                               reading_order=1, reading_order_evidence="Printed page 1 inspected")
        self.file([late])
        state = self.state()
        self.assertEqual(state["sources"][0]["copy"], original_name)
        self.assertEqual([r["order"] for r in state["sources"]], [1, 2])
        self.assertEqual([r["capture_order"] for r in state["sources"]], [2, 1])
        folder = self.destination / "Recognizable report"
        combined = (folder / filing.COMBINED).read_text()
        self.assertLess(combined.index("EARLIER CAPTURE"), combined.index("LATER CAPTURE"))
        self.assertIn("supported reading order", combined)
        self.assertIn("Printed page 1 inspected", (folder / "README.md").read_text())
        self.assertTrue(filing.validate_document(folder)["valid"])

    def test_unasserted_append_order_is_not_claimed_as_capture_order(self):
        self.file([self.assignment("IMG_0002.jpg")])
        self.file([self.assignment("IMG_0001.jpg")])
        folder = self.destination / "Recognizable report"
        self.assertIn("capture order and physical page order are not inferred", (folder / "README.md").read_text())
        self.assertNotIn("Order is capture order", (folder / "README.md").read_text())
        with self.assertRaisesRegex(ValueError, "evidence"):
            self.file([self.assignment("IMG_0003.jpg", reading_order=3)])

    def extraction_job(self):
        from agent_transcribe import scans
        import argparse
        good = self.originals / "good.txt"
        good.write_text("GOOD DISCOVERY PASSAGE")
        missing = self.originals / "missing.txt"
        output = self.root / "unfinished-job"
        args = argparse.Namespace(source=[str(good), str(missing)], sources_file=None,
            source_root=str(self.originals), output=str(output), reuse_index=[], reuse_root=None,
            pdf=False, fetch=False, best_effort=True, psm=3, rotate_clockwise=0, ocr_image_pdfs=False)
        scans.process(args)
        return output, good

    def test_partial_extraction_is_searchable_with_declared_gaps_before_filing(self):
        job, good = self.extraction_job()
        result = filing.preferred_search_paths(jobs=[job])
        self.assertFalse(result["complete"])
        self.assertEqual(result["jobs"][0]["selected"], 2)
        self.assertEqual(result["jobs"][0]["per_source_text"], 1)
        self.assertEqual(result["gaps"][0]["kind"], "unavailable_text")
        self.assertIn(str(job / "pages/good.txt.txt"), result["files"])
        self.assertNotIn(str(job / "partial-reading-0001.txt"), result["files"])
        (job / "pages/good.txt.txt").write_text("Edited output")
        with self.assertRaises(IntegrityError):
            filing.preferred_search_paths(jobs=[job])

    def test_filed_preferred_supersedes_extraction_fallback_for_same_original(self):
        job, good = self.extraction_job()
        row = {"source": "good.txt", "document": "Recognizable report", "description": "Good source",
               "disposition": "document", "source_sha256": checksum(good),
               "preferred_text": {"path": "unfinished-job/pages/good.txt.txt", "sha256": checksum(job / "pages/good.txt.txt")}}
        recipe = self.recipe([row])
        filing.file_assignments(recipe, self.destination, text_root=self.root)
        result = filing.preferred_search_paths([self.destination / "Recognizable report"], jobs=[job])
        self.assertEqual(result["jobs"][0]["preferred_filed_sources"], 1)
        self.assertNotIn(str(job / "pages/good.txt.txt"), result["files"])
        self.assertEqual(sum("Source text/" in path for path in result["files"]), 1)


    def test_idempotent_repeat_detects_selected_original_and_chosen_text_drift(self):
        row = self.assignment()
        self.file([row])
        source = self.originals / row["source"]
        original = source.read_bytes()
        source.write_bytes(b"changed original")
        with self.assertRaises(IntegrityError):
            self.file([row])
        source.write_bytes(original)
        (self.texts / row["preferred_text"]["path"]).write_text("changed selected text")
        with self.assertRaises(IntegrityError):
            self.file([row])

    def test_history_symlink_and_pending_symlink_rejected_without_external_writes(self):
        row = self.assignment()
        self.file([row])
        folder = self.destination / "Recognizable report"
        outside = self.root / "outside"
        outside.mkdir()
        history = folder / ".filing-history"
        history.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(IntegrityError):
            self.file([self.assignment("IMG_0002.jpg")])
        self.assertEqual(list(outside.iterdir()), [])
        history.unlink()
        external = outside / "pending.json"
        external.write_text("{}")
        (folder / ".filing.pending.json").symlink_to(external)
        with self.assertRaises(IntegrityError):
            self.file([row])
        self.assertEqual(external.read_text(), "{}")


    def test_reading_fetches_exact_controls_and_text_without_image_or_original_bytes(self):
        self.file([self.assignment()])
        folder = self.destination / "Recognizable report"
        with patch("agent_transcribe.filing.fetch", return_value=({"success": True}, True)) as intake:
            result = filing.fetch_selection([folder])
        paths = [p for call in intake.call_args_list for p in call.args[0]]
        self.assertIn(folder / filing.RECEIPT, paths)
        self.assertIn(folder / "README.md", paths)
        self.assertIn(folder / filing.COMBINED, paths)
        self.assertFalse(any(p.suffix == ".jpg" for p in paths))
        self.assertFalse(any(p.is_relative_to(self.originals) for p in paths))
        self.assertFalse(result["includes_copies"])
        with patch("agent_transcribe.filing.fetch", return_value=({"success": True}, True)) as intake:
            filing.fetch_selection([folder], include_copies=True, include_sources=True)
        paths = [p for call in intake.call_args_list for p in call.args[0]]
        self.assertIn(self.originals / "IMG_0001.jpg", paths)
        self.assertTrue(any(p.suffix == ".jpg" and p.is_relative_to(folder) for p in paths))


    def test_resume_rejects_receipt_symlink_before_publishing_outputs(self):
        recipe = self.recipe([self.assignment()])
        with patch("agent_transcribe.filing._publish_file", side_effect=OSError("stop")):
            with self.assertRaises(OSError):
                filing.file_assignments(recipe, self.destination)
        folder = self.destination / "Recognizable report"
        external = self.root / "external-receipt.json"
        (folder / filing.RECEIPT).symlink_to(external)
        with self.assertRaises(IntegrityError):
            filing.file_assignments(recipe, self.destination)
        self.assertFalse((folder / "Scans").exists())
        self.assertFalse(external.exists())
        self.assertTrue((folder / filing.RECEIPT).is_symlink())

    def test_current_managed_map_cannot_add_unrecorded_source_rows(self):
        self.file([self.assignment()])
        folder = self.destination / "Recognizable report"
        readme = folder / "README.md"
        block = filing._block(readme.read_text())
        source_row = next(line for line in block.splitlines() if line.startswith("| 0001 |"))
        changed = block.replace(filing.END, source_row.replace("0001", "0002") + "\n" + filing.END)
        readme.write_text(readme.read_text().replace(block, changed))
        state = self.state()
        state["readme_block_sha256"] = filing._digest(changed)
        (folder / filing.RECEIPT).write_text(json.dumps(state))
        with self.assertRaisesRegex(IntegrityError, "row count"):
            filing.preferred_search_paths([folder])

    def test_identical_intake_receipt_reuses_checkpoint_after_repeated_preparation_failures(self):
        recipe = self.recipe([self.assignment()])
        with patch("agent_transcribe.filing.fetch", return_value=({"success": True}, True)):
            for _ in range(2):
                with patch("agent_transcribe.filing._text_export", side_effect=OSError("before pending checkpoint")):
                    with self.assertRaises(OSError):
                        filing.file_assignments(recipe, self.destination, fetch_inputs=True)
            result = filing.file_assignments(recipe, self.destination, fetch_inputs=True)
        self.assertTrue(result["filing_complete"])


    def test_explicit_original_root_override_keeps_recorded_readme_locators(self):
        self.file([self.assignment()])
        folder = self.destination / "Recognizable report"
        before = (folder / "README.md").read_bytes()
        moved = self.root / "moved-originals"
        self.originals.rename(moved)
        result = filing.validate_document(folder, verify_sources=True, source_root=moved)
        self.assertTrue(result["source_bytes_verified"])
        self.assertEqual((folder / "README.md").read_bytes(), before)
        with patch("agent_transcribe.filing.fetch", return_value=({"success": True}, True)) as intake:
            filing.fetch_selection([folder], include_sources=True, source_root=moved)
        paths = [p for call in intake.call_args_list for p in call.args[0]]
        self.assertIn(moved / "IMG_0001.jpg", paths)
        self.assertNotIn(self.originals / "IMG_0001.jpg", paths)


    def test_fetch_materializes_exact_recipe_before_parsing_and_preserves_receipt(self):
        row = self.assignment()
        recipe = self.recipe([row])
        actual_read = Path.read_text
        fetched = []
        def read(path, *args, **kwargs):
            if path == recipe:
                self.assertIn(recipe, fetched)
            return actual_read(path, *args, **kwargs)
        def intake(paths):
            fetched.extend(paths)
            return {"success": True, "selected": [str(p) for p in paths]}, True
        with patch("agent_transcribe.filing.fetch", side_effect=intake), patch.object(Path, "read_text", read):
            filing.file_assignments(recipe, self.destination, fetch_inputs=True)
        self.assertEqual(fetched[0], recipe)
        self.assertTrue(list((self.destination / ".filing-history/recipe-inputs").glob("*.json")))


    def test_duplicate_primary_source_across_selected_documents_fails_search(self):
        row = self.assignment()
        self.file([row])
        other = dict(row, document="Other report")
        self.file([other])
        with self.assertRaisesRegex(IntegrityError, "multiple primary"):
            filing.preferred_search_paths([self.destination / "Recognizable report", self.destination / "Other report"])


if __name__ == "__main__":
    unittest.main()
