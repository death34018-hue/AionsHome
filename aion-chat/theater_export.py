"""Portable novel exports; never include planning, prompts, annotations or chat history."""
import io
import re
from pathlib import Path
from urllib.parse import unquote

from PIL import Image, ImageOps


class ExportError(ValueError):
    pass


def filename(title, format):
    title = re.sub(r'[\\/:*?"<>|\x00-\x1f\x7f]', '_', title).strip(' .')[:100] or '小剧场小说'
    if title.upper() in {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}:
        title = '_'+title
    return f'{title}.{format}'


def heading(chapter):
    title = chapter.get('title', '').strip()
    if not re.match(r'^第[^\s]{1,16}[章回节]', title):
        title = f'第{chapter["number"]}章' + (' '+title if title else '')
    return title + ('（未完成）' if chapter['status'] != 'ready' else '')


def local_image(url, roots):
    for prefix, root in sorted(roots.items(), key=lambda item: -len(item[0])):
        if url.startswith(prefix):
            root = Path(root).resolve()
            relative = unquote(url[len(prefix):])
            if '\\' in relative or ':' in relative:
                break
            path = (root/relative).resolve()
            if path.is_relative_to(root) and path.is_file():
                return path
            break
    raise ExportError('插画文件无法读取，请恢复图片，或选择 TXT 纯文字导出')


def image_bytes(url, roots):
    path = local_image(url, roots)
    try:
        with Image.open(path) as source:
            image = ImageOps.exif_transpose(source)
            image.thumbnail((2000, 2000))
            rgba = image.convert('RGBA')
            rgb = Image.new('RGB', rgba.size, 'white')
            rgb.paste(rgba, mask=rgba.getchannel('A'))
            stream = io.BytesIO()
            rgb.save(stream, format='JPEG', quality=90)
            stream.seek(0)
            return stream, rgb.width, rgb.height
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise ExportError('插画文件损坏或格式无法读取，请恢复图片，或选择 TXT 纯文字导出') from exc


def build_export(title, chapters, format, include_drafts, roots):
    if format not in ('txt', 'docx'):
        raise ExportError('请选择 Word 或 TXT 格式')
    chapters = sorted((c for c in chapters if c.get('content', '').strip()
                       and (include_drafts or c.get('status') == 'ready')), key=lambda c: c['number'])
    if not chapters:
        raise ExportError('还没有可导出的正文' if include_drafts else '还没有已完成章节，可先确认完成，或选择包含未完成正文')
    title = title.strip() or '小剧场小说'
    if format == 'txt':
        parts = [title]
        for chapter in chapters:
            parts.extend([heading(chapter), chapter['content'].strip('\r\n')])
        return ('\n\n'.join(parts)+'\n').encode('utf-8-sig')

    # Imported only for Word export, so plain-text download remains independent.
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Inches, Pt, RGBColor

    doc = Document()
    doc.core_properties.title = title
    doc.core_properties.author = ''
    doc.core_properties.last_modified_by = ''
    section = doc.sections[0]
    section.page_width, section.page_height = Inches(8.5), Inches(11)
    section.top_margin = section.bottom_margin = Inches(.85)
    section.left_margin = section.right_margin = Inches(1)
    for name, size in [('Normal', 11.5), ('Title', 26), ('Heading 1', 17)]:
        style = doc.styles[name]
        style.font.name = 'Georgia'
        style.font.size = Pt(size)
        style.font.color.rgb = RGBColor(0, 0, 0)
        style.element.get_or_add_rPr().rFonts.set(qn('w:eastAsia'), '宋体')
    normal = doc.styles['Normal'].paragraph_format
    normal.line_spacing = 1.5
    normal.space_after = Pt(6)
    normal.first_line_indent = Pt(23)
    normal.widow_control = True
    title_p = doc.add_paragraph(title, 'Title')
    title_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    title_p.paragraph_format.first_line_indent = Pt(0)
    title_p.paragraph_format.space_after = Pt(24)

    footer = section.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    footer.paragraph_format.first_line_indent = Pt(0)
    field = OxmlElement('w:fldSimple')
    field.set(qn('w:instr'), 'PAGE')
    footer._p.append(field)

    for index, chapter in enumerate(chapters):
        title_p = doc.add_paragraph(heading(chapter), 'Heading 1')
        title_p.paragraph_format.first_line_indent = Pt(0)
        title_p.paragraph_format.page_break_before = index > 0
        images = sorted((im for im in chapter.get('images', []) if im.get('status') == 'ready'),
                        key=lambda im: im.get('after', 0))
        lines = [m for m in re.finditer(r'[^\r\n]+', chapter['content']) if m.group().strip()]
        image_index = 0
        for line_index, line in enumerate(lines):
            doc.add_paragraph(line.group().strip())
            # An anchor in the blank separator belongs after the preceding paragraph.
            boundary = lines[line_index+1].start() if line_index+1 < len(lines) else len(chapter['content'])
            while image_index < len(images) and (images[image_index].get('after', 0) <= boundary or line_index == len(lines)-1):
                try:
                    stream, width, height = image_bytes(images[image_index].get('url', ''), roots)
                except ExportError as exc:
                    raise ExportError(f'{heading(chapter)}：{exc}') from exc
                scale = min(6.5/width, 7.4/height)
                picture = doc.add_paragraph()
                picture.alignment = WD_ALIGN_PARAGRAPH.CENTER
                picture.paragraph_format.first_line_indent = Pt(0)
                picture.paragraph_format.space_before = Pt(8)
                picture.paragraph_format.space_after = Pt(12)
                picture.add_run().add_picture(stream, width=Inches(width*scale), height=Inches(height*scale))
                image_index += 1
    output = io.BytesIO()
    doc.save(output)
    return output.getvalue()
