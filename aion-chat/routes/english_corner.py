from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path as ApiPath, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, StringConstraints

from english_corner import (
    EnglishCornerNotFoundError,
    EnglishCornerValidationError,
    generate_learning_pack,
    get_context_options,
    get_utterance_audio,
    list_cards,
    retry_utterance_audio,
    set_card_status,
)


router = APIRouter(prefix="/api/english-corner", tags=["english-corner"])


def get_english_corner_db_path():
    return None


def get_english_corner_audio_dir():
    return None


ActorId = Literal["aion", "connor"]
CardStatus = Literal["learning", "learned"]
RequestId = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=128),
]
VoiceId = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=512),
]


class LearningPackRequest(BaseModel):
    actor: ActorId
    context_limit: int = Field(ge=0, le=10_000)
    request_id: RequestId
    tts_voice: VoiceId
    learning_day_end: float | None = Field(default=None, gt=0)


class CardStatusRequest(BaseModel):
    status: CardStatus


def serialize_public_english_corner_payload(value):
    """Strip internal persistence fields from every JSON response."""
    if isinstance(value, list):
        return [
            serialize_public_english_corner_payload(item)
            for item in value
        ]
    if not isinstance(value, dict):
        return value

    is_audio_record = (
        "utterance_id" in value
        and "status" in value
        and any(
            key in value
            for key in ("voice", "file_path", "error")
        )
    )
    if is_audio_record:
        audio = {
            "id": value["id"],
            "utterance_id": value["utterance_id"],
            "status": value["status"],
        }
        if value["status"] == "ready":
            audio["url"] = (
                f"/api/english-corner/audio/{value['utterance_id']}"
            )
        elif value["status"] == "failed":
            audio["message"] = (
                "Audio is unavailable; retry is available."
            )
            audio["retry_url"] = (
                f"/api/english-corner/audio/{value['utterance_id']}/retry"
            )
        return audio

    private_fields = {
        "model_key",
        "tts_voice",
        "voice",
        "file_path",
        "error",
    }
    return {
        key: serialize_public_english_corner_payload(item)
        for key, item in value.items()
        if key not in private_fields
    }


def _public_participant_display_data() -> list[dict]:
    from chatroom import get_chatroom_names

    user_name, aion_name, connor_name = get_chatroom_names()
    return [
        {
            "id": "user",
            "name": user_name or "User",
            "avatar_url": "/public/UserIcon.png",
        },
        {
            "id": "aion",
            "name": aion_name or "AI",
            "avatar_url": "/public/gropicon1.png",
        },
        {
            "id": "connor",
            "name": connor_name or "Second AI",
            "avatar_url": "/public/codexicon.png",
        },
    ]


@router.get("/overview")
async def get_overview_route(
    db_path=Depends(get_english_corner_db_path),
):
    learning = await list_cards(
        "learning",
        limit=0,
        offset=0,
        db_path=db_path,
    )
    learned = await list_cards(
        "learned",
        limit=0,
        offset=0,
        db_path=db_path,
    )
    participants = _public_participant_display_data()
    return {
        "counts": {
            "learning": learning["total"],
            "learned": learned["total"],
        },
        "participants": participants,
        "actors": [
            participant
            for participant in participants
            if participant["id"] in {"aion", "connor"}
        ],
    }


@router.get("/cards")
async def list_cards_route(
    status: CardStatus = Query(...),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0, le=1_000_000),
    db_path=Depends(get_english_corner_db_path),
):
    try:
        return serialize_public_english_corner_payload(
            await list_cards(
                status,
                limit=limit,
                offset=offset,
                db_path=db_path,
            )
        )
    except EnglishCornerValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/context-options")
async def get_context_options_route(actor: ActorId = Query(...)):
    try:
        return await get_context_options(actor)
    except EnglishCornerValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/packs")
async def generate_learning_pack_route(body: LearningPackRequest):
    try:
        return serialize_public_english_corner_payload(
            await generate_learning_pack(
                body.actor,
                body.context_limit,
                body.request_id,
                tts_voice=body.tts_voice,
                snapshot_end=body.learning_day_end,
            )
        )
    except EnglishCornerValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail="Unable to generate the English learning pack; please retry.",
        ) from exc


@router.patch("/cards/{card_id}/status")
async def set_card_status_route(
    body: CardStatusRequest,
    card_id: int = ApiPath(..., ge=1),
    db_path=Depends(get_english_corner_db_path),
):
    try:
        return serialize_public_english_corner_payload(
            await set_card_status(
                card_id,
                body.status,
                db_path=db_path,
            )
        )
    except EnglishCornerValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except EnglishCornerNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/audio/{utterance_id}/retry")
async def retry_utterance_audio_route(
    utterance_id: int,
    db_path=Depends(get_english_corner_db_path),
    audio_dir=Depends(get_english_corner_audio_dir),
):
    try:
        return serialize_public_english_corner_payload(
            await retry_utterance_audio(
                utterance_id,
                db_path=db_path,
                audio_dir=audio_dir,
            )
        )
    except EnglishCornerValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except EnglishCornerNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.head("/audio/{utterance_id}")
@router.get("/audio/{utterance_id}")
async def get_utterance_audio_route(
    utterance_id: int,
    db_path=Depends(get_english_corner_db_path),
):
    audio = await get_utterance_audio(utterance_id, db_path=db_path)
    if audio is None:
        raise HTTPException(
            status_code=404,
            detail=f"Audio not found for utterance {utterance_id}.",
        )
    if (
        audio["status"] != "ready"
        or not Path(audio["file_path"]).is_file()
    ):
        raise HTTPException(
            status_code=404,
            detail=serialize_public_english_corner_payload(audio),
        )
    return FileResponse(audio["file_path"], media_type="audio/mpeg")
