"""
EPUB 解析模块 — 书籍导入、章节拆分、段落标注、图片提取
"""

import hashlib, json, os, re, shutil, uuid
from pathlib import Path
from typing import List, Optional, Tuple

import ebooklib
from ebooklib import epub
from bs4 import BeautifulSoup, NavigableString, Tag, XMLParsedAsHTMLWarning
import warnings
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

from config import DATA_DIR

# ── 路径 ──────────────────────────────────────────
BOOKS_DIR = DATA_DIR / "books"
BOOKS_DIR.mkdir(exist_ok=True)

# 单段最大用于 AI 批注的字数
SEGMENT_MAX_CHARS = 6500  # 软上限；按全章均分，保留自然段


# ── 工具函数 ──────────────────────────────────────

def _safe_text(s: str) -> str:
    """去除多余空白，保留单个换行"""
    s = re.sub(r'[ \t]+', ' ', s)
    s = re.sub(r'\n{3,}', '\n\n', s)
    return s.strip()


def _hash_id(book_id: str, ch_idx: int, p_idx: int) -> str:
    """生成段落级唯一 ID"""
    raw = f"{book_id}:{ch_idx}:{p_idx}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ── EPUB 解析 ─────────────────────────────────────

class ParsedChapter:
    __slots__ = ('index', 'title', 'html_content', 'text_content',
                 'paragraphs', 'char_count', 'segments_meta')

    def __init__(self, index: int, title: str, html_content: str,
                 text_content: str, paragraphs: list, char_count: int,
                 segments_meta: list):
        self.index = index
        self.title = title
        self.html_content = html_content
        self.text_content = text_content
        self.paragraphs = paragraphs
        self.char_count = char_count
        self.segments_meta = segments_meta


class ParsedBook:
    __slots__ = ('book_id', 'title', 'author', 'cover_path', 'chapters')

    def __init__(self):
        self.book_id: str = str(uuid.uuid4())
        self.title: str = ""
        self.author: str = ""
        self.cover_path: Optional[str] = None
        self.chapters: List[ParsedChapter] = []


def parse_epub(epub_path: str) -> ParsedBook:
    """
    解析 EPUB 文件，返回结构化的书籍数据。
    - 图片提取到 data/books/{book_id}/images/
    - HTML 中图片路径重写为 /api/books/{book_id}/images/{filename}
    - 纯文本版本完全剥离图片
    """
    book_obj = epub.read_epub(epub_path, options={"ignore_ncx": True})
    result = ParsedBook()
    book_id = result.book_id

    # ── 书籍元数据 ──
    result.title = _get_metadata(book_obj, 'title') or Path(epub_path).stem
    result.author = _get_metadata(book_obj, 'creator') or "未知作者"

    # ── 创建图片目录 ──
    img_dir = BOOKS_DIR / book_id / "images"
    img_dir.mkdir(parents=True, exist_ok=True)

    # ── 提取所有图片到磁盘，建立 EPUB 内部路径 → 服务端文件名 映射 ──
    img_map = {}  # epub_internal_href -> saved_filename
    for item in book_obj.get_items():
        if item.get_type() == ebooklib.ITEM_IMAGE or \
           (item.media_type and item.media_type.startswith('image/')):
            # 保存图片
            fname = Path(item.get_name()).name  # e.g. image00057.jpeg
            # 避免文件名冲突
            if (img_dir / fname).exists():
                base, ext = os.path.splitext(fname)
                fname = f"{base}_{hashlib.md5(item.get_name().encode()).hexdigest()[:6]}{ext}"
            (img_dir / fname).write_bytes(item.get_content())
            # 记录映射（EPUB 中可能用相对路径引用）
            img_map[item.get_name()] = fname
            # 也映射不带目录前缀的文件名
            img_map[Path(item.get_name()).name] = fname

    # ── 提取封面 ──
    result.cover_path = _extract_cover(book_obj, img_map, book_id)

    # ── 按 spine 顺序提取章节 ──
    spine_ids = [s[0] for s in book_obj.spine]
    id_to_item = {item.get_id(): item for item in book_obj.get_items()}

    ch_idx = 0
    for spine_id in spine_ids:
        item = id_to_item.get(spine_id)
        if not item or item.get_type() != ebooklib.ITEM_DOCUMENT:
            continue

        raw_html = item.get_content().decode('utf-8', errors='replace')
        soup = BeautifulSoup(raw_html, 'lxml')
        body = soup.find('body')
        if not body:
            continue

        # 提取章节标题
        ch_title = _extract_chapter_title(body, ch_idx)

        # 提取段落（纯文本列表）+ 带图片的 HTML
        paragraphs, html_content = _extract_paragraphs(
            body, img_map, book_id, item.get_name()
        )

        # 跳过过短的页面（封面/版权页/空白页等）
        text_content = '\n'.join(paragraphs)
        if len(text_content) < 200:
            continue

        # 计算分段元数据
        segments_meta = _compute_segments(paragraphs)

        chapter = ParsedChapter(
            index=ch_idx,
            title=ch_title,
            html_content=html_content,
            text_content=text_content,
            paragraphs=paragraphs,
            char_count=len(text_content),
            segments_meta=segments_meta,
        )
        result.chapters.append(chapter)
        ch_idx += 1

    return result


def _get_metadata(book_obj, field: str) -> Optional[str]:
    """安全提取 EPUB 元数据"""
    try:
        values = book_obj.get_metadata('DC', field)
        if values:
            return values[0][0]
    except:
        pass
    return None


def _extract_cover(book_obj, img_map: dict, book_id: str) -> Optional[str]:
    """尝试提取封面图片路径"""
    # 方法1: OPF metadata 中的 cover
    cover_id = None
    try:
        meta = book_obj.get_metadata('OPF', 'cover')
        if meta:
            cover_id = meta[0][1].get('content', '')
    except:
        pass

    if cover_id:
        for item in book_obj.get_items():
            if item.get_id() == cover_id:
                fname = img_map.get(item.get_name()) or img_map.get(Path(item.get_name()).name)
                if fname:
                    return f"/api/books/{book_id}/images/{fname}"

    # 方法2: 找名字含 cover 的图片
    for epub_path_key, fname in img_map.items():
        if 'cover' in epub_path_key.lower():
            return f"/api/books/{book_id}/images/{fname}"

    return None


def _extract_chapter_title(body: Tag, ch_idx: int) -> str:
    """从 body 中提取章节标题"""
    for tag_name in ['h1', 'h2', 'h3']:
        h = body.find(tag_name)
        if h and h.get_text(strip=True):
            return h.get_text(strip=True)
    # fallback: 第一个有文字的 p 或 div
    for p in body.find_all(['p', 'div'], limit=3):
        txt = p.get_text(strip=True)
        if txt and len(txt) < 50:
            return txt
    return f"第 {ch_idx + 1} 章"


def _resolve_img_src(src: str, img_map: dict, book_id: str, item_name: str) -> Optional[str]:
    """将 EPUB 内部的 img src 解析为服务端 URL"""
    if not src:
        return None
    # 尝试直接匹配文件名
    basename = Path(src.split('?')[0].split('#')[0]).name
    fname = img_map.get(basename)
    if fname:
        return f"/api/books/{book_id}/images/{fname}"

    # 尝试相对路径解析
    try:
        item_dir = str(Path(item_name).parent)
        if item_dir == '.':
            resolved = src
        else:
            resolved = str(Path(item_dir) / src)
        # 标准化路径
        resolved = resolved.replace('\\', '/')
        if resolved.startswith('/'):
            resolved = resolved[1:]
        fname = img_map.get(resolved) or img_map.get(Path(resolved).name)
        if fname:
            return f"/api/books/{book_id}/images/{fname}"
    except:
        pass

    return None


def _extract_paragraphs(body: Tag, img_map: dict, book_id: str,
                         item_name: str) -> Tuple[List[str], str]:
    """
    从 body 提取：
    1. paragraphs: 纯文字段落列表（发给 AI 用）
    2. html_content: 保留图片的 HTML（阅读器用）

    策略：遍历 body 下所有 block 级元素，文字提取为段落，图片保留在 HTML 中。
    """
    paragraphs = []     # 纯文字段落
    html_parts = []     # 阅读器 HTML 片段

    # 收集所有顶级 block 元素
    blocks = body.find_all(['p', 'div', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
                            'blockquote', 'pre', 'figure', 'section'], recursive=False)
    if not blocks:
        # fallback: body 下所有 p
        blocks = body.find_all('p')
    if not blocks:
        # 最终 fallback: 把 body 当作一整块
        blocks = [body]

    for block in blocks:
        # 检查是否含有图片
        imgs = block.find_all('img')
        svgs = block.find_all('svg')

        # 提取纯文字
        text = block.get_text(separator=' ', strip=True)
        text = _safe_text(text)

        if text and len(text) > 5:
            p_idx = len(paragraphs)
            paragraphs.append(text)
            # HTML 版本：给段落打上 data-p 标记
            html_parts.append(f'<p data-p="{p_idx}">{_html_escape(text)}</p>')

        # 处理图片（保留在 HTML 中，不加入纯文本）
        for img in imgs:
            src = img.get('src', '') or img.get('data-src', '')
            new_src = _resolve_img_src(src, img_map, book_id, item_name)
            if new_src:
                alt = img.get('alt', '')
                html_parts.append(
                    f'<div class="book-img"><img src="{new_src}" alt="{_html_escape(alt)}" loading="lazy"></div>'
                )

    # 如果段落为零但有文字，做最终尝试
    if not paragraphs:
        full_text = body.get_text(separator='\n', strip=True)
        for line in full_text.split('\n'):
            line = _safe_text(line)
            if line and len(line) > 5:
                p_idx = len(paragraphs)
                paragraphs.append(line)
                html_parts.append(f'<p data-p="{p_idx}">{_html_escape(line)}</p>')

    html_content = '\n'.join(html_parts)
    return paragraphs, html_content


def _html_escape(s: str) -> str:
    """基础 HTML 转义"""
    return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')


# ── 按全章长度均衡分段 ──────────────────────────

def _compute_segments(paragraphs: List[str]) -> list:
    """先确定批次数，再取最接近均分位置的自然段边界；不产生零碎尾批。"""
    from bisect import bisect_left
    if not paragraphs:
        return []
    offsets = [0]
    for paragraph in paragraphs:
        offsets.append(offsets[-1] + len(paragraph))
    count = min(len(paragraphs), max(1, (offsets[-1] + SEGMENT_MAX_CHARS - 1) // SEGMENT_MAX_CHARS))
    segments, start = [], 0
    for part in range(1, count + 1):
        if part == count:
            end = len(paragraphs)
        else:
            target = offsets[-1] * part / count
            low, high = start + 1, len(paragraphs) - (count - part)
            right = min(high, bisect_left(offsets, target, low, high + 1))
            end = min((max(low, right - 1), right), key=lambda i: abs(offsets[i] - target))
        segments.append(dict(start_p=start, end_p=end - 1,
                             char_count=offsets[end] - offsets[start], status='not_sent'))
        start = end
    return segments


def build_annotate_text(paragraphs: List[str], start_p: int, end_p: int) -> str:
    """
    构建发送给 AI 批注的段落文本，每段带 【P{n}】 编号标记。
    """
    lines = []
    for i in range(start_p, end_p + 1):
        if i < len(paragraphs):
            lines.append(f"【P{i}】{paragraphs[i]}")
    return '\n\n'.join(lines)


def delete_book_files(book_id: str):
    """删除书籍的所有本地文件"""
    book_dir = BOOKS_DIR / book_id
    if book_dir.exists():
        shutil.rmtree(book_dir, ignore_errors=True)
