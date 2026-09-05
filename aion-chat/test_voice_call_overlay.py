import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


class VoiceCallOverlayTests(unittest.TestCase):
    def test_tts_chunk_payload_includes_caption_text(self):
        async def run():
            import tts

            queue = asyncio.Queue()

            async def fake_request(text, voice, *, seq=None):
                return b"mp3"

            with patch.object(tts, "_request_tts_audio", fake_request):
                streamer = tts.TTSStreamer(
                    "vc_test",
                    "voice://test",
                    ws_manager=None,
                    sse_queue=queue,
                    min_chars=1,
                    max_chars=20,
                    cache_dir=ROOT / "data" / "test_tts_cache",
                )
                streamer.feed("First sentence. Second sentence.")
                await streamer.flush()

            chunks = []
            while not queue.empty():
                item = await queue.get()
                if item.get("type") == "tts_chunk":
                    chunks.append(item["data"])

            self.assertGreaterEqual(len(chunks), 1)
            self.assertEqual(chunks[0]["text"], "First sentence.")
            self.assertTrue(all("text" in chunk for chunk in chunks))

        asyncio.run(run())

    def test_private_and_chatroom_pages_load_voice_call_assets(self):
        chat_html = (ROOT / "static" / "chat.html").read_text(encoding="utf-8")
        chatroom_html = (ROOT / "static" / "chatroom.html").read_text(encoding="utf-8")

        for html in (chat_html, chatroom_html):
            self.assertIn("/static/voice-call.css", html)
            self.assertIn("/static/voice-call.js", html)

    def test_voice_call_adapters_and_tts_hooks_are_exposed(self):
        chat_js = (ROOT / "static" / "chat.js").read_text(encoding="utf-8")
        chatroom_js = (ROOT / "static" / "chatroom.js").read_text(encoding="utf-8")

        self.assertIn("window.PrivateVoiceCallAdapter", chat_js)
        self.assertIn("window.ChatroomVoiceCallAdapter", chatroom_js)
        self.assertIn("VoiceCall.handleTTSChunkStart", chat_js)
        self.assertIn("VoiceCall.handleTTSChunkStart", chatroom_js)
        self.assertIn("VoiceCall.handleTTSEnd", chat_js)
        self.assertIn("VoiceCall.handleTTSEnd", chatroom_js)
        self.assertIn("voiceCallActive", chat_js)
        self.assertIn("!voiceCallActive", chat_js)

    def test_voice_call_css_contains_theme_and_wave_surface(self):
        css = (ROOT / "static" / "voice-call.css").read_text(encoding="utf-8")

        self.assertIn('body[data-theme="dark"] .voice-call-overlay', css)
        self.assertIn('body[data-theme="light"] .voice-call-overlay', css)
        self.assertIn(".voice-call-wave-canvas", css)
        self.assertIn(".voice-call-overlay.speaking", css)

    def test_voice_call_mobile_layout_stays_inside_safe_viewport(self):
        css = (ROOT / "static" / "voice-call.css").read_text(encoding="utf-8")

        self.assertIn("--vc-visual-size", css)
        self.assertIn("height: 100svh", css)
        self.assertIn("max(env(safe-area-inset-bottom, 0px), 18px)", css)
        self.assertIn("grid-template-columns: repeat(2, minmax(0, auto))", css)
        self.assertIn(".voice-call-hold-control:not([hidden])", css)
        self.assertIn("@media (max-height: 760px)", css)

    def test_voice_call_canvas_uses_layered_programmatic_wave(self):
        js = (ROOT / "static" / "voice-call.js").read_text(encoding="utf-8")

        self.assertIn("drawAtmosphere", js)
        self.assertIn("drawOrbitalRings", js)
        self.assertIn("drawPulseRings", js)
        self.assertIn("drawSpectralWaveform", js)
        self.assertIn("drawRibbonWave", js)
        self.assertIn("const waveBins = 104", js)
        self.assertIn("globalCompositeOperation = 'lighter'", js)
        self.assertIn("canvas.width = Math.max(1, Math.floor(rect.width));", js)
        self.assertNotIn("const bars = 58", js)

    def test_voice_call_caption_header_is_status_only_and_tts_is_segmented(self):
        js = (ROOT / "static" / "voice-call.js").read_text(encoding="utf-8")

        self.assertNotIn('data-role="caption-label"', js)
        self.assertNotIn("实时语音转文字", js)
        self.assertIn("function splitCaptionSentences", js)
        self.assertIn("function startCaptionSegments", js)
        self.assertIn("function clearCaptionSegments", js)
        self.assertIn("startCaptionSegments(payload.text || '正在播放语音...')", js)
        self.assertNotIn("setStatus('speaking', payload.text || '正在播放语音...', '正在说话')", js)

    def test_voice_call_mobile_visuals_are_centered_and_more_compact(self):
        js = (ROOT / "static" / "voice-call.js").read_text(encoding="utf-8")
        css = (ROOT / "static" / "voice-call.css").read_text(encoding="utf-8")

        self.assertIn("const cx = w * 0.5;", js)
        self.assertIn("const cy = h * 0.50;", js)
        self.assertIn("const radius = Math.min(w, h) * 0.40;", js)
        self.assertIn("--vc-visual-size: min(96vw, 430px, 48dvh);", css)
        self.assertIn("font-size: 24px;", css)
        self.assertIn("font-size: 16px;", css)
        self.assertIn("width: 60px;", css)
        self.assertIn("width: 52px;", css)

    def test_voice_call_visual_uses_hero_timer_and_flowing_highlights(self):
        js = (ROOT / "static" / "voice-call.js").read_text(encoding="utf-8")
        css = (ROOT / "static" / "voice-call.css").read_text(encoding="utf-8")

        visual_start = js.index('<div class="voice-call-visual">')
        caption_start = js.index('<div class="voice-call-caption-card">')
        visual_markup = js[visual_start:caption_start]
        self.assertIn('data-role="timer"', visual_markup)
        self.assertIn("drawHeroGlow", js)
        self.assertIn("drawPulseRings", js)
        self.assertIn("drawOrbitalRings", js)

        self.assertIn("grid-template-rows: auto var(--vc-visual-size) auto auto;", css)
        self.assertIn("@property --vc-border-angle", css)
        self.assertIn("animation: voiceCallBorderSweep", css)
        self.assertIn("@keyframes voiceCallBorderSweep", css)

    def test_voice_call_background_uses_chat_assets_without_rotating_backdrop(self):
        css = (ROOT / "static" / "voice-call.css").read_text(encoding="utf-8")

        self.assertIn("url('/public/chat-bg-dark.jpg')", css)
        self.assertIn("url('/public/chat-bg-light.jpg')", css)
        self.assertNotIn("voiceCallBackdropFlow", css)
        self.assertNotIn("voiceCallRibbonDrift", css)

    def test_voice_call_caption_highlight_is_border_only_without_inner_sheen(self):
        css = (ROOT / "static" / "voice-call.css").read_text(encoding="utf-8")

        self.assertIn(".voice-call-caption-card::before", css)
        self.assertIn("-webkit-mask-composite: xor", css)
        self.assertIn("mask-composite: exclude", css)
        self.assertNotIn("voiceCallCaptionSheen", css)
        self.assertNotIn("voiceCallCaptionEdge", css)
        self.assertNotIn(".voice-call-caption-card::after", css)

    def test_voice_call_wave_glow_stays_inside_canvas_without_oval_layers(self):
        js = (ROOT / "static" / "voice-call.js").read_text(encoding="utf-8")
        css = (ROOT / "static" / "voice-call.css").read_text(encoding="utf-8")

        self.assertIn("m.radius * 1.18", js)
        self.assertIn("m.radius * 1.20", js)
        self.assertNotIn("ctx.ellipse", js)
        self.assertNotIn("drawVortexField", js)
        self.assertIn("drop-shadow(0 0 16px", css)

    def test_voice_call_caption_timing_is_tunable_and_slower_for_tts(self):
        js = (ROOT / "static" / "voice-call.js").read_text(encoding="utf-8")

        self.assertIn("CAPTION_SEGMENT_MIN_MS = 2600", js)
        self.assertIn("CAPTION_SEGMENT_MAX_MS = 9000", js)
        self.assertIn("CAPTION_SEGMENT_MS_PER_CHAR = 260", js)
        self.assertIn("return Math.min(CAPTION_SEGMENT_MAX_MS, Math.max(CAPTION_SEGMENT_MIN_MS, charLength(text) * CAPTION_SEGMENT_MS_PER_CHAR));", js)

    def test_voice_call_controls_are_refined_and_centered(self):
        css = (ROOT / "static" / "voice-call.css").read_text(encoding="utf-8")

        self.assertIn("background: transparent;", css)
        self.assertIn("box-shadow: none;", css)
        self.assertIn(".voice-call-round-btn::before", css)
        self.assertIn("left: 50%;", css)
        self.assertIn("transform: translateX(-50%);", css)
        self.assertIn("transform: translate(-50%, -50%) rotate(180deg);", css)
        self.assertIn("linear-gradient(180deg, rgba(16, 35, 76, 0.58), rgba(4, 10, 28, 0.78))", css)
        self.assertNotIn("color-mix(in srgb, var(--vc-accent) 48%, #10213f)", css)

    def test_voice_call_wave_is_larger_and_uses_deeper_blue_palette(self):
        js = (ROOT / "static" / "voice-call.js").read_text(encoding="utf-8")
        css = (ROOT / "static" / "voice-call.css").read_text(encoding="utf-8")
        chat_html = (ROOT / "static" / "chat.html").read_text(encoding="utf-8")
        chatroom_html = (ROOT / "static" / "chatroom.html").read_text(encoding="utf-8")

        self.assertIn("--vc-visual-size: min(90vw, 680px, 54dvh);", css)
        self.assertIn("drop-shadow(0 0 16px", css)
        self.assertIn("{ main: '#166dff', glow: '#39c5ff', soft: '#0d4fbd' }", js)
        self.assertIn("{ main: '#124cff', glow: '#35a8ff', soft: '#6d7dff' }", js)
        self.assertIn("voice-latency-20260902a", chat_html)
        self.assertIn("voice-latency-20260902a", chatroom_html)

    def test_voice_call_mobile_top_chrome_caption_and_back_button_are_polished(self):
        js = (ROOT / "static" / "voice-call.js").read_text(encoding="utf-8")
        css = (ROOT / "static" / "voice-call.css").read_text(encoding="utf-8")
        chat_html = (ROOT / "static" / "chat.html").read_text(encoding="utf-8")
        chatroom_html = (ROOT / "static" / "chatroom.html").read_text(encoding="utf-8")

        self.assertIn("function setVoiceCallThemeColor(active)", js)
        self.assertIn("document.querySelector('meta[name=\"theme-color\"]')", js)
        self.assertIn("AionStatusBar", js)
        self.assertIn("setVoiceCallThemeColor(true);", js)
        self.assertIn("setVoiceCallThemeColor(false);", js)

        self.assertIn("width: min(calc(100vw - 52px), 680px);", css)
        self.assertIn("max-width: calc(100% - 24px);", css)
        self.assertIn("margin-inline: auto;", css)
        self.assertIn("transform: translateX(var(--vc-caption-offset-x));", css)
        self.assertIn("--vc-caption-offset-x: -5px;", css)
        self.assertIn(".voice-call-icon-btn[data-action=\"minimize\"]", css)
        self.assertIn("color: var(--vc-accent-2);", css)
        self.assertIn("width: 32px;", css)
        self.assertIn("filter: drop-shadow(0 0 12px", css)

        self.assertIn("voice-latency-20260902a", chat_html)
        self.assertIn("voice-latency-20260902a", chatroom_html)


if __name__ == "__main__":
    unittest.main()
