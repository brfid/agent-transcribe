"""Synthetic accounting, pixel geometry and optional real offline inference."""
from dataclasses import asdict
import errno
import importlib.util
import os
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent_transcribe.docling_backend import (
    BackendOptions, DoclingBackend, _document_text, inspect_source, preflight, render_units, generated_image_budget,
)

HAS_PIL = importlib.util.find_spec("PIL") is not None
HAS_CORE = importlib.util.find_spec("docling_core") is not None
ARTIFACTS = os.environ.get("AGENT_TRANSCRIBE_TEST_MODELS")


@unittest.skipUnless(HAS_CORE and HAS_PIL, "Pinned Docling image extra needed")
class GeneratedImageBudgetTests(unittest.TestCase):
    def test_large_generated_native_image_exports_under_reserved_budget(self):
        from PIL import Image
        from docling_core.types.doc import DoclingDocument, ImageRef, Size
        from docling_core.utils.settings import settings
        from agent_transcribe.portable import export_part
        import base64
        import json
        # Random PNG data exceeds the real 20 MiB default, unlike solid fixtures.
        width = height = 2800
        image = Image.frombytes("RGB", (width, height), os.urandom(width * height * 3))
        doc = DoclingDocument(name="synthetic high resolution capture")
        doc.add_page(page_no=1, size=Size(width=width, height=height), image=ImageRef.from_pil(image, dpi=72))
        # Round-trip drops the private PIL cache, reproducing recovery replay.
        value = doc.model_dump(mode="json")
        encoded = value["pages"]["1"]["image"]["uri"]
        self.assertGreater(len(base64.b64decode(encoded.split(",", 1)[1])), 20 * 1024 * 1024)
        doc = DoclingDocument.model_validate(value)
        prior = settings.max_image_decoded_size
        with self.assertRaisesRegex(ValueError, "Decoded image exceeds"):
            _ = doc.pages[1].image.pil_image
        rendered = [{"decode_status": "ok", "width": width, "height": height}]
        reserve = width * height * 4 + height + 65536
        with tempfile.TemporaryDirectory() as temp:
            with generated_image_budget(rendered, reserve) as budget:
                self.assertEqual(budget["effective_bytes"], reserve)
                export_part({"document": doc, "units": [{"unit_id": 1, "unit_kind": "page", "doc_page_no": 1,
                    "text": "", "conversion_status": "ok"}]}, Path(temp),
                    source={"relative_path": "synthetic.png", "kind": "image"})
            self.assertEqual(settings.max_image_decoded_size, prior)
            native = json.loads((Path(temp) / "document.json").read_text())
            self.assertFalse(native["pages"]["1"]["image"]["uri"].startswith("data:"))

    def test_budget_restores_on_failure_and_retains_uri_and_size_guards(self):
        from PIL import Image
        from docling_core.types.doc import ImageRef
        from docling_core.utils.settings import settings
        from agent_transcribe.common import IntegrityError
        import base64
        prior, uri_policy = settings.max_image_decoded_size, settings.allow_image_file_uri
        rendered = [{"decode_status": "ok", "width": 10, "height": 10}]
        reserve = prior + 1024
        with self.assertRaisesRegex(RuntimeError, "synthetic"):
            with generated_image_budget(rendered, reserve):
                self.assertEqual(settings.allow_image_file_uri, uri_policy)
                # A forged embedded payload above the task bound still fails.
                ref = ImageRef.from_pil(Image.new("RGB", (1, 1)), dpi=72)
                data = ref.model_dump(mode="json")
                data["uri"] = "data:image/png;base64," + base64.b64encode(b"x" * (reserve + 1)).decode()
                with self.assertRaisesRegex(ValueError, "Decoded image exceeds"):
                    _ = ImageRef.model_validate(data).pil_image
                raise RuntimeError("synthetic export failure")
        self.assertEqual(settings.max_image_decoded_size, prior)
        self.assertEqual(settings.allow_image_file_uri, uri_policy)
        with self.assertRaises(IntegrityError):
            with generated_image_budget(rendered, 1):
                self.fail("under-reserved pixels must stop")


def fixture_tiff(path, frames, orientations=None):
    from PIL import TiffImagePlugin
    with TiffImagePlugin.AppendingTiffWriter(str(path), True) as writer:
        for n, image in enumerate(frames):
            tags = TiffImagePlugin.ImageFileDirectory_v2()
            tags[274] = (orientations or [1] * len(frames))[n]
            image.save(writer, format="TIFF", tiffinfo=tags, dpi=(96, 192))
            writer.newFrame()


def corners():
    from PIL import Image
    image = Image.new("RGB", (7, 5), "white")
    image.putpixel((0, 0), (255, 0, 0))
    image.putpixel((6, 0), (0, 255, 0))
    image.putpixel((0, 4), (0, 0, 255))
    image.putpixel((6, 4), (0, 0, 0))
    return image


def patch_middle_strip(path):
    from PIL import Image
    with Image.open(path) as image:
        image.seek(1)
        offset = image.tag_v2._offset
    # Pillow exposes the absolute IFD start offset.
    with path.open("r+b") as f:
        f.seek(offset)
        count = struct.unpack("<H", f.read(2))[0]
        for _ in range(count):
            entry = f.tell()
            tag, dtype, length, value = struct.unpack("<HHII", f.read(12))
            if tag == 273:
                f.seek(entry + 8)
                f.write(struct.pack("<I", path.stat().st_size + 10000))
                return
    raise AssertionError("fixture StripOffsets not found")


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="backend-tests-")
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_options_and_finite_ranges(self):
        self.assertEqual(asdict(BackendOptions())["threads"], 4)
        for rotation in (1, 45, -90, 360):
            with self.assertRaises(ValueError):
                BackendOptions(rotate_clockwise=rotation)
        backend = DoclingBackend()
        for bounds in ((0, 1), (1, 17), (3, 2)):
            with self.assertRaises(ValueError):
                backend.convert_range("unused.pdf", "pdf", *bounds)
        with self.assertRaises(ValueError):
            backend.convert_range("unused.docx", "docx", 2, 2)

    def test_text_strict_bom_and_exact_line_identity_no_models(self):
        source = self.root / "text.txt"
        source.write_bytes("FIRST\r\n\r\nTHIRD\n".encode("utf-16"))
        inventory = inspect_source(source, "txt")
        self.assertEqual(inventory["expected_count"], 3)
        self.assertEqual(inventory["unit_kind"], "line")
        result = DoclingBackend().convert_range(source, "txt", 1, 1)
        self.assertEqual(result["status"], "ok")
        self.assertIsNone(result["document"])
        self.assertEqual([x["unit_id"] for x in result["units"]], ["line:1", "line:2", "line:3"])
        self.assertEqual("".join(x["text"] for x in result["units"]), "FIRST\r\n\r\nTHIRD\n")
        self.assertTrue(all(x["doc_page_no"] is None for x in result["units"]))
        source.write_bytes(b"\xff\xfe\x00")
        result = DoclingBackend().convert_range(source, "txt", 1, 1)
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["errors"])

    def test_preflight_fails_closed_before_inference(self):
        result = preflight(BackendOptions(artifacts_path=str(self.root)))
        self.assertFalse(result["ready"])
        self.assertTrue(any("model" in error for error in result["errors"]))
        backend = DoclingBackend(BackendOptions(artifacts_path=str(self.root)))
        with patch.object(backend, "_converter", side_effect=RuntimeError("missing local artifacts")):
            result = backend.convert_range(self.root / "absent.tiff", "tiff", 2, 4)
        self.assertEqual([r["unit_id"] for r in result["units"]], [2, 3, 4])
        self.assertTrue(all(r["conversion_status"] == "failed" for r in result["units"]))

    def test_storage_exhaustion_is_not_conversion_gap(self):
        source = self.root / "text.txt"
        with patch("agent_transcribe.docling_backend._read_text", side_effect=OSError(errno.ENOSPC, "full")):
            with self.assertRaises(OSError):
                DoclingBackend().convert_range(source, "txt", 1, 1)

    def test_placeholders_and_explicit_errors_override_page_existence(self):
        class Document:
            pages = {2: object(), 3: object(), 4: object()}
            def export_to_markdown(self, **kwargs):
                return f"PAGE {kwargs['page_no']}"
        class Error:
            def model_dump(self, **kwargs):
                return {"page_no": 3, "error_message": "inference failed"}
        converted = SimpleNamespace(document=Document(), pages=[SimpleNamespace(page_no=2), SimpleNamespace(page_no=3)],
                                    status=SimpleNamespace(value="partial_success"), errors=[Error()])
        backend = DoclingBackend()
        with patch.object(backend, "_converter", return_value=SimpleNamespace(convert=lambda *a, **kw: converted)), \
                patch("agent_transcribe.docling_backend._visible_layers", return_value=set()):
            result = backend.convert_range("synthetic.pdf", "pdf", 2, 4)
        self.assertEqual([u["conversion_status"] for u in result["units"]], ["ok", "failed", "failed"])
        self.assertEqual(result["units"][0]["doc_page_no"], 2)
        self.assertEqual(result["status"], "partial")

    @unittest.skipUnless(HAS_PIL, "Pillow not installed")
    def test_exif_all_values_then_each_rotation_exact_pixels(self):
        from PIL import Image
        transpose = {2: Image.Transpose.FLIP_LEFT_RIGHT, 3: Image.Transpose.ROTATE_180,
                     4: Image.Transpose.FLIP_TOP_BOTTOM, 5: Image.Transpose.TRANSPOSE,
                     6: Image.Transpose.ROTATE_270, 7: Image.Transpose.TRANSVERSE, 8: Image.Transpose.ROTATE_90}
        source_image = corners()
        for orientation in range(1, 9):
            source = self.root / f"orientation-{orientation}.tiff"
            fixture_tiff(source, [source_image], [orientation])
            original = source.read_bytes()
            for rotation in (0, 90, 180, 270):
                destination = self.root / f"render-{orientation}-{rotation}"
                row = render_units(source, "tiff", [1], destination, rotate_clockwise=rotation)[0]
                self.assertEqual(row["decode_status"], "ok", row)
                expected = source_image.transpose(transpose[orientation]) if orientation in transpose else source_image.copy()
                expected = expected.rotate(-rotation, expand=True)
                with Image.open(destination / row["asset"]) as actual:
                    self.assertEqual(actual.size, expected.size)
                    self.assertEqual(actual.tobytes(), expected.tobytes(), (orientation, rotation))
                    self.assertNotIn(274, actual.getexif())
                self.assertEqual(source.read_bytes(), original)

    @unittest.skipUnless(HAS_PIL, "Pillow not installed")
    def test_blanks_duplicates_later_exif_and_mixed_dimensions(self):
        from PIL import Image
        for colors in (("white", "red", "blue"), ("red", "white", "blue"),
                       ("red", "blue", "white"), ("white", "white", "white"), ("red", "red", "red")):
            source = self.root / ("-".join(colors) + ".tiff")
            fixture_tiff(source, [Image.new("RGB", (7 + n, 5), c) for n, c in enumerate(colors)], [1, 6, 8])
            inventory = inspect_source(source, "tiff")
            self.assertEqual(inventory["expected_count"], 3)
            rows = render_units(source, "tiff", [1, 2, 3], self.root / source.stem)
            self.assertEqual([r["unit_id"] for r in rows], [1, 2, 3])
            self.assertEqual([r["decode_status"] for r in rows], ["ok"] * 3)
            self.assertEqual([(r["width"], r["height"]) for r in rows], [(7, 5), (5, 8), (5, 9)])
            self.assertEqual(rows[1]["dpi"], [192, 96])

    @unittest.skipUnless(HAS_PIL, "Pillow not installed")
    def test_middle_decode_failure_preserves_later_frame(self):
        from PIL import Image
        source = self.root / "corrupt-middle.tiff"
        fixture_tiff(source, [Image.new("RGB", (30, 20), c) for c in ("red", "white", "blue")])
        patch_middle_strip(source)
        self.assertEqual(inspect_source(source, "tiff")["expected_count"], 3)
        rows = render_units(source, "tiff", [1, 2, 3], self.root / "rendered")
        self.assertEqual([r["decode_status"] for r in rows], ["ok", "failed", "ok"])
        with Image.open(self.root / "rendered" / rows[2]["asset"]) as image:
            self.assertEqual(image.getpixel((0, 0)), (0, 0, 255))

    @unittest.skipUnless(HAS_PIL, "Pillow not installed")
    def test_invalid_exif_is_explicit_and_later_frame_survives(self):
        source = self.root / "invalid-exif.tiff"
        fixture_tiff(source, [corners(), corners(), corners()], [1, 9, 1])
        rows = render_units(source, "tiff", [1, 2, 3], self.root / "metadata-rendered")
        self.assertEqual([r["decode_status"] for r in rows], ["ok", "failed", "ok"])
        self.assertIn("EXIF orientation", rows[1]["errors"][0])

    @unittest.skipUnless(HAS_PIL, "Pillow not installed")
    def test_publication_failure_and_symlink_not_decode_gap(self):
        source = self.root / "one.tiff"
        fixture_tiff(source, [corners()])
        destination = self.root / "assets"
        render_units(source, "tiff", [1], destination)
        with self.assertRaises(FileExistsError):
            render_units(source, "tiff", [1], destination)
        alias = self.root / "alias"
        alias.symlink_to(destination, target_is_directory=True)
        with self.assertRaises(ValueError):
            render_units(source, "tiff", [1], alias)

    @unittest.skipUnless(HAS_PIL, "Pillow not installed")
    def test_broken_ifd_chain_retains_prefix_unknown_count(self):
        source = self.root / "chain.tiff"
        fixture_tiff(source, [corners(), corners(), corners()])
        inventory = inspect_source(source, "tiff")
        last = inventory["units"][-1]["ifd_offset"]
        with source.open("r+b") as f:
            f.seek(last)
            count = struct.unpack("<H", f.read(2))[0]
            f.seek(last + 2 + count * 12)
            f.write(struct.pack("<I", source.stat().st_size + 999))
        inventory = inspect_source(source, "tiff")
        self.assertIsNone(inventory["expected_count"])
        self.assertEqual(len(inventory["units"]), 3)
        self.assertTrue(inventory["count_warnings"])

    @unittest.skipUnless(HAS_CORE, "Docling core not installed")
    def test_visible_furniture_notes_and_picture_descendants(self):
        from docling_core.types.doc import DoclingDocument, ContentLayer, DocItemLabel, Size, ProvenanceItem, BoundingBox
        document = DoclingDocument(name="layers")
        document.add_page(page_no=2, size=Size(width=200, height=100))
        provenance = ProvenanceItem(page_no=2, charspan=(0, 5), bbox=BoundingBox(l=0, t=0, r=20, b=20))
        for layer in ContentLayer:
            document.add_text(label=DocItemLabel.TEXT, text=layer.value.upper(), prov=provenance, content_layer=layer)
        picture = document.add_picture(prov=provenance)
        document.add_text(label=DocItemLabel.TEXT, text="PICTURE CHILD", parent=picture, prov=provenance)
        text = _document_text(document, 2)
        for marker in ("BODY", "FURNITURE", "NOTES", "PICTURE CHILD"):
            self.assertIn(marker, text)
        for marker in ("BACKGROUND", "INVISIBLE"):
            self.assertNotIn(marker, text)


@unittest.skipUnless(ARTIFACTS and HAS_CORE, "set AGENT_TRANSCRIBE_TEST_MODELS for real offline inference")
class RealDoclingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        cls.backend = DoclingBackend(BackendOptions(artifacts_path=ARTIFACTS, document_timeout=120))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="docling-real-")
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_real_nonfirst_pdf_range_and_warm_converter(self):
        from reportlab.pdfgen.canvas import Canvas
        source = self.root / "three.pdf"
        canvas = Canvas(str(source), pagesize=(450, 600))
        for number in range(1, 4):
            canvas.drawString(50, 500, f"ORIGINAL PHYSICAL PAGE {number}")
            canvas.showPage()
        canvas.save()
        self.assertEqual(inspect_source(source, "pdf")["expected_count"], 3)
        original = source.read_bytes()
        result = self.backend.convert_range(source, "pdf", 2, 3)
        self.assertEqual(result["status"], "ok", result["errors"])
        self.assertEqual(set(result["document"].pages), {2, 3})
        self.assertEqual([u["unit_id"] for u in result["units"]], [2, 3])
        for unit in result["units"]:
            self.assertIn(f"ORIGINAL PHYSICAL PAGE {unit['unit_id']}", unit["text"])
            self.assertEqual(unit["doc_page_no"], unit["unit_id"])
        converter = self.backend._converter("pdf", "default")
        result = self.backend.convert_range(source, "pdf", 1, 1)
        self.assertEqual(result["status"], "ok", result["errors"])
        self.assertIs(converter, self.backend._converter("pdf", "default"))
        self.assertEqual(source.read_bytes(), original)
        rows = render_units(source, "pdf", [3], self.root / "rendered", images_scale=2)
        self.assertEqual((rows[0]["width"], rows[0]["height"]), (900, 1200))

    def test_real_tiff_all_frames_blank_and_original_ids(self):
        from PIL import Image, ImageDraw, ImageFont
        font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 48)
        frames = []
        for word in ("FRAME ONE ALPHA", "", "FRAME THREE GAMMA"):
            image = Image.new("RGB", (1000, 500), "white")
            ImageDraw.Draw(image).text((50, 100), word, font=font, fill="black")
            frames.append(image)
        source = self.root / "three.tiff"
        frames[2] = frames[2].rotate(270, expand=True)
        fixture_tiff(source, frames, [1, 1, 8])
        result = self.backend.convert_range(source, "tiff", 1, 3)
        self.assertEqual(result["status"], "ok", result["errors"])
        self.assertEqual([u["unit_id"] for u in result["units"]], [1, 2, 3])
        self.assertIn("ALPHA", result["units"][0]["text"])
        self.assertIn("GAMMA", result["units"][2]["text"])
        self.assertTrue(result["units"][1]["recognition_warnings"])
        self.assertEqual(set(result["document"].pages), {1, 2, 3})
        later = self.backend.convert_range(source, "tiff", 3, 3)
        self.assertEqual(later["status"], "ok", later["errors"])
        self.assertEqual(set(later["document"].pages), {3})
        self.assertIn("GAMMA", later["units"][0]["text"])
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.document import InputDocument
        converter = self.backend._converter("tiff", "default")
        backend_class = converter.format_to_options[InputFormat.IMAGE].backend
        # Run the actual installed adapter load path, not an inferred filename
        # or a renderer-only assertion. This also rejects constructor reset.
        self.assertEqual(backend_class.__name__, "RotatedImageBackend")
        options = BackendOptions(artifacts_path=ARTIFACTS, rotate_clockwise=90)
        rotated = DoclingBackend(options)
        rotated_converter = rotated._converter("tiff", "default")
        cls = rotated_converter.format_to_options[InputFormat.IMAGE].backend
        in_doc = InputDocument(path_or_stream=source, format=InputFormat.IMAGE, backend=cls)
        page = in_doc._backend.load_page(2)
        self.assertEqual(page._image.size, (500, 1000))
        page.unload()
        in_doc._backend.unload()


    def test_real_docx_has_structural_package_not_physical_pages(self):
        from docx import Document
        source = self.root / "structure.docx"
        document = Document()
        document.add_heading("SYNTHETIC STRUCTURAL HEADING", 1)
        document.add_paragraph("Paragraph with an explicit structural locator")
        table = document.add_table(rows=2, cols=2)
        table.cell(0, 0).text = "TABLE MARKER"
        table.cell(1, 1).text = "CELL VALUE"
        document.save(source)
        inventory = inspect_source(source, "docx")
        self.assertEqual(inventory["unit_kind"], "package")
        result = self.backend.convert_range(source, "docx", 1, 1)
        self.assertEqual(result["status"], "ok", result["errors"])
        self.assertEqual(result["units"][0]["unit_id"], "package:word/document.xml")
        self.assertIsNone(result["units"][0]["doc_page_no"])
        self.assertIn("TABLE MARKER", result["units"][0]["text"])
        self.assertTrue(result["units"][0]["structural_locators"])


if __name__ == "__main__":
    unittest.main()
