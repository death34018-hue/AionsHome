package com.aion.chat;

import android.Manifest;
import android.app.*;
import android.content.*;
import android.content.pm.PackageManager;
import android.content.pm.ServiceInfo;
import android.graphics.*;
import android.graphics.drawable.GradientDrawable;
import android.os.*;
import android.provider.Settings;
import android.text.Editable;
import android.text.InputType;
import android.text.TextWatcher;
import android.view.*;
import android.view.inputmethod.EditorInfo;
import android.view.inputmethod.InputMethodManager;
import android.widget.*;
import com.aion.chat.widget.WidgetAudioRecorder;
import com.aion.chat.widget.WidgetAsrClient;
import org.json.JSONObject;
import org.json.JSONArray;
import java.io.File;
import java.util.HashMap;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/** A small native window; recording is armed while the Activity is visible. */
public final class FloatingChatService extends Service {
    private static final String STOP = "floating_chat_stop";
    private static volatile FloatingChatService instance;
    private static volatile boolean appForeground = true;
    private static volatile String visiblePage = "/chat";
    private final Handler main = new Handler(Looper.getMainLooper());
    private final ExecutorService events = Executors.newSingleThreadExecutor();
    private final FloatingChatSession session = new FloatingChatSession();
    private final FloatingChatHistory history = new FloatingChatHistory();
    private final WidgetAudioRecorder recorder = new WidgetAudioRecorder();
    private final HashMap<String, Bitmap> avatars = new HashMap<>();
    private final java.util.ArrayDeque<PendingMessage> pending = new java.util.ArrayDeque<>();
    private WindowManager windows;
    private WindowManager.LayoutParams position;
    private LinearLayout root, controls, composer;
    private MessageScrollView messageScroll;
    private TextView bubble, content, sendButton, messageStatus;
    private JSONObject latestContext;
    private EditText replyInput;
    private ImageView microphone, together;
    private boolean shareScreen, touching;
    private FloatingChatClient api;
    private WidgetAsrClient asr;
    private String messageTarget = "", lastMessage = "按住麦克风说话 · 上滑取消";
    private boolean cardVisible, readerExpanded, busy, snapshotHidden;
    private boolean editing;
    private int keyboardHeight;
    private volatile boolean alive;
    private float pressY;
    private boolean cancelGesture, recordingWithScreen;
    private File audioFile;

    private static final class PendingMessage {
        final String type;
        final JSONObject data;
        final boolean allowPreview;
        PendingMessage(String type, JSONObject data) {
            this.type = type;
            this.data = data;
            this.allowPreview = !suppressesMessagePreview();
        }
    }

    public static boolean isRunning() { return instance != null; }

    public static boolean acceptsScreenRequest(JSONObject data) {
        FloatingChatService active = instance;
        return active != null && active.alive && active.api.acceptsScreenRequest(data);
    }

    public static void setAppForeground(boolean foreground) {
        boolean wasSuppressed = suppressesMessagePreview();
        appForeground = foreground;
        updatePageVisibility(wasSuppressed);
    }

    public static void setVisiblePage(String path) {
        boolean wasSuppressed = suppressesMessagePreview();
        visiblePage = path == null ? "" : path;
        updatePageVisibility(wasSuppressed);
    }

    static boolean suppressesMessagePreview() {
        return appForeground && ("/chat".equals(visiblePage) || "/chat/".equals(visiblePage)
                || "/chatroom".equals(visiblePage) || "/chatroom/".equals(visiblePage));
    }

    private static void updatePageVisibility(boolean wasSuppressed) {
        FloatingChatService active = instance;
        if (active != null) active.main.post(() -> {
            if (!active.alive) return;
            if (!wasSuppressed && suppressesMessagePreview()) {
                active.cancelRecording();
                active.dismissCard();
            }
            active.updateVisibility();
        });
    }

    public static void deliver(String type, JSONObject data) {
        FloatingChatService active = instance;
        if (active == null || data == null || !active.alive) return;
        // Remember arrival-time visibility too: a slow context/avatar request must
        // not pop up a message already seen in chat after the user leaves it.
        PendingMessage message = new PendingMessage(type, data);
        try { active.events.execute(() -> {
            active.pending.addLast(message);
            active.drainPending();
        }); }
        catch (java.util.concurrent.RejectedExecutionException ignored) {}
    }

    private final BroadcastReceiver screenReceiver = new BroadcastReceiver() {
        @Override public void onReceive(Context c, Intent intent) {
            if (Intent.ACTION_SCREEN_OFF.equals(intent.getAction())) { cancelRecording(); dismissCard(); }
            updateVisibility();
        }
    };

    @Override public void onCreate() {
        super.onCreate();
        if (!Settings.canDrawOverlays(this)
                || checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            stopSelf();
            return;
        }
        try {
            String channel = "floating_chat";
            NotificationManager notifications = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
            if (Build.VERSION.SDK_INT >= 26) notifications.createNotificationChannel(
                    new NotificationChannel(channel, "悬浮聊天", NotificationManager.IMPORTANCE_LOW));
            PendingIntent stop = PendingIntent.getService(this, 7402,
                    new Intent(this, FloatingChatService.class).setAction(STOP),
                    PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
            PendingIntent open = PendingIntent.getActivity(this, 7403,
                    new Intent(this, WebViewActivity.class), PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
            Notification.Builder builder = Build.VERSION.SDK_INT >= 26
                    ? new Notification.Builder(this, channel) : new Notification.Builder(this);
            Notification notification = builder.setSmallIcon(R.mipmap.ic_launcher)
                    .setContentTitle("悬浮聊天已开启").setContentText("按住说话，回复最新活跃窗口")
                    .setContentIntent(open).addAction(android.R.drawable.ic_menu_close_clear_cancel, "关闭", stop)
                    .setOngoing(true).build();
            if (Build.VERSION.SDK_INT >= 30) startForeground(7402, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE);
            else startForeground(7402, notification);
        } catch (RuntimeException error) {
            Toast.makeText(this, "请在小家前台重新开启悬浮聊天", Toast.LENGTH_LONG).show();
            stopSelf();
            return;
        }
        api = new FloatingChatClient(this);
        asr = new WidgetAsrClient(this);
        windows = (WindowManager) getSystemService(WINDOW_SERVICE);
        alive = true;
        buildWindow();
        IntentFilter filter = new IntentFilter(Intent.ACTION_SCREEN_OFF);
        filter.addAction(Intent.ACTION_USER_PRESENT);
        if (Build.VERSION.SDK_INT >= 33) registerReceiver(screenReceiver, filter, Context.RECEIVER_NOT_EXPORTED);
        else registerReceiver(screenReceiver, filter);
        instance = this;
        refreshContext();
    }

    @Override public int onStartCommand(Intent intent, int flags, int id) {
        if (intent != null && STOP.equals(intent.getAction())) {
            getSharedPreferences("aion_prefs", MODE_PRIVATE).edit().putBoolean("floating_chat_enabled", false).apply();
            stopSelf();
        }
        updateVisibility();
        // A killed microphone service is re-armed on the next visible App visit.
        return START_NOT_STICKY;
    }

    private int dp(int value) { return Math.round(value * getResources().getDisplayMetrics().density); }

    private TextView text(String content, int size, int color) {
        TextView view = new TextView(this);
        view.setText(content);
        view.setTextSize(size);
        view.setTextColor(color);
        return view;
    }

    private GradientDrawable background(int color, int radius) {
        GradientDrawable drawable = new GradientDrawable();
        drawable.setColor(color);
        drawable.setCornerRadius(dp(radius));
        return drawable;
    }

    /** Small icon, without a platform button's minimum size or opaque inset. */
    private static final class MicIcon extends android.graphics.drawable.Drawable {
        private final Paint paint = new Paint(Paint.ANTI_ALIAS_FLAG);
        @Override public void draw(Canvas canvas) {
            canvas.save();
            canvas.translate(getBounds().left, getBounds().top);
            canvas.scale(getBounds().width() / 24f, getBounds().height() / 24f);
            paint.setColor(Color.WHITE);
            paint.setStyle(Paint.Style.FILL);
            canvas.drawRoundRect(9, 3, 15, 14, 3, 3, paint);
            paint.setStyle(Paint.Style.STROKE);
            paint.setStrokeWidth(1.8f);
            paint.setStrokeCap(Paint.Cap.ROUND);
            canvas.drawArc(6, 7, 18, 18, 0, 180, false, paint);
            canvas.drawLine(12, 18, 12, 21, paint);
            canvas.drawLine(9, 21, 15, 21, paint);
            canvas.restore();
        }
        @Override public void setAlpha(int alpha) { paint.setAlpha(alpha); }
        @Override public void setColorFilter(ColorFilter filter) { paint.setColorFilter(filter); }
        @Override public int getOpacity() { return PixelFormat.TRANSLUCENT; }
    }

    /** Screen-sharing switch: an outlined screen when off, a mint screen when on. */
    private static final class ScreenIcon extends android.graphics.drawable.Drawable {
        private final Paint paint = new Paint(Paint.ANTI_ALIAS_FLAG);
        private final int color;
        ScreenIcon(int color) { this.color = color; }
        @Override public void draw(Canvas canvas) {
            canvas.save();
            canvas.translate(getBounds().left, getBounds().top);
            canvas.scale(getBounds().width() / 24f, getBounds().height() / 24f);
            paint.setColor(color);
            paint.setStyle(Paint.Style.STROKE);
            paint.setStrokeWidth(1.8f);
            paint.setStrokeCap(Paint.Cap.ROUND);
            canvas.drawRoundRect(3, 4, 21, 17, 2, 2, paint);
            canvas.drawLine(12, 17, 12, 21, paint);
            canvas.drawLine(8, 21, 16, 21, paint);
            // A small eye distinguishes this from a button that opens the app.
            Path eye = new Path();
            eye.moveTo(7, 10.5f);
            eye.quadTo(12, 5.5f, 17, 10.5f);
            eye.quadTo(12, 15.5f, 7, 10.5f);
            canvas.drawPath(eye, paint);
            paint.setStyle(Paint.Style.FILL);
            canvas.drawCircle(12, 10.5f, 1.3f, paint);
            canvas.restore();
        }
        @Override public void setAlpha(int alpha) { paint.setAlpha(alpha); }
        @Override public void setColorFilter(ColorFilter filter) { paint.setColorFilter(filter); }
        @Override public int getOpacity() { return PixelFormat.TRANSLUCENT; }
    }

    /** Fit short messages, while retaining a bounded scrolling area for long ones. */
    private static final class MessageScrollView extends ScrollView {
        int maxHeight;
        MessageScrollView(Context context) { super(context); }
        @Override protected void onMeasure(int widthSpec, int heightSpec) {
            if (maxHeight > 0) {
                int available = View.MeasureSpec.getMode(heightSpec) == View.MeasureSpec.UNSPECIFIED
                        ? maxHeight : Math.min(maxHeight, View.MeasureSpec.getSize(heightSpec));
                heightSpec = View.MeasureSpec.makeMeasureSpec(available, View.MeasureSpec.AT_MOST);
            }
            super.onMeasure(widthSpec, heightSpec);
        }
    }

    private void buildWindow() {
        root = new LinearLayout(this) {
            @Override public boolean dispatchKeyEvent(KeyEvent event) {
                if (editing && event.getKeyCode() == KeyEvent.KEYCODE_BACK) {
                    if (event.getAction() == KeyEvent.ACTION_UP) setInputActive(false);
                    return true;
                }
                return super.dispatchKeyEvent(event);
            }
            @Override public boolean dispatchTouchEvent(MotionEvent event) {
                int action = event.getActionMasked();
                if (action == MotionEvent.ACTION_DOWN) {
                    touching = true;
                    bubble.setAlpha(1f);
                    main.removeCallbacks(hideCard);
                }
                boolean handled = super.dispatchTouchEvent(event);
                if (action == MotionEvent.ACTION_UP || action == MotionEvent.ACTION_CANCEL) {
                    touching = false;
                    if (!cardVisible) bubble.setAlpha(0.2f);
                    scheduleHide();
                }
                return handled;
            }
        };
        root.setOrientation(LinearLayout.VERTICAL);
        root.setGravity(Gravity.TOP);
        root.setFocusableInTouchMode(true);
        root.setPadding(dp(8), dp(8), dp(8), dp(8));
        root.setBackground(background(Color.argb(175, 38, 34, 43), 20));
        // Consume taps on the card itself, while keeping everything outside it usable.
        root.setOnClickListener(view -> {});
        LinearLayout messageRow = new LinearLayout(this);
        messageRow.setOrientation(LinearLayout.HORIZONTAL);
        messageRow.setGravity(Gravity.TOP);
        root.addView(messageRow, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
        bubble = text("聊", 14, Color.WHITE);
        bubble.setGravity(Gravity.CENTER);
        bubble.setBackground(background(Color.argb(100, 45, 40, 49), 18));
        bubble.setClipToOutline(true);
        bubble.setContentDescription("悬浮聊天，点按打开或收起，拖动移动");
        LinearLayout leftColumn = new LinearLayout(this);
        leftColumn.setOrientation(LinearLayout.VERTICAL);
        leftColumn.setGravity(Gravity.CENTER_HORIZONTAL);
        messageRow.addView(leftColumn, new LinearLayout.LayoutParams(dp(36), ViewGroup.LayoutParams.WRAP_CONTENT));
        leftColumn.addView(bubble, new LinearLayout.LayoutParams(dp(36), dp(36)));
        bubble.setOnTouchListener(new View.OnTouchListener() {
            float x, y; int startX, startY; boolean moved;
            @Override public boolean onTouch(View view, MotionEvent event) {
                switch (event.getActionMasked()) {
                    case MotionEvent.ACTION_DOWN:
                        x = event.getRawX(); y = event.getRawY();
                        startX = position.x; startY = position.y; moved = false;
                        return true;
                    case MotionEvent.ACTION_MOVE:
                        float dx = event.getRawX() - x, dy = event.getRawY() - y;
                        moved |= Math.abs(dx) + Math.abs(dy) > dp(8);
                        if (moved) {
                            position.x = Math.max(0, Math.min(getResources().getDisplayMetrics().widthPixels - root.getWidth(), startX + (int) dx));
                            position.y = Math.max(dp(24), Math.min(getResources().getDisplayMetrics().heightPixels - root.getHeight() - dp(24), startY + (int) dy));
                            if (root.isAttachedToWindow()) windows.updateViewLayout(root, position);
                        }
                        return true;
                    case MotionEvent.ACTION_UP:
                        if (!moved && !recorder.isRecording()) {
                            if (cardVisible) dismissCard();
                            else { showCard(); refreshContext(); }
                        }
                        return true;
                    default: return true;
                }
            }
        });
        content = text(lastMessage, 13, Color.WHITE);
        // The default preview stays small; tapping its text opens the full message.
        content.setLines(5);
        content.setLineSpacing(dp(2), 1f);
        content.setEllipsize(android.text.TextUtils.TruncateAt.END);
        content.setIncludeFontPadding(false);
        content.setPadding(dp(8), 0, dp(8), 0);
        messageScroll = new MessageScrollView(this);
        messageScroll.setFillViewport(false);
        messageScroll.setVerticalScrollBarEnabled(false);
        messageScroll.setOverScrollMode(View.OVER_SCROLL_IF_CONTENT_SCROLLS);
        messageScroll.addView(content, new ScrollView.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
        messageRow.addView(messageScroll, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1));
        content.setOnClickListener(view -> {
            if (!readerExpanded && !recorder.isRecording()) {
                readerExpanded = true;
                main.removeCallbacks(hideCard);
                renderMessage(true);
                layoutCard();
                refreshHistory();
            }
        });
        controls = new LinearLayout(this);
        controls.setOrientation(LinearLayout.VERTICAL);
        controls.setGravity(Gravity.CENTER);
        LinearLayout.LayoutParams controlsParams = new LinearLayout.LayoutParams(dp(32), ViewGroup.LayoutParams.WRAP_CONTENT);
        controlsParams.topMargin = dp(6);
        leftColumn.addView(controls, controlsParams);
        together = new ImageView(this);
        together.setPadding(dp(6), dp(6), dp(6), dp(6));
        shareScreen = getSharedPreferences("aion_prefs", MODE_PRIVATE).getBoolean("floating_chat_share_screen", false);
        updateScreenButton();
        together.setOnClickListener(view -> {
            shareScreen = !shareScreen;
            getSharedPreferences("aion_prefs", MODE_PRIVATE).edit().putBoolean("floating_chat_share_screen", shareScreen).apply();
            updateScreenButton();
        });
        controls.addView(together, new LinearLayout.LayoutParams(dp(32), dp(32)));
        microphone = new ImageView(this);
        microphone.setImageDrawable(new MicIcon());
        microphone.setPadding(dp(7), dp(7), dp(7), dp(7));
        microphone.setContentDescription("按住说话，松开发送，上滑取消");
        microphone.setBackground(background(Color.argb(70, 225, 214, 235), 16));
        LinearLayout.LayoutParams micParams = new LinearLayout.LayoutParams(dp(32), dp(32));
        micParams.topMargin = dp(6);
        controls.addView(microphone, micParams);
        microphone.setOnTouchListener((view, event) -> {
            if (busy) return true;
            switch (event.getActionMasked()) {
                case MotionEvent.ACTION_DOWN:
                    setInputActive(false);
                    pressY = event.getRawY(); cancelGesture = false;
                    recordingWithScreen = shareScreen;
                    try {
                        if (recorder.start()) {
                            main.removeCallbacks(hideCard);
                            together.setEnabled(false);
                            updateComposer();
                            // Recording feedback must not shift the button under the held finger.
                            messageScroll.setMinimumHeight(messageScroll.getHeight());
                            showStatus("正在听… 松开发送 · 上滑取消");
                            microphone.setBackground(background(Color.argb(180, 116, 98, 137), 16));
                            main.postDelayed(recordingTimeout, 60000);
                        } else toast("麦克风暂时不可用");
                    } catch (RuntimeException error) { cancelRecording(); toast("录音失败，请检查麦克风权限"); }
                    return true;
                case MotionEvent.ACTION_MOVE:
                    cancelGesture = pressY - event.getRawY() > dp(60);
                    if (recorder.isRecording()) showStatus(cancelGesture ? "松开取消" : "正在听… 松开发送 · 上滑取消");
                    return true;
                case MotionEvent.ACTION_UP:
                    if (!recorder.isRecording()) return true;
                    main.removeCallbacks(recordingTimeout);
                    if (cancelGesture) cancelRecording(); else finishRecording();
                    return true;
                case MotionEvent.ACTION_CANCEL: cancelRecording(); return true;
                default: return true;
            }
        });
        buildComposer();
        position = new WindowManager.LayoutParams(Math.min(dp(280), getResources().getDisplayMetrics().widthPixels - dp(24)),
                WindowManager.LayoutParams.WRAP_CONTENT,
                Build.VERSION.SDK_INT >= 26 ? WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY : WindowManager.LayoutParams.TYPE_PHONE,
                WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE | WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL,
                PixelFormat.TRANSLUCENT);
        position.gravity = Gravity.TOP | Gravity.LEFT;
        position.softInputMode = WindowManager.LayoutParams.SOFT_INPUT_ADJUST_RESIZE
                | WindowManager.LayoutParams.SOFT_INPUT_STATE_ALWAYS_HIDDEN;
        position.x = dp(8); position.y = dp(220);
        root.getViewTreeObserver().addOnGlobalLayoutListener(() -> {
            if (!alive || snapshotHidden) return;
            Rect visible = new Rect();
            root.getWindowVisibleDisplayFrame(visible);
            int hidden = getResources().getDisplayMetrics().heightPixels - visible.bottom;
            int height = editing && hidden > dp(100) ? hidden : 0;
            if (keyboardHeight != height) {
                keyboardHeight = height;
                layoutCard();
            }
        });
        layoutCard();
    }

    private void buildComposer() {
        messageStatus = text("", 11, Color.argb(185, 235, 225, 242));
        messageStatus.setMaxLines(2);
        messageStatus.setVisibility(View.GONE);
        LinearLayout.LayoutParams statusParams = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        statusParams.topMargin = dp(6);
        root.addView(messageStatus, statusParams);
        composer = new LinearLayout(this);
        composer.setGravity(Gravity.CENTER_VERTICAL);
        LinearLayout.LayoutParams rowParams = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        rowParams.topMargin = dp(8);
        root.addView(composer, rowParams);
        replyInput = new EditText(this) {
            @Override public void onWindowFocusChanged(boolean hasFocus) {
                super.onWindowFocusChanged(hasFocus);
                if (hasFocus && editing) post(() -> showReplyKeyboard());
                else if (!hasFocus && editing) main.post(() -> { if (alive) setInputActive(false); });
            }
            @Override public boolean onKeyPreIme(int keyCode, KeyEvent event) {
                if (editing && keyCode == KeyEvent.KEYCODE_BACK) {
                    if (event.getAction() == KeyEvent.ACTION_UP) setInputActive(false);
                    return true;
                }
                return super.onKeyPreIme(keyCode, event);
            }
        };
        replyInput.setHint("输入消息…");
        replyInput.setContentDescription("回复最新活跃窗口");
        replyInput.setTextSize(13);
        replyInput.setTextColor(Color.WHITE);
        replyInput.setHintTextColor(Color.argb(155, 235, 225, 242));
        replyInput.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_FLAG_MULTI_LINE
                | InputType.TYPE_TEXT_FLAG_CAP_SENTENCES);
        replyInput.setImeOptions(EditorInfo.IME_ACTION_SEND | EditorInfo.IME_FLAG_NO_EXTRACT_UI);
        replyInput.setMinLines(1);
        replyInput.setMaxLines(3);
        replyInput.setMinHeight(dp(38));
        replyInput.setPadding(dp(10), dp(8), dp(10), dp(8));
        replyInput.setBackground(background(Color.argb(65, 225, 214, 235), 12));
        composer.addView(replyInput, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1));
        replyInput.setOnTouchListener((view, event) -> {
            if (event.getActionMasked() == MotionEvent.ACTION_DOWN) {
                setInputActive(true);
                replyInput.requestFocus();
                replyInput.post(this::showReplyKeyboard);
            }
            return false;
        });
        replyInput.setOnEditorActionListener((view, action, event) -> {
            if (action != EditorInfo.IME_ACTION_SEND) return false;
            sendTypedReply();
            return true;
        });
        replyInput.addTextChangedListener(new TextWatcher() {
            @Override public void beforeTextChanged(CharSequence s, int start, int count, int after) {}
            @Override public void onTextChanged(CharSequence s, int start, int before, int count) { updateComposer(); }
            @Override public void afterTextChanged(Editable text) {}
        });
        sendButton = text("发送", 13, Color.rgb(190, 245, 225));
        sendButton.setGravity(Gravity.CENTER);
        sendButton.setBackground(background(Color.argb(135, 45, 118, 96), 12));
        LinearLayout.LayoutParams sendParams = new LinearLayout.LayoutParams(dp(48), dp(38));
        sendParams.leftMargin = dp(6);
        composer.addView(sendButton, sendParams);
        sendButton.setOnClickListener(view -> sendTypedReply());
        updateComposer();
    }

    private void showReplyKeyboard() {
        if (!alive || !editing || !replyInput.hasWindowFocus()) return;
        ((InputMethodManager) getSystemService(INPUT_METHOD_SERVICE)).showSoftInput(replyInput, InputMethodManager.SHOW_IMPLICIT);
    }

    private void setInputActive(boolean active) {
        if (position == null || replyInput == null || editing == active) return;
        editing = active;
        if (active) position.flags &= ~WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE;
        else {
            ((InputMethodManager) getSystemService(INPUT_METHOD_SERVICE)).hideSoftInputFromWindow(replyInput.getWindowToken(), 0);
            replyInput.clearFocus();
            root.requestFocus();
            keyboardHeight = 0;
            position.flags |= WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE;
        }
        layoutCard();
    }

    private void updateComposer() {
        if (sendButton == null) return;
        replyInput.setEnabled(!recorder.isRecording());
        boolean ready = !busy && !recorder.isRecording() && !replyInput.getText().toString().trim().isEmpty();
        sendButton.setEnabled(ready);
        sendButton.setAlpha(ready ? 1f : 0.4f);
    }

    private void sendTypedReply() {
        if (busy || recorder.isRecording()) return;
        String draft = replyInput.getText().toString();
        if (draft.trim().isEmpty()) return;
        setInputActive(false);
        send(draft.trim(), shareScreen, draft);
    }

    private void updateScreenButton() {
        together.setSelected(shareScreen);
        together.setImageDrawable(new ScreenIcon(shareScreen ? Color.rgb(156, 237, 211) : Color.rgb(157, 157, 164)));
        together.setBackground(background(shareScreen ? Color.argb(135, 45, 118, 96) : Color.argb(35, 150, 145, 156), 16));
        together.setContentDescription(shareScreen ? "一起看已开启，点按关闭截图" : "一起看已关闭，点按随回复附截图");
    }

    private final Runnable recordingTimeout = () -> { cancelRecording(); toast("录音已到一分钟，请分段发送"); };
    private final Runnable hideCard = () -> { if (!touching && !recorder.isRecording()) dismissCard(); };

    private void scheduleHide() {
        main.removeCallbacks(hideCard);
        if (cardVisible && !readerExpanded && !touching && !recorder.isRecording()) main.postDelayed(hideCard, 5000);
    }

    private void dismissCard() {
        setInputActive(false);
        cardVisible = false;
        readerExpanded = false;
        touching = false;
        main.removeCallbacks(hideCard);
        renderMessage(false);
        layoutCard();
        updateVisibility();
    }

    private void showCard() {
        cardVisible = true;
        layoutCard();
        updateVisibility();
        scheduleHide();
    }

    private void layoutCard() {
        messageScroll.setVisibility(cardVisible ? View.VISIBLE : View.GONE);
        controls.setVisibility(cardVisible ? View.VISIBLE : View.GONE);
        composer.setVisibility(cardVisible && readerExpanded ? View.VISIBLE : View.GONE);
        messageStatus.setVisibility(cardVisible && !messageStatus.getText().toString().isEmpty() ? View.VISIBLE : View.GONE);
        int padding = cardVisible ? dp(8) : 0;
        root.setPadding(padding, padding, padding, padding);
        root.setBackground(cardVisible ? background(Color.argb(175, 38, 34, 43), 20) : null);
        bubble.setAlpha(cardVisible ? 1f : 0.2f);
        position.width = cardVisible ? Math.min(dp(280), getResources().getDisplayMetrics().widthPixels - dp(24)) : dp(36);
        if (readerExpanded) {
            content.setMinLines(1);
            content.setMaxLines(Integer.MAX_VALUE);
            content.setEllipsize(null);
            // Grow downwards, keeping enough space for the system bars at the bottom.
            composer.measure(View.MeasureSpec.makeMeasureSpec(position.width - dp(16), View.MeasureSpec.EXACTLY),
                    View.MeasureSpec.makeMeasureSpec(0, View.MeasureSpec.UNSPECIFIED));
            int available = getResources().getDisplayMetrics().heightPixels - keyboardHeight
                    - position.y - dp(88) - composer.getMeasuredHeight();
            messageScroll.maxHeight = Math.max(dp(72), Math.min(dp(260), available));
            messageScroll.getLayoutParams().height = ViewGroup.LayoutParams.WRAP_CONTENT;
            messageScroll.setVerticalScrollBarEnabled(true);
        } else {
            content.setLines(5);
            content.setEllipsize(android.text.TextUtils.TruncateAt.END);
            messageScroll.maxHeight = 0;
            messageScroll.getLayoutParams().height = ViewGroup.LayoutParams.WRAP_CONTENT;
            messageScroll.setVerticalScrollBarEnabled(false);
            messageScroll.scrollTo(0, 0);
        }
        root.measure(View.MeasureSpec.makeMeasureSpec(position.width, View.MeasureSpec.EXACTLY),
                View.MeasureSpec.makeMeasureSpec(0, View.MeasureSpec.UNSPECIFIED));
        position.x = Math.max(0, Math.min(position.x, getResources().getDisplayMetrics().widthPixels - position.width));
        position.y = Math.max(dp(24), Math.min(position.y, getResources().getDisplayMetrics().heightPixels
                - keyboardHeight - root.getMeasuredHeight() - dp(24)));
        root.requestLayout();
        if (root.isAttachedToWindow()) windows.updateViewLayout(root, position);
    }

    private void updateVisibility() {
        if (!alive || root == null) return;
        PowerManager power = (PowerManager) getSystemService(POWER_SERVICE);
        KeyguardManager keyguard = (KeyguardManager) getSystemService(KEYGUARD_SERVICE);
        boolean visible = !snapshotHidden && power.isInteractive()
                && !keyguard.isKeyguardLocked() && Settings.canDrawOverlays(this);
        try {
            if (visible && !root.isAttachedToWindow()) windows.addView(root, position);
            else if (!visible && root.isAttachedToWindow()) windows.removeView(root);
        } catch (RuntimeException error) {
            // Window lifecycle failures do not necessarily mean permission was lost.
            // Keep automatic visibility updates quiet; enable() handles permission guidance.
            android.util.Log.w("FloatingChat", "Unable to update overlay visibility", error);
        }
    }

    private void refreshContext() {
        if (!alive) return;
        events.execute(() -> {
            try {
                JSONObject latest = api.context();
                main.post(() -> { if (alive) useContext(latest); });
                drainPending(latest);
            } catch (Exception ignored) { /* Pending messages retry their context lookup below. */ }
        });
    }

    private void useContext(JSONObject latest) {
        latestContext = latest;
        String key = FloatingChatHistory.targetKey(latest);
        if (!key.equals(messageTarget)) {
            messageTarget = key;
            history.reset(latest);
            lastMessage = "按住麦克风说话 · 上滑取消";
            if (!recorder.isRecording()) {
                renderMessage(false);
                messageScroll.scrollTo(0, 0);
            }
        }
    }

    private void refreshHistory() {
        events.execute(() -> {
            try {
                JSONObject latest = api.context();
                JSONArray recent = api.history(latest);
                main.post(() -> {
                    if (!alive || !FloatingChatHistory.targetKey(latest).equals(messageTarget)) return;
                    latestContext = latest;
                    history.merge(latest, recent);
                    if (!history.latestBody().isEmpty()) lastMessage = history.latestBody();
                    renderMessage(true);
                    layoutCard();
                });
            } catch (Exception ignored) { /* Keep received messages available if history cannot load. */ }
        });
    }

    private void renderMessage(boolean showLatest) {
        if (content == null || recorder.isRecording()) return;
        String text = readerExpanded && latestContext != null ? history.text(latestContext) : lastMessage;
        if (text.isEmpty()) text = lastMessage;
        if (!android.text.TextUtils.equals(content.getText(), text)) content.setText(text);
        if (showLatest && readerExpanded) messageScroll.post(() -> {
            if (!alive || !readerExpanded || content.getLayout() == null) return;
            int line = content.getLayout().getLineForOffset(Math.min(history.latestOffset(), content.length()));
            messageScroll.scrollTo(0, content.getLayout().getLineTop(line));
        });
    }

    private void showStatus(String text) {
        messageStatus.setText(text);
        layoutCard();
    }

    private final Runnable retryPending = () -> {
        if (!alive) return;
        try { events.execute(this::drainPending); }
        catch (java.util.concurrent.RejectedExecutionException ignored) {}
    };

    private void drainPending() {
        if (!alive || pending.isEmpty()) return;
        try {
            drainPending(api.context());
        } catch (Exception error) {
            main.removeCallbacks(retryPending);
            main.postDelayed(retryPending, 3000);
        }
    }

    private void drainPending(JSONObject latest) throws Exception {
        main.removeCallbacks(retryPending);
        while (alive && !pending.isEmpty()) {
            PendingMessage message = pending.peekFirst();
            receive(latest, message.type, message.data, message.allowPreview);
            pending.removeFirst();
        }
    }

    private void receive(JSONObject latest, String type, JSONObject data, boolean allowPreview) throws Exception {
        if (!alive || !session.offer(latest, type, data)) return;
        final String body = FloatingChatSession.displayBody(data.optString("content", ""));
        String actor = "msg_created".equals(type) ? data.optString("role") : data.optString("sender");
        JSONObject info = latest.getJSONObject("actors").optJSONObject(actor);
        String path = info == null ? "" : info.optString("avatar");
        Bitmap avatar = avatars.get(path);
        if (avatar == null && !path.isEmpty()) {
            avatar = api.avatar(path);
            if (avatar != null) avatars.put(path, avatar);
        }
        final Bitmap picture = avatar;
        main.post(() -> {
            if (!alive) return;
            PowerManager power = (PowerManager) getSystemService(POWER_SERVICE);
            KeyguardManager keyguard = (KeyguardManager) getSystemService(KEYGUARD_SERVICE);
            if (!power.isInteractive() || keyguard.isKeyguardLocked()) return;
            useContext(latest);
            lastMessage = body;
            history.merge(latest, new JSONArray().put(data));
            renderMessage(true);
            if (!recorder.isRecording()) showStatus("");
            String name = info == null ? "AI" : info.optString("name", "AI");
            bubble.setText(picture == null ? name.substring(0, Math.min(1, name.length())) : "");
            android.graphics.drawable.BitmapDrawable drawable = picture == null ? null
                    : new android.graphics.drawable.BitmapDrawable(getResources(), picture);
            if (drawable != null) drawable.setGravity(Gravity.FILL);
            bubble.setForeground(drawable);
            bubble.setContentDescription(name + "，点按打开或收起，拖动移动");
            // Preview the latest reply; expanded mode retains older AI bodies above it.
            if (allowPreview && !suppressesMessagePreview() && !recorder.isRecording() && !touching) showCard();
        });
    }

    private void resetMicrophone() {
        microphone.setBackground(background(Color.argb(70, 225, 214, 235), 16));
        together.setEnabled(true);
        messageScroll.setMinimumHeight(0);
        renderMessage(false);
        showStatus("");
        updateComposer();
    }

    private void cancelRecording() {
        main.removeCallbacks(recordingTimeout);
        if (recorder.isRecording()) recorder.cancel();
        if (microphone != null) resetMicrophone();
        renderMessage(false);
        scheduleHide();
    }

    private void finishRecording() {
        try {
            audioFile = new File(getCacheDir(), "floating-chat.wav");
            File wav = recorder.stopToWav(audioFile);
            resetMicrophone();
            scheduleHide();
            if (wav == null) { renderMessage(false); toast("说话时间太短，再试一次吧"); return; }
            setBusy(true, "正在转写…");
            asr.transcribe(wav, new WidgetAsrClient.Listener() {
                @Override public void onText(String text) {
                    main.post(() -> {
                        deleteAudio();
                        if (!alive) return;
                        send(text, recordingWithScreen);
                    });
                }
                @Override public void onError(String message) {
                    main.post(() -> { deleteAudio(); if (alive) { setBusy(false, message); toast(message); } });
                }
            });
        } catch (Exception error) { deleteAudio(); setBusy(false, "录音失败，请重试"); }
    }

    private void send(String messageText, boolean attachScreen) {
        send(messageText, attachScreen, null);
    }

    private void send(String messageText, boolean attachScreen, String submittedDraft) {
        setBusy(true, attachScreen ? "正在读取当前画面…" : "正在发送…");
        // Newer Android can capture the underlying app window without removing this overlay.
        if (attachScreen && !AionAccessibilityService.supportsWindowCapture()) { snapshotHidden = true; updateVisibility(); }
        new Thread(() -> {
            try {
                String screenshot = attachScreen ? api.screen() : null;
                if (!alive) return;
                JSONObject latest = api.context();
                if (!alive) return;
                main.post(() -> { if (alive) { useContext(latest); snapshotHidden = false; updateVisibility(); } });
                api.send(latest, messageText, screenshot, () -> main.post(() -> {
                    if (!alive) return;
                    if (submittedDraft != null && submittedDraft.equals(replyInput.getText().toString())) replyInput.setText("");
                    showStatus("已发送，等待回复…");
                }));
                main.post(() -> {
                    if (!alive) return;
                    setBusy(false, "松开发送 · 上滑取消");
                });
            } catch (Exception error) {
                main.post(() -> {
                    if (!alive) return;
                    snapshotHidden = false; updateVisibility();
                    String message = error.getMessage() == null ? "发送失败，请检查网络" : error.getMessage();
                    setBusy(false, message);
                    toast(message);
                });
            }
        }, "FloatingChatSend").start();
    }

    private void setBusy(boolean value, String message) {
        busy = value;
        microphone.setEnabled(!value);
        microphone.setAlpha(value ? 0.4f : 1f);
        updateComposer();
        showStatus(value ? message : "");
        // Background progress never restarts the five-second preview timer.
    }

    private void toast(String message) { Toast.makeText(this, message, Toast.LENGTH_LONG).show(); }
    private void deleteAudio() { if (audioFile != null) audioFile.delete(); audioFile = null; }

    @Override public void onConfigurationChanged(android.content.res.Configuration configuration) {
        super.onConfigurationChanged(configuration);
        if (alive) { layoutCard(); updateVisibility(); }
    }

    @Override public void onDestroy() {
        setInputActive(false);
        alive = false;
        if (instance == this) instance = null;
        main.removeCallbacksAndMessages(null);
        recorder.cancel();
        if (asr != null) asr.cancel();
        if (api != null) api.close();
        events.shutdownNow();
        deleteAudio();
        if (root != null && root.isAttachedToWindow()) windows.removeView(root);
        try { unregisterReceiver(screenReceiver); } catch (IllegalArgumentException ignored) {}
        stopForeground(true);
        super.onDestroy();
    }

    @Override public IBinder onBind(Intent intent) { return null; }
}
