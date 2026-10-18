"""Exercise the supported reconciliation command and its public exit statuses."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

RUNNER = Path(__file__).resolve().parents[1] / "process.py"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ReconcileCLI(unittest.TestCase):
    def command(self, *args, expected=0):
        run = subprocess.run([sys.executable, str(RUNNER), *map(str, args)], capture_output=True, text=True)
        self.assertEqual(run.returncode, expected, run.stderr or run.stdout)
        return json.loads(run.stdout)

    def test_corrected_reading_gap_repeat_and_stale_receipt_through_cli(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            originals = root / "originals"
            originals.mkdir()
            source = originals / "selected.txt"
            source.write_text("Baseline discovery reading.\n")
            job, destination = root / "job", root / "organized"
            self.command("scan", "--source", source, "--source-root", originals, "--output", job)
            raw = job / "pages/selected.txt.txt"
            recipe = root / "assignments.json"
            recipe.write_text(json.dumps({"schema": 1, "source_root": "originals", "text_root": "job",
                "assignments": [{"source": source.name, "source_sha256": digest(source),
                    "document": "Selected record", "description": "Selected record", "disposition": "document",
                    "preferred_text": {"path": "pages/selected.txt.txt", "sha256": digest(raw)}}]}))
            self.command("file", "--assignments", recipe, "--output", destination)
            document = destination / "Selected record"
            receipt = document / ".filing-current.json"
            old = json.loads(receipt.read_text())
            copied = document / old["sources"][0]["copy"]
            original_copy = (copied.read_bytes(), copied.stat().st_mtime_ns)
            corrected = root / "corrected.txt"
            corrected.write_text("Corrected discovery reading.\n")
            changes = root / "changes.json"
            data = {"schema": 1, "expected_receipt_sha256": digest(receipt),
                "reason": "Selected correction", "evidence": "Synthetic source review",
                "changes": [{"source": source.name, "preferred_text": {"path": corrected.name, "sha256": digest(corrected)},
                    "gap_reason": "A declared unresolved synthetic layout limit"}]}
            changes.write_text(json.dumps(data))
            result = self.command("reconcile", "--document", document, "--changes", changes, expected=2)
            self.assertTrue(result["valid"] and result["reconciliation_complete"])
            self.assertEqual(result["text_gaps"], 1)
            checked = self.command("check", "--document", document, "--verify-sources", expected=2)
            self.assertTrue(checked["valid"])
            self.assertTrue(checked["documents"][0]["source_bytes_verified"])
            reading = self.command("reading", "--document", document, "--query", "Corrected discovery", expected=2)
            self.assertEqual(len(reading["matches"]), 1)
            repeat = self.command("reconcile", "--document", document, "--changes", changes, expected=2)
            self.assertTrue(repeat["repeated"])
            self.assertEqual((copied.read_bytes(), copied.stat().st_mtime_ns), original_copy)
            self.assertEqual(raw.read_text(), "Baseline discovery reading.\n")
            before = receipt.read_bytes()
            data["expected_receipt_sha256"] = "0" * 64
            changes.write_text(json.dumps(data))
            failed = subprocess.run([sys.executable, str(RUNNER), "reconcile", "--document", str(document),
                "--changes", str(changes)], capture_output=True, text=True)
            self.assertEqual(failed.returncode, 1)
            self.assertIn("Stale expected filing receipt", failed.stderr)
            self.assertEqual(receipt.read_bytes(), before)
