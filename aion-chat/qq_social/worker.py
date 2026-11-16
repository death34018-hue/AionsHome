"""Loopback-only management API and independent QQ process."""
from __future__ import annotations

import asyncio
import hmac
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException

from .service import Service
from .store import Store
from .transport import BotTransport
from qq_social_process import PORT

DIRECTORY = Path(__file__).resolve().parents[1] / 'data' / 'qq_social'


def create_app(directory=DIRECTORY):
    directory = Path(directory)
    store = Store(directory/'social.db')
    transports = {}
    service = Service(store, transports)
    token = (directory/'management.key').read_text(encoding='utf8').strip()
    bot_jobs, applied = {}, {}
    digest_job = None
    reload_lock = asyncio.Lock()
    reloading = False

    async def run_bot(actor, bot):
        nonlocal digest_job
        await bot.run()
        (directory/(actor+'.paused')).touch()
        service.cancel_pending(actor)
        await bot.close()
        if all((directory/(role+'.paused')).exists() for role in transports):
            (directory/'connection.paused').touch()
            service.cancel_pending()
            if digest_job:
                digest_job.cancel()

    async def reload_credentials(reconnect=False):
        nonlocal applied, reloading, digest_job
        async with reload_lock:
            credentials = json.loads((directory/'credentials.json').read_text(encoding='utf8'))
            changed = [actor for actor in ('aion','connor') if applied.get(actor)!=credentials.get(actor)
                       or (reconnect and actor in credentials and
                           (actor not in bot_jobs or bot_jobs[actor].done()
                            or getattr(transports.get(actor),'status',{}).get('retry_stopped')))]
            if not changed:
                return
            reloading = True
            try:
                # Stop pending sends before waiting for old WebSockets to close.
                service.cancel_pending()
                await service.drain()
                for actor in changed:
                    job = bot_jobs.pop(actor,None)
                    if job:
                        job.cancel()
                        await asyncio.gather(job,return_exceptions=True)
                    bot = transports.pop(actor,None)
                    if bot:
                        await bot.close()
                for actor in changed:
                    if actor not in credentials:
                        continue
                    if reconnect:
                        (directory/(actor+'.paused')).unlink(missing_ok=True)
                    async def event(kind,data,role=actor):
                        if not reloading:
                            await service.ingest(role,kind,data)
                    bot = transports[actor] = BotTransport(credentials[actor],event)
                    if (directory/'connection.paused').exists() or (directory/(actor+'.paused')).exists():
                        bot.status.update(state='已停止 · 手动连接', retry_stopped=True)
                    else:
                        bot_jobs[actor] = asyncio.create_task(run_bot(actor, bot))
                    service.last_error.pop(actor,None)
                applied = credentials
                if transports and all(getattr(bot,'status',{}).get('retry_stopped') for bot in transports.values()):
                    (directory/'connection.paused').touch()
                if any(not job.done() for job in bot_jobs.values()):
                    if digest_job is not None and digest_job.cancelling():
                        await asyncio.gather(digest_job, return_exceptions=True)
                    if digest_job is None or digest_job.done():
                        digest_job = asyncio.create_task(service.digest_groups())
            finally:
                reloading = False

    @asynccontextmanager
    async def lifespan(app):
        await reload_credentials()
        (directory/'worker.pid').write_text(str(os.getpid()),encoding='ascii')
        try:
            yield
        finally:
            jobs = [*bot_jobs.values(), *([digest_job] if digest_job else [])]
            for job in jobs:
                job.cancel()
            await service.close()
            await asyncio.gather(*jobs,return_exceptions=True)
            await asyncio.gather(*(bot.close() for bot in transports.values()))

    app = FastAPI(lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)

    @app.middleware('http')
    async def auth(request, call_next):
        from fastapi.responses import JSONResponse
        if not token or not hmac.compare_digest(request.headers.get('authorization',''),'Bearer '+token):
            return JSONResponse({'detail':'Unauthorized'},status_code=401)
        return await call_next(request)

    @app.post('/reload-credentials')
    async def reload_bots():
        await reload_credentials()
        return {'ok':True}

    @app.post('/reconnect')
    async def reconnect_bots():
        (directory/'connection.paused').unlink(missing_ok=True)
        await reload_credentials(reconnect=True)
        return {'ok':True}

    @app.get('/status')
    async def status():
        from .context import available_models, home_config
        cfg,models,_,names = home_config()
        actors = {}
        for actor in ('aion','connor'):
            bot = transports.get(actor)
            own = store.actor_settings(actor)
            home_model = cfg.get(actor+'_model','')
            actors[actor] = dict(name=names[actor],model=own['model'] or home_model,home_model=home_model,settings=own,
                **(bot.status if bot else {'connected':False,'state':'未配置'}),
                last_event=service.last_event.get(actor),last_send=service.last_send.get(actor),
                reply_error=service.last_error.get(actor,''),context=service.diagnostics.get(actor,{}))
        return {'settings':store.settings(),'actors':actors,'models':available_models(models),'groups':store.groups(),'pending':len(service.tasks),'pid':os.getpid()}

    @app.patch('/settings')
    async def settings(request: Request):
        try:
            return service.configure(await request.json())
        except (ValueError,TypeError):
            raise HTTPException(400,'设置值无效')

    @app.patch('/actors/{actor}/settings')
    async def actor_settings(actor:str,request:Request):
        try:
            patch = await request.json()
            if isinstance(patch,dict) and 'model' in patch:
                from .context import available_models, home_config
                model = patch['model']
                if not isinstance(model,str) or (model.strip() and model.strip() not in available_models(home_config()[1])):
                    raise HTTPException(400,'请选择可用于 QQ 群聊的现有模型')
            return service.configure(patch,actor=actor)
        except (ValueError,TypeError):
            raise HTTPException(400,'角色设置值无效')

    @app.patch('/groups/{row_id}')
    async def update_group(row_id:int,request:Request):
        try:
            store.update_group(row_id,await request.json())
            service.cancel_pending()
        except (ValueError,TypeError):
            raise HTTPException(400,'群设置无效')
        return {'ok':True}

    def find_group(row_id):
        for g in store.groups():
            if g['id']==row_id:
                return g
        raise HTTPException(404,'群不存在')

    @app.get('/groups/{row_id}')
    async def group_detail(row_id:int):
        g = find_group(row_id)
        key = (g['actor'],g['group_id'])
        return {'group':g,'messages':store.history(*key,100),'members':store.members(*key),'memories':store.memories(*key)}

    @app.patch('/groups/{row_id}/members/{member_id}')
    async def member(row_id:int,member_id:str,request:Request):
        g = find_group(row_id)
        body = await request.json()
        store.set_member(g['actor'],g['group_id'],member_id,body.get('alias',''),body.get('is_owner') is True)
        return {'ok':True}

    @app.delete('/memories/{memory_id}')
    async def delete_memory(memory_id:int):
        store.delete_memory(memory_id)
        return {'ok':True}

    return app


if __name__ == '__main__':
    import sys
    import uvicorn
    sys.stdout.reconfigure(encoding='utf8',errors='replace')
    sys.stderr.reconfigure(encoding='utf8',errors='replace')
    uvicorn.run(create_app(),host='127.0.0.1',port=PORT,access_log=False,log_level='warning')
