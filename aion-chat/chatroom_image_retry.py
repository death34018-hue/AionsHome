"""Freeze a failed group photo's inputs independently of the editable character anchors."""
import hashlib
import io
import json
import tempfile
import zipfile
from pathlib import Path

from config import SETTINGS


def marker(attempt=0, running=False):
    return {'type': 'chatroom_image_retry', 'attempt': attempt, 'running': running}


async def init_storage(db):
    await db.execute("""CREATE TABLE IF NOT EXISTS chatroom_image_retries (
        message_id TEXT PRIMARY KEY, snapshot BLOB NOT NULL)""")
    await db.execute("""CREATE TRIGGER IF NOT EXISTS delete_chatroom_image_retry
        AFTER DELETE ON chatroom_messages BEGIN
            DELETE FROM chatroom_image_retries WHERE message_id=OLD.id;
        END""")
    # Workers do not survive a restart. Restore the button without resubmitting.
    await db.execute("""UPDATE chatroom_messages SET content='上次重试已中断',
        attachments=json_set(attachments,'$[0].running',json('false'))
        WHERE id IN (SELECT message_id FROM chatroom_image_retries)
        AND json_extract(attachments,'$[0].running')=1""")


def _provider():
    if SETTINGS.get('image_gen_provider') == 'codex':
        return 'codex'
    if SETTINGS.get('image_gen_provider') == 'flowai':
        from image_gen import image_model_label
        return image_model_label()
    config = [SETTINGS.get(k) or '' for k in ('image_gen_base_url', 'image_gen_model')]
    return hashlib.sha256(json.dumps(config).encode()).hexdigest()


def _pack_snapshot(request, paths):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as archive:
        for entry, path in zip(request['files'], paths, strict=True):
            archive.writestr(entry['file'], Path(path).read_bytes())
        archive.writestr('request.json', json.dumps(request, ensure_ascii=False))
    return buf.getvalue()


def read_prompt(payload):
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        return json.loads(archive.read('request.json'))['prompt']


def replace_prompt(payload, prompt):
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        request = json.loads(archive.read('request.json'))
        if request['prompt'] == prompt:
            return payload
        request['prompt'] = prompt
        buf = io.BytesIO()
        # Rebuild the archive so only the new prompt remains. A previous Flow AI
        # ticket describes the old prompt and must not be resumed after editing.
        with zipfile.ZipFile(buf, 'w') as updated:
            for entry in archive.infolist():
                if entry.filename != 'request.json':
                    updated.writestr(entry, archive.read(entry))
            updated.writestr('request.json', json.dumps(request, ensure_ascii=False))
    return buf.getvalue()


async def snapshot(prompt, refs, is_selfie, sender, *, output_format=None):
    from image_gen import _selfie_reference_path
    legacy = is_selfie and not refs
    if legacy:
        path = _selfie_reference_path(sender)
        refs = [{'name': '', 'path': str(path)}] if path.is_file() else []
    paths = [r['path'] for r in refs]
    provider = _provider()
    with tempfile.TemporaryDirectory() as tmp:
        if (provider == 'codex' or provider.startswith('flowai/')) and paths and not legacy:
            from codex_image_gen import _prepare_reference_paths
            paths = await _prepare_reference_paths(paths, tmp)
        files = [{'name': ref['name'], 'file': f'{index}{Path(path).suffix}'}
                 for index, (ref, path) in enumerate(zip(refs, paths, strict=True))]
        request = dict(prompt=prompt, files=files, is_selfie=is_selfie,
                       sender=sender, legacy=legacy, provider=provider)
        if output_format:
            request['output_format'] = output_format
        return _pack_snapshot(request, paths)


async def generate(payload, *, on_snapshot=None):
    from image_gen import generate_image, _retry_selfie_reference
    with tempfile.TemporaryDirectory(prefix='aionshome-retry-') as tmp, zipfile.ZipFile(io.BytesIO(payload)) as archive:
        req = json.loads(archive.read('request.json'))
        refs = []
        for entry in req['files']:
            path = Path(tmp) / Path(entry['file']).name
            path.write_bytes(archive.read(entry['file']))
            refs.append({'name': entry['name'], 'path': str(path)})
        provider = _provider()
        job_id = archive.comment.decode('utf-8') or None
        if provider != req['provider']:
            # A remote ticket belongs to the model that submitted it.
            job_id = None
            if on_snapshot:
                req['provider'] = provider
                payload = _pack_snapshot(req, [ref['path'] for ref in refs])
                await on_snapshot(payload)
        kwargs = dict(is_selfie=req['is_selfie'], source_identity=req['sender'], raise_errors=True)
        if req.get('output_format'):
            kwargs['output_format'] = req['output_format']
        if provider.startswith('flowai/'):
            async def remember_job(job_id):
                if on_snapshot:
                    # The ZIP comment holds the current remote ticket, independently
                    # of the frozen prompt and reference files.
                    updated = io.BytesIO(payload)
                    with zipfile.ZipFile(updated, 'a') as copy:
                        copy.comment = (job_id or '').encode('utf-8')
                    await on_snapshot(updated.getvalue())
            kwargs.update(flow_model=provider.split('/', 1)[1], flow_job_id=job_id, on_flow_job=remember_job)
        if refs and not req['legacy']:
            kwargs['references'] = refs
        fallback = Path(refs[0]['path']) if refs else Path(tmp)/'missing.jpg'
        token = _retry_selfie_reference.set(fallback if req['legacy'] else None)
        try:
            return await generate_image(req['prompt'], **kwargs)
        finally:
            _retry_selfie_reference.reset(token)
