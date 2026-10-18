import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from _support import fixture_font

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_transcribe.common import job, checksum, prepare_sources, run, fetch, atomic, atomic_copy
from agent_transcribe import scans


class SafetyAndResume(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "original.txt"
        self.source.write_text("Original record.\n")
        self.output = self.root / "derivative"
        self.calls = 0

    def build(self, tmp):
        self.calls += 1
        (tmp / "result.txt").write_text("Extracted text")
        return {"pages/result.txt": tmp / "result.txt"}, {"checked": False}

    def execute(self):
        with job(self.output, "test", [self.source], {}, False) as current:
            current.step("page", self.build)

    def test_resume_skips_completed_work_and_preserves_source(self):
        before = checksum(self.source)
        self.execute()
        self.execute()
        self.assertEqual(self.calls, 1)
        self.assertEqual(checksum(self.source), before)

    def test_missing_or_modified_output_is_not_silently_overwritten(self):
        self.execute()
        (self.output / "pages/result.txt").write_text("Human correction")
        with self.assertRaisesRegex(ValueError, "modified"):
            self.execute()
        self.assertEqual((self.output / "pages/result.txt").read_text(), "Human correction")

    def test_untracked_output_is_never_overwritten(self):
        with job(self.output, "test", [self.source], {}, False) as current:
            target = self.output / "pages/result.txt"
            target.parent.mkdir()
            target.write_text("An existing unrelated file")
            with self.assertRaisesRegex(ValueError, "refusing to overwrite"):
                current.step("page", self.build)
            self.assertEqual(target.read_text(), "An existing unrelated file")

    def interrupt_after_output_publication(self):
        def interrupted(path, data, **options):
            atomic_copy(path, data, **options)
            if Path(path).name == "result.txt":
                raise KeyboardInterrupt()
        with patch("agent_transcribe.common.atomic_copy", side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.execute()

    def test_resume_after_interrupted_output_publication(self):
        self.interrupt_after_output_publication()
        before = checksum(self.output / "pages/result.txt")
        self.execute()
        self.assertEqual(checksum(self.output / "pages/result.txt"), before)
        self.assertEqual(self.calls, 1)

    def test_modified_incomplete_output_is_never_overwritten(self):
        self.interrupt_after_output_publication()
        target = self.output / "pages/result.txt"
        target.write_text("Human correction after interruption")
        with self.assertRaisesRegex(ValueError, "refusing to overwrite"):
            self.execute()
        self.assertEqual(target.read_text(), "Human correction after interruption")

    def test_atomic_creation_preserves_existing_file(self):
        with self.assertRaises(FileExistsError):
            atomic(self.source, b"Replacement", replace=False)
        self.assertEqual(self.source.read_text(), "Original record.\n")

    def test_changed_source_rejects_stale_receipt(self):
        self.execute()
        self.source.write_text("Changed source")
        with self.assertRaisesRegex(ValueError, "Source bytes changed"):
            self.execute()

    def test_interrupted_step_resumes(self):
        def interrupted(tmp):
            (tmp / "partial").write_text("incomplete")
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            with job(self.output, "test", [self.source], {}, False) as current:
                current.step("page", interrupted)
        state = json.loads((self.output / ".processing.json").read_text())
        self.assertEqual(state["steps"]["page"]["status"], "failed")
        self.execute()
        state = json.loads((self.output / ".processing.json").read_text())
        self.assertEqual(state["steps"]["page"]["attempts"], 2)

    def test_concurrent_writer_rejected(self):
        with job(self.output, "test", [self.source], {}, False):
            with self.assertRaisesRegex(RuntimeError, "active writer"):
                with job(self.output, "test", [self.source], {}, False):
                    pass

    def test_source_cannot_be_inside_output_folder(self):
        with self.assertRaisesRegex(ValueError, "original source"):
            with job(self.root, "test", [self.source], {}, False):
                pass

    def test_duplicate_inputs_rejected(self):
        with self.assertRaises(ValueError):
            prepare_sources([self.source, self.source])

    def test_parent_alias_cannot_hide_duplicate_or_icloud_input(self):
        (self.root / "alias").symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            prepare_sources([self.source, self.root / "alias" / self.source.name])
        cloud = self.root / "com~apple~CloudDocs"
        cloud.mkdir()
        (cloud / "original.txt").write_text("fixture")
        (self.root / "cloud-alias").symlink_to(cloud, target_is_directory=True)
        sources = prepare_sources([self.root / "cloud-alias/original.txt"])
        with self.assertRaisesRegex(ValueError, "Use --fetch"):
            with job(self.output, "test", sources, {}, False):
                self.fail("A parent alias must not bypass icmat")

    def test_shared_job_checks_source_on_exit(self):
        with self.assertRaisesRegex(RuntimeError, "Original changed"):
            with job(self.output, "test", [self.source], {}, False) as current:
                current.step("page", self.build)
                self.source.write_text("Changed while the adapter was running")

    def test_path_traversal_and_symlink_escape_rejected(self):
        with job(self.output, "test", [self.source], {}, False) as current:
            with self.assertRaises(ValueError):
                current.output_path("../original.txt")
            (self.output / "escape").symlink_to(self.root, target_is_directory=True)
            with self.assertRaises(ValueError):
                current.output_path("escape/original.txt")

    def test_readme_corrections_survive(self):
        self.output.mkdir()
        (self.output / "README.md").write_text("Human metadata and evidence")
        with job(self.output, "test", [self.source], {}, False) as current:
            current.write_readme("Generated content")
        self.assertEqual((self.output / "README.md").read_text(), "Human metadata and evidence")

    def test_folder_rename_preserves_resume(self):
        self.execute()
        renamed = self.root / "Better document title"
        self.output.rename(renamed)
        self.output = renamed
        self.execute()
        self.assertEqual(self.calls, 1)

    def test_recipe_change_requires_separate_variant(self):
        self.execute()
        with self.assertRaisesRegex(ValueError, "Inputs/settings differ"):
            with job(self.output, "test", [self.source], {"different": True}, False):
                pass

    def test_incomplete_icmat_stops_before_processing(self):
        with patch("agent_transcribe.common.fetch", return_value=({"success": False}, False)):
            with self.assertRaisesRegex(RuntimeError, "icmat"):
                with job(self.output, "test", [self.source], {}, True):
                    self.fail("Must not process after incomplete materialization")
        self.assertTrue((self.output / "icmat-receipt.json").exists())

    def test_external_command_timeout_is_bounded(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            run([sys.executable, "-c", "import time; time.sleep(10)"], timeout=0.05)

    def test_timeout_stops_children_after_parent_has_exited(self):
        marker = self.root / "surviving-child.txt"
        child = ("import pathlib,time; time.sleep(0.5); "
                 f"pathlib.Path({str(marker)!r}).write_text('still running')")
        parent = f"import subprocess,sys; subprocess.Popen([sys.executable, '-c', {child!r}])"
        with self.assertRaises(subprocess.TimeoutExpired):
            run([sys.executable, "-c", parent], timeout=0.15)
        time.sleep(0.6)
        self.assertFalse(marker.exists(), "A child process escaped timeout cleanup")

    def test_external_command_failure_preserves_diagnostic(self):
        with self.assertRaisesRegex(RuntimeError, "expected diagnostic"):
            run([sys.executable, "-c", "import sys; print('expected diagnostic',file=sys.stderr); sys.exit(2)"])

    def test_icmat_failure_receipt_uses_shared_command_runner(self):
        receipt = {"success": True, "enumerationComplete": True, "failures": [],
                   "counts": {"filesVerified": 1, "regularFiles": 1, "datalessFiles": 0}}
        with patch("agent_transcribe.common.run", return_value=subprocess.CompletedProcess(
                [], 1, json.dumps(receipt).encode(), b"failure")) as command:
            saved, ok = fetch([self.source])
        self.assertEqual(saved, receipt)
        self.assertFalse(ok)
        self.assertFalse(command.call_args.kwargs["check"])

    def test_icloud_source_requires_installed_icmat_route(self):
        source = self.root / "com~apple~CloudDocs" / "original.txt"
        source.parent.mkdir()
        source.write_text("fixture")
        with self.assertRaisesRegex(ValueError, "Use --fetch"):
            with job(self.output, "test", [source], {}, False):
                self.fail("iCloud input must go through icmat")


class RealScanTools(unittest.TestCase):
    def test_text_only_scan_avoids_pdf_and_upright_image_conversion(self):
        from PIL import Image, ImageDraw, ImageFont
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "upright.jpg"
            image = Image.new("RGB", (1200, 400), "white")
            font = fixture_font(42)
            ImageDraw.Draw(image).text((60, 100), "TEXT ONLY ARCHIVE TEST", font=font, fill="black")
            image.save(source)
            args = argparse.Namespace(source=[str(source)], output=str(root / "text only"),
                                      reuse_index=[], fetch=False, pdf=False)
            with patch("agent_transcribe.scans.run", wraps=run) as command:
                scans.process(args)
            self.assertIn("TEXT ONLY ARCHIVE TEST", (root / "text only/document.txt").read_text())
            self.assertEqual(list((root / "text only").rglob("*.pdf")), [])
            ocr_calls = [c.args[0] for c in command.call_args_list if "--psm" in c.args[0]]
            self.assertEqual(ocr_calls[0][1], source.resolve())

    def test_native_text_does_not_require_ocr_software(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "original.txt"
            source.write_text("Native source text")
            args = argparse.Namespace(source=[str(source)], output=str(root / "native"),
                                      reuse_index=[], fetch=False, pdf=False)
            with patch("agent_transcribe.scans.run", side_effect=AssertionError("No OCR tool needed")):
                scans.process(args)
            self.assertIn("Native source text", (root / "native/document.txt").read_text())

    def test_image_ocr_pdf_locators_and_resume(self):
        from PIL import Image, ImageDraw, ImageFont
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / "original photograph.png"
            image = Image.new("RGB", (1200, 1600), "white")
            font = fixture_font(42)
            draw = ImageDraw.Draw(image)
            draw.text((90, 100), "ARCHIVE WORKFLOW TEST", font=font, fill="black")
            draw.text((90, 200), "Original filename and source locator.", font=font, fill="black")
            image.save(original)
            before = checksum(original)
            args = argparse.Namespace(source=[str(original)], output=str(root / "Document title"),
                                      reuse_index=[], fetch=False, pdf=True)
            first = scans.process(args)
            pdf = root / "Document title/document.pdf"
            self.assertTrue(pdf.is_file())
            text = subprocess.check_output(["pdftotext", str(pdf), "-"]).decode()
            self.assertIn("ARCHIVE WORKFLOW TEST", " ".join(text.split()))
            self.assertEqual(checksum(original), before)
            receipt = json.loads((pdf.parent / ".processing.json").read_text())
            scans.process(args)
            repeated = json.loads((pdf.parent / ".processing.json").read_text())
            self.assertEqual(receipt["steps"], repeated["steps"])
            self.assertIn("../original%20photograph.png", (pdf.parent / "README.md").read_text())

    def test_existing_text_reuse_with_source_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / "original.txt"
            original.write_text("Original source bytes")
            extract = root / "old-extract.txt"
            extract.write_text("Retained OCR transcription")
            index = root / "index.json"
            index.write_text(json.dumps({"schema": 1, "kind": "retained-text", "entries": [
                {"source": str(original), "text": str(extract), "source_sha256": checksum(original),
                 "text_sha256": checksum(extract)}]}))
            args = argparse.Namespace(source=[str(original)], output=str(root / "copy"),
                                      reuse_index=[str(index)], fetch=False, pdf=False)
            with patch("agent_transcribe.scans.extract_page", side_effect=AssertionError("Must reuse retained text")):
                scans.process(args)
            self.assertIn("Retained OCR transcription", (root / "copy/document.txt").read_text())

    def test_retained_paths_use_explicit_base_instead_of_ancestor_search(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            support = root / "supporting"
            support.mkdir()
            source, extract = root / "original.txt", support / "text.txt"
            source.write_text("Original bytes")
            extract.write_text("Existing OCR")
            index = support / "index.json"
            index.write_text(json.dumps({"schema": 1, "kind": "retained-text", "entries": [
                {"source": "original.txt", "text": "supporting/text.txt", "source_sha256": checksum(source),
                 "text_sha256": checksum(extract)}]}))
            args = argparse.Namespace(source=[str(source)], output=str(root / "reused"),
                                      reuse_index=[str(index)], reuse_root=str(root), fetch=False, pdf=False)
            with patch("agent_transcribe.scans.extract_page", side_effect=AssertionError("Must reuse")):
                scans.process(args)
            self.assertIn("Existing OCR", (root / "reused/document.txt").read_text())

    def test_conflicting_reuse_records_are_not_silently_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "original.txt"
            source.write_text("Original bytes")
            rows = []
            for name in ("first.txt", "second.txt"):
                (root / name).write_text(name)
                rows.append({"source": str(source), "text": name, "source_sha256": checksum(source),
                             "text_sha256": checksum(root / name)})
            index = root / "index.json"
            index.write_text(json.dumps({"schema": 1, "kind": "retained-text", "entries": rows}))
            with self.assertRaisesRegex(ValueError, "Conflicting retained extracts"):
                scans.reuse_records([index], prepare_sources([source]))

    def test_reused_pdf_text_still_checks_physical_page_boundaries(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "two blank pages.pdf"
            page = Image.new("RGB", (100, 100), "white")
            page.save(source, save_all=True, append_images=[page])
            extract = root / "old.txt"
            extract.write_text("First page text\f\f")
            index = root / "index.json"
            index.write_text(json.dumps({"schema": 1, "kind": "retained-text", "entries": [
                {"source": str(source), "text": str(extract), "source_sha256": checksum(source),
                 "text_sha256": checksum(extract)}]}))
            args = argparse.Namespace(source=[str(source)], output=str(root / "review"),
                                      reuse_index=[str(index)], fetch=False, pdf=False)
            scans.process(args)
            self.assertIn("physical PDF pages 2", (root / "review/README.md").read_text())
            self.assertEqual(list((root / "review").rglob("*.pdf")), [])


if __name__ == "__main__":
    unittest.main()
