package com.aion.chat;

import android.content.Context;
import android.content.SharedPreferences;
import android.util.Base64;
import android.util.Log;
import android.webkit.JavascriptInterface;
import android.webkit.WebView;
import com.k2fsa.sherpa.onnx.*;
import org.json.JSONObject;
import java.io.ByteArrayOutputStream;
import java.util.concurrent.ScheduledThreadPoolExecutor;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;

/** One microphone, one decoding worker. Recognition never sends audio to a server. */
public final class LocalAsrBridge {
    private static final String MODEL = "asr/zipformer-small-bilingual/";
    private final Context context;
    private final WebView webView;
    private final AudioBridge audio;
    private final SharedPreferences prefs;
    private final ScheduledThreadPoolExecutor worker = new ScheduledThreadPoolExecutor(1);
    private final boolean installed;
    private volatile Session active;
    private volatile boolean closed;
    // Recognizer and streams are used and released exclusively on worker.
    private OnlineRecognizer recognizer;
    private ScheduledFuture<?> idleRelease;

    private final class Session implements AudioBridge.PcmListener {
        final String id;
        final ByteArrayOutputStream pcm = new ByteArrayOutputStream();
        final AtomicInteger queued = new AtomicInteger();
        volatile boolean canceled, finishing;
        OnlineStream stream;
        ScheduledFuture<?> limit;
        String committed = "", lastText = "";
        Session(String id) { this.id = id; }
        @Override public void onPcm(byte[] data) {
            if (canceled || closed) return;
            if (queued.incrementAndGet() > 150) {
                queued.decrementAndGet();
                if (!finishing) {
                    finishing = true;
                    worker.execute(() -> fail(this, "识别处理不及，请缩短录音后重试"));
                }
                return;
            }
            worker.execute(() -> {
                try {
                    if (canceled || stream == null) return;
                    if (pcm.size() + data.length > 16000 * 2 * 120) {
                        fail(this, "单次语音最多两分钟，请分段发送"); return;
                    }
                    pcm.write(data, 0, data.length);
                    stream.acceptWaveform(LocalAsrAudio.samples(data), 16000);
                    decode(this);
                    String segment = recognizer.getResult(stream).getText().trim();
                    String text = join(committed, segment);
                    if (!text.equals(lastText)) {
                        lastText = text;
                        emit(this, "partial", text, null, null);
                    }
                    if (recognizer.isEndpoint(stream)) {
                        committed = text;
                        recognizer.reset(stream);
                    }
                } catch (Exception | LinkageError e) {
                    Log.e("LocalAsr", "decode failed", e);
                    fail(this, "本地识别失败，请重试或在设置中切换云端");
                } finally { queued.decrementAndGet(); }
            });
        }
        @Override public void onStopped() {
            if (!finishing && !canceled && !closed) {
                worker.execute(() -> fail(this, "麦克风已中断，请重新按住说话"));
            }
        }
    }

    public LocalAsrBridge(Context context, WebView webView, AudioBridge audio) {
        this.context = context.getApplicationContext(); this.webView = webView; this.audio = audio;
        prefs = context.getSharedPreferences("local_asr", Context.MODE_PRIVATE);
        boolean found = false;
        try { found = context.getAssets().list(MODEL.substring(0, MODEL.length()-1)).length >= 4; }
        catch (Exception ignored) { }
        installed = found;
        worker.setRemoveOnCancelPolicy(true);
    }

    @JavascriptInterface public boolean available() { return installed && !closed; }
    @JavascriptInterface public String getMode() { return prefs.getString("mode", "local"); }
    @JavascriptInterface public void setMode(String mode) {
        if ("local".equals(mode) || "cloud".equals(mode)) prefs.edit().putString("mode", mode).apply();
    }

    @JavascriptInterface public synchronized boolean start(String id) {
        if (!available() || active != null || id == null || id.length() > 100) return false;
        Session s = new Session(id);
        active = s;
        worker.execute(() -> {
            if (s.canceled) return;
            try {
                ensureRecognizer();
                s.stream = recognizer.createStream("");
                emit(s, "ready", "", null, null);
            } catch (Exception | LinkageError e) {
                Log.e("LocalAsr", "load failed", e);
                fail(s, "本地模型加载失败，请在设置中切换云端后重试");
            }
        });
        if (!audio.startCapture(s)) {
            s.canceled = true; active = null;
            worker.execute(() -> releaseSession(s));
            return false;
        }
        s.limit = worker.schedule(() -> fail(s, "单次语音最多两分钟，请分段发送"), 120, TimeUnit.SECONDS);
        return true;
    }

    @JavascriptInterface public synchronized void finish(String id) {
        Session s = active;
        if (s == null || !s.id.equals(id) || s.finishing || s.canceled) return;
        s.finishing = true;
        audio.stopCapture(s); // Join capture before enqueueing finalization: no lost tail frames.
        if (s.limit != null) s.limit.cancel(false);
        worker.execute(() -> {
            if (s.canceled) return;
            try {
                s.stream.acceptWaveform(new float[8000], 16000);
                s.stream.inputFinished();
                decode(s);
                String text = join(s.committed, recognizer.getResult(s.stream).getText().trim());
                byte[] pcm = s.pcm.toByteArray();
                String wav = Base64.encodeToString(LocalAsrAudio.wav(pcm), Base64.NO_WRAP);
                synchronized (LocalAsrBridge.this) { if (active == s) active = null; }
                emit(s, "final", text, wav, pcm.length / 32000.0);
                releaseSession(s);
            } catch (Exception | LinkageError e) {
                Log.e("LocalAsr", "finish failed", e);
                fail(s, "本地识别收尾失败，请重试");
            }
        });
    }

    @JavascriptInterface public synchronized void cancel(String id) {
        Session s = active;
        if (s == null || !s.id.equals(id)) return;
        s.canceled = true; active = null;
        audio.stopCapture(s);
        emit(s, "canceled", "", null, null);
        worker.execute(() -> releaseSession(s));
    }

    public synchronized void cancelActive() { if (active != null) cancel(active.id); }

    /** Decode an already captured ambient segment without acquiring its microphone. */
    @JavascriptInterface public synchronized boolean transcribeWav(String id, String base64) {
        if (!available() || active != null || id == null || id.length() > 100 || base64 == null) return false;
        Session s = new Session(id);
        s.finishing = true;
        active = s;
        worker.execute(() -> {
            if (s.canceled) return;
            try {
                byte[] pcm = LocalAsrAudio.pcmFromWav(Base64.decode(base64, Base64.DEFAULT));
                ensureRecognizer();
                s.stream = recognizer.createStream("");
                s.stream.acceptWaveform(LocalAsrAudio.samples(pcm), 16000);
                s.stream.acceptWaveform(new float[8000], 16000);
                s.stream.inputFinished();
                decode(s);
                if (s.canceled) return;
                String text = recognizer.getResult(s.stream).getText().trim();
                synchronized (LocalAsrBridge.this) { if (active == s) active = null; }
                emit(s, "final", text, null, null);
                Log.i("LocalAsr", "Ambient segment transcribed locally");
                releaseSession(s);
            } catch (Exception | LinkageError e) {
                Log.e("LocalAsr", "ambient transcription failed", e);
                fail(s, "本地环境转写失败，请重试");
            }
        });
        return true;
    }

    private void ensureRecognizer() {
        if (idleRelease != null) idleRelease.cancel(false);
        if (recognizer != null) return;
        OnlineTransducerModelConfig transducer = new OnlineTransducerModelConfig();
        transducer.setEncoder(MODEL + "encoder-epoch-99-avg-1.int8.onnx");
        transducer.setDecoder(MODEL + "decoder-epoch-99-avg-1.onnx");
        transducer.setJoiner(MODEL + "joiner-epoch-99-avg-1.int8.onnx");
        OnlineModelConfig model = new OnlineModelConfig();
        model.setTransducer(transducer); model.setTokens(MODEL + "tokens.txt");
        model.setModelType("zipformer"); model.setNumThreads(1); model.setProvider("cpu");
        OnlineRecognizerConfig config = new OnlineRecognizerConfig();
        config.setModelConfig(model); config.setEnableEndpoint(true);
        recognizer = new OnlineRecognizer(context.getAssets(), config);
    }

    private void fail(Session s, String message) {
        synchronized (this) {
            if (s.canceled || active != s) return;
            s.canceled = true; active = null;
            audio.stopCapture(s);
        }
        emit(s, "error", message, null, null);
        releaseSession(s);
    }

    private void decode(Session s) {
        while (!s.canceled && recognizer.isReady(s.stream)) recognizer.decode(s.stream);
    }

    private static String join(String a, String b) {
        return a.isEmpty() ? b : b.isEmpty() ? a : a + " " + b;
    }

    private void releaseSession(Session s) {
        if (s.limit != null) s.limit.cancel(false);
        if (s.stream != null) { s.stream.release(); s.stream = null; }
        s.pcm.reset();
        if (idleRelease != null) idleRelease.cancel(false);
        if (!closed) idleRelease = worker.schedule(() -> {
            if (active == null) releaseModel();
        }, 30, TimeUnit.SECONDS);
    }

    private void releaseModel() {
        if (recognizer != null) { recognizer.release(); recognizer = null; }
    }

    private void emit(Session s, String type, String text, String wav, Double duration) {
        try {
            JSONObject e = new JSONObject().put("id", s.id).put("type", type);
            e.put(type.equals("error") ? "message" : "text", text);
            if (wav != null) e.put("wav", wav).put("duration", duration);
            String script = "window._aionLocalAsrDispatch&&window._aionLocalAsrDispatch(" + e + ")";
            webView.post(() -> {
                if (!closed && !(s.canceled && (type.equals("partial") || type.equals("ready") || type.equals("final"))))
                    webView.evaluateJavascript(script, null);
            });
        } catch (Exception e) { Log.e("LocalAsr", "callback failed", e); }
    }

    public synchronized void shutdown() {
        cancelActive(); closed = true;
        worker.execute(() -> { if (idleRelease != null) idleRelease.cancel(false); releaseModel(); });
        worker.shutdown();
    }
}
