"""
网易云音乐集成（兼容无 pyncm 环境）
为了应对 Python 3.11 无法安装 pyncm 库，此版本剥离了该依赖，
主程序依然可正常启动，仅网易云相关功能失效。
"""
import logging
import time

log = logging.getLogger(__name__)

def _ensure_login():
    log.info("未安装 pyncm，网易云音乐功能已禁用")

def _force_relogin():
    return

def reload_login():
    return

def search_songs(keyword: str, limit: int = 5) -> list[dict]:
    log.info("未安装 pyncm，无法搜索歌曲")
    return []

def get_song_detail(song_id: int) -> dict | None:
    return None

def get_audio_url(song_id: int) -> str | None:
    return None
