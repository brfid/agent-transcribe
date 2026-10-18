"""Disposable evidence, interruption and search-coverage cases for scan batches."""
import argparse
import errno
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from _support import fixture_font

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_transcribe import common, scans


class ScanCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def source(self, name="original.txt", text="A useful discovery passage.\n"):
        path = self.root / name
        path.write_text(text)
        return path

    def args(self, sources, folder="batch", **options):
        values = dict(source=[str(p) for p in sources], sources_file=None,
                      output=str(self.root / folder), reuse_index=[], reuse_root=None,
                      pdf=False, fetch=False, best_effort=False, psm=3, rotate_clockwise=0)
        values.update(options)
        return argparse.Namespace(**values)

    def state(self, args):
        return json.loads((Path(args.output) / ".processing.json").read_text())

    def retained(self, source, text):
        extract = self.root / "retained.txt"
        extract.write_text(text)
        index = self.root / "index.json"
        index.write_text(json.dumps({"schema": 1, "kind": "retained-text", "entries": [
            {"source": str(source), "text": str(extract), "source_sha256": common.checksum(source),
             "text_sha256": common.checksum(extract)}]}))
        return extract, index


class ScanReadiness(ScanCase):
    def test_blank_or_whitespace_reuse_falls_back_once_and_preserves_evidence(self):
        source = self.source()
        original = common.checksum(source)
        for i, old_text in enumerate(("", " \n\t", "\ufeff \n")):
            with self.subTest(retained=repr(old_text)):
                extract, index = self.retained(source, old_text)
                args = self.args([source], folder=f"fallback-{i}", reuse_index=[str(index)])
                with patch("agent_transcribe.scans.extract_page", wraps=scans.extract_page) as extractor:
                    result = scans.process(args)
                    scans.process(args)
                self.assertEqual(extractor.call_count, 1)
                self.assertTrue(result["extraction_complete"])
                self.assertEqual(result["blank_text"], 0)
                self.assertEqual(extract.read_text(), old_text)
                self.assertEqual(common.checksum(source), original)
                self.assertIn("fresh extraction after blank retained text",
                              self.state(args)["steps"][source.name]["details"]["method"])
                self.assertIn("A useful discovery passage", (Path(args.output) / "document.txt").read_text())

    def test_genuinely_blank_fresh_result_is_flagged_without_endless_retries(self):
        source = self.source(text=" \n\t")
        old, index = self.retained(source, "")
        args = self.args([source], reuse_index=[str(index)])
        with patch("agent_transcribe.scans.extract_page", wraps=scans.extract_page) as extractor:
            result = scans.process(args)
            scans.process(args)
        self.assertEqual(extractor.call_count, 1)
        self.assertEqual(result["blank_text"], 1)
        self.assertTrue(result["extraction_complete"])
        self.assertTrue(result["review_required"])
        self.assertIn("NO EXTRACTED TEXT", (Path(args.output) / "document.txt").read_text())
        self.assertEqual(old.read_text(), "")

    def test_best_effort_keeps_successes_and_retries_only_the_failed_source(self):
        sources = [self.source("first.txt", "FIRST PASSAGE"), self.source("middle.txt", "MIDDLE PASSAGE"),
                   self.source("last.txt", "LAST PASSAGE")]
        args = self.args(sources, best_effort=True)
        real_extract = scans.extract_page
        def fail_middle(source, *positional, **options):
            if source.name == "middle.txt":
                raise RuntimeError("synthetic independent extraction failure")
            return real_extract(source, *positional, **options)
        with patch("agent_transcribe.scans.extract_page", side_effect=fail_middle):
            result = scans.process(args)
        self.assertFalse(result["extraction_complete"])
        self.assertEqual(result["extracted"], 2)
        self.assertEqual(result["failures"][0]["source"], str(sources[1]))
        self.assertFalse((Path(args.output) / "document.txt").exists())
        retained = (Path(args.output) / "pages/first.txt.txt").read_bytes()
        readme = Path(args.output) / "README.md"
        readme.write_text(readme.read_text() + "\nUser note: a tentative relationship.\n")
        with patch("agent_transcribe.scans.extract_page", wraps=real_extract) as extractor:
            resumed = scans.process(args)
        self.assertTrue(resumed["extraction_complete"])
        self.assertEqual(extractor.call_count, 1)
        self.assertEqual(extractor.call_args.args[0], sources[1])
        combined = (Path(args.output) / "document.txt").read_text()
        self.assertLess(combined.index("FIRST PASSAGE"), combined.index("MIDDLE PASSAGE"))
        self.assertLess(combined.index("MIDDLE PASSAGE"), combined.index("LAST PASSAGE"))
        self.assertEqual((Path(args.output) / "pages/first.txt.txt").read_bytes(), retained)
        self.assertIn("User note", readme.read_text())
        self.assertIn("independent extraction failure", readme.read_text())

    def test_best_effort_never_treats_changed_completed_text_as_a_page_failure(self):
        source = self.source()
        args = self.args([source], best_effort=True)
        scans.process(args)
        target = Path(args.output) / "pages/original.txt.txt"
        target.write_text("A human correction that must survive.")
        with self.assertRaises(common.IntegrityError):
            scans.process(args)
        self.assertEqual(target.read_text(), "A human correction that must survive.")

    def test_best_effort_stops_if_a_recorded_original_changes(self):
        source = self.source()
        args = self.args([source], best_effort=True)
        scans.process(args)
        source.write_text("Changed original")
        with self.assertRaisesRegex(ValueError, "Source bytes changed"):
            scans.process(args)

    def test_best_effort_isolates_unavailable_intake_and_records_exact_receipts(self):
        sources = [self.source("available.txt"), self.source("unavailable.txt")]
        args = self.args(sources, best_effort=True, fetch=True)
        def intake(paths):
            ok = paths[0].name != "unavailable.txt"
            return {"success": ok, "fixture_source": str(paths[0])}, ok
        with patch("agent_transcribe.common.fetch", side_effect=intake) as fetcher:
            result = scans.process(args)
        self.assertEqual([c.args[0] for c in fetcher.call_args_list], [[p] for p in sources])
        self.assertEqual(result["extracted"], 1)
        self.assertEqual(result["failures"][0]["stage"], "intake")
        self.assertTrue((Path(args.output) / ".intake/unavailable.txt.json").is_file())
        self.assertNotIn(str(sources[1]), self.state(args)["sources"])
        with patch("agent_transcribe.common.fetch", return_value=({"success": True}, True)):
            resumed = scans.process(args)
        self.assertTrue(resumed["extraction_complete"])
        self.assertTrue((Path(args.output) / ".intake/unavailable.txt.json.previous").is_file())

    def test_publication_failure_stops_best_effort_without_losing_prepared_bytes(self):
        sources = [self.source("first.txt"), self.source("second.txt")]
        args = self.args(sources, best_effort=True)
        actual = common.atomic_copy
        def disk_full(path, source):
            if Path(path).name == "first.txt.txt":
                raise OSError(errno.ENOSPC, "synthetic full output disk")
            return actual(path, source)
        with patch("agent_transcribe.common.atomic_copy", side_effect=disk_full):
            with self.assertRaises(OSError):
                scans.process(args)
        state = self.state(args)
        self.assertIn("prepared_outputs", state["steps"]["first.txt"])
        self.assertNotIn("second.txt", state["steps"])
        with patch("agent_transcribe.scans.extract_page", wraps=scans.extract_page) as extractor:
            result = scans.process(args)
        self.assertTrue(result["extraction_complete"])
        self.assertEqual(extractor.call_count, 1)
        self.assertEqual(extractor.call_args.args[0], sources[1])

    def test_unavailable_original_does_not_hide_modified_completed_text(self):
        source = self.source()
        args = self.args([source], best_effort=True, fetch=True)
        with patch("agent_transcribe.common.fetch", return_value=({"success": True}, True)):
            scans.process(args)
        with patch("agent_transcribe.common.fetch", return_value=({"success": False}, False)):
            partial = scans.process(args)
        self.assertFalse(partial["extraction_complete"])
        self.assertEqual(partial["extracted"], 1)
        target = Path(args.output) / "pages/original.txt.txt"
        target.write_text("User change while the original is unavailable")
        with patch("agent_transcribe.common.fetch", return_value=({"success": False}, False)):
            with self.assertRaises(common.IntegrityError):
                scans.process(args)
        self.assertIn("User change", target.read_text())

    def test_ordered_list_handles_spaces_and_rejects_changed_scope_on_resume(self):
        first = self.source("z source.txt", "FIRST")
        second = self.source("a # source.txt", "SECOND")
        listing = self.root / "selection.txt"
        listing.write_bytes(("\ufeffz source.txt\r\n\r\na # source.txt\r\n").encode())
        args = self.args([], source=None, sources_file=str(listing))
        scans.process(args)
        combined = (Path(args.output) / "document.txt").read_text()
        self.assertLess(combined.index("FIRST"), combined.index("SECOND"))
        self.assertEqual(self.state(args)["specification"]["sources"], [first.name, second.name])
        listing.write_text("a # source.txt\nz source.txt\n")
        with self.assertRaisesRegex(ValueError, "Inputs/settings differ"):
            scans.process(args)

    def test_cli_reports_partial_coverage_and_keeps_other_text(self):
        good = self.source("good.txt", "SEARCHABLE GOOD TEXT")
        unsupported = self.source("context.bin", "retained contextual bytes")
        args = self.args([good, unsupported])
        runner = Path(__file__).resolve().parents[1] / "process.py"
        result = subprocess.run([sys.executable, runner, "scan", "--source", str(good),
                                 "--source", str(unsupported), "--output", args.output, "--best-effort"],
                                capture_output=True)
        self.assertEqual(result.returncode, 2, result.stderr.decode())
        payload = json.loads(result.stdout)
        self.assertFalse(payload["extraction_complete"])
        self.assertEqual(payload["extracted"], 1)
        self.assertTrue((Path(args.output) / "pages/good.txt.txt").is_file())
        self.assertEqual(unsupported.read_text(), "retained contextual bytes")

    def test_regrouping_reuses_completed_receipt_without_another_extraction(self):
        sources = [self.source("first.txt", "FIRST"), self.source("second.txt", "SECOND")]
        intake_args = self.args(sources, folder="identification")
        scans.process(intake_args)
        receipt = Path(intake_args.output) / ".processing.json"
        args = self.args(list(reversed(sources)), folder="document", reuse_index=[str(receipt)])
        with patch("agent_transcribe.scans.extract_page", side_effect=AssertionError("Reuse discovery text")):
            scans.process(args)
        combined = (Path(args.output) / "document.txt").read_text()
        self.assertLess(combined.index("SECOND"), combined.index("FIRST"))
        old = json.loads(receipt.read_text())
        old["sources"][sources[0].name]["verified_at"] = "2026-10-10T00:00:00+00:00"
        receipt.write_text(json.dumps(old))
        with patch("agent_transcribe.scans.extract_page", side_effect=AssertionError("Do not rerun")):
            scans.process(args)
        target = Path(intake_args.output) / "pages/first.txt.txt"
        target.write_text("Changed retained baseline")
        with self.assertRaisesRegex(common.IntegrityError, "Retained scan text was modified"):
            scans.process(args)
        self.assertEqual(target.read_text(), "Changed retained baseline")

    def test_unsupported_adapter_resume_preserves_receipt_and_outputs(self):
        source = self.source()
        args = self.args([source])
        scans.process(args)
        receipt = Path(args.output) / ".processing.json"
        output_before = (Path(args.output) / "document.txt").read_bytes()
        for adapter in (2, 3, 5):
            state = self.state(args)
            state["specification"]["settings"]["adapter_version"] = adapter
            receipt.write_text(json.dumps(state))
            saved = receipt.read_bytes()
            with patch("agent_transcribe.scans.extract_page", side_effect=AssertionError("Do not rerun")):
                with self.assertRaisesRegex(ValueError, "Unsupported scan processing format"):
                    scans.process(args)
            self.assertEqual(receipt.read_bytes(), saved)
            self.assertEqual((Path(args.output) / "document.txt").read_bytes(), output_before)

    def test_hundred_source_discovery_batch_preserves_order_and_user_metadata(self):
        sources = [self.source(f"capture-{i:03d}.txt", f"UNIQUE PASSAGE {i:03d}\n") for i in range(100)]
        sources.reverse()
        listing = self.root / "hundred.txt"
        listing.write_text("\n".join(p.name for p in sources))
        args = self.args([], source=None, sources_file=str(listing))
        output = Path(args.output)
        output.mkdir()
        readme = output / "README.md"
        readme.write_text("User-owned provisional batch description.\n")
        originals = {p: common.checksum(p) for p in sources}
        result = scans.process(args)
        self.assertEqual(result["extracted"], 100)
        self.assertTrue(result["extraction_complete"])
        combined = (output / "document.txt").read_text()
        positions = [combined.index(f"UNIQUE PASSAGE {i:03d}") for i in reversed(range(100))]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(readme.read_text(), "User-owned provisional batch description.\n")
        self.assertEqual({p: common.checksum(p) for p in sources}, originals)
        with patch("agent_transcribe.scans.extract_page", side_effect=AssertionError("Reuse all hundred")):
            scans.process(args)
        self.assertEqual(list(output.glob(".stage-*")), [])


class PreparedPublication(ScanCase):
    def exercise(self, output, source, calls):
        def build(tmp):
            calls.append(1)
            files = {}
            for extension in ("txt", "pdf", "tsv"):
                path = tmp / ("page." + extension)
                path.write_text(f"{extension} generation {len(calls)}")
                files["pages/page." + extension] = path
            return files, {"review_required": True}
        with common.job(output, "fixture", [source], {}, False) as current:
            current.step("page", build)

    def interrupt(self, output, source, calls, extension):
        actual = common.atomic_copy
        def interrupted(path, source):
            actual(path, source)
            if Path(path).name == "page." + extension:
                raise KeyboardInterrupt("synthetic publication interruption")
        with patch("agent_transcribe.common.atomic_copy", side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.exercise(output, source, calls)

    def test_volatile_outputs_resume_from_exact_bytes_at_every_publication_boundary(self):
        source = self.source()
        original = common.checksum(source)
        for extension in ("txt", "pdf", "tsv"):
            with self.subTest(after=extension):
                output, calls = self.root / extension, []
                self.interrupt(output, source, calls, extension)
                self.exercise(output, source, calls)
                self.assertEqual(len(calls), 1)
                for suffix in ("txt", "pdf", "tsv"):
                    self.assertEqual((output / ("pages/page." + suffix)).read_text(), f"{suffix} generation 1")
                self.assertEqual(list(output.glob(".stage-*")), [])
                self.assertEqual(common.checksum(source), original)

    def test_missing_modified_or_untracked_prepared_files_stop_without_regeneration(self):
        source = self.source()
        for failure in ("missing", "modified", "untracked"):
            with self.subTest(failure=failure):
                output, calls = self.root / failure, []
                self.interrupt(output, source, calls, "txt")
                state = json.loads((output / ".processing.json").read_text())
                step = state["steps"]["page"]
                stage = output / step["stage"]
                target = stage / step["prepared_outputs"]["pages/page.pdf"]["file"]
                if failure == "missing":
                    target.unlink()
                elif failure == "modified":
                    target.write_text("Human annotation in a prepared file")
                else:
                    (stage / "human-note.txt").write_text("Preserve this note")
                before = (output / "pages/page.txt").read_bytes()
                with self.assertRaises(common.IntegrityError):
                    self.exercise(output, source, calls)
                self.assertEqual(len(calls), 1)
                self.assertEqual((output / "pages/page.txt").read_bytes(), before)
                if failure == "modified":
                    self.assertIn("Human annotation", target.read_text())
                if failure == "untracked":
                    self.assertEqual((stage / "human-note.txt").read_text(), "Preserve this note")

    def test_interruption_before_prepared_checkpoint_preserves_old_stage_on_retry(self):
        source, output, calls = self.source(), self.root / "before-prepared", []
        actual = common.atomic_copy
        def interrupted(path, source):
            actual(path, source)
            if Path(path).name == "output-0000":
                raise KeyboardInterrupt("before prepared checkpoint")
        with patch("agent_transcribe.common.atomic_copy", side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.exercise(output, source, calls)
        first = json.loads((output / ".processing.json").read_text())["steps"]["page"]["stage"]
        self.exercise(output, source, calls)
        self.assertEqual(len(calls), 2)
        self.assertTrue((output / first / "output-0000").is_file())
        state = json.loads((output / ".processing.json").read_text())
        self.assertIn(first, state["steps"]["page"]["previous_stages"])

    def test_a_change_during_publication_is_detected_and_preserved(self):
        source, output, calls = self.source(), self.root / "changed-during-copy", []
        actual = common.atomic_copy
        def changed(path, source):
            actual(path, source)
            if Path(path).name == "page.pdf":
                Path(path).write_text("Unexpected change during publication")
        with patch("agent_transcribe.common.atomic_copy", side_effect=changed):
            with self.assertRaisesRegex(common.IntegrityError, "differs from prepared"):
                self.exercise(output, source, calls)
        self.assertEqual((output / "pages/page.pdf").read_text(), "Unexpected change during publication")
        self.assertEqual(len(calls), 1)
        with self.assertRaises(common.IntegrityError):
            self.exercise(output, source, calls)
        self.assertEqual(len(calls), 1)


class RealScanLocators(ScanCase):
    def image(self, name, rotated=False):
        from PIL import Image, ImageDraw, ImageFont
        path = self.root / name
        image = Image.new("RGB", (1200, 600), "white")
        font = fixture_font(42)
        ImageDraw.Draw(image).text((60, 100), "ARCHIVE SEARCH FIXTURE", font=font, fill="black")
        if rotated:
            image = image.rotate(90, expand=True)
        image.save(path)
        return path

    def test_real_pdf_interruption_finishes_without_another_ocr_call(self):
        source = self.image("capture.png")
        args = self.args([source], pdf=True)
        actual = common.atomic_copy
        def interrupted(path, source):
            actual(path, source)
            if Path(path).name == "capture.png.pdf":
                raise KeyboardInterrupt("after real PDF publication")
        with patch("agent_transcribe.common.atomic_copy", side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                scans.process(args)
        before = (Path(args.output) / "pages/capture.png.pdf").read_bytes()
        with patch("agent_transcribe.scans.extract_page", side_effect=AssertionError("Use prepared PDF")):
            result = scans.process(args)
        self.assertTrue(result["extraction_complete"])
        self.assertEqual((Path(args.output) / "document.pdf").read_bytes(), before)
        text = subprocess.check_output(["pdftotext", str(Path(args.output) / "document.pdf"), "-"]).decode()
        self.assertIn("ARCHIVE SEARCH FIXTURE", " ".join(text.split()))

    def test_explicit_rotation_retry_extracts_text_without_changing_original(self):
        source = self.image("rotated.png", rotated=True)
        before = common.checksum(source)
        args = self.args([source], rotate_clockwise=90, psm=6)
        scans.process(args)
        self.assertIn("ARCHIVE SEARCH FIXTURE", (Path(args.output) / "document.txt").read_text())
        self.assertEqual(common.checksum(source), before)
        self.assertEqual(self.state(args)["specification"]["settings"]["rotate_clockwise"], 90)
        args.psm = 11
        with self.assertRaisesRegex(ValueError, "Inputs/settings differ"):
            scans.process(args)

    def test_pdf_page_search_files_retain_physical_locators_and_blank_gaps(self):
        from PIL import Image
        source = self.root / "compound.pdf"
        page = Image.new("RGB", (100, 100), "white")
        page.save(source, save_all=True, append_images=[page])
        retained, index = self.retained(source, "DISCOVERY TOKEN\f\f")
        args = self.args([source], reuse_index=[str(index)])
        result = scans.process(args)
        output = Path(args.output)
        first = (output / "pages/compound.pdf.pages/page-0001.txt").read_text()
        second = (output / "pages/compound.pdf.pages/page-0002.txt").read_text()
        self.assertIn(str(source), first)
        self.assertIn("Physical PDF page: 1", first)
        self.assertIn("DISCOVERY TOKEN", first)
        self.assertIn("Physical PDF page: 2", second)
        self.assertIn("NO EXTRACTED TEXT", second)
        self.assertEqual((output / "pages/compound.pdf.txt").read_bytes(), retained.read_bytes())
        self.assertIn("=== Physical PDF page: 2 ===", (output / "document.txt").read_text())
        self.assertEqual(result["text_gaps"][0]["blank_pdf_pages"], [2])

    def test_local_ocr_of_image_pdf_retains_original_and_searchable_page_text(self):
        from PIL import Image
        image_source = self.image("page.png")
        source = self.root / "image-only.pdf"
        with Image.open(image_source) as image:
            image.save(source, resolution=150)
        before = common.checksum(source)
        args = self.args([source], ocr_image_pdfs=True)
        result = scans.process(args)
        self.assertTrue(result["extraction_complete"])
        self.assertEqual(result["text_gaps"], [])
        self.assertEqual(common.checksum(source), before)
        output = Path(args.output)
        leaf = (output / "pages/image-only.pdf.pages/page-0001.txt").read_text()
        self.assertIn("ARCHIVE SEARCH FIXTURE", leaf)
        self.assertIn("Physical PDF page: 1", leaf)
        self.assertTrue((output / "pages/image-only.pdf.pdf").is_file())


if __name__ == "__main__":
    unittest.main()
