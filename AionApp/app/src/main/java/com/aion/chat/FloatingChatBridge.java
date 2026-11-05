package com.aion.chat;

import android.Manifest;
import android.app.Activity;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Build;
import android.provider.Settings;
import android.webkit.JavascriptInterface;
import android.widget.Toast;
import org.json.JSONObject;

public final class FloatingChatBridge {
    private final Activity activity;
    public FloatingChatBridge(Activity activity) { this.activity = activity; }

    @JavascriptInterface public void enable() {
        activity.runOnUiThread(() -> {
            if (!Settings.canDrawOverlays(activity)) {
                activity.startActivity(new Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
                        Uri.parse("package:" + activity.getPackageName())));
                Toast.makeText(activity, "允许悬浮窗后，回来点开启", Toast.LENGTH_LONG).show();
                return;
            }
            if (activity.checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
                activity.requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO}, 7401);
                Toast.makeText(activity, "允许麦克风后，再点开启", Toast.LENGTH_LONG).show();
                return;
            }
            activity.getSharedPreferences("aion_prefs", Activity.MODE_PRIVATE).edit()
                    .putBoolean("floating_chat_enabled", true).apply();
            resume(activity);
        });
    }

    static void resume(Activity activity) {
        if (!activity.getSharedPreferences("aion_prefs", Activity.MODE_PRIVATE)
                .getBoolean("floating_chat_enabled", false)) return;
        if (!Settings.canDrawOverlays(activity)
                || activity.checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) return;
        Intent intent = new Intent(activity, FloatingChatService.class);
        try {
            if (Build.VERSION.SDK_INT >= 26) activity.startForegroundService(intent);
            else activity.startService(intent);
        } catch (RuntimeException error) {
            Toast.makeText(activity, "悬浮聊天启动失败，请保持小家在前台后重试", Toast.LENGTH_LONG).show();
        }
    }

    @JavascriptInterface public void disable() {
        activity.getSharedPreferences("aion_prefs", Activity.MODE_PRIVATE).edit()
                .putBoolean("floating_chat_enabled", false).apply();
        activity.stopService(new Intent(activity, FloatingChatService.class));
    }

    @JavascriptInterface public String getState() {
        return FloatingChatService.isRunning() ? "ready" : "off";
    }

    @JavascriptInterface public void setVisiblePage(String path) {
        activity.runOnUiThread(() -> FloatingChatService.setVisiblePage(path));
    }

    @JavascriptInterface public void rememberOptions(String type, String id, String raw) {
        if ((!"private".equals(type) && !"chatroom".equals(type)) || id == null || id.isEmpty()) return;
        try {
            JSONObject source = new JSONObject(raw), options = new JSONObject();
            for (String key : new String[]{"model", "connor_model", "context_limit", "temperature", "max_tokens",
                    "tts_enabled", "tts_voice", "tts_aion_voice", "tts_connor_voice", "whisper_mode", "fast_mode"}) {
                if (source.has(key)) options.put(key, source.get(key));
            }
            activity.getSharedPreferences("aion_prefs", Activity.MODE_PRIVATE).edit()
                    .putString(FloatingChatClient.optionsKey(type, id), options.toString()).apply();
        } catch (Exception ignored) {}
    }
}
