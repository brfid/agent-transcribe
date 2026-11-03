"""Synthetic receipt/metadata cases; no private archive content or OCR."""
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_transcribe import folder_intake as intake, filing
from agent_transcribe.common import IntegrityError, checksum


class FolderIntakeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.root = self.base / "originals"
        self.root.mkdir()
        self.output = self.base / "new bundle"

    def source(self, name="page.txt", content=b"original text"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def plan(self, **kwargs):
        return intake.build_folder_plan(self.root, self.output, **kwargs)

    def filing(self, names, *, gaps=None, order=None):
        text_root = self.base / "texts"
        text_root.mkdir(exist_ok=True)
        assignments = []
        for index, name in enumerate(names):
            source = self.root / name
            text = text_root / (str(index) + ".txt")
            text.write_text("accepted text " + str(index))
            row = {"source": name, "document": "Known grouping", "description": "Capture",
                   "disposition": "document", "source_sha256": checksum(source),
                   "preferred_text": {"path": text.name, "sha256": checksum(text)}, "capture_order": index + 1}
            if order:
                row.update(reading_order=order[index], reading_order_evidence="Synthetic printed sequence")
            if gaps and name in gaps:
                row["gap_reason"] = gaps[name]
            assignments.append(row)
        recipe = self.base / "recipe.json"
        recipe.write_text(json.dumps({"schema": 1, "source_root": "originals", "text_root": "texts", "assignments": assignments}))
        organized = self.base / "organized"
        filing.file_assignments(recipe, organized)
        return organized

    def scan_receipt(self, names, *, done=True, blank=False, folder="job"):
        job = self.base / folder
        job.mkdir()
        sources, steps = {}, {}
        for name in names:
            source = self.root / name
            relative = "pages/" + name + ".txt"
            text = job / relative
            text.parent.mkdir(parents=True, exist_ok=True)
            text.write_text("accepted extraction")
            sources[name] = {"sha256": checksum(source), "bytes": source.stat().st_size}
            steps[name] = {"status": "done" if done else "failed", "outputs": {relative: checksum(text)},
                           "details": {"blank_text": blank}, "error": None if done else "interrupted"}
        receipt = {"schema": 2, "specification": {"kind": "scans", "source_path_base": "source_root",
                   "settings": {"adapter_version": 4}, "sources": names},
                   "source_root": {"relative_to_job": "../originals"}, "sources": sources, "steps": steps}
        path = job / ".processing.json"
        path.write_text(json.dumps(receipt))
        return path

    def test_plan_reads_no_original_or_retained_text_payload(self):
        source = self.source()
        receipt = self.scan_receipt([source.name])
        original_open = Path.open
        opened = []
        def guard(path, *args, **kwargs):
            opened.append(path)
            if path != receipt:
                raise AssertionError("Payload read during planning: " + str(path))
            return original_open(path, *args, **kwargs)
        with patch.object(Path, "open", guard), patch.object(intake, "fetch", side_effect=AssertionError("implicit fetch")):
            plan = self.plan(reuse_from=[receipt])
        self.assertEqual(opened, [receipt])
        row = plan["sources"][0]
        self.assertEqual(row["sha256"], checksum(source))
        self.assertEqual(row["hash_status"], "receipt_pin_pending_intake")
        self.assertEqual(row["prior_coverage"], "verified_complete")

    def test_current_filing_preserves_order_grouping_gaps_and_pins(self):
        self.source("first.txt")
        self.source("second.txt")
        organized = self.filing(["first.txt", "second.txt"], order=[2, 1], gaps={"first.txt": "Illegible lower edge"})
        before = {str(p): checksum(p) for p in organized.rglob("*") if p.is_file()}
        plan = self.plan(reuse_from=[organized])
        self.assertEqual([r["relative_path"] for r in plan["sources"]], ["second.txt", "first.txt"])
        self.assertEqual(plan["sources"][0]["capture_order"], 2)
        self.assertEqual(plan["sources"][1]["prior_coverage"], "partial")
        reuse = plan["sources"][1]["reuse"]
        self.assertIn("Illegible lower edge", reuse["gaps"])
        self.assertEqual(reuse["name"], "Known grouping")
        self.assertEqual(reuse["provenance"]["format"], "scan-filing")
        prepared = intake.prepare_source(self.root, plan["sources"][1])
        self.assertEqual(prepared["sha256"], checksum(self.root / "first.txt"))
        self.assertEqual(before, {str(p): checksum(p) for p in organized.rglob("*") if p.is_file()})

    def test_scan_partial_failed_and_unknown_history_are_distinct(self):
        self.source("one.txt")
        self.source("two.txt")
        self.source("three.txt")
        receipt = self.scan_receipt(["one.txt"], blank=True)
        failed = self.scan_receipt(["two.txt"], done=False, folder="failed")
        plan = self.plan(reuse_from=[receipt, failed])
        rows = {r["relative_path"]: r for r in plan["sources"]}
        self.assertEqual(rows["one.txt"]["prior_coverage"], "partial")
        self.assertEqual(rows["two.txt"]["prior_coverage"], "partial")
        self.assertIsNone(rows["two.txt"]["reuse"])
        self.assertEqual(rows["three.txt"]["prior_coverage"], "no_recorded_processing")

    def test_conflicting_scan_variants_defer_but_filing_takes_precedence(self):
        self.source()
        one = self.scan_receipt(["page.txt"])
        two = self.scan_receipt(["page.txt"], folder="alternate")
        plan = self.plan(reuse_from=[one, two])
        self.assertEqual(plan["sources"][0]["prior_coverage"], "unresolved")
        self.assertIsNone(plan["sources"][0]["reuse"])
        organized = self.filing(["page.txt"])
        selected = self.plan(reuse_from=[one, two, organized])["sources"][0]
        self.assertTrue(selected["reuse"]["accepted_filing"])

    def test_control_unavailable_never_materialized(self):
        self.source()
        receipt = self.scan_receipt(["page.txt"])
        with patch.object(intake, "_dataless", return_value=True), patch.object(intake, "fetch", side_effect=AssertionError("fetch")):
            plan = self.plan(reuse_from=[receipt])
        self.assertTrue(plan["issues"])
        self.assertEqual(plan["sources"][0]["prior_coverage"], "unresolved")

    def test_sibling_same_basename_and_identical_bytes_are_not_deduplicated(self):
        self.source("a/page.txt", b"same")
        self.source("b/page.txt", b"same")
        plan = self.plan(recursive=True)
        self.assertEqual(len(plan["sources"]), 2)
        self.assertEqual([r["relative_path"] for r in plan["sources"]], ["a/page.txt", "b/page.txt"])
        prepared = [intake.prepare_source(self.root, r) for r in plan["sources"]]
        self.assertEqual(prepared[0]["sha256"], prepared[1]["sha256"])

    def test_hidden_bookkeeping_outputs_excluded_even_in_explicit_list(self):
        self.source()
        self.source(".hidden/scan.txt")
        self.source("derivatives/copied.txt")
        (self.root / "derivatives" / ".processing.json").write_text("{}")
        self.source("README.md")
        plan = self.plan(recursive=True)
        self.assertEqual([r["relative_path"] for r in plan["sources"]], ["page.txt"])
        self.assertEqual(len(plan["exclusions"]), 3)
        listing = self.base / "list.txt"
        listing.write_text("originals/derivatives/copied.txt\n")
        with self.assertRaises(IntegrityError):
            self.plan(sources_file=listing)

    def test_explicit_source_list_preserves_order_and_never_escapes(self):
        self.source("first.txt")
        self.source("second.txt")
        listing = self.base / "ordered.txt"
        listing.write_text("originals/second.txt\noriginals/first.txt\n")
        plan = self.plan(sources_file=listing)
        self.assertEqual([r["relative_path"] for r in plan["sources"]], ["second.txt", "first.txt"])
        for bad in ("../outside.txt", "originals/first.txt\noriginals/first.txt"):
            listing.write_text(bad)
            with self.assertRaises(IntegrityError):
                self.plan(sources_file=listing)

    def test_symlinks_and_output_overlap_refused(self):
        self.source()
        (self.root / "link.txt").symlink_to(self.root / "page.txt")
        self.assertEqual(len(self.plan()["sources"]), 1)
        listing = self.base / "list.txt"
        listing.write_text("originals/link.txt")
        with self.assertRaises(IntegrityError):
            self.plan(sources_file=listing)
        for output in (self.root / "new", self.base):
            with self.assertRaises(IntegrityError):
                intake.build_folder_plan(self.root, output)
        (self.base / "alias").symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(IntegrityError):
            intake.build_folder_plan(self.base / "alias", self.output)

    def test_prepare_sniffs_misleading_suffix_and_checks_frozen_identity(self):
        source = self.source("wrong.txt", b"%PDF-1.7\nsynthetic")
        row = self.plan()["sources"][0]
        prepared = intake.prepare_source(self.root, row)
        self.assertEqual(prepared["kind"], "pdf")
        self.assertEqual(prepared["planning_kind"], "txt")
        source.write_bytes(b"changed")
        with self.assertRaises(IntegrityError):
            intake.prepare_source(self.root, row)

    def test_prepare_rejects_changed_text_receipt_and_known_original_hash(self):
        source = self.source()
        receipt = self.scan_receipt([source.name])
        row = self.plan(reuse_from=[receipt])["sources"][0]
        text = Path(row["reuse"]["text_path"])
        text.write_text("modified accepted text")
        with self.assertRaises(IntegrityError):
            intake.prepare_source(self.root, row)
        text.write_text("accepted extraction")
        receipt.write_text(receipt.read_text() + "\n")
        with self.assertRaises(IntegrityError):
            intake.prepare_source(self.root, row)
        row["reuse"] = None
        row["sha256"] = "0" * 64
        with self.assertRaises(IntegrityError):
            intake.prepare_source(self.root, row)

    def test_exact_fetch_retries_once_and_records_intake(self):
        self.source()
        row = self.plan()["sources"][0]
        with patch.object(intake, "fetch", side_effect=[({"success": False}, False), ({"success": True}, True)]) as fetching:
            prepared = intake.prepare_source(self.root, row, fetch_inputs=True)
        self.assertEqual(fetching.call_count, 2)
        self.assertEqual(prepared["intake"], {"success": True})
        self.assertEqual(fetching.call_args.args[0], [self.root / "page.txt"])

    def test_archive_mixed_and_unbound_have_finite_exact_accounting(self):
        self.source("accepted.txt")
        self.source("new.txt")
        (self.base / "unbound.pdf").write_bytes(b"%PDF-1.7")
        receipt = self.scan_receipt(["accepted.txt"])
        bindings = [{"name": "Bound originals", "source_root": "originals", "destination": "portable",
                     "status": "originals/STATUS.md", "recursive": True, "reuse_from": [str(receipt)],
                     "priority": 1, "scope_state": "defer"}]
        plan = intake.build_archive_plan(self.base, bindings=bindings)
        self.assertEqual([j["action"] for j in plan["jobs"]], ["enrich", "convert"])
        self.assertTrue(all(j["scope_state"] == "defer" for j in plan["jobs"]))
        self.assertTrue(any(r["kind"] == "unbound_source" and r["path"] == "unbound.pdf" for r in plan["unresolved"]))
        selected = [s["relative_path"] for j in plan["jobs"] for s in j["sources"]]
        self.assertEqual(set(selected), {"accepted.txt", "new.txt"})
        self.source("later.txt")
        intake.validate_archive_plan(plan)
        self.assertNotIn("later.txt", [s["relative_path"] for j in plan["jobs"] for s in j["sources"]])
        destination = self.base / "plans" / "conversion.json"
        result = intake.write_archive_plan(plan, destination)
        self.assertTrue(Path(result["markdown"]).is_file())
        self.assertEqual(intake.load_archive_plan(destination), plan)

    def test_archive_overlap_wrong_inventory_and_nested_output_are_rejected(self):
        self.source()
        binding = {"name": "Bound", "source_root": "originals", "destination": "portable", "scope_state": "ready"}
        with self.assertRaises(IntegrityError):
            intake.build_archive_plan(self.base, bindings=[binding, {**binding, "destination": "second"}])
        plan = intake.build_archive_plan(self.base, bindings=[binding])
        damaged = deepcopy(plan)
        damaged["inventory"][0]["job_id"] = "missing"
        with self.assertRaises(IntegrityError):
            intake.validate_archive_plan(damaged)
        damaged = deepcopy(plan)
        damaged["jobs"][0]["output"] = str(self.root / "bad")
        with self.assertRaises(IntegrityError):
            intake.validate_archive_plan(damaged)

    def test_malformed_or_ambiguous_reading_order_is_not_silently_used(self):
        self.source("a.txt")
        self.source("b.txt")
        organized = self.filing(["a.txt", "b.txt"], order=[1, 2])
        receipt = organized / "Known grouping" / ".filing-current.json"
        data = json.loads(receipt.read_text())
        data["sources"][1]["reading_order"] = 1
        receipt.write_text(json.dumps(data))
        plan = self.plan(reuse_from=[organized])
        self.assertTrue(plan["issues"])
        self.assertTrue(all(r["prior_coverage"] == "unresolved" for r in plan["sources"]))


    def test_portable_reuse_is_a_pending_verification_claim(self):
        source = self.source()
        bundle = self.base / "accepted portable"
        item = bundle / "items" / "0001"
        item.mkdir(parents=True)
        reading = item / "reading.md"
        reading.write_text("retained portable reading")
        source_map = {"reading_path": "reading.md", "files": [{"path": "reading.md", "sha256": checksum(reading)}], "gaps": []}
        receipt = {"kind": "docling-folder", "schema": 1, "source_root": str(self.root),
                   "specification": {"source_root": str(self.root)}, "coverage_accounted": True,
                   "conversion_complete": True, "publication_complete": True,
                   "sources": [{"item_relative": "items/0001", "source": {"relative_path": source.name,
                                "sha256": checksum(source), "size": source.stat().st_size},
                                "inventory": {}, "parts": [], "state": "complete", "map": source_map}]}
        (bundle / "conversion.json").write_text(json.dumps(receipt))
        plan = intake.build_archive_plan(self.base, bindings=[{"name": "Portable", "source_root": "originals",
                    "destination": "new portable", "reuse_from": [str(bundle)], "scope_state": "defer"}])
        job = plan["jobs"][0]
        self.assertEqual(job["action"], "reuse")
        reuse = job["sources"][0]["reuse"]
        self.assertEqual(reuse["portable_bundle"], str(bundle))
        self.assertEqual(reuse["portable_verification"], "pending current bundle verification")
        self.assertEqual(job["scope_state"], "defer")

    def test_unknown_formats_are_context_deferred_in_archive_plan(self):
        self.source("context.heic", b"unknown bytes")
        self.source("allowed.txt")
        plan = intake.build_archive_plan(self.base, bindings=[{"name": "Mixed", "source_root": "originals",
                    "destination": "new portable", "scope_state": "ready"}])
        unknown = next(j for j in plan["jobs"] if j["sources"][0]["kind"] == "unknown")
        self.assertEqual(unknown["action"], "defer")
        self.assertEqual(unknown["scope_state"], "defer")
        self.assertIn("unsupported/context", unknown["sources"][0]["context_disposition"])

    def test_explicit_pinned_note_replay_preserves_snapshot_and_other_guards(self):
        self.source("page.txt")
        organized = self.filing(["page.txt"])
        plan = self.plan(reuse_from=[organized])
        row = plan["sources"][0]
        prepared = intake.prepare_source(self.root, row)
        snapshot = prepared["reuse"]
        notes = Path(snapshot["notes_path"])
        notes.write_text(notes.read_text() + "\nLater authorized review.\n")
        with self.assertRaises(IntegrityError):
            intake.prepare_source(self.root, row)
        replay = intake.prepare_source(self.root, row, notes_snapshot=snapshot)
        self.assertEqual(replay["reuse"], snapshot)
        corrupt = deepcopy(snapshot)
        corrupt["notes_text"] += "tampered"
        with self.assertRaises(IntegrityError):
            intake.prepare_source(self.root, row, notes_snapshot=corrupt)
        corrupt = deepcopy(snapshot)
        corrupt["notes_path"] = "/unrelated/README.md"
        with self.assertRaises(IntegrityError):
            intake.prepare_source(self.root, row, notes_snapshot=corrupt)
        text = Path(snapshot["text_path"])
        text.write_text("changed accepted text")
        with self.assertRaises(IntegrityError):
            intake.prepare_source(self.root, row, notes_snapshot=snapshot)

    def test_pinned_note_replay_preserves_utf8_bom_hash(self):
        self.source("page.txt")
        organized = self.filing(["page.txt"])
        notes = organized / "Known grouping" / "README.md"
        notes.write_bytes(b"\xef\xbb\xbf" + notes.read_bytes())
        row = self.plan(reuse_from=[organized])["sources"][0]
        prepared = intake.prepare_source(self.root, row)
        replay = intake.prepare_source(self.root, row, notes_snapshot=prepared["reuse"])
        self.assertEqual(replay, prepared)

    def test_human_notes_are_read_and_pinned_only_during_prepare(self):
        self.source()
        organized = self.filing(["page.txt"])
        notes = organized / "Known grouping" / "README.md"
        notes.write_text("Human identity evidence: uncertain date.\n" + notes.read_text())
        receipt = notes.parent / ".filing-current.json"
        original_open = Path.open
        def control_only(path, *args, **kwargs):
            if path != receipt:
                raise AssertionError("Planning read human notes or text")
            return original_open(path, *args, **kwargs)
        with patch.object(Path, "open", control_only):
            row = self.plan(reuse_from=[organized])["sources"][0]
        prepared = intake.prepare_source(self.root, row)
        retained = prepared["reuse"]
        self.assertIn("Human identity evidence", retained["notes_text"])
        self.assertEqual(retained["notes_sha256"], checksum(notes))
        self.assertIn("not source transcription", retained["provenance"]["notes_role"])
        notes.write_text(notes.read_text() + "changed")
        with self.assertRaises(IntegrityError):
            intake.prepare_source(self.root, row)

    def test_legacy_tiff_text_is_kept_but_frame_coverage_remains_partial(self):
        self.source("frames.tiff", b"synthetic opaque original")
        receipt = self.scan_receipt(["frames.tiff"])
        row = self.plan(reuse_from=[receipt])["sources"][0]
        self.assertEqual(row["prior_coverage"], "partial")
        self.assertIsNotNone(row["reuse"])
        self.assertIn("frame count pending", row["reuse"]["gaps"][0])

    def test_scan_locator_symlink_and_failed_hash_conflict_are_not_trusted(self):
        self.source()
        accepted = self.scan_receipt(["page.txt"])
        failed = self.scan_receipt(["page.txt"], folder="failed", done=False)
        data = json.loads(failed.read_text())
        data["sources"]["page.txt"]["sha256"] = "a" * 64
        failed.write_text(json.dumps(data))
        row = self.plan(reuse_from=[accepted, failed])["sources"][0]
        self.assertEqual(row["prior_coverage"], "unresolved")
        self.assertIsNone(row["reuse"])
        alias = self.base / "original alias"
        alias.symlink_to(self.root, target_is_directory=True)
        data = json.loads(accepted.read_text())
        data["source_root"]["relative_to_job"] = "../original alias"
        accepted.write_text(json.dumps(data))
        row = self.plan(reuse_from=[accepted])["sources"][0]
        self.assertEqual(row["prior_coverage"], "unresolved")
        self.assertIsNone(row["reuse"])

    def test_known_hash_rejects_changed_bytes_with_restored_metadata(self):
        source = self.source()
        receipt = self.scan_receipt(["page.txt"])
        row = self.plan(reuse_from=[receipt])["sources"][0]
        info = source.stat()
        source.write_bytes(b"X" * info.st_size)
        os.utime(source, ns=(info.st_atime_ns, info.st_mtime_ns))
        with self.assertRaises(IntegrityError):
            intake.prepare_source(self.root, row)

    def test_allowlisted_content_signatures_and_docx_package(self):
        cases = {"misleading-one.bin": (b"II*\x00opaque", "tiff"),
                 "misleading-two.pdf": (b"\x89PNG\r\n\x1a\nopaque", "image"),
                 "misleading-three.jpg": (b"\xff\xfea\x00b\x00", "txt"),
                 "unknown.bin": (b"\x00\x01\x02binary", "unknown")}
        for name, (content, _) in cases.items():
            self.source(name, content)
        docx = self.root / "package.txt"
        with zipfile.ZipFile(docx, "w") as archive:
            archive.writestr("word/document.xml", "<document/>")
        expected = {name: kind for name, (_, kind) in cases.items()}
        expected[docx.name] = "docx"
        for row in self.plan()["sources"]:
            self.assertEqual(intake.prepare_source(self.root, row)["kind"], expected[row["relative_path"]])

    def test_plan_rejects_sources_becoming_derivatives_before_intake(self):
        self.source("nested/page.txt")
        row = self.plan(recursive=True)["sources"][0]
        (self.root / "nested" / ".filing-current.json").write_text("{}")
        with self.assertRaises(IntegrityError):
            intake.prepare_source(self.root, row)

    def test_context_notes_do_not_alone_make_scan_coverage_partial(self):
        self.source()
        receipt = self.scan_receipt(["page.txt"])
        data = json.loads(receipt.read_text())
        data["steps"]["page.txt"]["details"]["notes"] = ["Native text method; unreviewed"]
        receipt.write_text(json.dumps(data))
        row = self.plan(reuse_from=[receipt])["sources"][0]
        self.assertEqual(row["prior_coverage"], "verified_complete")
        self.assertEqual(row["reuse"]["notes"], ["Native text method; unreviewed"])


    def test_human_readiness_and_job_dependencies_are_separate(self):
        self.source()
        other = self.base / "second originals"
        other.mkdir()
        (other / "second.txt").write_text("second synthetic text")
        bindings = [
            {"name": "First", "source_root": "originals", "destination": "first portable",
             "scope_state": "defer", "readiness_requirements": ["Review recorded scope"]},
            {"name": "Second", "source_root": "second originals", "destination": "second portable",
             "scope_state": "defer", "prerequisites": ["job-0001-01"]}]
        plan = intake.build_archive_plan(self.base, bindings=bindings)
        first, second = plan["jobs"]
        self.assertEqual(first["prerequisites"], [])
        self.assertEqual(second["prerequisites"], [first["id"]])
        self.assertIn("Review recorded scope", first["readiness_requirements"])
        self.assertIn("Execution approval required", first["readiness_requirements"])
        self.assertIn("Execution approval required", second["readiness_requirements"])
        saved = intake.write_archive_plan(plan, self.base / "plans" / "dependency-plan.json")
        readable = Path(saved["markdown"]).read_text()
        self.assertIn("Job prerequisites:", readable)
        self.assertIn("Readiness requirements:", readable)
        self.assertEqual(intake.load_archive_plan(saved["json"]), plan)

    def test_invalid_dependency_edges_and_cycles_are_rejected(self):
        self.source()
        other = self.base / "second originals"
        other.mkdir()
        (other / "second.txt").write_text("second synthetic text")
        plan = intake.build_archive_plan(self.base, bindings=[
            {"name": "First", "source_root": "originals", "destination": "first portable"},
            {"name": "Second", "source_root": "second originals", "destination": "second portable"}])
        invalid = [None, "job-0002-01", [1], [None], ["unknown-job"], ["job-0001-01"],
                   ["job-0002-01", "job-0002-01"]]
        for dependencies in invalid:
            with self.subTest(prerequisites=dependencies):
                damaged = deepcopy(plan)
                damaged["jobs"][0]["prerequisites"] = dependencies
                with self.assertRaises(IntegrityError):
                    intake.validate_archive_plan(damaged)
        cyclic = deepcopy(plan)
        cyclic["jobs"][0]["prerequisites"] = ["job-0002-01"]
        cyclic["jobs"][1]["prerequisites"] = ["job-0001-01"]
        with self.assertRaisesRegex(IntegrityError, "Cyclic"):
            intake.validate_archive_plan(cyclic)
        valid = deepcopy(plan)
        valid["jobs"][0]["prerequisites"] = ["job-0002-01"]
        intake.validate_archive_plan(valid)

    def test_deferred_generated_plan_runs_after_explicit_activation(self):
        # Actual B -> F -> backend TXT -> portable publication. No OCR/models.
        from agent_transcribe import conversion
        self.source()
        plan = intake.build_archive_plan(self.base, bindings=[
            {"name": "Activation", "source_root": "originals", "destination": "activated portable",
             "scope_state": "defer"}])
        self.assertEqual(plan["jobs"][0]["prerequisites"], [])
        self.assertEqual(plan["jobs"][0]["readiness_requirements"], ["Execution approval required"])
        deferred = conversion.run_archive_plan(plan)
        self.assertEqual(deferred["exit_code"], 2)
        self.assertFalse((self.base / "activated portable").exists())
        plan["jobs"][0]["scope_state"] = "ready"  # Explicit authorization change.
        result = conversion.run_archive_plan(plan)
        self.assertEqual(result["exit_code"], 0)
        self.assertTrue((self.base / "activated portable" / "conversion.json").is_file())


if __name__ == "__main__":
    unittest.main()
