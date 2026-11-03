"""Public conversion commands and compatibility routes, using synthetic sources."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_transcribe import cli

REPO = Path(__file__).resolve().parents[1]
RUNNER = REPO / "process.py"


class ConversionCLI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.originals = self.root / "originals"
        self.originals.mkdir()
        self.source = self.originals / "selected.txt"
        self.source.write_text("Synthetic retained source.\nLine two.\n")
        self.output = self.root / "bundle"

    def command(self, *args, expected=0):
        result = subprocess.run([sys.executable, str(RUNNER), *map(str, args)], capture_output=True, text=True)
        self.assertEqual(result.returncode, expected, (result.stderr or result.stdout)[:4000])
        return json.loads(result.stdout) if result.stdout.strip() else result.stderr

    def test_metadata_plan_without_inference_or_payload_hash(self):
        before = self.source.stat()
        with patch("agent_transcribe.docling_backend.preflight", side_effect=AssertionError("inference preflight forbidden")):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(cli.main(["convert", str(self.originals), "--output", str(self.output), "--plan"]), 0)
            plan = json.loads(output.getvalue())
        self.assertEqual([s["relative_path"] for s in plan["sources"]], [self.source.name])
        self.assertIsNone(plan["sources"][0]["sha256"])
        self.assertFalse(self.output.exists())
        self.assertEqual(self.source.stat().st_mtime_ns, before.st_mtime_ns)

    def test_plan_is_not_fetch_or_inference(self):
        for option in ("--fetch", "--retry-failed", "--artifacts-path=/models", "--rotate-clockwise=90", "--use-pinned-notes", "--raster-images-scale=1"):
            self.command("convert", self.originals, "--output", self.output, "--plan", option, expected=2)
        self.assertFalse(self.output.exists())

    def test_txt_convert_verify_resume_and_frozen_discovery(self):
        original_hash = hashlib.sha256(self.source.read_bytes()).hexdigest()
        result = self.command("convert", self.originals, "--output", self.output)
        self.assertTrue(result["publication_complete"])
        verified = self.command("verify", self.output)
        self.assertTrue(verified["valid"])
        copies = self.output / "Originals" / self.source.name
        self.assertEqual(hashlib.sha256(copies.read_bytes()).hexdigest(), original_hash)
        item = self.output / result["sources"][0]["item_relative"] / "reading.md"
        accepted = item.read_bytes()
        (self.originals / "new.txt").write_text("New material is outside frozen scope.")
        resumed = self.command("convert", self.originals, "--output", self.output)
        self.assertEqual(len(resumed["sources"]), 1)
        self.assertEqual(item.read_bytes(), accepted)
        self.command("convert", self.originals, "--output", self.output, "--recursive", expected=1)
        self.source.write_text("Changed original")
        self.command("convert", self.originals, "--output", self.output, expected=1)

    def test_sources_file_control_is_frozen(self):
        sources = self.root / "sources.txt"
        sources.write_text("originals/selected.txt\n")
        self.command("convert", self.originals, "--output", self.output, "--sources-file", sources)
        sources.write_text("originals/selected.txt\n\n")
        self.command("convert", self.originals, "--output", self.output, "--sources-file", sources, expected=1)

    def test_explicit_pinned_note_resume_keeps_frozen_text(self):
        from agent_transcribe import filing
        from agent_transcribe.common import checksum
        texts = self.root / "texts"
        texts.mkdir()
        (texts / "accepted.txt").write_text("Accepted synthetic reading\n")
        recipe = self.root / "recipe.json"
        recipe.write_text(json.dumps({"schema": 1, "source_root": "originals", "text_root": "texts",
            "assignments": [{"source": self.source.name, "document": "Known group", "description": "Capture",
                "disposition": "document", "source_sha256": checksum(self.source),
                "preferred_text": {"path": "accepted.txt", "sha256": checksum(texts / "accepted.txt")}}]}))
        organized = self.root / "organized"
        filing.file_assignments(recipe, organized)
        args = ("convert", self.originals, "--output", self.output, "--reuse-from", organized)
        first = self.command(*args)
        item = self.output / first["sources"][0]["item_relative"]
        preserved = {p.name: p.read_bytes() for p in item.iterdir() if p.is_file()}
        notes = organized / "Known group" / "README.md"
        notes.write_text(notes.read_text() + "\nNew review observations.\n")
        self.command(*args, expected=1)
        resumed = self.command(*args, "--use-pinned-notes")
        self.assertEqual(resumed["sources"][0]["source"]["reuse"]["notes_text"], first["sources"][0]["source"]["reuse"]["notes_text"])
        self.assertEqual(preserved, {p.name: p.read_bytes() for p in item.iterdir() if p.is_file()})
        self.assertIn("New review observations", notes.read_text())

    def test_resume_normalizes_dot_segments_and_rejects_control_symlinks(self):
        selection = self.root / "list.txt"
        selection.write_text("originals/selected.txt\n")
        destination = self.root / "unused" / ".." / "bundle"
        (self.root / "unused").mkdir()
        self.command("convert", self.originals, "--output", destination, "--sources-file", selection)
        self.command("convert", self.originals, "--output", destination, "--sources-file", selection)
        link = self.root / "linked-list.txt"
        link.symlink_to(selection)
        self.command("convert", self.originals, "--output", destination, "--sources-file", link, expected=1)

    def test_malformed_existing_receipt_is_configuration_failure(self):
        self.output.mkdir()
        for malformed in ([], {"kind": "docling-folder", "schema": 1, "specification": {"sources": "bad"}}):
            (self.output / "conversion.json").write_text(json.dumps(malformed))
            self.command("convert", self.originals, "--output", self.output, expected=1)

    def test_verify_tamper_is_failure(self):
        result = self.command("convert", self.originals, "--output", self.output)
        item = self.output / result["sources"][0]["item_relative"] / "reading.md"
        item.write_text("Unrecorded change")
        result = self.command("verify", self.output, expected=1)
        self.assertFalse(result["valid"])

    def test_archive_bindings_object_and_deferred_scope(self):
        bindings = self.root / "bindings.json"
        bindings.write_text(json.dumps({"bindings": [{"name": "Synthetic", "source_root": "originals",
            "destination": "converted", "status": "STATUS.md", "scope_state": "defer"}], "exclusions": ["bindings.json"]}))
        plan = self.root / "plan.json"
        result = self.command("plan", self.root, "--output", plan, "--bindings", bindings)
        self.assertFalse(result["execution_authorized"])
        self.assertTrue(plan.with_suffix(".md").is_file())
        ran = self.command("convert", "--plan-file", plan, expected=2)
        self.assertTrue(all(j["state"] == "deferred" for j in ran["jobs"]))
        self.assertFalse((self.root / "converted").exists())

    def test_archive_ready_plan_list_and_fixed_sources(self):
        bindings = self.root / "bindings.json"
        bindings.write_text(json.dumps([{"name": "Synthetic", "source_root": "originals",
            "destination": "converted", "status": "STATUS.md", "scope_state": "ready"}]))
        plan = self.root / "plan.json"
        self.command("plan", self.root, "--output", plan, "--bindings", bindings,
                     "--exclude", "bindings.json", "--exclude", "plan.json", "--exclude", "plan.md")
        (self.originals / "later.txt").write_text("Outside frozen selection")
        ran = self.command("convert", "--plan-file", plan)
        self.assertEqual(len(ran["jobs"]), 1)
        ledger = json.loads((self.root / "converted" / "conversion.json").read_text())
        self.assertEqual(len(ledger["sources"]), 1)

    def test_invalid_combination_and_overlap(self):
        self.command("convert", expected=2)
        self.command("convert", self.originals, "--plan-file", "somewhere", expected=2)
        self.command("convert", self.originals, "--output", self.originals / "generated", "--plan", expected=1)
        self.command("verify", self.output, expected=1)

    def test_exit_codes_and_interruption_dispatch(self):
        arguments = ["convert", str(self.originals), "--output", str(self.output)]
        for code in (0, 2):
            with patch("agent_transcribe.conversion.convert_folder", return_value={"exit_code": code}), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(arguments), code)
        for failure, code in ((KeyboardInterrupt(), 130), (RuntimeError("test error"), 1)):
            with patch("agent_transcribe.conversion.convert_folder", side_effect=failure), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(arguments), code)

    def test_legacy_scan_and_reading_still_operate(self):
        job = self.root / "legacy"
        result = self.command("scan", "--source", self.source, "--output", job)
        self.assertTrue(result["extraction_complete"])
        read = self.command("reading", "--job", job, "--query", "Synthetic")
        self.assertTrue(read["matches"])
        for mode in ("scan", "file", "check", "reading", "reconcile"):
            proc = subprocess.run([sys.executable, str(RUNNER), mode, "--help"], capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_missing_models_fails_before_worker(self):
        with patch("agent_transcribe.conversion.preflight", return_value={"ready": False, "errors": ["missing local model"]}), patch("agent_transcribe.conversion._Worker", side_effect=AssertionError("worker must not start")):
            # Image suffix and bytes must match; Pillow is an optional legacy extra.
            try:
                from PIL import Image
            except ImportError:
                self.skipTest("Pillow unavailable")
            self.source.unlink()
            Image.new("RGB", (20, 20), "white").save(self.originals / "page.png")
            with contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(cli.main(["convert", str(self.originals), "--output", str(self.output)]), 1)
            self.assertIn("missing local model", err.getvalue())

    def test_isolated_installer_preflight_rejects_relative_and_legacy_paths(self):
        script = REPO / "scripts/install-convert.sh"
        cases = ({"AGENT_TRANSCRIBE_CONVERT_VENV": "relative"},
                 {"AGENT_TRANSCRIBE_CONVERT_BIN_DIR": "relative"},
                 {"AGENT_TRANSCRIBE_CONVERT_VENV": str(self.root / "same"), "AGENT_TRANSCRIBE_VENV": str(self.root / "same")})
        for changes in cases:
            run = subprocess.run([str(script)], env={**os.environ, **changes}, capture_output=True, text=True)
            self.assertNotEqual(run.returncode, 0)
        run = subprocess.run([str(script), "--help"], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0)


@unittest.skipUnless(os.environ.get("AGENT_TRANSCRIBE_TEST_MODELS") and importlib.util.find_spec("docling"),
                     "set AGENT_TRANSCRIBE_TEST_MODELS in the pinned runtime for real inference")
class RealConversionCLI(unittest.TestCase):
    setUp = ConversionCLI.setUp
    command = ConversionCLI.command
    def test_actual_pdf_tiff_docx_and_relocated_bundle(self):
        from PIL import Image, ImageDraw
        from reportlab.pdfgen.canvas import Canvas
        from docx import Document
        from _support import fixture_font
        self.source.unlink()
        pdf = self.originals / "synthetic.pdf"
        canvas = Canvas(str(pdf))
        canvas.drawString(72, 720, "Synthetic PDF physical page one")
        canvas.showPage()
        canvas.drawString(72, 720, "Synthetic PDF physical page two")
        canvas.save()
        frames = []
        for label in ("TIFF frame one", "TIFF frame two"):
            frame = Image.new("RGB", (900, 500), "white")
            ImageDraw.Draw(frame).text((50, 150), label, fill="black", font=fixture_font(38))
            frames.append(frame)
        frames[0].save(self.originals / "synthetic.tiff", save_all=True, append_images=frames[1:])
        document = Document()
        document.add_paragraph("Synthetic DOCX structural paragraph")
        document.save(self.originals / "synthetic.docx")
        result = self.command("convert", self.originals, "--output", self.output,
                              "--artifacts-path", os.environ["AGENT_TRANSCRIBE_TEST_MODELS"])
        self.assertEqual(len(result["sources"]), 3)
        for entry in result["sources"]:
            expected = 1 if entry["source"]["kind"] == "docx" else 2
            self.assertEqual(entry["inventory"]["expected_count"], expected)
        moved = self.root / "relocated"
        shutil.move(str(self.output), str(moved))
        self.assertFalse(self.output.exists())
        self.assertTrue(self.command("verify", moved)["valid"])


if __name__ == "__main__":
    unittest.main()
