"""Small, disposable transfer probes. No files or chat messages are stored."""
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

router = APIRouter(prefix='/api/network-check')


@router.get('/ping')
def ping():
    return JSONResponse({'ok': True}, headers={'Cache-Control': 'no-store'})


@router.post('/upload')
async def upload(request: Request):
    count = 0
    async for chunk in request.stream():
        count += len(chunk)
        if count > 512 * 1024:
            raise HTTPException(413, 'Probe is limited to 512 KiB')
    return JSONResponse({'bytes': count}, headers={'Cache-Control': 'no-store'})
