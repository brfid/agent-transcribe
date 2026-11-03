"""Independent-review regressions; synthetic inputs, no archive payloads."""
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
from agent_transcribe import folder_intake as intake, docling_backend as backend, portable
from agent_transcribe.common import checksum


class ReviewRegressions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve())
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def test_utf32_be_intake_matches_backend(self):
        root = self.base / 'originals'; root.mkdir()
        source = root / 'text.txt'
        source.write_bytes(b'\x00\x00\xfe\xff' + 'VISIBLE'.encode('utf-32-be'))
        plan = intake.build_folder_plan(root, self.base/'bundle')
        prepared = intake.prepare_source(root, plan['sources'][0])
        self.assertEqual(prepared['kind'], 'txt')
        self.assertEqual(backend.inspect_source(source, 'txt')['expected_count'], 1)

    def test_empty_tiff_directory_is_unknown_not_complete(self):
        source = self.base/'empty.tiff'
        source.write_bytes(b'II'+struct.pack('<HI', 42, 0))
        inventory = backend.inspect_source(source, 'tiff')
        self.assertIsNone(inventory['expected_count'])
        self.assertTrue(inventory['count_warnings'])

    def text_bundle(self, reuse=False):
        root=self.base/'bundle'; original=root/'Originals'/'literal.txt';original.parent.mkdir(parents=True)
        literal='[literal source wording](no-such-asset.png)\n``` text inside original\n![source](missing.jpg)\n````\n'
        original.write_text(literal)
        source={'relative_path':'literal.txt','kind':'txt','order':1,'sha256':checksum(original),'size':original.stat().st_size}
        inventory=backend.inspect_source(original,'txt'); result=backend.DoclingBackend().convert_range(original,'txt',1,1)
        item='Items/000001-literal.txt'; part='parts/000001';directory=root/item/part
        manifest=portable.export_part(result,directory,source=source)
        retained=None
        if reuse:
            accepted=self.base/'accepted.txt';accepted.write_text(literal)
            retained={'text_path':str(accepted),'text_sha256':checksum(accepted),'source_sha256':source['sha256'],'gaps':['Unproofread source'],'group':'prior/Accepted folder','name':'Known descriptive group','notes_text':'Prior note [external relative reference](old/location.md)'}
        source_map=portable.write_item(root,item,source,inventory,[{'relative_path':part,'manifest':manifest}],reuse=retained)
        receipt={'kind':'docling-folder','schema':1,'state':'complete','publication_complete':True,'coverage_accounted':True,'conversion_complete':True,'sources':[{'item_relative':item,'source':source,'inventory':inventory,'map':source_map,'state':'complete'}]}
        receipt.update(portable.write_bundle_views(root,receipt));(root/'conversion.json').write_text(json.dumps(receipt))
        return root,literal

    def test_literal_markdown_shaped_transcription_is_not_required_link(self):
        root,_=self.text_bundle()
        result=portable.verify_bundle(root)
        self.assertEqual(result['exit_code'],0,result)

    def test_accepted_bytes_and_group_survive(self):
        root,literal=self.text_bundle(reuse=True)
        result=portable.verify_bundle(root)
        self.assertEqual(result['exit_code'],0,result)
        self.assertEqual((root/'Items/000001-literal.txt/accepted.txt').read_bytes(),literal.encode())
        self.assertIn('## Known descriptive group',(root/'README.md').read_text())

    def test_rotated_range_remaps_invisible_native_provenance(self):
        try:
            from PIL import Image
            from docling_core.types.doc import DoclingDocument,DocItemLabel,ContentLayer,ProvenanceItem,BoundingBox,Size,ImageRef
        except ImportError:
            self.skipTest('Pinned Docling environment required')
        doc=DoclingDocument(name='synthetic rotated range')
        units=[]
        for n in (1,2):
            doc.add_page(page_no=n,size=Size(width=50,height=30),image=ImageRef.from_pil(Image.new('RGB',(50,30),'white'),dpi=72))
            for layer in (ContentLayer.BODY,ContentLayer.INVISIBLE):
                doc.add_text(label=DocItemLabel.TEXT,text=f'layer {layer.value} page{n}',content_layer=layer,prov=ProvenanceItem(page_no=n,bbox=BoundingBox(l=1,t=1,r=20,b=15,coord_origin='TOPLEFT'),charspan=(0,5)))
            units.append({'unit_id':n,'doc_page_no':n,'text':f'page{n}','conversion_status':'ok','decode_status':'ok','recognition_warnings':[]})
        class FakeRaster:
            def convert_range(self,*args,**kwargs):return {'document':doc,'units':units,'status':'ok','errors':[],'provenance':{}}
        instance=backend.DoclingBackend(backend.BackendOptions(rotate_clockwise=90));instance._raster_backend=FakeRaster()
        def render(path,kind,ids,destination,**kwargs):
            destination.mkdir();rows=[]
            for n in ids:
                Image.new('RGB',(50,30),'white').save(destination/f'{n}.png')
                rows.append({'unit_id':n,'asset':f'{n}.png','decode_status':'ok','errors':[],'transform':{'rotate_clockwise':90}})
            return rows
        with patch.object(backend,'render_units',side_effect=render):
            result=instance._rotated_pdf(self.base/'original.pdf',3,4,'default',self.base)
        self.assertEqual({p.page_no for item,_ in doc.iterate_items(traverse_pictures=True,included_content_layers=set(ContentLayer)) for p in getattr(item,'prov',[])},{3,4})
        exported=portable.export_part(result,self.base/'export',source={'relative_path':'original.pdf','kind':'pdf','sha256':'0'*64})
        self.assertEqual({r['unit_id'] for r in exported['item_refs']},{3,4})

class RecoveryReviewRegressions(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve());self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name)

    def test_corrupt_jpeg_decode_is_content_gap_and_budget_finite(self):
        import test_conversion_recovery as fixture
        from agent_transcribe.recovery import recover_range
        path=self.base/'bad.jpg';path.write_bytes(b'\xff\xd8\xffCORRUPT JPEG BYTES')
        initial=fixture.result([fixture.unit(1)])
        result=recover_range(fixture.Backend(),path,'image',1,1,initial,scratch=self.base/'scratch')
        self.assertEqual(result['status'],'failed')
        self.assertLessEqual(len(result['recovery_attempts']),5)
        self.assertEqual(path.read_bytes(),b'\xff\xd8\xffCORRUPT JPEG BYTES')

    def test_corrupt_docx_media_is_raw_bytes_with_readable_salvage(self):
        import zipfile
        import test_conversion_recovery as fixture
        from agent_transcribe.recovery import recover_range
        path=self.base/'broken.docx'
        with zipfile.ZipFile(path,'w') as z:
            z.writestr('word/document.xml','<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:p><w:r><w:t>READABLE MAIN BODY</w:t></w:r></w:p></w:document>')
            z.writestr('word/media/image.png',b'CORRUPT IMAGE BYTES')
        result=recover_range(fixture.Backend(),path,'docx',1,1,fixture.result([fixture.unit('package:word/document.xml')]),scratch=self.base/'scratch')
        media=next(a for a in result['recovery_artifacts'] if a.get('package_member')=='word/media/image.png')
        self.assertTrue(media['path'].endswith('.bin'))
        self.assertEqual(media['media_decode_status'],'failed')
        self.assertEqual((self.base/'scratch'/media['path']).read_bytes(),b'CORRUPT IMAGE BYTES')
        self.assertIn('READABLE MAIN BODY',str(result['units']))

    def test_duplicate_docx_member_names_keep_occurrence_identity(self):
        import warnings,zipfile
        import test_conversion_recovery as fixture
        from agent_transcribe.recovery import recover_range
        path=self.base/'duplicates.docx'
        with warnings.catch_warnings():
            warnings.simplefilter('ignore',UserWarning)
            with zipfile.ZipFile(path,'w') as z:
                for word in ('FIRST','SECOND'):
                    z.writestr('word/document.xml','<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:p><w:r><w:t>'+word+'</w:t></w:r></w:p></w:document>')
        result=recover_range(fixture.Backend(),path,'docx',1,1,fixture.result([fixture.unit('package:word/document.xml')]),scratch=self.base/'scratch')
        self.assertEqual(result['status'],'partial')
        units=result['units'];self.assertEqual(len({r['unit_id'] for r in units}),len(units))
        self.assertIn('FIRST',str(units));self.assertIn('SECOND',str(units))


class MalformedImageVerification(unittest.TestCase):
    def test_broken_tiff_directory_is_source_gap_but_derivative_failure(self):
        from PIL import Image
        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as temp:
            root=Path(temp)/'bundle';original=root/'Originals'/'broken.tiff'
            original.parent.mkdir(parents=True)
            frame=Image.new('RGB',(10,10),'white')
            frame.save(original,save_all=True,append_images=[frame],compression='raw')
            inventory=backend.inspect_source(original,'tiff')
            last=inventory['units'][-1]['ifd_offset']
            with original.open('r+b') as stream:
                stream.seek(last);count=struct.unpack('<H',stream.read(2))[0]
                stream.seek(last+2+count*12);stream.write(struct.pack('<I',original.stat().st_size+99))
            inventory=backend.inspect_source(original,'tiff')
            self.assertIsNone(inventory['expected_count'])
            source={'relative_path':'broken.tiff','kind':'tiff','order':1,'sha256':checksum(original),'size':original.stat().st_size}
            item='Items/000001-broken.tiff';part='parts/000001'
            result={'document':None,'units':[{'unit_id':r['unit_id'],'conversion_status':'failed','decode_status':'failed','text':'','recognition_warnings':[]} for r in inventory['units']],'status':'failed','errors':['synthetic malformed TIFF']}
            manifest=portable.export_part(result,root/item/part,source=source)
            source_map=portable.write_item(root,item,source,inventory,[{'relative_path':part,'manifest':manifest}])
            receipt={'kind':'docling-folder','schema':1,'state':'partial','publication_complete':True,'coverage_accounted':False,'conversion_complete':False,'sources':[{'item_relative':item,'source':source,'inventory':inventory,'map':source_map,'state':'partial'}]}
            receipt.update(portable.write_bundle_views(root,receipt));(root/'conversion.json').write_text(json.dumps(receipt))
            report=portable.verify_bundle(root)
            self.assertTrue(report['valid'],report)
            self.assertEqual(report['exit_code'],2)
            self.assertTrue(any('Original image decode gap' in w for w in report['recognition_warnings']))
            # The same malformed bytes advertised as a generated image are fatal.
            (root/'broken-derivative.tiff').write_bytes(original.read_bytes())
            report=portable.verify_bundle(root)
            self.assertEqual(report['exit_code'],1)
            self.assertIn('Undecodable derivative image',str(report['errors']))


if __name__=='__main__':unittest.main()
