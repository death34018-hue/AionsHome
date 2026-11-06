"""Named portrait references shared by everyday chat and the theater."""
import io
import json
import logging
import re
from collections import Counter
from pathlib import Path

from PIL import Image

from config import IMAGE_ANCHORS_DIR, UPLOADS_DIR
from database import get_db


async def load_anchors(*, db_factory=None):
    # Chats and theater books refer to the same character IDs.
    async with (db_factory or get_db)() as db:
        cursor = await db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='theater_studio'")
        if not await cursor.fetchone():
            return []
        cursor = await db.execute("SELECT data FROM theater_studio WHERE kind='anchor' ORDER BY rowid")
        return [json.loads(row[0]) for row in await cursor.fetchall()]


def reference_path(url, root=None):
    if str(url).startswith('/image-anchors/'):
        root, relative = IMAGE_ANCHORS_DIR, url.removeprefix('/image-anchors/')
    elif str(url).startswith('/uploads/'):
        root, relative = root if root is not None else UPLOADS_DIR, url.removeprefix('/uploads/')
    else:
        raise ValueError('请先上传人物锚点照片')
    root = Path(root).resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or not path.is_file() or path.suffix.lower() not in ('.jpg', '.jpeg', '.png', '.webp'):
        raise ValueError('人物锚点照片失效，请重新上传')
    return path


def store_anchor_image(name, data):
    """Keep original bytes; an edited or same-named portrait never overwrites another."""
    try:
        with Image.open(io.BytesIO(data)) as photo:
            extension = {'JPEG': '.jpg', 'PNG': '.png', 'WEBP': '.webp'}.get(photo.format)
            if not extension:
                raise ValueError('人物锚点照片仅支持 JPG、PNG 或 WebP')
            photo.verify()
    except (OSError, SyntaxError, Image.DecompressionBombError) as exc:
        raise ValueError('人物锚点照片无法读取，请重新选择图片') from exc
    stem = re.sub(r'[^\w-]+', '_', name).strip('_')[:80] or 'portrait'
    if re.fullmatch(r'CON|PRN|AUX|NUL|COM[0-9¹²³]|LPT[0-9¹²³]', stem, re.I):
        stem = '_' + stem
    IMAGE_ANCHORS_DIR.mkdir(parents=True, exist_ok=True)
    index = 1
    while True:
        filename = (stem if index == 1 else f'{stem}-{index}') + extension
        path = IMAGE_ANCHORS_DIR / filename
        try:
            with path.open('xb') as target:
                target.write(data)
        except FileExistsError:
            if path.is_symlink() or not path.is_file() or path.read_bytes() != data:
                index += 1
                continue
        return '/image-anchors/' + filename


async def migrate_anchor_images():
    async with get_db() as db:
        if not await (await db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='theater_studio'")).fetchone():
            return
        rows = await (await db.execute(
            "SELECT id,data FROM theater_studio WHERE kind='anchor' AND json_extract(data,'$.image') LIKE '/uploads/%'"
        )).fetchall()
        for aid, raw in rows:
            anchor = json.loads(raw)
            try:
                url = store_anchor_image(anchor['name'], reference_path(anchor['image']).read_bytes())
            except (OSError, ValueError) as error:
                logging.getLogger(__name__).warning('未能迁移人物锚点 %s：%s', aid, error)
                continue
            await db.execute("UPDATE theater_studio SET data=json_set(data,'$.image',?) WHERE id=? AND data=?",
                             (url, aid, raw))
        await db.commit()


def identity_names():
    from chatroom import get_chatroom_names
    return dict(zip(('user', 'aion', 'connor'), get_chatroom_names()))


def anchor_identity(anchor, names):
    if anchor.get('identity'):
        return anchor['identity']
    labels = {name.strip().casefold() for name in
              [anchor['name'], *re.split(r'[,，;；]', anchor.get('aliases', ''))] if name.strip()}
    matches = [key for key, name in names.items() if name.casefold() in labels]
    return matches[0] if len(matches) == 1 else ''


def _character_keys(anchors):
    names = Counter(a['name'].strip().casefold() for a in anchors)
    reserved = {a['id'].casefold() for a in anchors} | {'none'}
    keys = {}
    for anchor in anchors:
        name = anchor['name'].strip()
        simple = (name and names[name.casefold()] == 1 and name.casefold() not in reserved
                  and not re.search(r'[,，|\[\]\r\n]', name))
        keys[anchor['id']] = name if simple else anchor['id']
    return keys


async def chat_image_prompt(who='aion'):
    from image_gen import supports_gif_generation
    intro = (
        '【聊天生图】应请求或聊天情境发图时，每次回复最多用一个生图指令，场景提示词用英文。'
        '注意生图提示词不要自行添加挑逗、性暗示或露骨的身体细节，会被审核不通过。可用隐晦描述提示词聚焦人物、服饰、构图、光线和动作，减少过于直白的用词。例如不要写“亲密场景”“性爱”“挑逗”等。参考用词方面，可以用“短裤”，但不可以用“内裤”。'
        'GIF 每次只安排 1—2 个简单动作，避免在短循环中堆叠多个连续事件。'
        '对方只说“发张自拍”时，默认只拍你自己，用 [SELFIE: English scene prompt]，系统按当前发言者选参考照。'
    )
    if supports_gif_generation():
        intro += (
            '\n只有对方明确要求 GIF、动图或动画时，才在生图指令内容最前面加 format=gif |；'
            '人物选择规则不变，英文场景写清动作和循环方式。'
            '例如 [DRAW: format=gif | A tiny lantern gently sways in a seamless loop]。'
            '自拍动图用 [SELFIE: format=gif | English scene and loop]，合拍则在 format=gif | 后加人物名单。'
            '动图默认 9 帧、每帧 200 毫秒，适合眨眼、挥手、发光等短循环；普通生图不要加这个参数。\n'
        )
    anchors = await load_anchors()
    if not anchors:
        return intro + '与你合拍的自拍也用 SELFIE，其他照片或绘画用 [DRAW: English scene prompt]。'
    names = identity_names()
    keys = _character_keys(anchors)
    roster = []
    for anchor in anchors:
        identity = anchor_identity(anchor, names)
        relation = '当前发言者（你）' if identity == who else ('聊天用户' if identity == 'user' else '另一位聊天伙伴' if identity else '已保存的人物')
        key = keys[anchor['id']]
        label = key if key == anchor['name'] else f"{key} = {anchor['name']}"
        roster.append(f"{label}（别名：{anchor.get('aliases') or '无'}；{relation}）")
    return (
        intro + '\n人物锚点：\n' + '\n'.join(roster) + '\n'
        '按说话人和上下文判断“我、你、我们”指谁，只列实际入镜者，使用名单上的名字（标了编号的用等号左边的编号）。\n'
        '合拍用 [SELFIE: characters=名字1,名字2 | English scene prompt]，其他照片或绘画用 [DRAW: characters=名字 | English scene prompt]。'
        '没有名单中的人入镜时用 [DRAW: characters=none | English scene prompt]。\n'
        '系统会附对应参考照，只固定外貌。英文场景中用人物原名写清各自的动作和位置，服饰、画风和环境按本次要求。'
    )


async def resolve_chat_references(prompt, is_selfie=False, source_identity=''):
    anchors = await load_anchors()
    names = identity_names()
    speaker = source_identity or 'aion'
    match = re.match(r'^\s*characters\s*=\s*([^|]*)\|\s*(.+)$', prompt, flags=re.I | re.S)
    selected = []
    if match:
        raw, prompt = match.groups()
        tokens = list(dict.fromkeys(token.strip() for token in re.split(r'[,，]', raw) if token.strip()))
        if tokens == ['none']:
            if is_selfie:
                raise ValueError('自拍需要明确出场人物')
            tokens = []
        elif not tokens:
            raise ValueError('请明确生图的出场人物')
        by_id = {a['id']: a for a in anchors}
        by_name = {key.casefold(): by_id[aid] for aid, key in _character_keys(anchors).items() if key != aid}
        for token in tokens:
            anchor = by_id.get(token) or by_name.get(token.casefold())
            if anchor is None:
                raise ValueError('生图人物锚点不存在或名字有歧义，请按名单选择人物')
            if anchor not in selected:
                selected.append(anchor)
    elif re.match(r'^\s*characters\s*=', prompt, re.I):
        raise ValueError('生图人物格式不完整，请重试')
    elif is_selfie:
        selected = [a for a in anchors if anchor_identity(a, names) == speaker]
        if len(selected) > 1:
            raise ValueError('多个锚点对应同一个聊天身份，请在人物锚点中调整')
    refs = [{'name': a['name'], 'path': str(reference_path(a.get('image', '')))} for a in selected]
    if selected:
        appearances = '\n'.join(f"{a['name']}：{a.get('appearance') or '以参考照片为准'}" for a in selected)
        prompt = f'人物固定外貌：\n{appearances}\n参考照片仅约束各自外貌；只画以下场景要求的出场人物，服饰、动作和环境按场景决定。\n{prompt}'
    return prompt, refs
