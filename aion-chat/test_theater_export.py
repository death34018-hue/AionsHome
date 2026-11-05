"""Small offline checks for the portable novel export, using invented prose and images."""
import io
import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile
from xml.etree import ElementTree as ET

from PIL import Image
import theater_export as export


class NovelExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        Image.new('RGB', (240, 360), 'lightblue').save(self.root/'scene.webp')
        self.chapters = [
            dict(id='two', number=2, title='星光', status='draft', content='尚未完成的故事', images=[]),
            dict(id='one', number=1, title='初遇', status='ready', content='月光落在窗前。\n\n我们一起出门。',
                 images=[dict(status='ready', after=8, url='/uploads/album/scene.webp', prompt='不应出现在导出里的生图提示词')]),
        ]
        self.roots = {'/uploads/album/':self.root, '/uploads/':self.root/'other'}

    def test_txt_defaults_to_finished_prose_and_explicitly_includes_drafts(self):
        data = export.build_export('月下故事', self.chapters, 'txt', False, self.roots)
        text = data.decode('utf-8-sig')
        self.assertIn('第1章 初遇', text)
        self.assertIn('月光落在窗前。\n\n我们一起出门。', text)
        self.assertNotIn('尚未完成', text)
        self.assertNotIn('生图提示词', text)
        all_text = export.build_export('月下故事', self.chapters, 'txt', True, self.roots).decode('utf-8-sig')
        self.assertLess(all_text.index('初遇'), all_text.index('星光'))
        self.assertIn('未完成', all_text)

    def test_docx_embeds_webp_as_portable_image_between_paragraphs(self):
        data = export.build_export('月下故事', self.chapters, 'docx', False, self.roots)
        with ZipFile(io.BytesIO(data)) as archive:
            self.assertEqual(len([n for n in archive.namelist() if n.startswith('word/media/')]), 1)
            xml = archive.read('word/document.xml')
            root = ET.fromstring(xml)
            ns = {'w':'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
            blocks = root.findall('w:body/w:p', ns)
            content = [(''.join(p.itertext()), bool(p.findall('.//w:drawing', ns))) for p in blocks]
            first = next(i for i,(s,_) in enumerate(content) if '月光落在窗前' in s)
            image = next(i for i,(_,pic) in enumerate(content) if pic)
            second = next(i for i,(s,_) in enumerate(content) if '我们一起出门' in s)
            self.assertLess(first, image)
            self.assertLess(image, second)
            self.assertNotIn('生图提示词', xml.decode())
            self.assertNotIn('TargetMode="External"', archive.read('word/_rels/document.xml.rels').decode())

    def test_missing_or_outside_image_is_reported_and_txt_remains_available(self):
        for url in ['/uploads/album/missing.png', '/uploads/album/../outside.png', 'https://example.com/image.png']:
            self.chapters[1]['images'][0]['url'] = url
            with self.assertRaisesRegex(export.ExportError, '插画'):
                export.build_export('故事', self.chapters, 'docx', False, self.roots)
            self.assertTrue(export.build_export('故事', self.chapters, 'txt', False, self.roots))
        with self.assertRaisesRegex(export.ExportError, '完成'):
            export.build_export('故事', [self.chapters[0]], 'txt', False, self.roots)


class NovelExportRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from test_theater_studio import StudioTests
        await StudioTests.asyncSetUp(self)

    async def asyncTearDown(self):
        from test_theater_studio import StudioTests
        await StudioTests.asyncTearDown(self)

    async def test_route_returns_a_download_without_mutating_the_story(self):
        import theater_studio as studio
        from urllib.parse import unquote
        await studio.change('chapter', content='可分享的正文', status='ready')
        before = await studio.rows('book')
        response = await studio.export_novel('book', 'txt', False)
        self.assertEqual(response.status_code, 200)
        self.assertIn('可分享的正文', response.body.decode('utf-8-sig'))
        self.assertIn('attachment;', response.headers['content-disposition'])
        self.assertIn('Test.txt', unquote(response.headers['content-disposition']))
        self.assertEqual(await studio.rows('book'), before)
        word = await studio.export_novel('book', 'docx', False)
        self.assertTrue(word.body.startswith(b'PK'))


if __name__ == '__main__':
    unittest.main()
