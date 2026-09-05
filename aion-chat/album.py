"""相册文件与记录：只新增原图，移出相册永远不删除本机文件。"""

import asyncio
import hashlib
import io
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import date
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from config import ALBUM_DIR

MAX_IMAGE_BYTES = 40 * 1024 * 1024
IMAGE_EXTENSIONS = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp", "GIF": "gif"}
ALBUM_IDS = ("family", "aion", "connor")


class AlbumStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.images_dir = self.root / "images"
        self.thumbnails_dir = self.root / "thumbnails"
        self.references_dir = self.root / "references"
        for directory in (self.images_dir, self.thumbnails_dir, self.references_dir):
            directory.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "album.sqlite3"
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE IF NOT EXISTS photos (
                id TEXT PRIMARY KEY, filename TEXT NOT NULL UNIQUE,
                source TEXT NOT NULL, actor TEXT NOT NULL DEFAULT '',
                kind TEXT NOT NULL DEFAULT '', prompt TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '', original_name TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '',
                taken_on TEXT NOT NULL, created_at REAL NOT NULL,
                width INTEGER NOT NULL, height INTEGER NOT NULL, size_bytes INTEGER NOT NULL,
                reference_filename TEXT NOT NULL DEFAULT '', favorite INTEGER NOT NULL DEFAULT 0,
                removed_at REAL
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS photos_timeline ON photos(removed_at, taken_on DESC, created_at DESC)")
            if "album_id" not in {row[1] for row in db.execute("PRAGMA table_info(photos)")}:
                db.execute("ALTER TABLE photos ADD COLUMN album_id TEXT NOT NULL DEFAULT 'family'")
                db.execute("""UPDATE photos SET album_id = CASE
                    WHEN source='upload' THEN 'family'
                    WHEN actor='connor' THEN 'connor' ELSE 'aion' END""")
            db.execute("CREATE INDEX IF NOT EXISTS photos_album ON photos(album_id, removed_at)")
            db.execute("""CREATE TABLE IF NOT EXISTS photo_views (
                actor TEXT NOT NULL, photo_id TEXT NOT NULL, viewed_at REAL NOT NULL,
                PRIMARY KEY(actor, photo_id))""")

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.db_path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _date(value):
        try:
            return date.fromisoformat(value).isoformat() if value else date.today().isoformat()
        except (ValueError, TypeError):
            raise ValueError("照片日期格式应为 YYYY-MM-DD")

    @staticmethod
    def _photo(row):
        photo = dict(row)
        photo["favorite"] = bool(photo["favorite"])
        photo["url"] = "/uploads/album/" + photo["filename"]
        photo["thumbnail_url"] = "/api/album/photos/" + photo["id"] + "/thumbnail"
        photo["reference_url"] = ("/api/album/photos/" + photo["id"] + "/reference") if photo["reference_filename"] else ""
        return photo

    def save_photo(self, data: bytes, *, source: str, actor: str = "", kind: str = "",
                   prompt: str = "", model: str = "", original_name: str = "",
                   taken_on: str = "", reference_bytes: bytes | None = None, album_id: str = "") -> dict:
        if source not in ("generated", "upload"):
            raise ValueError("未知照片来源")
        taken_on = self._date(taken_on)
        album_id = album_id or ("family" if source == "upload" else "connor" if actor == "connor" else "aion")
        if album_id not in ALBUM_IDS:
            raise ValueError("未知相册分类")
        if not data or len(data) > MAX_IMAGE_BYTES:
            raise ValueError("单张照片不能超过 40 MB，也不能为空")
        try:
            with Image.open(io.BytesIO(data)) as image:
                ext = IMAGE_EXTENSIONS.get(image.format)
                if not ext:
                    raise ValueError("支持 JPG、PNG、WebP 和 GIF 图片")
                if image.width * image.height > 40_000_000:
                    raise ValueError("照片像素过大，请缩小后上传")
                image.load()
                thumbnail = ImageOps.exif_transpose(image)
                width, height = thumbnail.size
                thumbnail.thumbnail((480, 480), Image.Resampling.LANCZOS)
                if thumbnail.mode not in ("RGB", "RGBA"):
                    thumbnail = thumbnail.convert("RGB")
                thumb_bytes = io.BytesIO()
                thumbnail.save(thumb_bytes, "WEBP", quality=78)
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
            raise ValueError("无法读取图片，请上传完整的 JPG、PNG、WebP 或 GIF")

        photo_id = uuid.uuid4().hex
        filename = photo_id + "." + ext
        # 原图保持字节不变，包括上传照片的 EXIF 信息；不经过压缩或覆盖。
        (self.images_dir / filename).write_bytes(data)
        (self.thumbnails_dir / (photo_id + ".webp")).write_bytes(thumb_bytes.getvalue())
        reference_filename = ""
        if reference_bytes:
            reference_filename = hashlib.sha256(reference_bytes).hexdigest() + ".jpg"
            reference_path = self.references_dir / reference_filename
            if not reference_path.exists():
                reference_path.write_bytes(reference_bytes)
        with self._connect() as db:
            db.execute("""INSERT INTO photos
                (id, filename, source, actor, kind, prompt, model, original_name, taken_on,
                 created_at, width, height, size_bytes, reference_filename, album_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (photo_id, filename, source, actor, kind, prompt, model, original_name,
                 taken_on, time.time(), width, height, len(data), reference_filename, album_id))
        return self.get_photo(photo_id)

    def get_photo(self, photo_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM photos WHERE id=? AND removed_at IS NULL", (photo_id,)).fetchone()
        return self._photo(row) if row else None

    def list_photos(self, *, offset: int = 0, limit: int = 60, source: str = "",
                    favorite: bool = False, query: str = "", album_id: str = "") -> dict:
        where, values = ["removed_at IS NULL"], []
        if album_id:
            if album_id not in ALBUM_IDS:
                raise ValueError("未知相册分类")
            where.append("album_id=?")
            values.append(album_id)
        if source:
            where.append("source=?")
            values.append(source)
        if favorite:
            where.append("favorite=1")
        if query.strip():
            where.append("(title LIKE ? OR note LIKE ? OR prompt LIKE ? OR original_name LIKE ?)")
            values.extend(["%" + query.strip() + "%"] * 4)
        condition = " AND ".join(where)
        with self._connect() as db:
            total = db.execute("SELECT COUNT(*) FROM photos WHERE " + condition, values).fetchone()[0]
            rows = db.execute("SELECT * FROM photos WHERE " + condition +
                              " ORDER BY taken_on DESC, created_at DESC, id DESC LIMIT ? OFFSET ?",
                              [*values, min(max(limit, 1), 100), max(offset, 0)]).fetchall()
        return {"photos": [self._photo(row) for row in rows], "total": total}

    def update_photo(self, photo_id: str, **changes) -> dict | None:
        allowed = {key: value for key, value in changes.items()
                   if key in ("title", "note", "taken_on", "favorite", "album_id") and value is not None}
        if "album_id" in allowed and allowed["album_id"] not in ALBUM_IDS:
            raise ValueError("未知相册分类")
        if "taken_on" in allowed:
            allowed["taken_on"] = self._date(allowed["taken_on"])
        if allowed:
            with self._connect() as db:
                db.execute("UPDATE photos SET " + ", ".join(key + "=?" for key in allowed) +
                           " WHERE id=? AND removed_at IS NULL", [*allowed.values(), photo_id])
        return self.get_photo(photo_id)

    def move_photos(self, photo_ids: list[str], album_id: str) -> int:
        if album_id not in ALBUM_IDS:
            raise ValueError("未知相册分类")
        # 一次事务只更新分类；原图、其他元数据和各角色的浏览记录不变。
        with self._connect() as db:
            result = db.executemany(
                "UPDATE photos SET album_id=? WHERE id=? AND removed_at IS NULL",
                [(album_id, photo_id) for photo_id in dict.fromkeys(photo_ids)])
            return result.rowcount

    def remove_photo(self, photo_id: str) -> bool:
        # 不执行 unlink，不扫描文件自动补回，不影响聊天和礼物引用的原图。
        with self._connect() as db:
            result = db.execute("UPDATE photos SET removed_at=? WHERE id=? AND removed_at IS NULL",
                                (time.time(), photo_id))
            return result.rowcount > 0

    def random_unseen_photos(self, actor: str, limit: int = 2) -> list[dict]:
        if actor not in ("aion", "connor"):
            raise ValueError("未知浏览角色")
        selected = []
        with self._connect() as db:
            rows = db.execute("""SELECT p.* FROM photos p
                WHERE p.album_id='family' AND p.removed_at IS NULL
                AND NOT EXISTS (SELECT 1 FROM photo_views v WHERE v.actor=? AND v.photo_id=p.id)
                ORDER BY RANDOM()""", (actor,))
            for row in rows:
                if not (self.images_dir / row["filename"]).is_file():
                    continue
                selected.append(self._photo(row))
                if len(selected) >= min(max(limit, 1), 2):
                    break
        return selected

    def has_unseen_photos(self, actor: str) -> bool:
        return bool(self.random_unseen_photos(actor, limit=1))

    def get_photo_views(self, photo_id: str) -> list[dict]:
        with self._connect() as db:
            rows = db.execute("SELECT actor, viewed_at FROM photo_views WHERE photo_id=? ORDER BY actor",
                              (photo_id,)).fetchall()
        return [dict(row) for row in rows]

    def mark_viewed(self, actor: str, photo_ids: list[str]) -> None:
        if actor not in ("aion", "connor"):
            raise ValueError("未知浏览角色")
        with self._connect() as db:
            db.executemany("""INSERT OR IGNORE INTO photo_views(actor, photo_id, viewed_at)
                SELECT ?, id, ? FROM photos WHERE id=? AND album_id='family' AND removed_at IS NULL""",
                [(actor, time.time(), photo_id) for photo_id in photo_ids])


@lru_cache(maxsize=1)
def get_album_store() -> AlbumStore:
    return AlbumStore(ALBUM_DIR)


async def save_generated_image(data: bytes, *, prompt: str, model: str, actor: str = "",
                               kind: str = "draw", reference_bytes: bytes | None = None) -> str:
    photo = await asyncio.to_thread(get_album_store().save_photo, data, source="generated",
                                   actor=actor or "aion", kind=kind, prompt=prompt, model=model,
                                   reference_bytes=reference_bytes)
    # 保留现有调用者 /uploads/{filename} 的契约，实际目录独立挂载。
    return "album/" + photo["filename"]
