package com.aion.chat.homecoming;

import android.media.MediaPlayer;

import java.util.UUID;

public final class HomecomingAudioPlayer implements HomecomingTtsEngine.Player {
    private final HomecomingMediaCache cache;
    private MediaPlayer current;
    private String currentToken;

    public HomecomingAudioPlayer(HomecomingMediaCache cache) {
        this.cache = cache;
    }

    @Override
    public synchronized void play(byte[] audio, Runnable completion) {
        stop();
        String token = "tts-" + UUID.randomUUID();
        try {
            cache.put(token, audio, System.currentTimeMillis());
            cache.pin(token);
            MediaPlayer player = new MediaPlayer();
            current = player;
            currentToken = token;
            player.setDataSource(cache.fileForPlayback(token).getAbsolutePath());
            player.setOnCompletionListener(ignored -> finish(token, completion));
            player.setOnErrorListener((ignored, what, extra) -> {
                finish(token, completion);
                return true;
            });
            player.prepare();
            player.start();
        } catch (Exception exception) {
            finish(token, completion);
        }
    }

    @Override
    public synchronized void stop() {
        if (current != null) {
            try {
                current.stop();
            } catch (RuntimeException ignored) {
            }
            current.release();
            current = null;
        }
        if (currentToken != null) {
            cache.unpin(currentToken);
            cache.remove(currentToken);
            currentToken = null;
        }
    }

    private synchronized void finish(String token, Runnable completion) {
        if (current != null) {
            current.release();
            current = null;
        }
        cache.unpin(token);
        cache.remove(token);
        if (token.equals(currentToken)) currentToken = null;
        completion.run();
    }
}
