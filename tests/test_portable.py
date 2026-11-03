"""Synthetic portability checks; real pinned Docling model and exporters."""
import copy
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw

from agent_transcribe.common import IntegrityError, checksum, json_bytes
from agent_transcribe.portable import export_part, write_item, write_bundle_views, verify_bundle

try:
    from docling_core.types.doc import (DoclingDocument, DocItemLabel, ImageRef,
        ProvenanceItem, BoundingBox, Size, ContentLayer, TableData, TableCell)
except ImportError:
    DoclingDocument = None


@unittest.skipIf(DoclingDocument is None, "Pinned Docling integration extra is not installed")
class Portable(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "bundle original # café"
        self.root.mkdir()
        self.source_name = "dir # 日本語/source # café.pdf"
        original = self.root / "Originals" / self.source_name
        original.parent.mkdir(parents=True)
        # A real five-page PDF, readable by ordinary tools after relocation.
        try:
            from reportlab.pdfgen import canvas
            canvas = canvas.Canvas(str(original), pagesize=(100, 100))
            for n in range(1, 6):
                canvas.drawString(5, 50, f"ORIGINAL_PAGE_{n}")
                canvas.showPage()
            canvas.save()
        except ImportError:
            from pypdf import PdfWriter
            writer = PdfWriter()
            for _ in range(5):
                writer.add_blank_page(width=100, height=100)
            with original.open("wb") as stream:
                writer.write(stream)
        self.source = {"relative_path": self.source_name, "kind": "pdf", "sha256": checksum(original), "order": 1, "size": original.stat().st_size}
        self.item_relative = "Items/000001-source # café.pdf"
        self.item = self.root / self.item_relative
        self.part_relative = "parts/000003-000004"
        self.part = self.item / self.part_relative
        self.part.mkdir(parents=True)
        self.inventory = {"expected_count": 2, "unit_kind": "page", "units": [{"unit_id": 3}, {"unit_id": 4}], "count_method": "synthetic selected range", "count_warnings": []}

    def image(self):
        # Top-left origin, 2x source scale. Crop (10,10)-(40,40)
        # must be red, unlike the bottom-left blue landmark.
        image = Image.new("RGB", (200, 200), "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle((20, 20, 79, 79), fill="red")
        draw.rectangle((20, 120, 79, 179), fill="blue")
        return image

    def result(self, *, failed=False):
        units = []
        for n in (3, 4):
            units.append({"unit_id": n, "doc_page_no": n, "decode_status": "ok", "conversion_status": "failed" if failed else "ok", "text": "" if failed else f"BODY_PAGE_{n}\nHEADER_PAGE_{n}\nNOTE_PAGE_{n}\nPICTURE_CHILD_PAGE_{n}\nTABLE_PAGE_{n}", "recognition_warnings": ["synthetic recognition warning"] if n == 4 else [], "attempts": [], "transform": {"clockwise": 0}})
        return {"document": None if failed else self.document(), "units": units, "status": "partial" if failed else "ok", "errors": ["synthetic inference failure"] if failed else []}

    def document(self):
        if DoclingDocument is None:
            self.skipTest("Pinned Docling integration extra is not installed")
        doc = DoclingDocument(name="source # café.pdf")
        for n in (3, 4):
            doc.add_page(page_no=n, size=Size(width=100, height=100), image=ImageRef.from_pil(self.image(), dpi=144))
            def prov(text):
                return ProvenanceItem(page_no=n, bbox=BoundingBox(l=10, t=10, r=40, b=40, coord_origin="TOPLEFT"), charspan=(0, len(text)))
            for label, layer, prefix in ((DocItemLabel.TEXT, ContentLayer.BODY, "BODY"), (DocItemLabel.PAGE_HEADER, ContentLayer.FURNITURE, "HEADER"), (DocItemLabel.TEXT, ContentLayer.NOTES, "NOTE")):
                text = f"{prefix}_PAGE_{n}"
                doc.add_text(label=label, text=text, content_layer=layer, prov=prov(text))
            picture = doc.add_picture(prov=prov("picture"), image=ImageRef.from_pil(Image.new("RGB", (60, 60), "green"), dpi=144))
            # A second figure span must use base geometry, never stored green pixels.
            picture.prov.append(ProvenanceItem(page_no=n, bbox=BoundingBox(l=10, t=40, r=40, b=10, coord_origin="BOTTOMLEFT"), charspan=(0, 1)))
            doc.add_text(label=DocItemLabel.TEXT, text=f"PICTURE_CHILD_PAGE_{n}", parent=picture, prov=prov("child"))
            doc.add_table(data=TableData(num_rows=1, num_cols=1, table_cells=[TableCell(text=f"TABLE_PAGE_{n}", start_row_offset_idx=0, end_row_offset_idx=1, start_col_offset_idx=0, end_col_offset_idx=1)]), prov=prov("table"))
        return doc

    def render(self):
        directory = self.part / "assets-rendered"
        directory.mkdir()
        rows = []
        for n in (3, 4):
            asset = directory / f"page-{n}.png"
            self.image().save(asset)
            rows.append({"unit_id": n, "decode_status": "ok", "asset": f"assets-rendered/page-{n}.png", "width": 200, "height": 200, "dpi": 144, "transform": {"clockwise": 0}, "errors": []})
        return rows

    def bundle(self, *, failed=False, reuse=None, state="complete"):
        manifest = export_part(self.result(failed=failed), self.part, source=self.source, rendered=self.render())
        item_map = write_item(self.root, self.item_relative, self.source, self.inventory, [{"relative_path": self.part_relative, "manifest": manifest}], reuse=reuse)
        entry = {"item_relative": self.item_relative, "source": self.source, "inventory": self.inventory, "parts": [{"relative_path": self.part_relative, "manifest": manifest}], "state": state, "map": item_map, "files": item_map["files"]}
        receipt = {"kind": "docling-folder", "schema": 1, "state": state, "sources": [entry]}
        receipt.update(write_bundle_views(self.root, receipt))
        (self.root / "conversion.json").write_bytes(json_bytes(receipt))
        return manifest, item_map, receipt

    @unittest.skipIf(DoclingDocument is None, "Pinned Docling extra unavailable")
    def test_real_export_layers_native_ids_and_relative_assets(self):
        manifest, item_map, _ = self.bundle()
        self.assertEqual(manifest["native_json"], "document.json")
        native = json.loads((self.part / "document.json").read_text())
        self.assertEqual(native["schema_name"], "DoclingDocument")
        self.assertEqual({r["unit_id"] for r in manifest["item_refs"]}, {3, 4})
        self.assertIn("#/pictures/0", {r["item_ref"] for r in manifest["item_refs"]})
        for n in (3, 4):
            text = (self.part / "units" / f"{n-2:06d}.md").read_text().replace("\\_", "_")
            for prefix in ("BODY", "HEADER", "NOTE", "PICTURE_CHILD", "TABLE"):
                self.assertIn(f"{prefix}_PAGE_{n}", text)
                self.assertNotIn(f"{prefix}_PAGE_{7-n}", text)
        self.assertTrue(item_map["coverage_accounted"])
        self.assertEqual(verify_bundle(self.root)["exit_code"], 0)

    @unittest.skipIf(DoclingDocument is None, "Pinned Docling extra unavailable")
    def test_crop_every_span_base_geometry_origin_and_scale(self):
        manifest = export_part(self.result(), self.part, source=self.source, rendered=self.render())
        crops = [ref for ref in manifest["item_refs"] if ref["crop"]]
        self.assertEqual(len(crops), 6)
        for ref in crops:
            with Image.open(self.part / ref["crop"]) as image:
                self.assertEqual(image.size, (60, 60))
                expected = (0, 0, 255) if ref["bbox"]["coord_origin"] == "BOTTOMLEFT" else (255, 0, 0)
                self.assertEqual(image.getpixel((30, 30)), expected)

    @unittest.skipIf(DoclingDocument is None, "Pinned Docling extra unavailable")
    def test_standalone_relocation_nonfirst_range_and_native_reload(self):
        self.bundle()
        moved = self.base / "relocated # 日本語"
        shutil.move(self.root, moved)
        self.assertFalse(self.root.exists())
        with patch("os.getcwd", return_value="/"):
            result = verify_bundle(moved)
        self.assertEqual(result["exit_code"], 0, result)
        part = moved / self.item_relative / self.part_relative
        restored = DoclingDocument.load_from_json(part / "document.json", allow_local_images=True)
        self.assertEqual(set(restored.pages), {3, 4})
        self.assertTrue(all(page.image.pil_image for page in restored.pages.values()))
        self.assertTrue(all(picture.image.pil_image for picture in restored.pictures))
        text = (moved / self.item_relative / "reading.md").read_text()
        self.assertIn("#page=3", text)
        self.assertIn("#page=4", text)
        self.assertNotIn("#page=1", text)
        import pypdfium2 as pdfium
        original = moved / "Originals" / self.source_name
        with pdfium.PdfDocument(original) as pdf:
            self.assertEqual(len(pdf), 5)
            page = pdf[2]
            text_page = page.get_textpage()
            self.assertIn("ORIGINAL_PAGE_3", text_page.get_text_range())
            text_page.close()
            page.close()

    def test_failed_inference_keeps_independent_images_and_no_fake_native_json(self):
        manifest, item_map, _ = self.bundle(failed=True)
        self.assertIsNone(manifest["native_json"])
        self.assertFalse((self.part / "document.json").exists())
        self.assertTrue(all((self.part / unit["render"]["asset"]).is_file() for unit in manifest["units"]))
        self.assertFalse(item_map["conversion_complete"])
        checked = verify_bundle(self.root)
        self.assertTrue(checked["valid"], checked)
        self.assertTrue(checked["coverage_accounted"])
        self.assertEqual(checked["exit_code"], 2)

    def test_pending_publication_is_valid_exit_two(self):
        self.bundle(failed=True, state="pending")
        checked = verify_bundle(self.root)
        self.assertTrue(checked["valid"], checked)
        self.assertFalse(checked["publication_complete"])
        self.assertEqual(checked["exit_code"], 2)

    def test_changed_original_is_integrity_failure(self):
        self.bundle(failed=True)
        (self.root / "Originals" / self.source_name).write_bytes(b"tampered")
        self.assertEqual(verify_bundle(self.root)["exit_code"], 1)

    def test_changed_accepted_view_is_integrity_failure(self):
        self.bundle(failed=True)
        (self.item / "reading.md").write_text("tampered")
        self.assertEqual(verify_bundle(self.root)["exit_code"], 1)

    def test_symlink_is_integrity_failure(self):
        self.bundle(failed=True)
        (self.root / "escape.png").symlink_to(self.part / "assets-rendered/page-3.png")
        self.assertEqual(verify_bundle(self.root)["exit_code"], 1)

    def test_traversal_part_and_duplicate_units_are_rejected(self):
        result = self.result(failed=True)
        result["units"].append(copy.deepcopy(result["units"][0]))
        with self.assertRaises(IntegrityError):
            export_part(result, self.part, source=self.source)
        with self.assertRaises(IntegrityError):
            write_item(self.root, "../outside", self.source, self.inventory, [])

    def test_missing_accounting_rejected_and_unknown_count_stays_partial(self):
        manifest = export_part(self.result(failed=True), self.part, source=self.source, rendered=self.render())
        parts = [{"relative_path": self.part_relative, "manifest": manifest}]
        inventory = copy.deepcopy(self.inventory)
        inventory["units"].append({"unit_id": 5})
        inventory["expected_count"] = 3
        with self.assertRaises(IntegrityError):
            write_item(self.root, self.item_relative, self.source, inventory, parts)
        inventory = copy.deepcopy(self.inventory)
        inventory["expected_count"] = None
        item_map = write_item(self.root, self.item_relative, self.source, inventory, parts)
        self.assertFalse(item_map["coverage_accounted"])
        self.assertFalse(item_map["conversion_complete"])

    def test_reuse_preserves_accepted_reading_gaps_and_separate_candidate(self):
        accepted = self.base / "accepted.txt"
        accepted.write_text("ACCEPTED WORDING\n[known missing segment]\n")
        reuse = {"text_path": str(accepted), "text_sha256": checksum(accepted), "source_sha256": self.source["sha256"], "gaps": ["missing segment"], "notes": "Descriptive evidence only"}
        _, item_map, _ = self.bundle(failed=True, reuse=reuse)
        preferred = (self.item / "reading.md").read_text()
        self.assertIn(accepted.read_text(), preferred)
        self.assertIn("missing segment", preferred)
        self.assertIn("boundaries are not inferred", preferred)
        self.assertEqual(item_map["candidate_path"], "pages.md")
        self.assertTrue((self.item / "pages.md").exists())
        self.assertIn("additional mapped candidate", (self.root / "readings.txt").read_text())
        self.assertIn("not source wording", (self.item / "README.md").read_text())
        self.assertEqual(verify_bundle(self.root)["exit_code"], 2)

    def test_txt_and_docx_salvage_do_not_invent_physical_pages(self):
        for kind, unit_kind, uid in (("txt", "line", "1-3"), ("docx", "package", "word/document.xml#paragraph-1")):
            with self.subTest(kind=kind):
                source = dict(self.source, kind=kind)
                part = self.base / kind
                manifest = export_part({"document": None, "units": [{"unit_id": uid, "unit_kind": unit_kind, "conversion_status": "ok", "decode_status": "not_applicable", "text": "Text"}]}, part, source=source)
                self.assertFalse((part / "document.json").exists())
                self.assertEqual(manifest["units"][0]["original_locator"]["unit_kind"], unit_kind)
                self.assertEqual(manifest["units"][0]["original_locator"]["unit_id"], uid)

    def test_existing_part_cannot_be_rewritten(self):
        result = self.result(failed=True)
        export_part(result, self.part, source=self.source)
        with self.assertRaises(IntegrityError):
            export_part(result, self.part, source=self.source)


if __name__ == "__main__":
    unittest.main()
