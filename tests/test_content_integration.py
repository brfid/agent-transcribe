"""Real offline converter-to-portable integration on only synthetic material."""
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from agent_transcribe import docling_backend as backend, portable
from agent_transcribe.common import checksum
ARTIFACTS=os.environ.get('AGENT_TRANSCRIBE_TEST_MODELS')

@unittest.skipUnless(ARTIFACTS,'Set AGENT_TRANSCRIBE_TEST_MODELS for real local model integration')
class RealPortableIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve());self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name);self.root=self.base/'bundle # café';self.root.mkdir()

    def publish(self,path,kind,options,ranges):
        source={'relative_path':path.name,'kind':kind,'order':1,'sha256':checksum(path),'size':path.stat().st_size}
        original=self.root/'Originals'/path.name;original.parent.mkdir();shutil.copy2(path,original)
        instance=backend.DoclingBackend(options);inventory=backend.inspect_source(path,kind);parts=[]
        item='Items/000001-source'
        for start,end in ranges:
            rel=f'parts/{start:06d}-{end:06d}';directory=self.root/item/rel;directory.mkdir(parents=True)
            rendered=backend.render_units(path,kind,range(start,end+1),directory/'rendered',rotate_clockwise=options.rotate_clockwise,images_scale=options.images_scale)
            for row in rendered:
                if row.get('asset'):row['asset']='rendered/'+row['asset']
            result=instance.convert_range(path,kind,start,end,scratch=self.base)
            self.assertEqual(result['status'],'ok',result['errors'])
            manifest=portable.export_part(result,directory,source=source,rendered=rendered)
            parts.append({'relative_path':rel,'manifest':manifest})
        mapped=portable.write_item(self.root,item,source,inventory,parts)
        receipt={'kind':'docling-folder','schema':1,'state':'complete','publication_complete':True,'coverage_accounted':True,'conversion_complete':True,'sources':[{'item_relative':item,'source':source,'inventory':inventory,'parts':parts,'map':mapped,'state':'complete'}]}
        receipt.update(portable.write_bundle_views(self.root,receipt));(self.root/'conversion.json').write_text(json.dumps(receipt))
        self.assertEqual(checksum(path),source['sha256'])
        moved=self.base/'moved 日本語';shutil.move(self.root,moved);path.unlink()
        verified=portable.verify_bundle(moved)
        self.assertEqual(verified['exit_code'],0,verified)
        return mapped,moved

    def test_real_mixed_pdf_nonfirst_part_native_tables_and_relocation(self):
        from PIL import Image,ImageDraw,ImageFont
        from reportlab.pdfgen.canvas import Canvas
        from reportlab.lib.utils import ImageReader
        source=self.base/'mixed # source.pdf';c=Canvas(str(source),pagesize=(612,792))
        c.setFont('Helvetica',18);c.drawString(40,740,'ORIGINAL NATIVE ALPHA')
        c.setFont('Helvetica',13)
        for n in range(8):c.drawString(40,700-n*18,'Synthetic archival layout with native text and explicit physical identity.')
        for y in (340,380,420,460):c.line(50,y,550,y)
        for x in (50,215,380,550):c.line(x,340,x,460)
        for row in range(3):
            for col in range(3):c.drawString(60+col*165,435-row*40,('ITEM','COUNT','STATUS')[col] if row==0 else f'VALUE {row} {col}')
        c.drawString(50,40,'Printed label X');c.showPage()
        raster=Image.new('RGB',(1224,1584),'white');draw=ImageDraw.Draw(raster);font=ImageFont.load_default(size=34)
        draw.text((80,100),'ORIGINAL SCANNED BETA',fill='black',font=font)
        for n in range(14):draw.text((80,180+n*65),'Readable synthetic scan words preserve physical page two.',fill='black',font=font)
        c.drawImage(ImageReader(raster),0,0,width=612,height=792);c.showPage()
        c.setFont('Helvetica',18);c.drawString(40,740,'ORIGINAL THIRD GAMMA')
        c.setFont('Helvetica',13);c.drawString(50,40,'Printed label X');c.showPage();c.save()
        mapped,moved=self.publish(source,'pdf',backend.BackendOptions(artifacts_path=ARTIFACTS),[(1,1),(2,3)])
        self.assertEqual([r['unit_id'] for r in mapped['units']],[1,2,3])
        for row,marker in zip(mapped['units'],['ALPHA','BETA','GAMMA']):self.assertIn(marker,row['text'])
        refs=[r for p in mapped['parts'] for r in p['manifest']['item_refs']]
        tables=[r for r in refs if r['label']=='table'];self.assertTrue(tables,'Real native table structure not detected')
        self.assertTrue(all(r['unit_id']==1 and r['crop'] for r in tables))
        second=mapped['parts'][1]['manifest'];self.assertEqual({r['unit_id'] for r in second['item_refs']},{2,3})

    def test_real_tiff_exif_then_rotation_all_frames_export(self):
        from PIL import Image,ImageDraw,ImageFont,TiffImagePlugin
        font=ImageFont.load_default(size=44);frames=[]
        for marker in ['FRAME ALPHA','FRAME BETA','FRAME GAMMA']:
            image=Image.new('RGB',(1000,500),'white');ImageDraw.Draw(image).text((70,100),marker,fill='black',font=font);frames.append(image)
        raw=[frames[0].rotate(90,expand=True),frames[1].rotate(180,expand=True),frames[2]]
        source=self.base/'transformed.tiff'
        with TiffImagePlugin.AppendingTiffWriter(str(source),True) as writer:
            for image,orientation in zip(raw,[1,6,8]):
                image.save(writer,format='TIFF',tiffinfo={274:orientation},dpi=(144,144));writer.newFrame()
        mapped,moved=self.publish(source,'tiff',backend.BackendOptions(artifacts_path=ARTIFACTS,rotate_clockwise=90),[(1,3)])
        for row,marker in zip(mapped['units'],['ALPHA','BETA','GAMMA']):
            self.assertIn(marker,row['text']);self.assertEqual(row['original_locator']['unit_id'],['ALPHA','BETA','GAMMA'].index(marker)+1)
            self.assertEqual((row['render']['width'],row['render']['height']),(1000,500))
            self.assertEqual(row['render']['transform']['rotate_clockwise'],90)

if __name__=='__main__':unittest.main()
