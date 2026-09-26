package com.aion.chat;

import android.content.Context;
import android.media.AudioAttributes;
import android.media.MediaPlayer;
import android.media.audiofx.LoudnessEnhancer;
import android.net.Uri;
import android.os.Looper;
import android.webkit.CookieManager;
import android.webkit.JavascriptInterface;
import android.webkit.WebView;

import org.json.JSONObject;

import java.util.HashMap;
import java.util.Map;
import java.util.HashSet;
import java.util.Set;

/** Plays TTS without claiming Android audio focus, allowing other apps to keep playing. */
public final class TtsAudioBridge {
    private final Context context;
    private final WebView webView;
    private final Map<String, MediaPlayer> players = new HashMap<>();
    private final Map<String, LoudnessEnhancer> enhancers = new HashMap<>();
    private final Set<String> preparedPlayers = new HashSet<>();
    private final Map<String, Runnable> progressCallbacks = new HashMap<>();

    public TtsAudioBridge(Context context, WebView webView) {
        this.context = context.getApplicationContext();
        this.webView = webView;
    }

    @JavascriptInterface
    public void setAutoPlaybackState(boolean enabled, String voice, double activeAt) {
        if (!Double.isFinite(activeAt)) return;
        android.content.SharedPreferences prefs = context.getSharedPreferences("aion_prefs", Context.MODE_PRIVATE);
        synchronized (TtsAudioBridge.class) {
            if (activeAt < Double.longBitsToDouble(prefs.getLong("background_tts_active_at", 0))) return;
            prefs.edit().putBoolean("background_tts_enabled", enabled)
                    .putString("background_tts_voice", voice == null ? "" : voice)
                    .putLong("background_tts_active_at", Double.doubleToLongBits(activeAt))
                    .putLong("background_tts_updated", System.nanoTime()).apply();
        }
    }

    @JavascriptInterface
    public void stopAutoPlayback() {
        context.getSharedPreferences("aion_prefs", Context.MODE_PRIVATE).edit()
                .putLong("background_tts_stop", System.nanoTime()).apply();
    }

    @JavascriptInterface
    public int getLoudnessGainDb() {
        return PlaybackLoudness.getGainDb(context);
    }

    @JavascriptInterface
    public int setLoudnessGainDb(int gainDb) {
        return PlaybackLoudness.setGainDb(context, gainDb);
    }

    @JavascriptInterface
    public boolean play(String playerId, String playbackUrl) {
        if (!validPlayerId(playerId) || playbackUrl == null || playbackUrl.trim().isEmpty()) {
            return false;
        }
        webView.post(() -> startOnMainThread(playerId, playbackUrl, true));
        return true;
    }

    @JavascriptInterface
    public boolean prepareAudio(String playerId, String playbackUrl) {
        if (!validPlayerId(playerId) || playbackUrl == null || playbackUrl.trim().isEmpty()) {
            return false;
        }
        webView.post(() -> startOnMainThread(playerId, playbackUrl, false));
        return true;
    }

    @JavascriptInterface
    public void pauseAudio(String playerId) {
        webView.post(() -> {
            MediaPlayer player = players.get(playerId);
            if (player == null || !preparedPlayers.contains(playerId)) return;
            stopProgress(playerId);
            try {
                player.pause();
                emitPosition(playerId, "timeupdate", player);
            } catch (RuntimeException exception) {
                finish(playerId, player, "error");
            }
        });
    }

    @JavascriptInterface
    public void resumeAudio(String playerId) {
        webView.post(() -> {
            MediaPlayer player = players.get(playerId);
            if (player == null || !preparedPlayers.contains(playerId)) return;
            try {
                player.start();
                emit(playerId, "playing");
                startProgress(playerId, player);
            } catch (RuntimeException exception) {
                finish(playerId, player, "error");
            }
        });
    }

    @JavascriptInterface
    public void seekAudio(String playerId, double seconds) {
        if (!Double.isFinite(seconds)) return;
        webView.post(() -> {
            MediaPlayer player = players.get(playerId);
            if (player == null || !preparedPlayers.contains(playerId)) return;
            try {
                int target = (int) Math.max(0, Math.min(seconds * 1000, player.getDuration()));
                player.seekTo(target);
            } catch (RuntimeException exception) {
                finish(playerId, player, "error");
            }
        });
    }

    @JavascriptInterface
    public void stop(String playerId) {
        if (!validPlayerId(playerId)) return;
        webView.post(() -> releasePlayer(playerId));
    }

    public void shutdown() {
        if (Looper.myLooper() == Looper.getMainLooper()) {
            releaseAll();
        } else {
            webView.post(this::releaseAll);
        }
    }

    private void startOnMainThread(String playerId, String playbackUrl, boolean autoPlay) {
        String resolvedUrl = TtsAudioUrlResolver.resolve(webView.getUrl(), playbackUrl);
        if (resolvedUrl == null) {
            emit(playerId, "error");
            return;
        }

        releasePlayer(playerId);
        MediaPlayer player = new MediaPlayer();
        players.put(playerId, player);
        try {
            player.setAudioAttributes(new AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_MEDIA)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                    .build());

            Map<String, String> headers = new HashMap<>();
            String cookie = CookieManager.getInstance().getCookie(resolvedUrl);
            if (cookie != null && !cookie.isEmpty()) headers.put("Cookie", cookie);
            String userAgent = webView.getSettings().getUserAgentString();
            if (userAgent != null && !userAgent.isEmpty()) headers.put("User-Agent", userAgent);

            player.setDataSource(context, Uri.parse(resolvedUrl), headers);
            player.setOnPreparedListener(prepared -> {
                if (players.get(playerId) != prepared) {
                    prepared.release();
                    return;
                }
                try {
                    preparedPlayers.add(playerId);
                    emitPosition(playerId, "loadedmetadata", prepared);
                    if (autoPlay) {
                        prepared.start();
                        emit(playerId, "playing");
                    }
                } catch (RuntimeException exception) {
                    finish(playerId, prepared, "error");
                }
            });
            player.setOnSeekCompleteListener(seeked -> {
                if (players.get(playerId) == seeked) emitPosition(playerId, "timeupdate", seeked);
            });
            player.setOnCompletionListener(completed -> {
                if (players.get(playerId) == completed) emitPosition(playerId, "timeupdate", completed);
                finish(playerId, completed, "ended");
            });
            player.setOnErrorListener((failed, what, extra) -> {
                finish(playerId, failed, "error");
                return true;
            });
            player.prepareAsync();
            enableLoudness(playerId, player);
        } catch (Exception exception) {
            finish(playerId, player, "error");
        }
    }

    private void enableLoudness(String playerId, MediaPlayer player) {
        LoudnessEnhancer enhancer = PlaybackLoudness.attach(context, player);
        if (enhancer != null) enhancers.put(playerId, enhancer);
    }

    private void startProgress(String playerId, MediaPlayer player) {
        stopProgress(playerId);
        Runnable callback = new Runnable() {
            @Override public void run() {
                if (players.get(playerId) != player || progressCallbacks.get(playerId) != this) return;
                emitPosition(playerId, "timeupdate", player);
                webView.postDelayed(this, 250);
            }
        };
        progressCallbacks.put(playerId, callback);
        webView.post(callback);
    }

    private void stopProgress(String playerId) {
        Runnable callback = progressCallbacks.remove(playerId);
        if (callback != null) webView.removeCallbacks(callback);
    }

    private void emitPosition(String playerId, String type, MediaPlayer player) {
        try {
            emitEvent(new JSONObject().put("playerId", playerId).put("type", type)
                    .put("duration", player.getDuration() / 1000.0)
                    .put("currentTime", player.getCurrentPosition() / 1000.0));
        } catch (Exception ignored) {
        }
    }

    private void releaseLoudness(String playerId) {
        LoudnessEnhancer enhancer = enhancers.remove(playerId);
        if (enhancer != null) enhancer.release();
    }

    private void finish(String playerId, MediaPlayer player, String event) {
        if (players.get(playerId) != player) return;
        releasePlayer(playerId);
        emit(playerId, event);
    }

    private void releasePlayer(String playerId) {
        stopProgress(playerId);
        preparedPlayers.remove(playerId);
        MediaPlayer player = players.remove(playerId);
        releaseLoudness(playerId);
        if (player != null) player.release();
    }

    private void releaseAll() {
        for (String playerId : new HashSet<>(players.keySet())) releasePlayer(playerId);
    }

    private void emit(String playerId, String type) {
        try {
            emitEvent(new JSONObject()
                    .put("playerId", playerId)
                    .put("type", type));
        } catch (Exception ignored) {
        }
    }

    private void emitEvent(JSONObject event) {
        webView.evaluateJavascript(
                "window.onAionNativeTtsEvent&&window.onAionNativeTtsEvent(" + event + ")", null);
    }

    private static boolean validPlayerId(String playerId) {
        return playerId != null && playerId.matches("tts-[a-z0-9-]{3,64}");
    }
}
