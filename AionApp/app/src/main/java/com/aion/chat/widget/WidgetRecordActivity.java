package com.aion.chat.widget;

import android.Manifest;
import android.content.pm.PackageManager;
import android.os.Bundle;
import android.view.MotionEvent;
import android.view.View;
import android.widget.Button;
import android.widget.EditText;
import android.widget.ImageButton;
import android.widget.TextView;

import androidx.annotation.NonNull;
import androidx.appcompat.app.AppCompatActivity;
import androidx.core.content.ContextCompat;

import com.aion.chat.R;

import java.io.File;
import java.io.IOException;

public final class WidgetRecordActivity extends AppCompatActivity {
    private static final int REQUEST_AUDIO = 5102;

    private final WidgetAudioRecorder recorder = new WidgetAudioRecorder();
    private WidgetAsrClient asrClient;
    private TextView status;
    private ImageButton microphone;
    private EditText transcription;
    private View actions;
    private File temporaryWav;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_widget_record);
        setFinishOnTouchOutside(true);

        status = findViewById(R.id.widget_record_status);
        microphone = findViewById(R.id.widget_record_microphone);
        transcription = findViewById(R.id.widget_record_text);
        actions = findViewById(R.id.widget_record_actions);
        Button retry = findViewById(R.id.widget_record_retry);
        Button save = findViewById(R.id.widget_record_save);
        findViewById(R.id.widget_record_close).setOnClickListener(v -> finish());

        asrClient = new WidgetAsrClient(this);
        microphone.setOnTouchListener(this::handleMicrophoneTouch);
        retry.setOnClickListener(v -> resetForRecording());
        save.setOnClickListener(v -> saveMemo());
    }

    private boolean handleMicrophoneTouch(View view, MotionEvent event) {
        if (event.getAction() == MotionEvent.ACTION_DOWN) {
            beginRecording();
            return true;
        }
        if (event.getAction() == MotionEvent.ACTION_UP) {
            if (recorder.isRecording()) finishRecording();
            view.performClick();
            return true;
        }
        if (event.getAction() == MotionEvent.ACTION_CANCEL) {
            recorder.cancel();
            resetForRecording();
            return true;
        }
        return false;
    }

    private void beginRecording() {
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO)
                != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO}, REQUEST_AUDIO);
            return;
        }
        asrClient.cancel();
        deleteTemporaryWav();
        transcription.setVisibility(View.GONE);
        actions.setVisibility(View.GONE);
        try {
            if (recorder.start()) {
                microphone.setPressed(true);
                status.setText(R.string.widget_recording);
            } else {
                status.setText(R.string.widget_record_start_failed);
            }
        } catch (SecurityException e) {
            status.setText(R.string.widget_record_permission_needed);
        }
    }

    private void finishRecording() {
        microphone.setPressed(false);
        microphone.setEnabled(false);
        status.setText(R.string.widget_transcribing);
        temporaryWav = new File(getCacheDir(), "widget-memo.wav");
        try {
            File wav = recorder.stopToWav(temporaryWav);
            if (wav == null) {
                microphone.setEnabled(true);
                status.setText(R.string.widget_record_too_short);
                return;
            }
            asrClient.transcribe(wav, new WidgetAsrClient.Listener() {
                @Override
                public void onText(String text) {
                    runOnUiThread(() -> showTranscription(text));
                }

                @Override
                public void onError(String message) {
                    runOnUiThread(() -> {
                        microphone.setEnabled(true);
                        status.setText(message);
                        actions.setVisibility(View.VISIBLE);
                    });
                }
            });
        } catch (IOException e) {
            microphone.setEnabled(true);
            status.setText(R.string.widget_record_start_failed);
        }
    }

    private void showTranscription(String text) {
        microphone.setEnabled(true);
        status.setText(R.string.widget_transcribe_done);
        transcription.setText(text);
        transcription.setVisibility(View.VISIBLE);
        actions.setVisibility(View.VISIBLE);
        deleteTemporaryWav();
    }

    private void resetForRecording() {
        asrClient.cancel();
        deleteTemporaryWav();
        microphone.setEnabled(true);
        microphone.setPressed(false);
        transcription.setVisibility(View.GONE);
        actions.setVisibility(View.GONE);
        status.setText(R.string.widget_record_hint);
    }

    private void saveMemo() {
        String text = transcription.getText().toString().trim();
        if (text.isEmpty()) {
            status.setText(R.string.widget_record_empty);
            return;
        }
        try (PrivateMemoStore store = new PrivateMemoStore(this)) {
            String source = getIntent().getStringExtra("memo_source");
            store.addPending(text, source == null ? "widget" : source);
            CompanionWidgetProvider.refreshAll(this);
            PrivateMemoSyncClient.sync(this, null);
            finish();
        } catch (RuntimeException e) {
            status.setText(R.string.widget_save_failed);
        }
    }

    private void deleteTemporaryWav() {
        if (temporaryWav != null && temporaryWav.exists()) temporaryWav.delete();
        temporaryWav = null;
    }

    @Override
    protected void onDestroy() {
        recorder.cancel();
        asrClient.cancel();
        deleteTemporaryWav();
        super.onDestroy();
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, @NonNull String[] permissions,
                                           @NonNull int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode == REQUEST_AUDIO) {
            status.setText(grantResults.length > 0
                    && grantResults[0] == PackageManager.PERMISSION_GRANTED
                    ? R.string.widget_record_hint : R.string.widget_record_permission_needed);
        }
    }
}
