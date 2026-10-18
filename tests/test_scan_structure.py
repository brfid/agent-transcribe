"""Portable scan receipts and useful partial reading without touching archive sources."""
import argparse
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_transcribe import common, scans


class ScanStructure(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.archive = self.root / "archive"
        self.sources = self.archive / "originals"
        self.sources.mkdir(parents=True)

    def source(self, relative, text="Unchecked machine source text"):
        path = self.sources / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def args(self, sources, name="job", **options):
        values = dict(source=[str(p) for p in sources], sources_file=None,
                      source_root=str(self.sources), output=str(self.archive / name),
                      reuse_index=[], reuse_root=None, pdf=False, fetch=False,
                      best_effort=True, psm=3, rotate_clockwise=0, ocr_image_pdfs=False)
        values.update(options)
        return argparse.Namespace(**values)

    def state(self, args):
        return json.loads((Path(args.output) / ".processing.json").read_text())

    def test_missing_source_has_disposition_partial_reading_and_resumes(self):
        first = self.source("first.txt", "FIRST GOOD PASSAGE")
        absent = self.sources / "later.txt"
        last = self.source("last.txt", "LAST GOOD PASSAGE")
        args = self.args([first, absent, last])
        result = scans.process(args)
        self.assertEqual((result["extracted"], result["extraction_complete"]), (2, False))
        self.assertEqual(result["failures"][0]["stage"], "intake")
        self.assertIn("later.txt", self.state(args)["intake_failures"])
        partial = Path(result["reading_text"])
        original_partial = partial.read_bytes()
        text = partial.read_text()
        self.assertIn("PARTIAL DISCOVERY READING", text)
        self.assertLess(text.index("FIRST GOOD PASSAGE"), text.index("SOURCE UNAVAILABLE"))
        self.assertLess(text.index("SOURCE UNAVAILABLE"), text.index("LAST GOOD PASSAGE"))
        self.assertFalse((Path(args.output) / "document.txt").exists())
        readme = Path(args.output) / "README.md"
        readme.write_text("Human description and boundary evidence.\n")
        absent.write_text("NEWLY AVAILABLE PASSAGE")
        with patch("agent_transcribe.scans.extract_page", wraps=scans.extract_page) as extractor:
            resumed = scans.process(args)
        self.assertEqual(extractor.call_count, 1)
        self.assertEqual(extractor.call_args.args[0], absent)
        self.assertTrue(resumed["extraction_complete"])
        self.assertIsNone(resumed["partial_text"])
        self.assertEqual(partial.read_bytes(), original_partial)
        self.assertEqual(readme.read_text(), "Human description and boundary evidence.\n")
        self.assertIn("NEWLY AVAILABLE PASSAGE", Path(resumed["reading_text"]).read_text())

    def test_all_missing_sources_still_produce_a_readable_account(self):
        missing = self.sources / "unavailable-folder" / "absent.txt"
        args = self.args([missing], source_root=None)
        result = scans.process(args)
        self.assertEqual(result["extracted"], 0)
        self.assertIn("NO SAVED TEXT AVAILABLE", Path(result["partial_text"]).read_text())
        original_specification = self.state(args)["specification"]
        missing.parent.mkdir()
        missing.write_text("NOW READABLE")
        resumed = scans.process(args)
        self.assertTrue(resumed["extraction_complete"])
        self.assertEqual(original_specification, self.state(args)["specification"])
        self.assertIn("NOW READABLE", Path(resumed["combined_text"]).read_text())

    def test_modified_partial_snapshot_is_preserved_and_stops_resume(self):
        good = self.source("good.txt")
        absent = self.sources / "absent.txt"
        args = self.args([good, absent])
        result = scans.process(args)
        partial = Path(result["partial_text"])
        partial.write_text("A human annotation that must survive.")
        with self.assertRaisesRegex(common.IntegrityError, "missing or modified"):
            scans.process(args)
        self.assertEqual(partial.read_text(), "A human annotation that must survive.")

    def test_partial_publication_resumes_prepared_bytes(self):
        good = self.source("good.txt")
        args = self.args([good, self.sources / "missing.txt"])
        actual = common.atomic_copy
        def interrupt(path, source):
            actual(path, source)
            if Path(path).name == "partial-reading-0001.txt":
                raise KeyboardInterrupt("after partial publication")
        with patch("agent_transcribe.common.atomic_copy", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                scans.process(args)
        before = (Path(args.output) / "partial-reading-0001.txt").read_bytes()
        with patch("agent_transcribe.scans.extract_page", side_effect=AssertionError("Do not repeat extraction")):
            result = scans.process(args)
        self.assertEqual(Path(result["partial_text"]).read_bytes(), before)
        self.assertEqual(list(Path(args.output).glob(".stage-*")), [])

    def test_duplicate_basenames_and_path_suffix_collisions_are_distinct(self):
        originals = [self.source("same.txt", "TOP"), self.source("a/same.txt", "A"),
                     self.source("b/same.txt", "B"), self.source("same.txt.txt/child.txt", "CHILD")]
        args = self.args(originals, fetch=True)
        with patch("agent_transcribe.common.fetch", return_value=({"success": True}, True)):
            result = scans.process(args)
        self.assertTrue(result["extraction_complete"])
        receipt = Path(args.output) / ".processing.json"
        entries = scans.receipt_sources(receipt)
        self.assertEqual([row[0] for row in entries], ["same.txt", "a/same.txt", "b/same.txt", "same.txt.txt/child.txt"])
        for (_, original, _, output), expected in zip(entries, ("TOP", "A", "B", "CHILD")):
            self.assertIn(original, originals)
            self.assertEqual((receipt.parent / output).read_text(), expected)
        self.assertTrue((receipt.parent / ".intake/by-source/a/same.txt/receipt.json").is_file())

    def test_archive_relocation_resumes_without_extraction_or_output_changes(self):
        originals = [self.source("a/same.txt", "FIRST"), self.source("b/same.txt", "SECOND")]
        args = self.args(originals)
        scans.process(args)
        receipt = Path(args.output) / ".processing.json"
        before = self.state(args)
        output_bytes = (receipt.parent / "document.txt").read_bytes()
        moved = self.root / "relocated"
        self.archive.rename(moved)
        args.source = [str(moved / p.relative_to(self.archive)) for p in originals]
        args.output = str(moved / "job")
        args.source_root = None  # Infer the original declared root from relative source identity.
        with patch("agent_transcribe.scans.extract_page", side_effect=AssertionError("Do not repeat extraction")):
            result = scans.process(args)
        after = self.state(args)
        self.assertEqual(before["specification"], after["specification"])
        self.assertEqual(before["steps"], after["steps"])
        self.assertEqual(Path(result["combined_text"]).read_bytes(), output_bytes)
        self.assertEqual([p for _, p, _, _ in scans.receipt_sources(Path(args.output) / ".processing.json")],
                         [Path(p) for p in args.source])

    def test_reused_portable_job_survives_relocation(self):
        source = self.source("original.txt", "RETAINED")
        baseline = self.args([source], name="baseline")
        scans.process(baseline)
        args = self.args([source], name="reuse", reuse_index=[str(Path(baseline.output) / ".processing.json")])
        scans.process(args)
        before = self.state(args)["specification"]
        moved = self.root / "moved"
        self.archive.rename(moved)
        args.source = [str(moved / "originals/original.txt")]
        args.source_root = str(moved / "originals")
        args.output = str(moved / "reuse")
        args.reuse_index = [str(moved / "baseline/.processing.json")]
        with patch("agent_transcribe.scans.extract_page", side_effect=AssertionError("Reuse portable text")):
            scans.process(args)
        self.assertEqual(before, self.state(args)["specification"])

    def test_unsupported_scan_schema_rejects_resume_reuse_and_reading_without_mutation(self):
        from agent_transcribe import filing
        source = self.source("original.txt")
        args = self.args([source])
        scans.process(args)
        receipt = Path(args.output) / ".processing.json"
        state = self.state(args)
        state["schema"] = 1
        receipt.write_text(json.dumps(state))
        before = receipt.read_bytes()
        for operation in (lambda: scans.process(args),
                          lambda: scans.receipt_sources(receipt),
                          lambda: filing.preferred_search_paths(jobs=[args.output]),
                          lambda: scans.process(self.args([source], name="regrouped", reuse_index=[str(receipt)]))):
            with self.subTest(operation=operation):
                with self.assertRaisesRegex(ValueError, "Unsupported scan processing format"):
                    operation()
                self.assertEqual(receipt.read_bytes(), before)
        self.assertFalse((self.archive / "regrouped").exists())

    def test_retained_text_indexes_require_explicit_current_format_and_both_hashes(self):
        source = self.source("original.txt")
        text = self.root / "retained.txt"
        text.write_text("A preserved extraction")
        row = {"source": str(source), "text": str(text), "source_sha256": common.checksum(source),
               "text_sha256": common.checksum(text)}
        current = {"schema": 1, "kind": "retained-text", "entries": [row]}
        invalid = [[], [row], {"entries": [row]}, dict(current, schema=2),
                   dict(current, entries=[{key: value for key, value in row.items() if key != "text_sha256"}]),
                   dict(current, entries=[dict(row, source_sha256="")])]
        index = self.root / "index.json"
        for value in invalid:
            with self.subTest(value=value):
                index.write_text(json.dumps(value))
                before = index.read_bytes()
                with self.assertRaisesRegex(ValueError, "retained-text|Retained-text"):
                    scans.process(self.args([source], reuse_index=[str(index)]))
                self.assertEqual(index.read_bytes(), before)
                self.assertFalse((self.archive / "job").exists())
        index.write_text(json.dumps(current))
        with patch("agent_transcribe.scans.extract_page", side_effect=AssertionError("Use the pinned retained text")):
            result = scans.process(self.args([source], reuse_index=[str(index)]))
        self.assertIn(text.read_text(), Path(result["reading_text"]).read_text())
        self.assertEqual(text.read_text(), "A preserved extraction")

    def test_retained_text_hash_and_original_mismatch_stop_without_ocr(self):
        source = self.source("original.txt")
        text = self.root / "retained.txt"
        text.write_text("Preserved retained text")
        row = {"source": str(source), "text": str(text), "source_sha256": common.checksum(source),
               "text_sha256": common.checksum(text)}
        index = self.root / "index.json"
        for field in ("text_sha256", "source_sha256"):
            with self.subTest(field=field):
                index.write_text(json.dumps({"schema": 1, "kind": "retained-text", "entries": [dict(row, **{field: "0" * 64})]}))
                with patch("agent_transcribe.scans.extract_page", side_effect=AssertionError("Do not repeat OCR")):
                    with self.assertRaisesRegex(common.IntegrityError, "modified|source hash differs"):
                        scans.process(self.args([source], name=field, reuse_index=[str(index)]))
                self.assertEqual(text.read_text(), "Preserved retained text")
                self.assertFalse((self.archive / field / "document.txt").exists())

    def test_unsupported_regular_file_is_accounted_for_but_directories_and_symlinks_stop(self):
        good = self.source("good.txt")
        context = self.source("context.bin", "Original context bytes")
        args = self.args([good, context])
        result = scans.process(args)
        self.assertEqual(result["failures"][0]["stage"], "extraction")
        self.assertIn("EXTRACTION FAILED", Path(result["partial_text"]).read_text())
        self.assertEqual(context.read_text(), "Original context bytes")
        linked = self.sources / "link.txt"
        linked.symlink_to(good)
        for invalid in (self.sources, linked):
            with self.subTest(path=invalid):
                with self.assertRaisesRegex(ValueError, "regular files"):
                    scans.process(self.args([good, invalid], name="invalid"))
                self.assertFalse((self.archive / "invalid").exists())


if __name__ == "__main__":
    unittest.main()
