"""Actual finite recovery orchestration, native synthetic docs, real TIFF pixels."""
import copy
from pathlib import Path
import tempfile
import unittest
import test_image_placeholder as helpers
from test_conversion_recovery import Backend,frames,result
from agent_transcribe.recovery import recover_range
from agent_transcribe.portable import export_part

class OrientationNativeEmpty(unittest.TestCase):
    def run_case(self, root, readings):
        helper=helpers.PlaceholderTests();source=root/"frames.tiff";frames(source)
        initial=helper.adapt(helper.document());initial["units"][0].update(unit_id=2,recognition_warnings=["sparse native text"])
        baseline=copy.deepcopy(initial["units"])
        def behavior(path,kind,n,mode):
            index=len(backend.calls)-1
            # First the existing full-page rescue; then four orientation calls.
            text=None if index==0 else readings[index-1]
            value=helper.adapt(helper.document(text))
            value["units"][0]["unit_id"]=n
            return value
        backend=Backend(behavior)
        recovered=recover_range(backend,source,"tiff",2,2,initial,scratch=root/"scratch")
        self.assertEqual(len(backend.calls),5)
        self.assertEqual(len(recovered["variants"]),6)
        self.assertEqual(recovered["variants"][0]["units"],baseline)
        calls=len(backend.calls)
        replay=recover_range(backend,source,"tiff",2,2,initial,scratch=root/"scratch")
        self.assertEqual(len(backend.calls),calls);self.assertEqual(replay["units"],recovered["units"])
        return recovered
    def test_sole_real_orientation_selected_with_native_link_and_original_locator(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);r=self.run_case(root,[None,"Synthetic foreground 5821",None,None]);u=r["units"][0]
            self.assertIn("Synthetic foreground 5821",u["text"])
            self.assertEqual(u["transform"]["rotate_clockwise"],90)
            self.assertEqual(u["unit_id"],2);self.assertEqual(u["doc_page_no"],1)
            self.assertEqual(u["variant_index"],3)
            self.assertFalse(any("ambiguous orientation" in x for x in r["recognition_warnings"]))
            v=r["variants"][u["variant_index"]];self.assertIsNotNone(v["document"])
            self.assertEqual(v["provenance"]["original_mapping"],{"1":2})
            for name,value in [("primary",r),("native",v)]:
                out=root/name;out.mkdir();m=export_part(value,out,source={"relative_path":"frames.tiff","kind":"tiff","order":1})
                self.assertEqual(m["units"][0]["original_locator"]["unit_id"],2)
                if name=="native":self.assertTrue(m["item_refs"]);self.assertTrue(all(x["original_locator"]["unit_id"]==2 for x in m["item_refs"]))
                else:self.assertEqual(m["units"][0]["variant_index"],3)
    def test_multiple_real_readings_remain_ambiguous(self):
        with tempfile.TemporaryDirectory() as tmp:
            r=self.run_case(Path(tmp),[None,"First real reading","Second real reading",None])
            self.assertTrue(r["units"][0]["native_text_empty"])
            self.assertNotIn("variant_index",r["units"][0])
            self.assertTrue(any("ambiguous orientation" in x for x in r["recognition_warnings"]))
    def test_literal_native_marker_counts_as_real_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            r=self.run_case(Path(tmp),[None,"<!-- image -->",None,None])
            self.assertFalse(r["units"][0]["native_text_empty"])
            self.assertEqual(r["units"][0]["variant_index"],3)
