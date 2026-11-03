"""Synthetic native picture-only accounting; no private archive fixtures."""
import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from PIL import Image
from docling_core.types.doc import DoclingDocument, Size, ImageRef, ProvenanceItem, BoundingBox, DocItemLabel
from agent_transcribe.docling_backend import DoclingBackend, BackendOptions, _document_text, native_text_empty
from agent_transcribe.portable import export_part
from agent_transcribe.recovery import recover_range
from test_conversion_recovery import Backend, result, unit, frames

class PlaceholderTests(unittest.TestCase):
    def document(self, caption=None):
        d=DoclingDocument(name="synthetic figure")
        d.add_page(page_no=1,size=Size(width=100,height=100),image=ImageRef.from_pil(Image.new("RGB",(100,100),"white"),dpi=72))
        p=ProvenanceItem(page_no=1,charspan=(0,0),bbox=BoundingBox(l=1,t=1,r=99,b=99,coord_origin="TOPLEFT"))
        picture=d.add_picture(prov=p)
        if caption: d.add_text(label=DocItemLabel.TEXT,text=caption,parent=picture,prov=p)
        return d
    def adapt(self,d):
        return DoclingBackend()._adapt(SimpleNamespace(document=d,errors=[],status="success",pages=[SimpleNamespace(page_no=1)]),"image",[1],{"units":[],"provenance":{"mode":"default"}})
    def test_native_image_only_warns_without_altering_markdown(self):
        d=self.document(); text=_document_text(d,1); self.assertEqual(text.strip(),"<!-- image -->")
        r=self.adapt(d);self.assertEqual(r["units"][0]["text"],text);self.assertIn("no text detected",r["units"][0]["recognition_warnings"][0]);self.assertEqual(r["status"],"ok")
    def test_caption_and_numeric_table_markdown_are_not_empty(self):
        self.assertFalse(self.adapt(self.document("Figure 1: 17.25 units"))["units"][0]["recognition_warnings"])
        for text in ("<!-- image -->", "| Item | 17.25 |", "A", "<!-- other -->"):
            self.assertFalse(native_text_empty(self.document(text),1))
    def test_export_native_empty_flag_but_literal_txt_docx_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            r=self.adapt(self.document()); out=Path(tmp)/"native";out.mkdir()
            manifest=export_part(r,out,source={"relative_path":"fixture.png","kind":"image","order":1})
            self.assertTrue(manifest["units"][0]["text_empty"]);self.assertTrue(manifest["item_refs"])
            self.assertIn("No searchable text detected",(out/manifest["units"][0]["text_path"]).read_text())
            for kind in ("txt","docx"):
                out=Path(tmp)/kind;out.mkdir(); rr=copy.deepcopy(r);rr["document"]=None if kind=="txt" else self.document();rr["units"][0]["text"]="<!-- image -->"
                m=export_part(rr,out,source={"relative_path":"fixture."+kind,"kind":kind,"order":1})
                self.assertFalse(m["units"][0]["text_empty"])
    def test_one_fullpage_attempt_and_restart_retains_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/"test.tiff";frames(source)
            initial=self.adapt(self.document());backend=Backend(lambda p,k,n,m:result([unit(n,"Synthetic foreground 1974 633-4","ok")]))
            value=recover_range(backend,source,"tiff",1,1,initial,scratch=root/"scratch")
            self.assertEqual(len(backend.calls),1);self.assertEqual(backend.calls[0][-1],"full_page");self.assertEqual(value["variants"][0]["units"][0]["text"],"<!-- image -->")
            self.assertEqual(value["units"][0]["text"],"Synthetic foreground 1974 633-4")
            replay=recover_range(backend,source,"tiff",1,1,initial,scratch=root/"scratch");self.assertEqual(len(backend.calls),1);self.assertEqual(replay["units"],value["units"])
    def test_unrecovered_image_only_retains_warning_and_finite_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/"test.tiff";frames(source)
            r=self.adapt(self.document());backend=Backend(lambda p,k,n,m:self.adapt(self.document()))
            value=recover_range(backend,source,"tiff",1,1,r,scratch=root/"scratch")
            self.assertEqual(len(backend.calls),1);self.assertIn("no text detected",value["units"][0]["recognition_warnings"][0]);self.assertEqual(len(value["variants"]),2)

    def test_strict_txt_literal_survives_backend_and_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/"literal.txt";source.write_text("<!-- image -->")
            r=DoclingBackend().convert_range(source,"txt",1,1)
            self.assertEqual(r["units"][0]["text"],"<!-- image -->")
            self.assertFalse(r["units"][0]["recognition_warnings"])
            out=Path(tmp)/"part";out.mkdir();m=export_part(r,out,source={"relative_path":"literal.txt","kind":"txt","order":1})
            self.assertFalse(m["units"][0]["text_empty"])
    def test_native_table_text_survives_image_marker_suppression(self):
        from docling_core.types.doc import TableData,TableCell
        d=self.document();d.add_table(data=TableData(num_rows=1,num_cols=1,table_cells=[TableCell(text="Balance 419.00",start_row_offset_idx=0,end_row_offset_idx=1,start_col_offset_idx=0,end_col_offset_idx=1)]),prov=ProvenanceItem(page_no=1,charspan=(0,0),bbox=BoundingBox(l=1,t=1,r=50,b=50,coord_origin="TOPLEFT")))
        self.assertFalse(native_text_empty(d,1));self.assertFalse(self.adapt(d)["units"][0]["recognition_warnings"])

    def test_selected_image_only_recovery_exports_preserved_native_empty(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/"synthetic.pdf";source.write_bytes(b"synthetic fixture, inventory mocked")
            backend=Backend(lambda p,k,n,m:self.adapt(self.document()))
            inventory={"expected_count":1,"unit_kind":"page","units":[{"unit_id":1}],"count_warnings":[]}
            with patch("agent_transcribe.recovery.inspect_source",return_value=inventory):
                recovered=recover_range(backend,source,"pdf",1,1,result([unit(1,"","failed")]),scratch=root/"scratch")
            self.assertIsNone(recovered["document"]);self.assertTrue(recovered["units"][0]["native_text_empty"])
            self.assertEqual(len(backend.calls),2) # singleton then finite full-page
            out=root/"part";out.mkdir();m=export_part(recovered,out,source={"relative_path":"synthetic.pdf","kind":"pdf","order":1})
            self.assertTrue(m["units"][0]["text_empty"])
            self.assertIn("No searchable text detected",(out/m["units"][0]["text_path"]).read_text())
            self.assertEqual(m["units"][0]["original_locator"]["unit_id"],1)
    def test_mixed_recovery_retains_empty_and_literal_native_units(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/"synthetic.pdf";source.write_bytes(b"synthetic fixture, inventory mocked")
            empty=self.adapt(self.document())["units"][0]
            literal=self.adapt(self.document("<!-- image -->"))["units"][0];literal["unit_id"]=3;literal["doc_page_no"]=3
            initial=result([empty,unit(2,"","failed"),literal])
            def recover(p,k,n,mode):
                r=self.adapt(self.document("Recovered foreground 42"));r["units"][0]["unit_id"]=n;r["units"][0]["doc_page_no"]=n;return r
            backend=Backend(recover)
            inventory={"expected_count":3,"unit_kind":"page","units":[{"unit_id":n} for n in range(1,4)],"count_warnings":[]}
            # The existing empty unit's full-page candidate remains image-only.
            backend.behavior=lambda p,k,n,mode: self.adapt(self.document()) if n==1 else recover(p,k,n,mode)
            with patch("agent_transcribe.recovery.inspect_source",return_value=inventory):
                recovered=recover_range(backend,source,"pdf",1,3,initial,scratch=root/"scratch")
            self.assertIsNone(recovered["document"]);out=root/"part";out.mkdir()
            m=export_part(recovered,out,source={"relative_path":"synthetic.pdf","kind":"pdf","order":1})
            self.assertEqual([u["text_empty"] for u in m["units"]],[True,False,False])
            self.assertIn("Recovered foreground 42",m["units"][1]["text"])
            self.assertIn("image",m["units"][2]["text"])
            self.assertEqual([u["original_locator"]["unit_id"] for u in m["units"]],[1,2,3])
            calls=len(backend.calls)
            with patch("agent_transcribe.recovery.inspect_source",return_value=inventory):
                replay=recover_range(backend,source,"pdf",1,3,initial,scratch=root/"scratch")
            self.assertEqual(len(backend.calls),calls);self.assertEqual(replay["units"],recovered["units"])
