"""Opt-in raster export geometry, frozen settings and offline recognition."""
import json
import math
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
from agent_transcribe.docling_backend import BackendOptions, DoclingBackend, render_units
from agent_transcribe import conversion as c
from agent_transcribe.common import IntegrityError
from agent_transcribe.folder_intake import build_folder_plan
from agent_transcribe.portable import verify_bundle

class RasterEconomyTests(unittest.TestCase):
    def test_default_frozen_contract_and_invalid_scales(self):
        self.assertEqual(BackendOptions().frozen(), dict(artifacts_path=None, threads=4, document_timeout=300, images_scale=3, rotate_clockwise=0))
        self.assertEqual(BackendOptions(raster_images_scale=1).frozen()['raster_images_scale'], 1)
        for value in (0, -1, math.inf, math.nan, True):
            with self.assertRaises(ValueError): BackendOptions(raster_images_scale=value)

    def test_new_settings_resume_and_relocation(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve(); source=root/'originals'; source.mkdir(); (source/'a.txt').write_text('Accepted synthetic reading.\n')
            output=root/'bundle'; plan=build_folder_plan(source,output)
            old=c.convert_folder(plan)
            self.assertNotIn('raster_images_scale',old['specification']['options'])
            self.assertTrue(c.convert_folder(plan)['publication_complete'])
            with self.assertRaises(IntegrityError): c.convert_folder(plan,options=BackendOptions(raster_images_scale=1))
            output2=root/'new'; plan2=build_folder_plan(source,output2)
            new=c.convert_folder(plan2,options=BackendOptions(raster_images_scale=1))
            before=(output2/new['sources'][0]['item_relative']/'reading.md').read_bytes()
            c.convert_folder(plan2,options=BackendOptions(raster_images_scale=1))
            self.assertEqual(before,(output2/new['sources'][0]['item_relative']/'reading.md').read_bytes())
            with self.assertRaises(IntegrityError): c.convert_folder(plan2)
            shutil.move(output2,root/'moved'); self.assertTrue(verify_bundle(root/'moved')['valid'])

    def test_storage_keeps_original_pixels_and_profile_policy(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'camera.png'; Image.new('RGB',(3024,4032),'white').save(path)
            source=dict(kind='image',absolute_path=str(path),size=path.stat().st_size,sha256='synthetic')
            inventory={'units':[{'unit_id':1}], 'expected_count':1}
            old=c._storage_profile(source,inventory,BackendOptions())
            new=c._storage_profile(source,inventory,BackendOptions(raster_images_scale=1))
            self.assertGreater(old['units'][0]['bytes'],new['units'][0]['bytes']*8)
            self.assertGreaterEqual(new['units'][0]['bytes'],3024*4032*4)
            rows=render_units(path,'image',[1],Path(temp)/'render',images_scale=3)
            self.assertEqual((rows[0]['width'],rows[0]['height']),(3024,4032))
            # Landscape source becomes portrait after a quarter-turn; its
            # PNG row overhead still fits the independently checked budget.
            landscape=Path(temp)/'landscape.png'; Image.new('RGB',(4032,3024),'white').save(landscape)
            rotated_source={**source,'absolute_path':str(landscape),'size':landscape.stat().st_size}
            reserve=c._storage_profile(rotated_source,inventory,BackendOptions(raster_images_scale=1,rotate_clockwise=90))['units'][0]['bytes']
            self.assertGreaterEqual(reserve,c._pixel_bytes(3024,4032))


    @unittest.skipUnless(os.environ.get('AGENT_TRANSCRIBE_TEST_MODELS'),'local model path required')
    def test_real_pipeline_scales_pdf_independence_and_crop_geometry(self):
        from docling.datamodel.base_models import InputFormat
        options=BackendOptions(artifacts_path=os.environ['AGENT_TRANSCRIBE_TEST_MODELS'],raster_images_scale=1)
        backend=DoclingBackend(options); converter=backend._converter('image','default')
        pdf=converter.format_to_options[InputFormat.PDF].pipeline_options
        raster=converter.format_to_options[InputFormat.IMAGE].pipeline_options
        self.assertEqual(pdf.images_scale,3); self.assertEqual(raster.images_scale,1)
        self.assertEqual(pdf.ocr_options.model_dump(),raster.ocr_options.model_dump())
        self.assertIs(converter,backend._converter('pdf','default'))
        # Native crop implementation must receive matching page-scale geometry.
        from docling.backend.image_backend import _ImagePageBackend
        from docling_core.types.doc import BoundingBox,CoordOrigin
        from PIL import Image
        page=_ImagePageBackend(Image.new('RGB',(3024,4032),'white'),0,(72,72))
        self.assertEqual(page.get_page_image(scale=raster.images_scale).size,(3024,4032))
        crop=page.get_page_image(scale=raster.images_scale,cropbox=BoundingBox(l=100,t=200,r=500,b=600,coord_origin=CoordOrigin.TOPLEFT))
        self.assertEqual(crop.size,(400,400))
