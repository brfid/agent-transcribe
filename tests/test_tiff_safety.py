"""Synthetic fail-closed legacy TIFF accounting and preservation regressions."""
import argparse
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_transcribe import common, scans


class TiffSafety(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def fixture(self, orientations=(1, 1, 1), colors=("red", "white", "blue"), name="original.tiff"):
        path = self.root / name
        frames = [Image.new("RGB", (10 + i, 6 + i), color) for i, color in enumerate(colors)]
        frames[0].save(path, format="TIFF", save_all=True, append_images=frames[1:], tiffinfo={274: 1}, compression="raw")
        data = bytearray(path.read_bytes())
        endian = "<" if data[:2] == b"II" else ">"
        offset = struct.unpack_from(endian + "I", data, 4)[0]
        for orientation in orientations:
            count = struct.unpack_from(endian + "H", data, offset)[0]
            entries = [offset + 2 + i * 12 for i in range(count)]
            tag = next(entry for entry in entries if struct.unpack_from(endian + "H", data, entry)[0] == 274)
            struct.pack_into(endian + "H", data, tag + 8, orientation)
            offset = struct.unpack_from(endian + "I", data, offset + 2 + 12 * count)[0]
        path.write_bytes(data)
        return path

    def args(self, source, folder="job", **extra):
        values = dict(source=[str(source)], sources_file=None, source_root=None,
                      output=str(self.root / folder), reuse_index=[], reuse_root=None,
                      pdf=False, fetch=False, best_effort=True, psm=3, rotate_clockwise=0)
        values.update(extra)
        return argparse.Namespace(**values)

    def run_ocr(self, command, **kwargs):
        if command[:2] == ["tesseract", "--version"]:
            return subprocess.CompletedProcess(command, 0, stdout=b"tesseract synthetic\n")
        self.assertEqual(command[0], "tesseract")
        Path(str(command[2]) + ".txt").write_text("LEGACY FIRST-FRAME TEXT\n")
        return subprocess.CompletedProcess(command, 0, stdout=b"")

    def state(self, args):
        return json.loads((Path(args.output) / ".processing.json").read_text())

    def assert_preserved(self, source, before):
        self.assertEqual((source.read_bytes(), source.stat().st_mtime_ns), before)

    def test_all_multipage_rotation_and_later_only_exif_paths_fail_closed(self):
        for orientations in ((1, 1, 1), (1, 6, 8)):
            for rotation in (0, 90, 180, 270):
                with self.subTest(orientations=orientations, rotation=rotation):
                    source = self.fixture(orientations, name=f"{orientations}-{rotation}.tiff")
                    before = source.read_bytes(), source.stat().st_mtime_ns
                    args = self.args(source, folder=f"job-{orientations}-{rotation}", rotate_clockwise=rotation)
                    with patch("agent_transcribe.scans.run", side_effect=self.run_ocr) as runner:
                        result = scans.process(args)
                    self.assertEqual(runner.call_count, 1)  # Version only: no OCR can lose frames.
                    self.assertFalse(result["extraction_complete"])
                    self.assertEqual(result["extracted"], 0)
                    self.assertIsNone(result["combined_text"])
                    inventory = result["tiff_frames"][source.name]
                    self.assertEqual(inventory["expected_frame_ids"], [1, 2, 3])
                    self.assertEqual(inventory["gap_frame_ids"], [1, 2, 3])
                    self.assertEqual(inventory["produced_frame_ids"], [])
                    self.assertEqual([r["exif_orientation"] for r in inventory["frames"]], list(orientations))
                    reading = Path(result["partial_text"]).read_text()
                    for frame_id in (1, 2, 3):
                        self.assertIn(f"TIFF frame {frame_id}: NO VERIFIED FRAME-MAPPED TEXT", reading)
                    self.assertEqual(self.state(args)["tiff_safety"][source.name], inventory)
                    self.assert_preserved(source, before)

    def test_blank_and_repeated_frames_remain_distinct_gaps(self):
        colorsets = (("white", "red", "blue"), ("red", "white", "blue"),
                     ("red", "blue", "white"), ("white",) * 3, ("red",) * 3)
        for i, colors in enumerate(colorsets):
            source = self.fixture(colors=colors, name=f"blank-repeat-{i}.tif")
            inventory = scans.tiff_frame_inventory(source)
            self.assertEqual(inventory["expected_frame_ids"], [1, 2, 3])
            self.assertEqual([r["frame_id"] for r in inventory["frames"]], [1, 2, 3])
            with self.assertRaises(scans.TiffSafetyError):
                scans.require_legacy_tiff_coverage(inventory)

    def test_direct_extractor_rejects_before_creating_flattened_derivative(self):
        source = self.fixture((1, 6, 8))
        before = source.read_bytes(), source.stat().st_mtime_ns
        temporary = self.root / "temporary"
        temporary.mkdir()
        with patch("agent_transcribe.scans.run") as runner:
            with self.assertRaises(scans.TiffSafetyError) as failure:
                scans.extract_page(source, temporary, make_pdf=True, rotate_clockwise=90)
        runner.assert_not_called()
        self.assertEqual(failure.exception.inventory["gap_frame_ids"], [1, 2, 3])
        self.assertEqual(list(temporary.iterdir()), [])
        self.assert_preserved(source, before)

    def test_all_eight_single_frame_orientations_then_clockwise_rotations_exact_pixels(self):
        base = Image.new("RGB", (4, 3))
        base.putdata([(i * 15, i * 7, i * 3) for i in range(12)])
        operations = {1: None, 2: Image.Transpose.FLIP_LEFT_RIGHT, 3: Image.Transpose.ROTATE_180,
                      4: Image.Transpose.FLIP_TOP_BOTTOM, 5: Image.Transpose.TRANSPOSE,
                      6: Image.Transpose.ROTATE_270, 7: Image.Transpose.TRANSVERSE,
                      8: Image.Transpose.ROTATE_90}
        for orientation, operation in operations.items():
            for rotation in (0, 90, 180, 270):
                with self.subTest(orientation=orientation, rotation=rotation):
                    source = self.root / f"single-{orientation}-{rotation}.tiff"
                    base.save(source, tiffinfo={274: orientation}, compression="raw")
                    before = source.read_bytes(), source.stat().st_mtime_ns
                    expected = base.copy() if operation is None else base.transpose(operation)
                    if rotation:
                        expected = expected.rotate(-rotation, expand=True)
                    temporary = self.root / f"temporary-{orientation}-{rotation}"
                    temporary.mkdir()
                    def check_pixels(command, **kwargs):
                        with Image.open(command[1]) as image:
                            image.load()
                            self.assertEqual(image.size, expected.size)
                            self.assertEqual(image.convert("RGB").tobytes(), expected.tobytes())
                            self.assertIn(image.getexif().get(274, 1), (None, 1))
                        return self.run_ocr(command, **kwargs)
                    with patch("agent_transcribe.scans.run", side_effect=check_pixels):
                        scans.extract_page(source, temporary, rotate_clockwise=rotation)
                    self.assert_preserved(source, before)

    def test_retained_text_index_cannot_bypass_frame_guard(self):
        source = self.fixture()
        text = self.root / "accepted.txt"
        text.write_text("ACCEPTED SOURCE-LEVEL READING")
        index = self.root / "index.json"
        index.write_text(json.dumps({"schema": 1, "kind": "retained-text", "entries": [
            {"source": str(source), "text": str(text), "source_sha256": common.checksum(source),
             "text_sha256": common.checksum(text)}]}))
        before = text.read_bytes(), index.read_bytes()
        args = self.args(source, reuse_index=[str(index)])
        with patch("agent_transcribe.scans.run", side_effect=self.run_ocr):
            result = scans.process(args)
        self.assertFalse(result["extraction_complete"])
        self.assertEqual((text.read_bytes(), index.read_bytes()), before)
        self.assertFalse((Path(args.output) / "document.txt").exists())

    def test_old_done_receipt_and_receipt_reuse_cannot_claim_complete(self):
        source = self.fixture()
        args = self.args(source)
        with patch("agent_transcribe.scans.tiff_frame_inventory", return_value=None), \
                patch("agent_transcribe.scans.run", side_effect=self.run_ocr):
            scans.process(args)  # Simulate the pre-guard frame-losing adapter.
        output = Path(args.output)
        accepted = {p: p.read_bytes() for p in (output / "document.txt", output / "pages/original.tiff.txt")}
        receipt = output / ".processing.json"
        old_receipt = receipt.read_bytes()
        reused = self.args(source, folder="reused", reuse_index=[str(receipt)])
        with patch("agent_transcribe.scans.run", side_effect=self.run_ocr):
            reused_result = scans.process(reused)
        self.assertFalse(reused_result["extraction_complete"])
        self.assertEqual(receipt.read_bytes(), old_receipt)
        with patch("agent_transcribe.scans.run", side_effect=self.run_ocr):
            result = scans.process(args)
        self.assertFalse(result["extraction_complete"])
        self.assertEqual(result["extracted"], 0)
        self.assertTrue(self.state(args)["current_reading"]["partial"])
        self.assertIn("SAVED LEGACY TEXT", Path(result["reading_text"]).read_text())
        self.assertIn("LEGACY FIRST-FRAME TEXT", Path(result["reading_text"]).read_text())
        for path, before in accepted.items():
            self.assertEqual(path.read_bytes(), before)

    def test_old_prepared_checkpoint_cannot_publish_or_hide_modified_bytes(self):
        source = self.fixture()
        args = self.args(source)
        actual = common.atomic_copy
        def interrupt(path, source):
            if Path(path).name == "original.tiff.txt":
                raise KeyboardInterrupt("before legacy text publication")
            return actual(path, source)
        with patch("agent_transcribe.scans.tiff_frame_inventory", return_value=None), \
                patch("agent_transcribe.scans.run", side_effect=self.run_ocr), \
                patch("agent_transcribe.common.atomic_copy", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                scans.process(args)
        step_before = self.state(args)["steps"][source.name]
        with patch("agent_transcribe.scans.run", side_effect=self.run_ocr):
            result = scans.process(args)
        self.assertFalse(result["extraction_complete"])
        self.assertFalse((Path(args.output) / "pages/original.tiff.txt").exists())
        self.assertEqual(self.state(args)["steps"][source.name], step_before)
        staged = Path(args.output) / step_before["stage"] / "output-0000"
        staged.write_text("HUMAN CHANGE")
        with patch("agent_transcribe.scans.run", side_effect=self.run_ocr):
            with self.assertRaises(common.IntegrityError):
                scans.process(args)
        self.assertEqual(staged.read_text(), "HUMAN CHANGE")

    def test_strict_rejection_saves_inventory_and_never_assembles(self):
        source = self.fixture()
        args = self.args(source, best_effort=False)
        with patch("agent_transcribe.scans.run", side_effect=self.run_ocr):
            with self.assertRaises(scans.TiffSafetyError):
                scans.process(args)
        self.assertEqual(self.state(args)["tiff_safety"][source.name]["gap_frame_ids"], [1, 2, 3])
        self.assertFalse((Path(args.output) / "document.txt").exists())

    def test_unknown_count_is_explicit_and_later_source_continues(self):
        source = self.root / "broken.tiff"
        source.write_bytes(b"not a readable TIFF")
        later = self.root / "later.txt"
        later.write_text("HEALTHY LATER SOURCE")
        args = self.args(source)
        args.source.append(str(later))
        with patch("agent_transcribe.scans.run", side_effect=self.run_ocr):
            result = scans.process(args)
        self.assertFalse(result["extraction_complete"])
        self.assertEqual(result["extracted"], 1)
        self.assertIsNone(result["tiff_frames"][source.name]["expected_frame_ids"])
        self.assertIn("TIFF FRAME COUNT UNKNOWN", Path(result["reading_text"]).read_text())
        self.assertIn("HEALTHY LATER SOURCE", Path(result["reading_text"]).read_text())

    def test_later_frame_metadata_failure_does_not_hide_following_frame(self):
        class MetadataFailure:
            format = "TIFF"
            n_frames = 3
            size = (10, 6)
            index = 0
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def seek(self, index):
                self.index = index
            def getexif(self):
                if self.index == 1:
                    raise ValueError("synthetic later-frame EXIF failure")
                return {274: (1, 6, 8)[self.index]}
        source = self.fixture()
        with patch("PIL.Image.open", return_value=MetadataFailure()):
            inventory = scans.tiff_frame_inventory(source)
        self.assertEqual(inventory["expected_frame_ids"], [1, 2, 3])
        self.assertIn("metadata_error", inventory["frames"][1])
        self.assertEqual(inventory["frames"][2]["exif_orientation"], 8)
        with self.assertRaises(scans.TiffSafetyError):
            scans.require_legacy_tiff_coverage(inventory)

    def test_single_frame_success_has_explicit_coverage_and_blank_remains_accounted(self):
        for blank in (False, True):
            source = self.fixture(orientations=(1,), colors=("white",), name=f"single-{blank}.tif")
            args = self.args(source, folder=f"single-job-{blank}")
            def extract(command, **kwargs):
                result = self.run_ocr(command, **kwargs)
                if blank and command[:2] != ["tesseract", "--version"]:
                    Path(str(command[2]) + ".txt").write_text("")
                return result
            with patch("agent_transcribe.scans.run", side_effect=extract):
                result = scans.process(args)
            inventory = result["tiff_frames"][source.name]
            self.assertTrue(result["extraction_complete"])
            self.assertTrue(inventory["coverage_complete"])
            self.assertEqual(inventory["expected_frame_ids"], [1])
            self.assertEqual(inventory["produced_frame_ids"], [1])
            self.assertEqual(inventory["gap_frame_ids"], [])
            self.assertEqual(result["blank_text"], int(blank))

    def test_broken_directory_chain_is_unknown_rather_than_a_single_frame_success(self):
        source = self.fixture(orientations=(1,), colors=("red",))
        data = bytearray(source.read_bytes())
        endian = "<" if data[:2] == b"II" else ">"
        offset = struct.unpack_from(endian + "I", data, 4)[0]
        count = struct.unpack_from(endian + "H", data, offset)[0]
        struct.pack_into(endian + "I", data, offset + 2 + 12 * count, len(data) + 1000)
        source.write_bytes(data)
        before = source.read_bytes(), source.stat().st_mtime_ns
        inventory = scans.tiff_frame_inventory(source)
        self.assertFalse(inventory["count_known"])
        self.assertFalse(inventory["coverage_complete"])
        self.assertIn("inventory_error", inventory)
        with self.assertRaises(scans.TiffSafetyError):
            scans.require_legacy_tiff_coverage(inventory)
        self.assert_preserved(source, before)

    def test_tiff_content_under_another_image_suffix_is_guarded(self):
        source = self.fixture(name="misnamed.jpg")
        self.assertEqual(scans.tiff_frame_inventory(source)["expected_frame_ids"], [1, 2, 3])
        with self.assertRaises(scans.TiffSafetyError):
            scans.require_legacy_tiff_coverage(scans.tiff_frame_inventory(source))


if __name__ == "__main__":
    unittest.main()
