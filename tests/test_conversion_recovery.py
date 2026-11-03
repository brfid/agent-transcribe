"""Synthetic recovery tests: real qpdf/Poppler/Pillow/ZIP, controlled inference."""
import codecs
import copy
import errno
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from agent_transcribe.common import IntegrityError
from agent_transcribe.docling_backend import BackendOptions, DoclingBackend, inspect_source, render_units
from agent_transcribe.recovery import ATTEMPT_CAPS, _fingerprints, _qpdf, _Journal, recover_range


def unit(uid, text="", status="failed", warnings=()):
    return {"unit_id": uid, "text": text, "decode_status": "unknown", "conversion_status": status,
            "recognition_warnings": list(warnings), "doc_page_no": uid if isinstance(uid, int) else None,
            "attempts": [], "transform": {}, "error": "synthetic failure" if status == "failed" else None}


def result(rows):
    return {"units": rows, "document": None, "errors": [], "status": "ok" if rows and all(r["conversion_status"] == "ok" for r in rows) else "failed", "provenance": {}}


class Backend:
    options = BackendOptions()

    def __init__(self, behavior=None):
        self.calls = []
        self.behavior = behavior

    def convert_range(self, path, kind, start, end, *, mode="default", scratch=None):
        self.calls.append((Path(path), kind, start, end, mode))
        if self.behavior:
            return self.behavior(Path(path), kind, start, mode)
        return result([unit(n) for n in range(start, end + 1)])


def pdf(path, pages=3):
    # PDFium creates an ordinary synthetic PDF; no external fixtures.
    import pypdfium2 as pdfium
    document = pdfium.PdfDocument.new()
    for n in range(pages):
        document.new_page(200 + n * 10, 300 + n * 10)
    document.save(path)
    document.close()


def frames(path):
    from PIL import Image
    images = [Image.new("RGB", (60 + n * 10, 40), color) for n, color in enumerate(("red", "green", "blue"))]
    images[0].save(path, save_all=True, append_images=images[1:], compression="raw")


def corrupt_tiff_strip(path, frame=2):
    from PIL import Image
    with Image.open(path) as image:
        image.seek(frame - 1)
        offset = image.tag_v2[273][0]
    data = bytearray(path.read_bytes())
    endian = "<" if data[:2] == b"II" else ">"
    ifd = struct.unpack_from(endian + "I", data, 4)[0]
    for _ in range(frame - 1):
        count = struct.unpack_from(endian + "H", data, ifd)[0]
        ifd = struct.unpack_from(endian + "I", data, ifd + 2 + count * 12)[0]
    count = struct.unpack_from(endian + "H", data, ifd)[0]
    for index in range(count):
        at = ifd + 2 + index * 12
        if struct.unpack_from(endian + "H", data, at)[0] == 273:
            struct.pack_into(endian + "I", data, at + 8, len(data) + 100000)
    path.write_bytes(data)


@unittest.skipUnless(__import__("importlib.util", fromlist=["find_spec"]).find_spec("docling"), "Conversion extra required")
class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_success_retained_and_singleton_failures_only(self):
        source = self.root / "test.tiff"
        frames(source)
        initial = result([unit(1, "accepted", "ok"), unit(2), unit(3)])
        saved = copy.deepcopy(initial)
        backend = Backend(lambda p, k, n, m: result([unit(n, f"recovered {n}", "ok")]))
        value = recover_range(backend, source, "tiff", 1, 3, initial, scratch=self.root / "scratch")
        self.assertEqual(initial, saved)
        self.assertEqual(value["units"][0], initial["units"][0])
        self.assertEqual([c[2] for c in backend.calls], [2, 3])
        self.assertEqual(value["status"], "ok")
        self.assertEqual(value["variants"][0]["units"], initial["units"])
        self.assertIn("variant_index", value["units"][1])
        calls = len(backend.calls)
        replayed = recover_range(backend, source, "tiff", 1, 3, initial, scratch=self.root / "scratch")
        self.assertEqual(replayed["units"][1]["text"], "recovered 2")
        self.assertEqual(len(backend.calls), calls)

    def test_full_page_mechanical_preference_and_plausible_conflict(self):
        source = self.root / "test.tiff"
        frames(source)
        backend = Backend(lambda p, k, n, m: result([unit(n, "new plausible words", "ok")]))
        initial = result([unit(1, "", "ok"), unit(2, "plausible native wording", "ok", ["sparse native text"]), unit(3, "healthy", "ok")])
        value = recover_range(backend, source, "tiff", 1, 3, initial, scratch=self.root / "scratch")
        self.assertEqual(value["units"][0]["text"], "new plausible words")
        self.assertEqual(value["units"][1]["text"], "plausible native wording")
        self.assertTrue(any("ambiguous orientation" in s for s in value["recognition_warnings"]))
        self.assertEqual(len([a for a in value["recovery_attempts"] if a["stage"] == "orientation"]), 4)

    def test_budget_persists_after_crash_and_exhaustion(self):
        source = self.root / "source.png"
        from PIL import Image
        Image.new("RGB", (20, 30), "white").save(source)
        initial = result([unit(1)])
        backend = Backend(lambda *args: (_ for _ in ()).throw(KeyboardInterrupt()))
        with self.assertRaises(KeyboardInterrupt):
            recover_range(backend, source, "image", 1, 1, initial, scratch=self.root / "scratch")
        backend = Backend()
        value = recover_range(backend, source, "image", 1, 1, initial, scratch=self.root / "scratch")
        self.assertEqual(len([a for a in value["recovery_attempts"] if a["stage"] == "singleton"]), 1)
        self.assertEqual(len([a for a in value["recovery_attempts"] if a["stage"] == "orientation"]), 4)
        count = len(backend.calls)
        second = recover_range(backend, source, "image", 1, 1, initial, scratch=self.root / "scratch")
        self.assertEqual(len(backend.calls), count)
        self.assertTrue(second["recovery_exhausted"])

    def test_fatal_integrity_and_storage_errors_escape(self):
        from PIL import Image
        source = self.root / "source.png"
        Image.new("RGB", (20, 30)).save(source)
        for index, failure in enumerate((IntegrityError("changed output"), OSError(errno.ENOSPC, "full"), PermissionError("publication denied"))):
            backend = Backend(lambda *args, failure=failure: (_ for _ in ()).throw(failure))
            with self.assertRaises(type(failure)):
                recover_range(backend, source, "image", 1, 1, result([unit(1)]), scratch=self.root / str(index))

    def test_journal_source_drift_and_artifact_changes_refused(self):
        source = self.root / "source.txt"
        source.write_bytes(b"caf\xe9")
        value = recover_range(Backend(), source, "txt", 1, 1, result([]), scratch=self.root / "scratch")
        artifact = self.root / "scratch" / value["recovery_artifacts"][0]["path"]
        artifact.write_text("changed")
        with self.assertRaises(IntegrityError):
            recover_range(Backend(), source, "txt", 1, 1, result([]), scratch=self.root / "scratch")
        source.write_bytes(b"other")
        with self.assertRaises(IntegrityError):
            recover_range(Backend(), source, "txt", 1, 1, result([]), scratch=self.root / "scratch")

    def test_text_bom_strict_and_ambiguous_legacy(self):
        for encoding, text in (("utf-16", "hello\nworld"), ("utf-32", "greetings"), ("utf-8-sig", "café")):
            source = self.root / (encoding + ".txt")
            source.write_bytes(text.encode(encoding))
            value = recover_range(Backend(), source, "txt", 1, 1, result([]), scratch=self.root / encoding)
            self.assertEqual("".join(u["text"] for u in value["units"]), text)
            self.assertEqual(value["status"], "ok")
        source = self.root / "legacy.txt"
        source.write_bytes(b"caf\xe9\n")
        value = recover_range(Backend(), source, "txt", 1, 1, result([]), scratch=self.root / "legacy")
        self.assertEqual(value["status"], "partial")
        self.assertIn("ambiguity", value["units"][0]["recognition_warnings"][0])
        source = self.root / "bad-bom.txt"
        source.write_bytes(codecs.BOM_UTF16_LE + b"\x00")
        value = recover_range(Backend(), source, "txt", 1, 1, result([]), scratch=self.root / "bad-bom")
        self.assertEqual(value["status"], "failed")
        self.assertFalse(value["recovery_artifacts"])

    def test_docx_real_malformed_xml_salvages_other_parts_and_media(self):
        source = self.root / "broken.docx"
        with zipfile.ZipFile(source, "w") as package:
            package.writestr("word/document.xml", "<broken main part")
            package.writestr("word/header1.xml", '<w:hdr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:p><w:r><w:t>header survives</w:t></w:r></w:p></w:hdr>')
            package.writestr("word/media/../image.png", b"image member bytes")
        initial = result([unit("package:word/document.xml")])
        value = recover_range(Backend(), source, "docx", 1, 1, initial, scratch=self.root / "scratch")
        self.assertEqual(value["status"], "partial")
        recovered = next(u for u in value["units"] if u["unit_id"] == "package:word/header1.xml")
        self.assertEqual(recovered["text"], "header survives")
        self.assertIsNone(recovered["doc_page_no"])
        self.assertTrue(any(u["unit_id"] == "package:word/document.xml" and u["conversion_status"] == "failed" for u in value["units"]))
        self.assertEqual(len(value["recovery_artifacts"]), 3)
        self.assertTrue(value["errors"])
        self.assertTrue(all((self.root / "scratch" / a["path"]).resolve().is_relative_to((self.root / "scratch").resolve()) for a in value["recovery_artifacts"]))

    def test_docx_partial_main_text_is_preserved_during_member_salvage(self):
        source = self.root / "partial.docx"
        with zipfile.ZipFile(source, "w") as package:
            package.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:p><w:r><w:t>main words</w:t></w:r></w:p></w:document>')
            package.writestr("word/header1.xml", "<broken")
        initial = result([unit("package:word/document.xml", "retained main text", "partial")])
        value = recover_range(Backend(), source, "docx", 1, 1, initial, scratch=self.root / "scratch")
        self.assertEqual(value["units"][0]["text"], "retained main text")
        self.assertEqual(value["status"], "partial")
        self.assertIn("main words", value["variants"][1]["units"][0]["text"])

    def test_invalid_orientation_metadata_pixels_are_retained(self):
        from PIL import Image
        source = self.root / "invalid-orientation.png"
        pixels = Image.new("RGB", (60, 40), "red")
        exif = Image.Exif()
        exif[274] = 9
        pixels.save(source, exif=exif)
        original = source.read_bytes()
        initial = result([unit(1, "", "failed", ["invalid EXIF orientation"])])
        value = recover_range(Backend(), source, "image", 1, 1, initial, scratch=self.root / "scratch")
        assets = value["recovery_artifacts"]
        self.assertEqual(len(assets), 4)
        self.assertTrue(all(a["transform"]["metadata_removed"] for a in assets))
        self.assertTrue(all(a["transform"]["orientation_uncertain"] for a in assets))
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(value["status"], "failed")

    def test_invalid_dpi_render_warning_enters_bounded_metadata_phase(self):
        from PIL import Image
        source = self.root / "invalid-dpi.png"
        Image.new("RGB", (40, 30), "white").save(source, dpi=(0, 0))
        rendered = render_units(source, "image", [1], self.root / "renders")
        self.assertEqual(rendered[0]["dpi"], [72., 72.])
        value = recover_range(Backend(), source, "image", 1, 1, result([unit(1, "retained", "ok")]), scratch=self.root / "scratch", rendered=rendered)
        self.assertEqual(len([a for a in value["recovery_attempts"] if a["stage"] == "orientation"]), 4)
        self.assertEqual(value["units"][0]["text"], "retained")

    def test_broken_zip_is_explicit_gap(self):
        source = self.root / "broken.docx"
        source.write_bytes(b"PK\x03\x04missing central directory")
        value = recover_range(Backend(), source, "docx", 1, 1, result([unit("package:word/document.xml")]), scratch=self.root / "scratch")
        self.assertEqual(value["status"], "failed")
        self.assertIn("BadZipFile", value["recovery_attempts"][0]["errors"][0])

    def test_corrupt_middle_tiff_pixels_preserve_later_frame(self):
        source = self.root / "bad-strip.tiff"
        frames(source)
        corrupt_tiff_strip(source)
        inventory = inspect_source(source, "tiff")
        self.assertEqual(inventory["expected_count"], 3)
        rows = render_units(source, "tiff", [1, 2, 3], self.root / "renders")
        self.assertEqual([r["decode_status"] for r in rows], ["ok", "failed", "ok"])
        def behavior(path, kind, uid, mode):
            rows = render_units(path, kind, [uid], Path(tempfile.mkdtemp(dir=self.root)))
            return result([unit(uid, f"frame {uid}" if rows[0]["decode_status"] == "ok" else "", "ok" if rows[0]["decode_status"] == "ok" else "failed")])
        value = recover_range(Backend(behavior), source, "tiff", 1, 3, result([unit(1), unit(2), unit(3)]), scratch=self.root / "scratch")
        self.assertEqual([r["conversion_status"] for r in value["units"]], ["ok", "failed", "ok"])
        self.assertEqual(value["status"], "partial")

    def test_broken_tiff_chain_unknown_count_never_complete(self):
        source = self.root / "broken-chain.tiff"
        frames(source)
        data = bytearray(source.read_bytes())
        endian = "<" if data[:2] == b"II" else ">"
        ifd = struct.unpack_from(endian + "I", data, 4)[0]
        count = struct.unpack_from(endian + "H", data, ifd)[0]
        struct.pack_into(endian + "I", data, ifd + 2 + count * 12, len(data) + 100)
        source.write_bytes(data)
        value = recover_range(Backend(), source, "tiff", 1, 1, result([unit(1, "readable", "ok")]), scratch=self.root / "scratch")
        self.assertIsNone(value["recovery_inventory"]["expected_count"])
        self.assertEqual(value["status"], "partial")

    @unittest.skipUnless(shutil.which("qpdf"), "qpdf unavailable")
    def test_real_qpdf_broken_startxref_warning3_count_order(self):
        source = self.root / "broken-xref.pdf"
        pdf(source)
        raw = source.read_bytes()
        at = raw.rfind(b"startxref\r\n")
        marker = b"startxref\r\n"
        if at < 0:
            at, marker = raw.rfind(b"startxref\n"), b"startxref\n"
        self.assertGreaterEqual(at, 0)
        digits = at + len(marker)
        finish = raw.index(b"\n", digits)
        source.write_bytes(raw[:digits] + b"0" + raw[finish:])
        before = source.read_bytes()
        scratch = self.root / "scratch"
        journal = _Journal(scratch, source, "pdf", 1, 3, {})
        value = journal.attempt("qpdf", "source", "synthetic broken xref", lambda d, r: _qpdf(source, d, r, journal), tool="qpdf")
        self.assertIsNotNone(value)
        self.assertTrue(value["warning_exit_3"])
        self.assertEqual(_fingerprints(source), _fingerprints(value["path"]))
        self.assertEqual(source.read_bytes(), before)
        self.assertTrue(journal.data["attempts"][0]["order_checked"])

    @unittest.skipUnless(shutil.which("pdftoppm"), "Poppler unavailable")
    def test_real_poppler_individual_page_raster_mapping(self):
        source = self.root / "source.pdf"
        pdf(source, pages=2)
        def behavior(path, kind, uid, mode):
            return result([unit(uid, "raster text", "ok")]) if kind == "image" else result([unit(uid)])
        value = recover_range(Backend(behavior), source, "pdf", 1, 2, result([unit(1), unit(2)]), scratch=self.root / "scratch")
        self.assertEqual(value["status"], "ok")
        raster = [a for a in value["recovery_artifacts"] if a["role"].startswith("Poppler")]
        self.assertEqual([a["original_unit_mapping"] for a in raster], [[1], [2]])
        self.assertEqual([u["unit_id"] for u in value["units"]], [1, 2])
        self.assertEqual([u["doc_page_no"] for u in value["units"]], [1, 1])

    @unittest.skipUnless(shutil.which("qpdf"), "qpdf unavailable")
    def test_encrypted_pdf_stays_gap_with_finite_attempts(self):
        source, locked = self.root / "source.pdf", self.root / "locked.pdf"
        pdf(source, 1)
        subprocess.run([shutil.which("qpdf"), "--encrypt", "secret", "owner", "256", "--", str(source), str(locked)], check=True, capture_output=True)
        initial = result([unit(1)])
        value = recover_range(Backend(), locked, "pdf", 1, 1, initial, scratch=self.root / "scratch")
        self.assertEqual(value["status"], "failed")
        self.assertLessEqual(len(value["recovery_attempts"]), 4)
        self.assertTrue(value["recovery_exhausted"])

    def test_optional_tools_absent_are_explicit(self):
        source = self.root / "source.pdf"
        pdf(source, 1)
        with patch("agent_transcribe.recovery.shutil.which", return_value=None):
            value = recover_range(Backend(), source, "pdf", 1, 1, result([unit(1)]), scratch=self.root / "scratch")
        errors = " ".join(str(a["errors"]) for a in value["recovery_attempts"])
        self.assertIn("unavailable", errors)
        self.assertEqual(value["status"], "failed")


@unittest.skipUnless(os.environ.get("AGENT_TRANSCRIBE_TEST_MODELS"), "set AGENT_TRANSCRIBE_TEST_MODELS for real offline inference")
class RealRecoveryTests(unittest.TestCase):
    def test_real_native_candidate_retained_with_exact_frame(self):
        from PIL import Image, ImageDraw, ImageFont
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "synthetic.png"
            pixels = Image.new("RGB", (1100, 700), "white")
            draw = ImageDraw.Draw(pixels)
            font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 36)
            draw.text((80, 120), "RECOVERY FRAME ONE", font=font, fill="black")
            draw.text((80, 200), "Synthetic searchable document.", font=font, fill="black")
            pixels.save(source)
            backend = DoclingBackend(BackendOptions(artifacts_path=os.environ["AGENT_TRANSCRIBE_TEST_MODELS"], document_timeout=120))
            value = recover_range(backend, source, "image", 1, 1, result([unit(1)]), scratch=root / "scratch")
            self.assertEqual(value["status"], "ok")
            self.assertIn("RECOVERY", value["units"][0]["text"])
            self.assertIsNone(value["document"])
            candidate = value["variants"][value["units"][0]["variant_index"]]
            self.assertIsNotNone(candidate["document"])
            self.assertEqual(candidate["units"][0]["unit_id"], 1)
            self.assertEqual(candidate["units"][0]["doc_page_no"], 1)
            replayed = recover_range(backend, source, "image", 1, 1, result([unit(1)]), scratch=root / "scratch")
            retained = replayed["variants"][replayed["units"][0]["variant_index"]]
            self.assertIsNotNone(retained["document"])
            self.assertEqual(replayed["units"][0]["text"], value["units"][0]["text"])


if __name__ == "__main__":
    unittest.main()
