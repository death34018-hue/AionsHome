package com.aion.chat.supervision;

import android.content.Context;
import android.content.Intent;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.os.Build;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.provider.Settings;
import android.view.Gravity;
import android.view.View;
import android.view.WindowManager;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

import java.text.DateFormat;
import java.util.Collections;
import java.util.Date;
import java.util.LinkedHashMap;
import java.util.Map;

public class AppSupervisionOverlay {
    private final Context context;
    private final WindowManager windowManager;
    private final Handler handler;
    private View root;
    private TextView countdown;
    private TextView commanderValue;
    private TimedDirective visibleLock;
    private boolean visible;
    private Map<String, String> roleLabels = Collections.emptyMap();

    protected AppSupervisionOverlay() {
        context = null;
        windowManager = null;
        handler = null;
    }

    public AppSupervisionOverlay(Context context) {
        this.context = context.getApplicationContext();
        windowManager = (WindowManager) this.context.getSystemService(Context.WINDOW_SERVICE);
        handler = new Handler(Looper.getMainLooper());
    }

    public void evaluate(String packageName, EffectiveState state, AppGroup group,
            AppGroupState.Snapshot snapshot, SupervisionTime now) {
        if (context == null) return;
        if (state != EffectiveState.LOCKED || group == null || snapshot == null
                || snapshot.getLock() == null || !Settings.canDrawOverlays(context)) {
            hide();
            return;
        }
        TimedDirective nextLock = snapshot.getLock();
        boolean attached = visible && root != null && root.isAttachedToWindow();
        String visibleCommandId = visibleLock == null
                ? "" : visibleLock.getCommandId();
        if (shouldReuseVisibleLock(attached, visibleCommandId, nextLock)) {
            visibleLock = nextLock;
            return;
        }
        show(group, nextLock, now);
    }

    public void evaluateDevice(TimedDirective directive, SupervisionTime now) {
        if (context == null) return;
        if (directive == null || !Settings.canDrawOverlays(context)) {
            hide();
            return;
        }
        boolean attached = visible && root != null && root.isAttachedToWindow();
        String visibleCommandId = visibleLock == null
                ? "" : visibleLock.getCommandId();
        if (shouldReuseVisibleLock(attached, visibleCommandId, directive)) {
            visibleLock = directive;
            return;
        }
        showLock("手机专注模式", directive, true);
    }

    public void hide() {
        if (handler != null) handler.removeCallbacksAndMessages(null);
        visibleLock = null;
        if (visible && root != null && windowManager != null) {
            try {
                windowManager.removeView(root);
            } catch (Exception ignored) {
            }
        }
        visible = false;
        root = null;
        countdown = null;
        commanderValue = null;
    }

    public void onScreenOff() {
        hide();
    }

    public void setRoleLabels(Map<String, String> labels) {
        roleLabels = labels == null
                ? Collections.emptyMap()
                : Collections.unmodifiableMap(new LinkedHashMap<>(labels));
        if (commanderValue != null && visibleLock != null) {
            commanderValue.setText(roleLabel(visibleLock.getRoleId()));
        }
    }

    String roleLabel(String roleId) {
        String label = roleLabels.get(roleId);
        return label == null || label.trim().isEmpty() ? "AI" : label;
    }

    static boolean shouldReuseVisibleLock(boolean attached,
            String visibleCommandId, TimedDirective nextLock) {
        return attached && nextLock != null && visibleCommandId != null
                && visibleCommandId.equals(nextLock.getCommandId());
    }

    private void show(AppGroup group, TimedDirective lock, SupervisionTime now) {
        showLock(group.getDisplayName(), lock, false);
    }

    private void showLock(String displayName, TimedDirective lock, boolean deviceLock) {
        hide();
        visibleLock = lock;
        FrameLayout shell = new FrameLayout(context);
        shell.setBackground(gradient(
                GradientDrawable.Orientation.TL_BR,
                new int[]{Color.rgb(5, 11, 30), Color.rgb(18, 25, 66),
                        Color.rgb(42, 18, 82)}, 0));
        shell.setSystemUiVisibility(overlaySystemUiFlags());
        addGlow(shell, Color.argb(70, 91, 119, 255), -120, 120, 430);
        addGlow(shell, Color.argb(55, 174, 86, 255), 740, 1620, 460);

        ScrollView scroll = new ScrollView(context);
        scroll.setFillViewport(true);
        scroll.setVerticalScrollBarEnabled(false);
        LinearLayout page = new LinearLayout(context);
        page.setOrientation(LinearLayout.VERTICAL);
        page.setGravity(Gravity.CENTER);
        page.setPadding(dp(24), dp(44), dp(24), dp(36));
        scroll.addView(page, new ScrollView.LayoutParams(
                ScrollView.LayoutParams.MATCH_PARENT,
                ScrollView.LayoutParams.MATCH_PARENT));
        shell.addView(scroll, new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.MATCH_PARENT));

        TextView status = text(deviceLock ? "手机已进入专注模式" : "应用已锁定",
                13, Color.rgb(184, 203, 255));
        status.setGravity(Gravity.CENTER);
        status.setPadding(dp(16), dp(7), dp(16), dp(7));
        status.setBackground(rounded(Color.argb(120, 52, 67, 129), 999,
                Color.argb(150, 120, 145, 255), 1));
        page.addView(status, centeredWrap(0));

        TextView icon = text("🔒", 46, Color.WHITE);
        icon.setGravity(Gravity.CENTER);
        page.addView(icon, centeredWrap(18));

        TextView title = text(displayName, 31, Color.WHITE);
        title.setTypeface(Typeface.DEFAULT_BOLD);
        title.setGravity(Gravity.CENTER);
        page.addView(title, centeredWrap(4));

        String lockMessage = lock.getMessage() == null ? "" : lock.getMessage().trim();
        if (!lockMessage.isEmpty()) {
            TextView message = text(lockMessage, 17, Color.rgb(218, 226, 255));
            message.setGravity(Gravity.CENTER);
            message.setLineSpacing(dp(3), 1.0f);
            page.addView(message, centeredMatch(16));
        }

        TextView remainingLabel = text("剩余时间", 13, Color.rgb(151, 168, 218));
        remainingLabel.setGravity(Gravity.CENTER);
        page.addView(remainingLabel, centeredWrap(24));

        countdown = text("", 50, Color.rgb(238, 241, 255));
        countdown.setTypeface(Typeface.create("sans-serif-light", Typeface.NORMAL));
        countdown.setLetterSpacing(0.06f);
        countdown.setGravity(Gravity.CENTER);
        page.addView(countdown, centeredMatch(0));

        LinearLayout detailCard = new LinearLayout(context);
        detailCard.setOrientation(LinearLayout.VERTICAL);
        detailCard.setPadding(dp(20), dp(12), dp(20), dp(12));
        detailCard.setBackground(rounded(Color.argb(135, 18, 27, 67), 22,
                Color.argb(105, 132, 151, 230), 1));
        for (Map.Entry<String, String> entry
                : buildDetailRows(lock, roleLabel(lock.getRoleId())).entrySet()) {
            addDetailRow(detailCard, entry.getKey(), entry.getValue());
        }
        page.addView(detailCard, centeredMatch(22));

        String appLabel = context.getString(com.aion.chat.R.string.app_name);
        TextView home = text(deviceLock ? context.getString(
                com.aion.chat.R.string.device_lock_home_button) : "返回桌面",
                17, Color.WHITE);
        home.setTypeface(Typeface.DEFAULT_BOLD);
        home.setGravity(Gravity.CENTER);
        home.setPadding(dp(18), dp(15), dp(18), dp(15));
        home.setBackground(gradient(GradientDrawable.Orientation.LEFT_RIGHT,
                new int[]{Color.rgb(91, 85, 238), Color.rgb(121, 82, 224),
                        Color.rgb(65, 166, 235)}, 18));
        home.setOnClickListener(view -> {
            if (deviceLock) openAionsHome();
            else openHome();
        });
        page.addView(home, centeredMatch(20));

        TextView footer = text(
                deviceLock
                        ? "你可以在 " + appLabel + " 中查看状态、临时解锁或结束专注"
                        : "锁定结束后可重新打开此应用",
                12, Color.rgb(135, 151, 197));
        footer.setGravity(Gravity.CENTER);
        page.addView(footer, centeredWrap(12));

        int type = Build.VERSION.SDK_INT >= Build.VERSION_CODES.O
                ? WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY
                : WindowManager.LayoutParams.TYPE_PHONE;
        WindowManager.LayoutParams params = new WindowManager.LayoutParams(
                WindowManager.LayoutParams.MATCH_PARENT,
                WindowManager.LayoutParams.MATCH_PARENT,
                type,
                WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE
                        | WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN
                        | WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS
                        | WindowManager.LayoutParams.FLAG_FULLSCREEN,
                android.graphics.PixelFormat.OPAQUE);
        params.gravity = Gravity.TOP | Gravity.START;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
            params.layoutInDisplayCutoutMode =
                    WindowManager.LayoutParams.LAYOUT_IN_DISPLAY_CUTOUT_MODE_SHORT_EDGES;
        }
        windowManager.addView(shell, params);
        root = shell;
        visible = true;
        updateCountdown();
    }

    static int overlaySystemUiFlags() {
        return View.SYSTEM_UI_FLAG_LAYOUT_STABLE
                | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
                | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
                | View.SYSTEM_UI_FLAG_FULLSCREEN
                | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                | View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY;
    }

    private void updateCountdown() {
        if (!visible || visibleLock == null || countdown == null) return;
        long remainingMs = Math.max(
                0L, visibleLock.getDeadlineElapsedMs() - SystemClock.elapsedRealtime());
        countdown.setText(formatRemaining(remainingMs));
        if (remainingMs == 0L) {
            hide();
        } else {
            handler.postDelayed(this::updateCountdown, 1_000L);
        }
    }

    private void openHome() {
        Intent intent = new Intent(Intent.ACTION_MAIN);
        intent.addCategory(Intent.CATEGORY_HOME);
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        context.startActivity(intent);
        hide();
    }

    private void openAionsHome() {
        Intent intent = new Intent(context, com.aion.chat.WebViewActivity.class);
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK
                | Intent.FLAG_ACTIVITY_CLEAR_TOP
                | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        context.startActivity(intent);
        hide();
    }

    private TextView text(String value, int sizeSp, int color) {
        TextView view = new TextView(context);
        view.setText(value);
        view.setTextSize(sizeSp);
        view.setTextColor(color);
        return view;
    }

    private void addDetailRow(LinearLayout parent, String label, String value) {
        LinearLayout row = new LinearLayout(context);
        row.setOrientation(LinearLayout.HORIZONTAL);
        row.setGravity(Gravity.CENTER_VERTICAL);
        row.setPadding(0, dp(8), 0, dp(8));
        TextView labelView = text(label, 14, Color.rgb(142, 158, 205));
        TextView valueView = text(value, 14, Color.rgb(235, 239, 255));
        valueView.setGravity(Gravity.END);
        valueView.setTypeface(Typeface.DEFAULT_BOLD);
        row.addView(labelView, new LinearLayout.LayoutParams(0,
                LinearLayout.LayoutParams.WRAP_CONTENT, 1f));
        row.addView(valueView, new LinearLayout.LayoutParams(0,
                LinearLayout.LayoutParams.WRAP_CONTENT, 1.4f));
        if ("下令人".equals(label)) commanderValue = valueView;
        parent.addView(row, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT));
    }

    private void addGlow(FrameLayout parent, int color, int left, int top, int size) {
        View glow = new View(context);
        GradientDrawable drawable = new GradientDrawable();
        drawable.setShape(GradientDrawable.OVAL);
        drawable.setColor(color);
        glow.setBackground(drawable);
        FrameLayout.LayoutParams layout = new FrameLayout.LayoutParams(dp(size), dp(size));
        layout.leftMargin = dp(left);
        layout.topMargin = dp(top);
        parent.addView(glow, layout);
    }

    private GradientDrawable gradient(GradientDrawable.Orientation orientation,
            int[] colors, int radiusDp) {
        GradientDrawable drawable = new GradientDrawable(orientation, colors);
        drawable.setCornerRadius(dp(radiusDp));
        return drawable;
    }

    private GradientDrawable rounded(int color, int radiusDp,
            int strokeColor, int strokeDp) {
        GradientDrawable drawable = new GradientDrawable();
        drawable.setColor(color);
        drawable.setCornerRadius(dp(radiusDp));
        if (strokeDp > 0) drawable.setStroke(dp(strokeDp), strokeColor);
        return drawable;
    }

    private LinearLayout.LayoutParams centeredWrap(int topMarginDp) {
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT,
                LinearLayout.LayoutParams.WRAP_CONTENT);
        params.gravity = Gravity.CENTER_HORIZONTAL;
        params.topMargin = dp(topMarginDp);
        return params;
    }

    private LinearLayout.LayoutParams centeredMatch(int topMarginDp) {
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT);
        params.gravity = Gravity.CENTER_HORIZONTAL;
        params.topMargin = dp(topMarginDp);
        return params;
    }

    private int dp(int value) {
        return Math.round(value * context.getResources().getDisplayMetrics().density);
    }

    private static String formatWall(long wallMs) {
        return DateFormat.getDateTimeInstance(DateFormat.SHORT, DateFormat.SHORT)
                .format(new Date(wallMs));
    }

    static String formatRemaining(long remainingMs) {
        long totalSeconds = (Math.max(0L, remainingMs) + 999L) / 1_000L;
        if (totalSeconds >= 3_600L) {
            return String.format(java.util.Locale.US, "%02d:%02d:%02d",
                    totalSeconds / 3_600L,
                    (totalSeconds % 3_600L) / 60L,
                    totalSeconds % 60L);
        }
        return String.format(java.util.Locale.US, "%02d:%02d",
                totalSeconds / 60L, totalSeconds % 60L);
    }

    static Map<String, String> buildDetailRows(
            TimedDirective lock, String commanderLabel) {
        LinkedHashMap<String, String> rows = new LinkedHashMap<>();
        rows.put("下令人", commanderLabel == null || commanderLabel.trim().isEmpty()
                ? "AI" : commanderLabel.trim());
        rows.put("开始时间", formatWall(lock.getReceivedWallMs()));
        rows.put("结束时间", formatWall(lock.getDeadlineWallMs()));
        rows.put("锁定时长", (lock.getDurationMs() / 60_000L) + " 分钟");
        return rows;
    }
}
