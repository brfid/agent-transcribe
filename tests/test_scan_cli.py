"""Explicit scan/filing/reading CLI scopes; interviews use their own tools."""
from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

RUNNER = Path(__file__).resolve().parents[1] / "process.py"
sys.path.insert(0, str(RUNNER.parent / "src"))
from agent_transcribe import cli


class ScanCommand(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.originals = self.root / "originals"
        self.originals.mkdir()
        self.job = self.root / "job"
        self.destination = self.root / "organized"
        self.document = self.destination / "Descriptive statement"

    def command(self, *args, expected=0):
        result = subprocess.run([sys.executable, str(RUNNER), *map(str, args)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, expected, result.stderr or result.stdout)
        return json.loads(result.stdout)

    def source(self, name="nested/original.txt", text="Before\nA+B evidence\nAfter\nAlpha finding\n"):
        source = self.originals / name
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(text)
        return source

    def extract(self, source, *extra, expected=0):
        return self.command("scan", "--source", source, "--source-root", self.originals,
                            "--output", self.job, *extra, expected=expected)

    def file_source(self, source, *, extra=(), expected=0):
        state = json.loads((self.job / ".processing.json").read_text())
        key = source.relative_to(self.originals).as_posix()
        text_path = next(path for path in state["steps"][key]["outputs"] if path.endswith(".txt"))
        text = self.job / text_path
        rows = [{"source": key, "document": self.document.name, "description": "Statement",
                 "disposition": "document", "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                 "preferred_text": {"path": text_path, "sha256": hashlib.sha256(text.read_bytes()).hexdigest()}}]
        rows.extend(extra)
        recipe = self.root / "assignments.json"
        recipe.write_text(json.dumps({"schema": 1, "source_root": "originals", "text_root": "job",
                                     "assignments": rows}))
        return self.command("file", "--assignments", recipe, "--output", self.destination,
                            "--source-root", self.originals, "--text-root", self.job, expected=expected)

    def test_help_advertises_scan_file_reconcile_check_reading_and_excludes_audio(self):
        result = subprocess.run([sys.executable, RUNNER, "--help"], capture_output=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn(b"{scan,file,reconcile,check,reading}", result.stdout)
        self.assertNotIn(b"audio", result.stdout)

    def test_audio_mode_is_rejected_before_processing(self):
        result = subprocess.run([sys.executable, RUNNER, "audio", "--help"], capture_output=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn(b"invalid choice", result.stderr)

    def test_extract_file_check_and_preferred_literal_reading_end_to_end(self):
        source = self.source()
        extracted = self.extract(source)
        self.assertTrue(extracted["extraction_complete"])
        state = json.loads((self.job / ".processing.json").read_text())
        self.assertEqual(state["specification"]["sources"], ["nested/original.txt"])
        self.assertTrue(self.file_source(source)["filing_complete"])
        readme = self.document / "README.md"
        readme.write_text(readme.read_text() + "\nHuman note: ALPHA relevance.\n")
        checked = self.command("check", "--document", self.document, "--verify-sources")
        self.assertTrue(checked["valid"])
        self.assertTrue(checked["documents"][0]["source_bytes_verified"])
        selected = self.command("reading", "--document", self.document, "--document", self.document,
                                "--job", self.job, "--job", self.job)
        self.assertEqual(len(selected["files"]), 3)  # document note, source export, job note
        self.assertEqual(selected["jobs"][0]["preferred_filed_sources"], 1)
        self.assertFalse(any(path.endswith("OCR discovery text.txt") or "/pages/" in path
                             for path in selected["files"]))
        result = self.command("reading", "--document", self.document, "--job", self.job,
                              "--query", "a+b", "--query", "ALPHA", "--context", 1)
        self.assertEqual(len(result["matches"]), 3)
        self.assertEqual({match["kind"] for match in result["matches"]}, {"note", "source_text"})
        literal = next(match for match in result["matches"] if "a+b" in match["matched_terms"])
        self.assertEqual([line["text"] for line in literal["excerpt"]], ["Before", "A+B evidence", "After"])
        self.assertFalse(result["limit_reached"])
        limited = self.command("reading", "--document", self.document, "--query", "alpha", "--limit", 1,
                               "--context", 0)
        self.assertEqual(len(limited["matches"]), 1)
        self.assertEqual(len(limited["matches"][0]["excerpt"]), 1)
        self.assertTrue(limited["limit_reached"])
        absent = self.command("reading", "--document", self.document, "--query", "not in this record")
        self.assertEqual(absent["matches"], [])
        self.assertTrue(absent["complete"])

    def test_pending_extraction_fallback_and_declared_gaps_remain_readable(self):
        source = self.source()
        missing = self.originals / "absent.txt"
        result = self.extract(source, "--source", missing, "--best-effort", expected=2)
        self.assertTrue(result["partial_text"])
        read = self.command("reading", "--job", self.job, "--query", "A+B", expected=2)
        self.assertEqual(len(read["matches"]), 1)
        self.assertEqual(read["jobs"][0]["per_source_text"], 1)
        self.assertEqual(read["gaps"][0]["kind"], "unavailable_text")
        self.assertNotIn(result["partial_text"], read["files"])
        missing_row = {"source": "absent.txt", "document": self.document.name,
                       "description": "Unavailable enclosure", "disposition": "unavailable",
                       "reason": "Declared source absent", "gap_reason": "No source text"}
        self.file_source(source, extra=[missing_row], expected=2)
        checked = self.command("check", "--document", self.document, expected=2)
        self.assertTrue(checked["valid"])
        self.assertFalse(checked["complete"])
        read = self.command("reading", "--document", self.document, "--job", self.job,
                            "--query", "A+B", expected=2)
        self.assertEqual(len(read["matches"]), 1)
        self.assertEqual(len(read["gaps"]), 1)
        self.assertEqual(read["gaps"][0]["kind"], "unavailable_original")

    def test_integrity_errors_are_explicit_and_other_job_text_remains_searchable(self):
        source = self.source()
        self.extract(source)
        self.file_source(source)
        state = json.loads((self.document / ".filing-current.json").read_text())
        changed = self.document / state["sources"][0]["text"]
        changed.write_text("Changed human text stays untouched")
        check = self.command("check", "--document", self.document, expected=1)
        self.assertFalse(check["valid"])
        self.assertTrue(check["errors"])
        read = self.command("reading", "--document", self.document, "--job", self.job,
                            "--query", "A+B", expected=2)
        self.assertEqual(len(read["errors"]), 1)
        self.assertEqual(len(read["matches"]), 1)
        self.assertEqual(read["matches"][0]["kind"], "source_text")
        self.assertNotIn(str(changed), read["files"])
        self.assertEqual(changed.read_text(), "Changed human text stays untouched")

    def test_search_excerpts_bound_long_lines_and_preserve_literal_unicode_match(self):
        source = self.source(text="z" * 1600 + " Straße A+B " + "z" * 1400 + "\n")
        self.extract(source)
        result = self.command("reading", "--job", self.job, "--query", "STRASSE", "--context", 0)
        excerpt = result["matches"][0]["excerpt"][0]
        self.assertLessEqual(len(excerpt["text"]), 1000)
        self.assertTrue(excerpt["truncated"])
        self.assertIn("Straße", excerpt["text"])

    def test_reading_requires_explicit_scope_nonempty_queries_and_bounded_controls(self):
        for args in (("reading",), ("reading", "--job", "unused", "--query", " "),
                     ("reading", "--job", "unused", "--limit", "0"),
                     ("reading", "--job", "unused", "--context", "-1"),
                     ("reading", "--job", "unused", "--limit", "1001")):
            with self.subTest(args=args):
                result = subprocess.run([sys.executable, RUNNER, *args], capture_output=True)
                self.assertEqual(result.returncode, 2)
                self.assertIn(b"error:", result.stderr)

    def test_fetch_scope_reads_text_only_and_check_can_fetch_copies_and_override_originals(self):
        selected = {"files": [], "errors": [], "gaps": [], "jobs": [], "complete": True}
        with patch.object(cli.filing, "fetch_selection", return_value={}) as fetch, \
             patch.object(cli.filing, "preferred_search_paths", return_value=selected), redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["reading", "--document", "document", "--job", "job", "--fetch"]), 0)
            fetch.assert_called_once_with(["document"], jobs=["job"])
        checked = {"unavailable": 0, "text_gaps": 0, "valid": True}
        with patch.object(cli.filing, "fetch_selection", return_value={}) as fetch, \
             patch.object(cli.filing, "validate_document", return_value=checked) as validate, redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["check", "--document", "document", "--source-root", "moved",
                                       "--verify-sources", "--fetch"]), 0)
            fetch.assert_called_once_with(["document"], include_copies=True, include_sources=True, source_root="moved")
            validate.assert_called_once_with("document", verify_sources=True, source_root="moved")
