package com.aion.chat;

import android.util.Base64;
import android.util.Log;
import android.webkit.JavascriptInterface;
import android.webkit.WebView;
import org.json.JSONObject;
import java.util.Arrays;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicInteger;

/** Classifies the shared microphone before the page sends speech clips to ASR. */
final class AmbientSoundBridge {
    private final WebView webView;
    private final AudioBridge audio;
    private final ExecutorService worker = Executors.newSingleThreadExecutor();
    private volatile Session active;
    private final TtsPlaybackCoordinator.Listener ttsListener;

    AmbientSoundBridge(WebView webView, AudioBridge audio) {
        this.webView = webView;
        this.audio = audio;
        ttsListener = speaking -> {
            if (speaking) stop();
            webView.post(() -> webView.evaluateJavascript(
                    "window._ambientTtsChanged&&window._ambientTtsChanged(" + speaking + ")", null));
        };
        TtsPlaybackCoordinator.shared.addListener(ttsListener);
    }

    @JavascriptInterface public boolean isTtsPlaying() { return TtsPlaybackCoordinator.shared.isSpeaking(); }
    @JavascriptInterface public boolean isRunning() { return active != null; }

    @JavascriptInterface public synchronized boolean start(String id) {
        if (active != null || audio.isRecording() || isTtsPlaying() || worker.isShutdown()) return false;
        Session session = new Session(id);
        active = session;
        worker.execute(() -> {
            try {
                if (active != session) return;
                session.model = new YamnetClassifier(webView.getContext().getApplicationContext());
                session.model.classify(new short[YamnetClassifier.WINDOW]);
                synchronized (AmbientSoundBridge.this) {
                    if (active != session || isTtsPlaying()) return;
                    session.started = System.currentTimeMillis();
                    if (!audio.startCapture(session)) throw new IllegalStateException("麦克风被占用");
                }
                emit(session, new JSONObject().put("type", "ready"));
            } catch (Exception | LinkageError e) {
                fail(session, e.getMessage());
            }
        });
        return true;
    }

    @JavascriptInterface public void stop() {
        Session session;
        synchronized (this) { session = active; active = null; }
        if (session == null) return;
        audio.stopCapture(session);
        worker.execute(() -> { if (session.model != null) session.model.close(); });
    }

    void shutdown() {
        TtsPlaybackCoordinator.shared.removeListener(ttsListener);
        stop();
        worker.shutdown();
    }

    private void fail(Session session, String message) {
        Log.e("AmbientSound", message == null ? "Capture failed" : message);
        try { emit(session, new JSONObject().put("type", "error").put("message", message)); }
        catch (Exception ignored) { }
    }

    private void emit(Session session, JSONObject event) {
        try { event.put("id", session.id); } catch (Exception ignored) { }
        webView.post(() -> {
            if (active != session || (isTtsPlaying() && !"error".equals(event.optString("type")))) return;
            webView.evaluateJavascript("window._ambientSoundOnFrame&&window._ambientSoundOnFrame(" + event + ")", null);
        });
    }

    private final class Session implements AudioBridge.PcmListener {
        final String id;
        final byte[] buffer = new byte[YamnetClassifier.WINDOW * 2];
        final AtomicInteger queued = new AtomicInteger();
        int filled;
        long started, samples;
        YamnetClassifier model; // Only the worker touches the model.
        Session(String id) { this.id = id; }

        @Override public void onPcm(byte[] data) {
            if (active != this || isTtsPlaying()) { filled = 0; return; }
            for (int offset = 0; offset < data.length;) {
                int count = Math.min(buffer.length - filled, data.length - offset);
                System.arraycopy(data, offset, buffer, filled, count);
                filled += count;
                offset += count;
                if (filled < buffer.length) continue;
                byte[] pcm = Arrays.copyOf(buffer, buffer.length);
                filled = 0;
                long start = started + samples * 1000 / YamnetClassifier.SAMPLE_RATE;
                samples += YamnetClassifier.WINDOW;
                long end = started + samples * 1000 / YamnetClassifier.SAMPLE_RATE;
                if (queued.incrementAndGet() > 3) { queued.decrementAndGet(); continue; }
                worker.execute(() -> {
                    try {
                        if (active != this || isTtsPlaying()) return;
                        short[] input = new short[YamnetClassifier.WINDOW];
                        for (int i = 0; i < input.length; i++) input[i] = (short) ((pcm[i * 2] & 255) | (pcm[i * 2 + 1] << 8));
                        String kind = SoundScores.ambientKind(model.classify(input));
                        emit(this, new JSONObject().put("type", "frame").put("kind", kind)
                                .put("start", start).put("end", end)
                                .put("pcm", Base64.encodeToString(pcm, Base64.NO_WRAP)));
                    } catch (Exception e) { fail(this, e.getMessage()); }
                    finally { queued.decrementAndGet(); }
                });
            }
        }

        @Override public void onStopped() {
            if (active == this) fail(this, "麦克风已中断，请重新开启侦听");
        }
    }
}
