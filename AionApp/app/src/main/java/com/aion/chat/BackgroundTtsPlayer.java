package com.aion.chat;

import android.content.Context;
import android.media.AudioAttributes;
import android.media.MediaPlayer;
import android.media.audiofx.LoudnessEnhancer;
import android.net.Uri;
import android.os.Handler;
import android.os.PowerManager;
import android.util.Log;
import android.webkit.CookieManager;
import org.json.JSONObject;
import java.util.HashMap;
import java.util.Map;

/** Owns background speech without relying on a running WebView or its callbacks. */
final class BackgroundTtsPlayer {
    private final Context context;
    private final Handler handler;
    private final Runnable stateChanged;
    private final BackgroundTtsQueue queue = new BackgroundTtsQueue();
    private MediaPlayer player;
    private LoudnessEnhancer enhancer;
    private Runnable prepareTimeout;

    BackgroundTtsPlayer(Context context, Handler handler, Runnable stateChanged) {
        this.context = context;
        this.handler = handler;
        this.stateChanged = stateChanged;
    }

    boolean hasPending() { return queue.hasPending(); }

    void connectionLost() {
        queue.finishPending();
        stateChanged.run();
        playNext();
    }

    void cancelGeneration(JSONObject data) {
        org.json.JSONArray ids = data == null ? null : data.optJSONArray("message_ids");
        if (ids == null) return;
        for (int i = 0; i < ids.length(); i++) {
            if (queue.contains(ids.optString(i))) { stop(); return; }
        }
    }

    void receive(String type, JSONObject data, String baseUrl) {
        if (data == null) return;
        String id = data.optString("msg_id", "");
        if ("tts_chunk".equals(type)) {
            String url = TtsAudioUrlResolver.resolve(baseUrl, data.optString("url", ""));
            if (url == null) return;
            queue.offer(id, data.optInt("seq", -1), url);
            Log.i("AionBackgroundTts", "queued msg=" + id + " seq=" + data.optInt("seq"));
        } else if ("tts_done".equals(type)) {
            queue.finish(id);
        }
        stateChanged.run();
        playNext();
    }

    private void playNext() {
        if (player != null) return;
        String url = queue.next();
        if (url == null) { stateChanged.run(); return; }
        MediaPlayer current = new MediaPlayer();
        player = current;
        try {
            current.setAudioAttributes(new AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_MEDIA)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build());
            current.setWakeMode(context, PowerManager.PARTIAL_WAKE_LOCK);
            Map<String, String> headers = new HashMap<>();
            String cookie = CookieManager.getInstance().getCookie(url);
            if (cookie != null && !cookie.isEmpty()) headers.put("Cookie", cookie);
            current.setDataSource(context, Uri.parse(url), headers);
            current.setOnPreparedListener(prepared -> {
                if (player != prepared) return;
                cancelPrepareTimeout();
                try {
                    enhancer = PlaybackLoudness.attach(context, prepared);
                    prepared.start();
                    Log.i("AionBackgroundTts", "playing");
                } catch (RuntimeException error) {
                    Log.w("AionBackgroundTts", "start failed", error);
                    complete(prepared);
                }
            });
            current.setOnCompletionListener(this::complete);
            current.setOnErrorListener((failed, what, extra) -> {
                Log.w("AionBackgroundTts", "playback failed " + what + "/" + extra);
                complete(failed);
                return true;
            });
            prepareTimeout = () -> {
                Log.w("AionBackgroundTts", "audio loading timed out");
                complete(current);
            };
            handler.postDelayed(prepareTimeout, 30_000);
            current.prepareAsync();
        } catch (Exception error) {
            Log.w("AionBackgroundTts", "prepare failed", error);
            complete(current);
        }
    }

    private void complete(MediaPlayer expected) {
        if (player != expected) return;
        release();
        queue.advance();
        Log.i("AionBackgroundTts", "segment finished; pending=" + queue.hasPending());
        stateChanged.run();
        handler.post(this::playNext);
    }

    void stop() {
        release();
        queue.clear();
        stateChanged.run();
    }

    private void cancelPrepareTimeout() {
        if (prepareTimeout != null) handler.removeCallbacks(prepareTimeout);
        prepareTimeout = null;
    }

    private void release() {
        cancelPrepareTimeout();
        if (enhancer != null) enhancer.release();
        enhancer = null;
        if (player != null) player.release();
        player = null;
    }
}
