from chatroom import get_chatroom_names


def role_catalog() -> list[dict]:
    _, ai_name, companion_name = get_chatroom_names()
    return [
        {"id": "aion", "label": ai_name or "AI"},
        {"id": "connor", "label": companion_name or "第二AI"},
    ]
