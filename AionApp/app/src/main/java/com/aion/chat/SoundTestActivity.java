package com.aion.chat;

import android.Manifest;
import android.app.Activity;
import android.content.pm.PackageManager;
import android.content.res.ColorStateList;
import android.graphics.Color;
import android.media.AudioFormat;
import android.media.AudioManager;
import android.media.AudioRecord;
import android.media.MediaRecorder;
import android.os.Bundle;
import android.os.SystemClock;
import android.util.Log;
import android.view.View;
import android.view.WindowManager;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.ScrollView;
import android.widget.TextView;
import java.util.Locale;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/** Foreground-only listening trial, independent of chat and ASR sessions. */
public final class SoundTestActivity extends Activity {
    private static final int REQUEST_MIC = 7410;
    private final ExecutorService worker = Executors.newSingleThreadExecutor();
    private final TextView[] scoreText = new TextView[4];
    private final ProgressBar[] bars = new ProgressBar[4];
    private final String[] names = {"说话", "唱歌", "哼唱", "鼾声"};
    private TextView state, headline, details, timing;
    private Button toggle;
    private boolean resumed;
    private boolean requested;
    private final TtsPlaybackCoordinator.Listener ttsListener = speaking -> {
        if (!requested) return;
        if (speaking) {
            stopListening();
            state.setText("TTS 播放中，暂停试听");
            toggle.setText("停止试听");
        } else {
            toggle.postDelayed(() -> {
                if (resumed && requested && !TtsPlaybackCoordinator.shared.isSpeaking()) startListening();
            }, 1200);
        }
    };
    private volatile Session active;

    private static final class Session {
        volatile boolean stopped;
        AudioRecord recorder; // Access under the session lock; the worker owns release.
    }

    @Override public void onCreate(Bundle saved) {
        super.onCreate(saved);
        getWindow().setStatusBarColor(Color.rgb(247, 244, 250));
        getWindow().getDecorView().setSystemUiVisibility(View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR);
        ScrollView scroll = new ScrollView(this);
        scroll.setFillViewport(true);
        scroll.setBackgroundColor(Color.rgb(247, 244, 250));
        LinearLayout body = new LinearLayout(this);
        body.setOrientation(LinearLayout.VERTICAL);
        body.setPadding(dp(24), dp(20), dp(24), dp(28));
        scroll.addView(body);
        Button back = new Button(this);
        back.setText("返回");
        back.setOnClickListener(v -> finish());
        body.addView(back, new LinearLayout.LayoutParams(dp(84), dp(48)));
        addText(body, "声音识别", 28, 24);
        addText(body, "本地试听 · 离开此页即停止收音", 14, 8);
        state = addText(body, "准备好了", 15, 28);
        headline = addText(body, "听听现在的声音", 27, 8);
        addText(body, "模型分数", 14, 28);
        for (int i = 0; i < names.length; i++) {
            scoreText[i] = addText(body, names[i] + "   —", 18, 18);
            bars[i] = new ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal);
            bars[i].setMax(1000);
            bars[i].setProgressTintList(ColorStateList.valueOf(Color.rgb(121, 101, 151)));
            LinearLayout.LayoutParams p = new LinearLayout.LayoutParams(-1, dp(8));
            p.topMargin = dp(8);
            body.addView(bars[i], p);
        }
        details = addText(body, "最高分的环境声音会显示在这里", 15, 28);
        timing = addText(body, "", 13, 12);
        toggle = new Button(this);
        toggle.setText("开始试听");
        toggle.setOnClickListener(v -> {
            if (requested) { requested = false; stopListening(); return; }
            requested = true;
            if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
                requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO}, REQUEST_MIC);
            } else startListening();
        });
        LinearLayout.LayoutParams p = new LinearLayout.LayoutParams(-1, dp(56));
        p.topMargin = dp(28);
        body.addView(toggle, p);
        setContentView(scroll);
    }

    private TextView addText(LinearLayout parent, String text, int size, int margin) {
        TextView view = new TextView(this);
        view.setText(text);
        view.setTextSize(size);
        view.setTextColor(Color.rgb(56, 46, 65));
        LinearLayout.LayoutParams p = new LinearLayout.LayoutParams(-1, -2);
        p.topMargin = dp(margin);
        parent.addView(view, p);
        return view;
    }

    private int dp(int value) { return Math.round(value * getResources().getDisplayMetrics().density); }

    private void startListening() {
        if (!resumed || active != null) return;
        requested = true;
        if (TtsPlaybackCoordinator.shared.isSpeaking()) {
            state.setText("TTS 播放中，暂停试听");
            toggle.setText("停止试听");
            return;
        }
        Session session = new Session();
        active = session;
        state.setText("正在加载模型…");
        headline.setText("等一小会儿");
        toggle.setText("停止试听");
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        worker.execute(() -> listen(session));
    }

    private void listen(Session session) {
        String failure = null;
        try (YamnetClassifier model = new YamnetClassifier(getApplicationContext())) {
            short[] pcm = new short[YamnetClassifier.WINDOW];
            // Exercise the exact model/runtime before opening the microphone.
            float[] warmup = model.classify(pcm);
            for (float score : warmup) if (!Float.isFinite(score)) throw new IllegalStateException("模型输出异常");
            Log.i("AionSoundTest", "YAMNet ready: 15600 samples, 521 scores");
            if (session.stopped) return;
            AudioManager manager = (AudioManager) getSystemService(AUDIO_SERVICE);
            int source = "true".equals(manager.getProperty(AudioManager.PROPERTY_SUPPORT_AUDIO_SOURCE_UNPROCESSED))
                    ? MediaRecorder.AudioSource.UNPROCESSED : MediaRecorder.AudioSource.VOICE_RECOGNITION;
            int minBuffer = AudioRecord.getMinBufferSize(YamnetClassifier.SAMPLE_RATE,
                    AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT);
            synchronized (session) {
                if (session.stopped) return;
                session.recorder = new AudioRecord(source, YamnetClassifier.SAMPLE_RATE,
                        AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT,
                        Math.max(minBuffer, YamnetClassifier.WINDOW * 8));
                if (session.recorder.getState() != AudioRecord.STATE_INITIALIZED)
                    throw new IllegalStateException("麦克风不可用，请先结束其他录音或通话");
                session.recorder.startRecording();
            }
            AudioRecord recorder = session.recorder;
            int filled = 0;
            long frames = 0;
            while (!session.stopped) {
                int read = recorder.read(pcm, filled, pcm.length - filled, AudioRecord.READ_BLOCKING);
                if (session.stopped) break;
                if (read <= 0) throw new IllegalStateException("麦克风已中断，请重新开始试听");
                filled += read;
                if (filled < pcm.length) continue;
                long before = SystemClock.elapsedRealtimeNanos();
                float[] scores = model.classify(pcm);
                double elapsed = (SystemClock.elapsedRealtimeNanos() - before) / 1_000_000.0;
                int[] top = SoundScores.top(scores, 3);
                String winner = scores[top[0]] >= 0.25f ? model.label(top[0]) : "暂不确定";
                StringBuilder ranked = new StringBuilder();
                for (int index : top) {
                    if (ranked.length() > 0) ranked.append('\n');
                    ranked.append(String.format(Locale.CHINA, "%s  %.1f%%", model.label(index), scores[index] * 100));
                }
                double square = 0;
                for (short sample : pcm) square += (double) sample * sample;
                double db = 20 * Math.log10(Math.max(1.0, Math.sqrt(square / pcm.length)) / 32768.0);
                String metrics = String.format(Locale.CHINA, "输入 %.0f dBFS · 推理 %.0f ms · %d 帧", db, elapsed, ++frames);
                float[] focus = SoundScores.focus(scores);
                String ranking = ranked.toString();
                runOnUiThread(() -> {
                    if (active != session || session.stopped || TtsPlaybackCoordinator.shared.isSpeaking()) return;
                    state.setText("正在听");
                    headline.setText(winner);
                    for (int i = 0; i < names.length; i++) {
                        scoreText[i].setText(String.format(Locale.CHINA, "%s   %.1f%%", names[i], focus[i] * 100));
                        bars[i].setProgress(Math.round(focus[i] * 1000));
                    }
                    details.setText(ranking);
                    timing.setText(metrics);
                });
                System.arraycopy(pcm, YamnetClassifier.HOP, pcm, 0, pcm.length - YamnetClassifier.HOP);
                filled = pcm.length - YamnetClassifier.HOP;
            }
        } catch (Exception | LinkageError error) {
            failure = error.getMessage();
            Log.e("AionSoundTest", "Listening failed", error);
        } finally {
            synchronized (session) {
                if (session.recorder != null) {
                    try { session.recorder.stop(); } catch (IllegalStateException ignored) { }
                    session.recorder.release();
                    session.recorder = null;
                }
            }
            String message = failure;
            runOnUiThread(() -> {
                if (active != session) return;
                active = null;
                requested = false;
                getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
                toggle.setText("开始试听");
                state.setText(message == null ? "已停止" : "试听未能开始");
                if (message != null) headline.setText(message);
            });
        }
    }

    private void stopListening() {
        Session session = active;
        active = null;
        if (session != null) {
            session.stopped = true;
            synchronized (session) {
                if (session.recorder != null) {
                    try { session.recorder.stop(); } catch (IllegalStateException ignored) { }
                }
            }
        }
        getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        toggle.setText("开始试听");
        state.setText("已停止");
    }

    @Override public void onRequestPermissionsResult(int request, String[] permissions, int[] grants) {
        super.onRequestPermissionsResult(request, permissions, grants);
        if (request != REQUEST_MIC) return;
        boolean granted = grants.length > 0 && grants[0] == PackageManager.PERMISSION_GRANTED;
        state.setText(granted ? "权限已允许，点开始试听" : "需要麦克风权限才能试听");
        if (granted && resumed) startListening();
    }

    @Override protected void onResume() {
        super.onResume(); resumed = true;
        TtsPlaybackCoordinator.shared.addListener(ttsListener);
    }
    @Override protected void onPause() {
        resumed = false; requested = false;
        TtsPlaybackCoordinator.shared.removeListener(ttsListener);
        stopListening(); super.onPause();
    }
    @Override protected void onDestroy() { worker.shutdown(); super.onDestroy(); }
}
