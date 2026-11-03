"""Synthetic lightweight baseline, without mandatory filing or conversion."""
import argparse
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

import test_scan_structure as structure
import test_tiff_safety as tiff_safety
from agent_transcribe import cli, common, filing, scans


class BaselineWorkflow(unittest.TestCase):
    # Reuse the existing disposable archive and exact-source argument helpers.
    setUp = structure.ScanStructure.setUp
    source = structure.ScanStructure.source
    args = structure.ScanStructure.args
    state = structure.ScanStructure.state

    def reading(self, job, query=None):
        return cli.read_selection(argparse.Namespace(document=[], job=[str(job)], fetch=False,
                                  query=[query] if query else [], limit=100, context=0))

    def test_lightweight_exact_subset_reuses_text_and_repairs_only_missing_source(self):
        from PIL import Image
        first = self.source("box/first.txt", "FIRST BASELINE PASSAGE")
        image = self.sources / "box/capture.png"
        Image.new("RGB", (4, 4), "white").save(image)
        retained = self.archive / "accepted.txt"
        retained.write_text("PREFERRED RETAINED PASSAGE")
        index = self.archive / "retained.json"
        index.write_text(json.dumps({"schema": 1, "kind": "retained-text", "entries": [
            {"source": str(image), "text": str(retained), "source_sha256": common.checksum(image),
             "text_sha256": common.checksum(retained)}]}))
        missing = self.sources / "later.txt"
        unrelated = self.source("outside-selection.txt", "EXCLUDED")
        listing = self.archive / "selected.txt"
        listing.write_text("\n".join(str(p) for p in (image, missing, first)))
        args = self.args([], source=None, sources_file=str(listing), reuse_index=[str(index)])
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in (first, image, retained, index, unrelated)}
        extractor = scans.extract_page
        with patch("agent_transcribe.scans.run", return_value=subprocess.CompletedProcess([], 0, stdout=b"synthetic\n")), \
                patch("agent_transcribe.scans.extract_page", wraps=extractor) as calls:
            result = scans.process(args)
        self.assertEqual([call.args[0] for call in calls.call_args_list], [first])
        self.assertFalse(result["extraction_complete"])
        read = self.reading(args.output, "PASSAGE")
        self.assertFalse(read["complete"])
        self.assertEqual(read["jobs"][0]["per_source_text"], 2)
        self.assertEqual(len(read["matches"]), 2)
        partial = Path(result["reading_text"])
        partial_bytes = partial.read_bytes()
        missing.write_text("LATER BASELINE PASSAGE")
        with patch("agent_transcribe.scans.run", return_value=subprocess.CompletedProcess([], 0, stdout=b"synthetic\n")), \
                patch("agent_transcribe.scans.extract_page", wraps=extractor) as calls:
            resumed = scans.process(args)
        self.assertEqual([call.args[0] for call in calls.call_args_list], [missing])
        self.assertTrue(resumed["extraction_complete"])
        self.assertTrue(self.reading(args.output)["complete"])
        self.assertEqual(self.state(args)["specification"]["sources"], ["box/capture.png", "later.txt", "box/first.txt"])
        combined = Path(resumed["reading_text"]).read_text()
        self.assertLess(combined.index("PREFERRED RETAINED"), combined.index("LATER BASELINE"))
        self.assertLess(combined.index("LATER BASELINE"), combined.index("FIRST BASELINE"))
        self.assertEqual(partial.read_bytes(), partial_bytes)
        for path, saved in before.items():
            self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), saved)
        output = Path(args.output)
        self.assertFalse([p for p in output.rglob("*.png") if p.is_file()])
        self.assertFalse(list(output.rglob("*.pdf")))
        self.assertFalse(list(output.rglob(".filing-current.json")))
        self.assertFalse((output / "conversion.json").exists())

    def test_interruption_keeps_completed_work_and_changed_selection_stops(self):
        first = self.source("first.txt", "FIRST")
        second = self.source("second.txt", "SECOND")
        args = self.args([first, second])
        extractor = scans.extract_page
        def interrupt(source, *positional, **options):
            if source == second:
                raise KeyboardInterrupt("synthetic interruption")
            return extractor(source, *positional, **options)
        with patch("agent_transcribe.scans.extract_page", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                scans.process(args)
        before = (Path(args.output) / "pages/first.txt.txt").read_bytes()
        partial = self.reading(args.output, "FIRST")
        self.assertFalse(partial["complete"])
        self.assertEqual(len(partial["matches"]), 1)
        with patch("agent_transcribe.scans.extract_page", wraps=extractor) as calls:
            result = scans.process(args)
        self.assertEqual([call.args[0] for call in calls.call_args_list], [second])
        self.assertTrue(result["extraction_complete"])
        self.assertEqual((Path(args.output) / "pages/first.txt.txt").read_bytes(), before)
        with self.assertRaisesRegex(ValueError, "Inputs/settings differ"):
            scans.process(self.args([second, first]))

    def test_job_reading_needs_no_original_payload_and_rejects_ambiguous_variants(self):
        source = self.source("source.txt", "PREFERRED BASELINE")
        first = self.args([source], name="first-job")
        second = self.args([source], name="second-job")
        scans.process(first)
        scans.process(second)
        source.unlink()  # Reading validates saved text, not current original availability.
        self.assertTrue(self.reading(first.output)["complete"])
        with self.assertRaisesRegex(common.IntegrityError, "Multiple unfiled extraction readings"):
            filing.preferred_search_paths(jobs=[first.output, second.output])
        text = Path(first.output) / "pages/source.txt.txt"
        text.write_text("HUMAN CHANGE")
        with self.assertRaises(common.IntegrityError):
            filing.preferred_search_paths(jobs=[first.output])
        self.assertEqual(text.read_text(), "HUMAN CHANGE")


class BaselineTiffCoverage(unittest.TestCase):
    setUp = tiff_safety.TiffSafety.setUp
    fixture = tiff_safety.TiffSafety.fixture
    args = tiff_safety.TiffSafety.args
    run_ocr = tiff_safety.TiffSafety.run_ocr

    def assert_frame_gap(self, result, expected):
        self.assertFalse(result["complete"])
        matches = [item for item in result["gaps"] if item["kind"] == "tiff_frame_coverage_gap"]
        self.assertEqual(len(matches), 1, result["gaps"])
        gap = matches[0]
        self.assertEqual(gap["tiff_frames"]["expected_frame_ids"], expected)
        self.assertFalse(gap["tiff_frames"]["coverage_complete"])
        return gap

    def test_new_multipage_and_unknown_count_jobs_expose_existing_frame_inventory(self):
        for unknown in (False, True):
            with self.subTest(unknown=unknown):
                source = self.fixture(name=f"source-{unknown}.tiff")
                if unknown:
                    source.write_bytes(b"not a readable TIFF")
                args = self.args(source, folder=f"job-{unknown}")
                with patch("agent_transcribe.scans.run", side_effect=self.run_ocr):
                    result = scans.process(args)
                selected = filing.preferred_search_paths(jobs=[args.output])
                gap = self.assert_frame_gap(selected, None if unknown else [1, 2, 3])
                self.assertEqual(gap["tiff_frames"], result["tiff_frames"][source.name])
                self.assertEqual(selected["jobs"][0]["per_source_text"], 0)
                self.assertEqual(len(selected["gaps"]), 1)

    def test_old_done_text_stays_searchable_without_claiming_frame_coverage(self):
        source = self.fixture()
        args = self.args(source)
        with patch("agent_transcribe.scans.tiff_frame_inventory", return_value=None), \
                patch("agent_transcribe.scans.run", side_effect=self.run_ocr):
            scans.process(args)  # Simulate a retained pre-guard, first-frame-only job.
        output = Path(args.output)
        saved = {p: p.read_bytes() for p in (output / "document.txt", output / "pages/original.tiff.txt")}
        with patch("agent_transcribe.scans.run", side_effect=self.run_ocr):
            scans.process(args)  # The scan guard records complete expected-frame gaps.
        receipt_before = (output / ".processing.json").read_bytes()
        with redirect_stdout(io.StringIO()) as stdout:
            status = cli.main(["reading", "--job", str(output), "--query", "LEGACY FIRST-FRAME TEXT"])
        self.assertEqual(status, 2)
        selected = json.loads(stdout.getvalue())
        self.assert_frame_gap(selected, [1, 2, 3])
        self.assertEqual(selected["jobs"][0]["per_source_text"], 1)
        self.assertEqual(len([m for m in selected["matches"] if m["kind"] == "source_text"]), 1)
        self.assertEqual((output / ".processing.json").read_bytes(), receipt_before)
        for path, before in saved.items():
            self.assertEqual(path.read_bytes(), before)

    def test_malformed_tiff_coverage_is_an_integrity_error(self):
        source = self.fixture(orientations=(1,), colors=("white",))
        args = self.args(source)
        with patch("agent_transcribe.scans.run", side_effect=self.run_ocr):
            scans.process(args)
        receipt = Path(args.output) / ".processing.json"
        saved = json.loads(receipt.read_text())
        inventories = [None, [], "invalid", {source.name: None}, {source.name: []},
                       {source.name: "invalid"}]
        inventories.extend({source.name: {"coverage_complete": flag}}
                           for flag in (None, 0, 1, "false", "true"))
        for inventory in inventories:
            with self.subTest(inventory=inventory):
                receipt.write_text(json.dumps({**saved, "tiff_safety": inventory}))
                with self.assertRaises(common.IntegrityError):
                    filing.preferred_search_paths(jobs=[args.output])
                result = filing.preferred_search_paths(jobs=[args.output], allow_discrepancies=True)
                self.assertFalse(result["complete"])
                self.assertEqual(result["files"], [])
                self.assertEqual(len(result["errors"]), 1)

    def test_missing_tiff_completion_flag_remains_a_gap(self):
        source = self.fixture(orientations=(1,), colors=("white",))
        args = self.args(source)
        with patch("agent_transcribe.scans.run", side_effect=self.run_ocr):
            scans.process(args)
        receipt = Path(args.output) / ".processing.json"
        saved = json.loads(receipt.read_text())
        del saved["tiff_safety"][source.name]["coverage_complete"]
        receipt.write_text(json.dumps(saved))
        result = filing.preferred_search_paths(jobs=[args.output])
        self.assertFalse(result["complete"])
        self.assertEqual(result["jobs"][0]["per_source_text"], 1)
        self.assertEqual([gap["kind"] for gap in result["gaps"]], ["tiff_frame_coverage_gap"])

    def test_single_frame_complete_inventory_does_not_create_false_gap(self):
        source = self.fixture(orientations=(1,), colors=("white",))
        args = self.args(source)
        with patch("agent_transcribe.scans.run", side_effect=self.run_ocr):
            scans.process(args)
        result = filing.preferred_search_paths(jobs=[args.output])
        self.assertTrue(result["complete"])
        self.assertEqual(result["gaps"], [])


if __name__ == "__main__":
    unittest.main()
