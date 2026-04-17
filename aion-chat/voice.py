"""
语音唤醒 + 半双工通话模块
- sounddevice 录音
- WebRTC VAD 语音检测（频谱分析，不靠音量阈值）
- 硅基流动 ASR 识别
- 通过内部 API 发送消息到聊天
- WebSocket 广播通话状态
"""

import io, wave, time, threading, asyncio, re
import numpy as np
import sounddevice as sd
import httpx
import webrtcvad

_EMOJI_RE = re.compile(
    "[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF"
    "\U0001F1E0-\U0001F1FF\U00002702-\U000027B0\U000024C2-\U0001F251"
    "\U0001F900-\U0001F9FF\U0001FA00-\U0001FA6F\U0001FA70-\U0001FAFF"
    "\U00002600-\U000026FF\U0000FE00-\U0000FE0F\U0000200D]+"
)

from config import get_key

# ─── ASR 配置 ──────────────────────────────────────
ASR_URL = "https://api.siliconflow.cn/v1/audio/transcriptions"
ASR_MODEL = "FunAudioLLM/SenseVoiceSmall"

SAMPLE_RATE = 16000
CHANNELS = 1
# WebRTC VAD 要求帧长为 10/20/30ms，16kHz × 30ms = 480 samples
VAD_FRAME_SIZE = 480       # 30ms @ 16kHz
VAD_FRAME_BYTES = VAD_FRAME_SIZE * 2  # int16 = 2 bytes

# VAD 参数
VAD_MODE = 2                   # 0=最宽松 ~ 3=最严格，2 适合中等环境
MIN_SPEECH_FRAMES = 8          # 连续 8 帧(240ms)以上才算说话
MAX_SILENCE_FRAMES = 27        # ~0.8秒静音截断唤醒词 (27×30ms)
CMD_SILENCE_SECS = 1.5         # 说话后1.5秒静音 → 一句话结束
MIN_RECORD_SECS = 0.5

HANGUP_KEYWORDS = ["再见", "拜拜", "挂断", "结束通话", "挂了"]


class VoiceWakeup:
    """语音唤醒 + 半双工通话管理器"""

    def __init__(self):
        self.enabled = False
        self.wake_word = "老公"
        self.in_call = False
        self.ai_speaking = False

        self._vad = webrtcvad.Vad(VAD_MODE)
        self._loop: asyncio.AbstractEventLoop = None
        self._ws_manager = None
        self._stop_evt = threading.Event()
        self._thread: threading.Thread = None
        self._stream = None

    def set_event_loop(self, loop):
        self._loop = loop

    def set_ws_manager(self, mgr):
        self._ws_manager = mgr

    # ── 外部控制 ──────────────────────────────────

    def start(self, wake_word: str = "老公"):
        """开启语音监听"""
        if self._thread and self._thread.is_alive():
            return
        self.wake_word = wake_word
        self.enabled = True
        self.in_call = False
        self.ai_speaking = False
        self._stop_evt.clear()
        self._thread = threading.Thread(target=self._main_loop, daemon=True)
        self._thread.start()
        self._broadcast_state("voice_state", {"enabled": True, "status": "calibrating"})

    def stop(self):
        """关闭语音监听，全部状态复位"""
        self.enabled = False
        self.in_call = False
        self.ai_speaking = False
        self._stop_evt.set()
        self._broadcast_state("voice_state", {"enabled": False, "status": "off"})

    def notify_ai_speaking(self, speaking: bool):
        """前端通知: AI 开始/结束说话 (TTS 播放状态)"""
        self.ai_speaking = speaking
        if not speaking and self.in_call:
            # AI 说完了，用户可以说话了
            self._broadcast_state("voice_state", {
                "enabled": True, "status": "listening_cmd",
                "message": "轮到你说话了..."
            })

    def notify_cam_check_start(self):
        """AI 触发了 CAM_CHECK，保持 AI 说话状态"""
        self.ai_speaking = True

    # ── 广播工具 ──────────────────────────────────

    def _broadcast_state(self, msg_type: str, data: dict):
        if self._loop and self._ws_manager:
            asyncio.run_coroutine_threadsafe(
                self._ws_manager.broadcast({"type": msg_type, "data": data}),
                self._loop
            )

    # ── 音频工具 ──────────────────────────────────

    @staticmethod
    def _to_wav(audio):
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(CHANNELS)
            wf.setsampwidth(2)
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(audio.tobytes())
        buf.seek(0)
        return buf.read()

    def _asr(self, audio) -> str:
        """调硅基流动 ASR"""
        key = get_key("siliconflow")
        if not key:
            return ""
        wav = self._to_wav(audio)
        try:
            resp = httpx.post(
                ASR_URL,
                headers={"Authorization": f"Bearer {key}"},
                files={"file": ("s.wav", wav, "audio/wav")},
                data={"model": ASR_MODEL, "language": "zh"},
                timeout=15,
            )
            resp.raise_for_status()
            text = resp.json().get("text", "").strip()
            return _EMOJI_RE.sub("", text).strip()
        except Exception as e:
            print(f"[Voice ASR Error] {e}")
            return ""

    @staticmethod
    def _flush(stream):
        """清空音频缓冲区"""
        avail = stream.read_available
        if avail > 0:
            stream.read(avail)

    def _is_speech(self, frame_bytes: bytes) -> bool:
        """WebRTC VAD 判断是否是人声"""
        try:
            return self._vad.is_speech(frame_bytes, SAMPLE_RATE)
        except Exception:
            return False

    def _record(self, stream, silence_frames, timeout_sec=15) -> np.ndarray | None:
        """使用 WebRTC VAD 录制一段语音"""
        self._flush(stream)
        frames = []
        speech_n = 0
        silence_n = 0
        recording = False
        wait_limit = int(SAMPLE_RATE / VAD_FRAME_SIZE * timeout_sec)
        wait_n = 0

        while not self._stop_evt.is_set():
            # AI 在说话时持续读取但丢弃
            if self.ai_speaking:
                try:
                    stream.read(VAD_FRAME_SIZE)
                except Exception:
                    time.sleep(0.1)
                wait_n = 0
                continue

            data, _ = stream.read(VAD_FRAME_SIZE)
            frame = data[:, 0] if data.ndim > 1 else data
            frame_bytes = frame.astype(np.int16).tobytes()
            is_speech = self._is_speech(frame_bytes)

            if not recording:
                if is_speech:
                    speech_n += 1
                    frames.append(frame.copy())
                    if speech_n >= MIN_SPEECH_FRAMES:
                        recording = True
                        print(f"[Voice] Speech detected, recording...")
                else:
                    speech_n = 0
                    frames.clear()
                    wait_n += 1
                    if wait_n > wait_limit:
                        return None
            else:
                frames.append(frame.copy())
                if not is_speech:
                    silence_n += 1
                    if silence_n > silence_frames:
                        break
                else:
                    silence_n = 0
                # 最长 30 秒
                if len(frames) > int(SAMPLE_RATE / VAD_FRAME_SIZE * 30):
                    break

        if self._stop_evt.is_set():
            return None
        if not frames:
            return None

        audio = np.concatenate(frames)
        duration = len(audio) / SAMPLE_RATE
        print(f"[Voice] Recorded {duration:.1f}s")
        if duration < MIN_RECORD_SECS:
            return None
        return audio

    # ── 发送消息到聊天 ─────────────────────────────

    def _send_chat_message(self, text: str):
        """通过内部 HTTP 调用发送消息"""
        if self._loop:
            # 同步设置 ai_speaking，避免异步调度的竞态
            if self.in_call:
                self.ai_speaking = True
                self._broadcast_state("voice_state", {
                    "enabled": True, "status": "ai_thinking",
                    "message": "AI 思考中..."
                })
            asyncio.run_coroutine_threadsafe(
                self._async_send(text), self._loop
            )

    async def _async_send(self, text: str):
        """异步发送消息给当前对话"""
        from database import get_db

        # 获取最近活跃的对话
        async with get_db() as db:
            db.row_factory = __import__('aiosqlite').Row
            cur = await db.execute(
                "SELECT id FROM conversations ORDER BY updated_at DESC LIMIT 1"
            )
            conv = await cur.fetchone()

        if not conv:
            print("[Voice] No conversation found")
            return

        conv_id = conv["id"]

        # 通过 HTTP 调用自己的 send API
        try:
            # 语音通话时，让服务端也走 TTS 流式合成（前端通过 WS 接收音频分段）
            tts_enabled = False
            tts_voice = ""
            if self._ws_manager:
                tts_enabled = self._ws_manager.any_tts_enabled()
                if tts_enabled:
                    tts_voice = self._ws_manager.get_tts_voice() or ""

            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    "http://127.0.0.1:8080/api/conversations/" + conv_id + "/send",
                    json={"content": text, "context_limit": 20, "fast_mode": True,
                          "tts_enabled": tts_enabled, "tts_voice": tts_voice},
                    timeout=60,
                )
                # SSE 流 — 读取完毕即表示 AI 文本已生成
                # TTS 播放由前端通过 WS tts_chunk 消息触发
        except Exception as e:
            print(f"[Voice] Send error: {e}")

    # ── 主循环 ────────────────────────────────────

    def _main_loop(self):
        try:
            stream = sd.InputStream(
                samplerate=SAMPLE_RATE, channels=CHANNELS,
                dtype="int16", blocksize=VAD_FRAME_SIZE
            )
            stream.start()
            self._stream = stream

            print(f"[Voice] Started with WebRTC VAD (mode={VAD_MODE})")
            self._broadcast_state("voice_state", {"enabled": True, "status": "waiting", "wake_word": self.wake_word})

            cmd_silence = int(SAMPLE_RATE / VAD_FRAME_SIZE * CMD_SILENCE_SECS)

            while not self._stop_evt.is_set():
                # ══ 待命：等待唤醒词 ══
                print(f"[Voice] === Waiting for wakeup (ai_speaking={self.ai_speaking}, in_call={self.in_call}) ===")
                self._broadcast_state("voice_state", {
                    "enabled": True, "status": "waiting",
                    "wake_word": self.wake_word
                })

                audio = self._record(stream, MAX_SILENCE_FRAMES)
                if audio is None or self._stop_evt.is_set():
                    print(f"[Voice] _record returned None (timeout or stopped)")
                    continue

                print(f"[Voice] Got audio, sending to ASR...")
                text = self._asr(audio)
                if not text:
                    print(f"[Voice] ASR returned empty")
                    continue

                print(f"[Voice] Heard: {text}")
                print(f"[Voice] wake_word='{self.wake_word}' repr={repr(self.wake_word)}, text repr={repr(text)}, match={self.wake_word in text}")

                if self.wake_word not in text:
                    continue

                # ══ 唤醒成功 → 进入通话 ══
                print(f"[Voice] Wakeup detected!")
                self.in_call = True
                self.ai_speaking = True  # 将要播放唤醒回复

                self._broadcast_state("voice_state", {
                    "enabled": True, "status": "wakeup",
                    "message": "唤醒成功！"
                })

                # 前端收到 wakeup 后会播放回复音频 + TTS
                # 等 AI 说完（前端会通过 notify_ai_speaking 通知）
                # 先给前端一点时间开始播放
                time.sleep(0.5)

                # 通话循环
                while not self._stop_evt.is_set() and self.in_call:

                    # 等 AI 说完（持续读取流，防止缓冲区溢出）
                    while self.ai_speaking and not self._stop_evt.is_set():
                        try:
                            stream.read(FRAME_SIZE)
                        except Exception:
                            time.sleep(0.1)

                    if self._stop_evt.is_set() or not self.in_call:
                        break

                    # 聆听用户
                    self._broadcast_state("voice_state", {
                        "enabled": True, "status": "listening_cmd",
                        "message": "聆听中..."
                    })

                    user_audio = self._record(stream, cmd_silence, timeout_sec=60)

                    if self._stop_evt.is_set():
                        break

                    if user_audio is None:
                        # 60秒超时 → 挂断
                        self.in_call = False
                        self._broadcast_state("voice_state", {
                            "enabled": True, "status": "hangup",
                            "message": "通话超时结束"
                        })
                        break

                    self._broadcast_state("voice_state", {
                        "enabled": True, "status": "recognizing",
                        "message": "识别中..."
                    })

                    user_text = self._asr(user_audio)
                    if not user_text:
                        continue

                    print(f"[Voice] User said: {user_text}")

                    # 检查挂断
                    if any(kw in user_text for kw in HANGUP_KEYWORDS):
                        self.in_call = False
                        self._broadcast_state("voice_state", {
                            "enabled": True, "status": "hangup",
                            "message": "通话结束"
                        })
                        # 发送再见消息让 AI 也回应
                        self._send_chat_message(user_text)
                        break

                    # 发送用户语音内容到聊天
                    self._send_chat_message(user_text)
                    # ai_speaking 会在 _send_chat_message 里设为 True
                    # 前端 TTS 播完后通过 WS 通知 ai_speaking = False

                # ══ 通话结束，重置状态 ══
                self.ai_speaking = False
                self.in_call = False

            stream.stop()
            stream.close()
            self._stream = None
        except Exception as e:
            print(f"[Voice] Error: {e}")
            import traceback; traceback.print_exc()
        finally:
            self.enabled = False
            self.in_call = False
            self._broadcast_state("voice_state", {"enabled": False, "status": "off"})


# 全局单例
voice = VoiceWakeup()
