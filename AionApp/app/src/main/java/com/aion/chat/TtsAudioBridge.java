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
    private static final String MONITOR_ALERT_ID = "tts-monitor-alert";
    private final Context context;
    private final WebView webView;
    private final Map<String, MediaPlayer> players = new HashMap<>();
    private final Map<String, LoudnessEnhancer> enhancers = new HashMap<>();
    private final Set<String> preparedPlayers = new HashSet<>();
    private final Map<String, Runnable> progressCallbacks = new HashMap<>();
    private final Map<String, Object> playbackOwners = new HashMap<>();
    private final Set<String> speechSessions = new HashSet<>();
    private volatile boolean foreground = true;

    public TtsAudioBridge(Context context, WebView webView) {
        this.context = context.getApplicationContext();
        this.webView = webView;
    }

    @JavascriptInterface
    public boolean isForeground() { return foreground; }

    @JavascriptInterface
    public void prefetch(String playbackUrl) {
        webView.post(() -> {
            String url = TtsAudioUrlResolver.resolve(webView.getUrl(), playbackUrl);
            if (url != null && java.net.URI.create(url).getPath().startsWith("/api/tts/audio/")) {
                MediaCacheStore.shared(context).loadAudio(url, audioHeaders(url), true, file -> {});
            }
        });
    }

    private Map<String, String> audioHeaders(String url) {
        Map<String, String> headers = new HashMap<>();
        String cookie = CookieManager.getInstance().getCookie(url);
        if (cookie != null && !cookie.isEmpty()) headers.put("Cookie", cookie);
        String userAgent = webView.getSettings().getUserAgentString();
        if (userAgent != null && !userAgent.isEmpty()) headers.put("User-Agent", userAgent);
        return headers;
    }

    /** Called on the activity thread, before the WebView can be suspended. */
    public void setForeground(boolean active) {
        foreground = active;
        if (active) return;
        // Let a playing clip finish, but never reserve silence for a JS callback
        // that Android may postpone until the user opens the app again.
        speechSessions.clear();
        for (String id : new HashSet<>(playbackOwners.keySet())) {
            if (!players.containsKey(id)) releasePlaybackOwner(id);
        }
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
    public void beginSpeech(String playerId) {
        if (validPlayerId(playerId)) webView.post(() -> {
            if (foreground) speechSessions.add(playerId);
        });
    }

    @JavascriptInterface
    public void endSpeech(String playerId) {
        if (!validPlayerId(playerId)) return;
        webView.post(() -> {
            speechSessions.remove(playerId);
            if (!players.containsKey(playerId)) releasePlaybackOwner(playerId);
        });
    }

    @JavascriptInterface
    public boolean playMonitorAlert() {
        // AionPushService owns monitor cues in both foreground and background.
        // A delayed WebView callback must never replay an already handled cue.
        return true;
    }

    @JavascriptInterface
    public boolean play(String playerId, String playbackUrl) {
        if (!validPlayerId(playerId) || playbackUrl == null || playbackUrl.trim().isEmpty()) {
            return false;
        }
        // Older cached pages still use the generic entry point for this cue.
        if (MONITOR_ALERT_ID.equals(playerId)) return playMonitorAlert();
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
                if (player.isPlaying()) player.pause();
                releasePlaybackOwner(playerId);
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
            TtsPlaybackCoordinator.shared.request(playbackOwner(playerId), () -> {
                try {
                    player.start();
                    emit(playerId, "playing");
                    startProgress(playerId, player);
                } catch (RuntimeException exception) {
                    finish(playerId, player, "error");
                }
            });
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
        webView.post(() -> {
            speechSessions.remove(playerId);
            releasePlayer(playerId);
        });
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
        if (autoPlay) {
            TtsPlaybackCoordinator.shared.request(playbackOwner(playerId),
                    () -> preparePlayer(playerId, player, resolvedUrl, true));
        } else {
            preparePlayer(playerId, player, resolvedUrl, false);
        }
    }

    private void preparePlayer(String playerId, MediaPlayer player, String resolvedUrl, boolean autoPlay) {
        Map<String, String> headers = audioHeaders(resolvedUrl);
        if (java.net.URI.create(resolvedUrl).getPath().startsWith("/api/tts/audio/")) {
            MediaCacheStore.shared(context).loadAudio(resolvedUrl, headers, false, file -> webView.post(() -> {
                if (players.get(playerId) != player) return;
                prepareSource(playerId, player, resolvedUrl, autoPlay, headers, file);
            }));
        } else prepareSource(playerId, player, resolvedUrl, autoPlay, headers, null);
    }

    private void prepareSource(String playerId, MediaPlayer player, String resolvedUrl, boolean autoPlay,
                               Map<String, String> headers, java.io.File cached) {
        try {
            player.setAudioAttributes(new AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_MEDIA)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                    .build());

            if (cached != null && cached.isFile()) {
                try (java.io.FileInputStream input = new java.io.FileInputStream(cached)) {
                    player.setDataSource(input.getFD());
                }
            } else player.setDataSource(context, Uri.parse(resolvedUrl), headers);
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
        if (player != null) {
            player.release();
        }
        if (!speechSessions.contains(playerId)) releasePlaybackOwner(playerId);
    }

    private Object playbackOwner(String playerId) {
        return playbackOwners.computeIfAbsent(playerId, id -> new Object());
    }

    private void releasePlaybackOwner(String playerId) {
        Object owner = playbackOwners.remove(playerId);
        if (owner != null) TtsPlaybackCoordinator.shared.release(owner);
    }

    private void releaseAll() {
        speechSessions.clear();
        for (String playerId : new HashSet<>(players.keySet())) releasePlayer(playerId);
        for (String playerId : new HashSet<>(playbackOwners.keySet())) releasePlaybackOwner(playerId);
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
