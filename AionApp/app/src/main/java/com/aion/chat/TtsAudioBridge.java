package com.aion.chat;

import android.content.Context;
import android.media.AudioAttributes;
import android.media.MediaPlayer;
import android.net.Uri;
import android.os.Looper;
import android.webkit.CookieManager;
import android.webkit.JavascriptInterface;
import android.webkit.WebView;

import org.json.JSONObject;

import java.util.HashMap;
import java.util.Map;

/** Plays TTS without claiming Android audio focus, allowing other apps to keep playing. */
public final class TtsAudioBridge {
    private final Context context;
    private final WebView webView;
    private final Map<String, MediaPlayer> players = new HashMap<>();

    public TtsAudioBridge(Context context, WebView webView) {
        this.context = context.getApplicationContext();
        this.webView = webView;
    }

    @JavascriptInterface
    public boolean play(String playerId, String playbackUrl) {
        if (!validPlayerId(playerId) || playbackUrl == null || playbackUrl.trim().isEmpty()) {
            return false;
        }
        webView.post(() -> startOnMainThread(playerId, playbackUrl));
        return true;
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

    private void startOnMainThread(String playerId, String playbackUrl) {
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
                    prepared.start();
                    emit(playerId, "playing");
                } catch (RuntimeException exception) {
                    finish(playerId, prepared, "error");
                }
            });
            player.setOnCompletionListener(completed -> finish(playerId, completed, "ended"));
            player.setOnErrorListener((failed, what, extra) -> {
                finish(playerId, failed, "error");
                return true;
            });
            player.prepareAsync();
        } catch (Exception exception) {
            finish(playerId, player, "error");
        }
    }

    private void finish(String playerId, MediaPlayer player, String event) {
        if (players.get(playerId) != player) return;
        players.remove(playerId);
        player.release();
        emit(playerId, event);
    }

    private void releasePlayer(String playerId) {
        MediaPlayer player = players.remove(playerId);
        if (player != null) player.release();
    }

    private void releaseAll() {
        for (MediaPlayer player : players.values()) player.release();
        players.clear();
    }

    private void emit(String playerId, String type) {
        try {
            String event = new JSONObject()
                    .put("playerId", playerId)
                    .put("type", type)
                    .toString();
            webView.evaluateJavascript(
                    "window.onAionNativeTtsEvent&&window.onAionNativeTtsEvent(" + event + ")",
                    null);
        } catch (Exception ignored) {
        }
    }

    private static boolean validPlayerId(String playerId) {
        return playerId != null && playerId.matches("tts-[a-z0-9-]{3,64}");
    }
}
