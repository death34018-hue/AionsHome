package com.aion.chat;

import android.app.AlarmManager;
import android.app.KeyguardManager;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.bluetooth.BluetoothAdapter;
import android.bluetooth.BluetoothDevice;
import android.bluetooth.BluetoothGatt;
import android.bluetooth.BluetoothGattCallback;
import android.bluetooth.BluetoothGattCharacteristic;
import android.bluetooth.BluetoothGattDescriptor;
import android.bluetooth.BluetoothGattService;
import android.bluetooth.BluetoothManager;
import android.bluetooth.BluetoothProfile;
import android.bluetooth.le.BluetoothLeScanner;
import android.bluetooth.le.ScanCallback;
import android.bluetooth.le.ScanRecord;
import android.bluetooth.le.ScanResult;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.ServiceInfo;
import android.content.res.AssetFileDescriptor;
import android.net.ConnectivityManager;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.net.NetworkRequest;
import android.net.wifi.WifiManager;
import android.os.Build;
import android.os.IBinder;
import android.os.PowerManager;
import android.os.SystemClock;
import android.util.Base64;
import android.util.DisplayMetrics;
import android.util.Log;
import android.webkit.CookieManager;

import androidx.annotation.Nullable;
import androidx.core.app.NotificationCompat;

import org.json.JSONArray;
import org.json.JSONObject;

import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.Response;
import okhttp3.WebSocket;
import okhttp3.WebSocketListener;

import java.util.ArrayList;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;

import android.media.AudioAttributes;
import android.media.MediaPlayer;

import android.Manifest;
import android.content.pm.PackageManager;
import android.location.Location;
import android.location.LocationListener;
import android.location.LocationManager;
import android.os.Bundle;
import androidx.core.content.ContextCompat;
import okhttp3.MediaType;
import okhttp3.RequestBody;

import com.aion.chat.miband.MiBandHealthUploader;
import com.aion.chat.miband.MiBandCommandInbox;
import com.aion.chat.miband.MiBandRuntime;
import com.aion.chat.miband.MiBandStatus;
import com.aion.chat.miband.MiBandSyncSchedule;

import android.app.usage.UsageStats;
import android.app.usage.UsageStatsManager;
import android.app.usage.UsageEvents;
import android.provider.Settings;

import android.content.BroadcastReceiver;
import android.content.IntentFilter;
import android.graphics.Bitmap;
import android.graphics.PixelFormat;
import android.hardware.display.DisplayManager;
import android.hardware.display.VirtualDisplay;
import android.media.Image;
import android.media.ImageReader;
import android.media.projection.MediaProjection;
import android.media.projection.MediaProjectionManager;

import android.hardware.Sensor;
import android.hardware.SensorEvent;
import android.hardware.SensorEventListener;
import android.hardware.SensorManager;

import android.os.Handler;
import android.os.Looper;

import java.text.SimpleDateFormat;
import java.io.ByteArrayOutputStream;
import java.nio.ByteBuffer;
import java.util.Calendar;
import java.util.Locale;
import java.util.UUID;

/**
 * 前台服务 — OkHttp WebSocket 长连接
 * 针对 vivo/OPPO 等 ROM 做了适配：
 * 1. Thread.sleep 心跳（不依赖 Handler/Looper）
 * 2. ConnectivityManager.NetworkCallback 监听网络变化
 * 3. synchronized connectWebSocket 防并发竞争
 * 4. onFailure 不阻塞 OkHttp 回调线程
 * 5. fullScreenIntent 闹铃通知（锁屏也能亮屏弹出）
 */
public class AionPushService extends Service {

    private static final String TAG = "AionPush";
    public static final String ACTION_REFRESH_CLOUDFLARE_AUTH = "refresh_cloudflare_auth";
    public static final String ACTION_RELEASE_RING_FOR_PAGE = "release_ring_for_page";
    public static final String ACTION_ACQUIRE_RING_FOR_BACKGROUND = "acquire_ring_for_background";
    public static final String ACTION_RING_FEATURE_CHANGED = "ring_feature_changed";
    public static final String ACTION_MI_BAND_SETTINGS_CHANGED = "mi_band_settings_changed";
    public static final String ACTION_ARM_PHONE_CAMERA = "arm_phone_camera";
    public static final String ACTION_DISARM_PHONE_CAMERA = "disarm_phone_camera";
    public static final String EXTRA_PHONE_CAMERA_FACING = "phone_camera_facing";
    public static final String EXTRA_PHONE_CAMERA_ZOOM = "phone_camera_zoom";
    private static final String PREFS = "aion_prefs";
    private static final String PREF_SAVED_URL = "saved_url";
    private static final String DEFAULT_PAGE_URL = "http://192.168.xx.xxx:8080/chat";
    private static final String RING_PREFS_NAME = "aion_ring_ble";
    private static final String KEY_RING_ENABLED = "ring_enabled";

    private static final String CH_KEEPALIVE = "aion_keepalive";
    private static final String CH_MESSAGE   = "aion_message_heads_up_v2";
    private static final String CH_ALARM     = "aion_alarm";

    private static final int NOTIF_FOREGROUND = 1;
    private static final int NOTIF_MSG_BASE   = 1000;

    private static final long HEARTBEAT_MS  = 45_000;  // 45s 心跳（省电）
    private static final long HEALTH_TIMEOUT = 120_000; // 120s 无消息 → 重连
    private static final long PHONE_CAMERA_SHUTTER_OFFSET_MS = 5_000L;

    private OkHttpClient client;
    private volatile WebSocket webSocket;
    private volatile String serverUrl;
    private int notifCounter = 0;

    private final AtomicInteger wsGeneration = new AtomicInteger(0);
    private final AtomicBoolean wsConnected = new AtomicBoolean(false);
    private final AtomicBoolean wsConnecting = new AtomicBoolean(false);

    private volatile int reconnectDelay = 3000;
    private static final int MAX_RECONNECT_DELAY = 30000;
    private volatile boolean shouldRun = true;
    private volatile boolean isForegroundActive = false;
    private final PhoneCameraState phoneCameraState = new PhoneCameraState();
    private PhoneCameraArmPersistence phoneCameraArmPersistence;
    private final ExecutorService phoneCameraStateSync =
            Executors.newSingleThreadExecutor();
    private PhoneCameraController phoneCameraController;
    private final AtomicInteger phoneCameraCaptureGeneration = new AtomicInteger();
    private volatile Runnable phoneCameraRetryRunnable;

    private PowerManager.WakeLock wakeLock;
    private WifiManager.WifiLock wifiLock;
    private Thread heartbeatThread;
    private MediaPlayer mediaPlayer;
    private final Object phoneCameraAlertLock = new Object();
    private MediaPlayer phoneCameraAlertPlayer;

    private volatile int msgReceived = 0;
    private volatile long lastMessageTime = 0;

    private ConnectivityManager connectivityManager;
    private ConnectivityManager.NetworkCallback networkCallback;

    // ── ESP32-CAM 桥接 ──
    private volatile boolean esp32BridgeActive = false;
    private volatile String esp32CaptureUrl = "";
    private Thread esp32BridgeThread;

    // ── 定位上报 ──
    private static final long LOCATION_INTERVAL = 10 * 60_000;          // 统一 10 分钟（服务端做智能过滤，非每次都调 API）
    private static final long LOCATION_INTERVAL_DISABLED = 10 * 60_000; // 功能未启用/静默时段时低频轮询开关状态
    private Thread locationThread;
    private volatile long locationInterval = LOCATION_INTERVAL;
    private LocationManager locationManager;
    private volatile Location lastKnownLocation;
    private volatile boolean locationEnabled = false;  // 服务端定位开关状态

    // ── 戒指心率后台同步 ──
    private static final long RING_SYNC_INTERVAL = 10 * 60_000L;
    private static final int RING_SYNC_OFFSET_MINUTE = 2; // 戒指整 10 分钟测量后，错后 2 分钟拉取
    private Thread ringSyncThread;
    private volatile RingBackgroundSync ringBackgroundSync;
    private final Object ringSyncSignal = new Object();
    private volatile boolean ringAcquireRequested = false;

    // ── 小米手环 7：独立于戒指的单一 BLE 运行时与自适应同步线程 ──
    private MiBandRuntime miBandRuntime;
    private final MiBandCommandInbox miBandCommandInbox = new MiBandCommandInbox();
    private final AtomicBoolean miBandCommandFetchActive = new AtomicBoolean(false);
    private final AtomicBoolean appSupervisionCommandFetchActive = new AtomicBoolean(false);
    private static final String PREF_APP_SUPERVISION_RESULTS = "app_supervision_command_results";
    private MiBandRuntime.Listener miBandCommandListener;
    private Thread miBandSyncThread;
    private final Object miBandSyncSignal = new Object();

    // ── 活动上报 ──
    private static final long ACTIVITY_INTERVAL = 60_000;  // 60秒检测一次前台应用
    private static final long ACTIVITY_RE_REPORT_MS = 5 * 60_000;  // 同一App超过5分钟重新上报
    private Thread activityThread;
    private volatile String lastReportedApp = "";
    private volatile long lastReportedTime = 0;
    private volatile boolean screenOn = true;
    private BroadcastReceiver screenReceiver;

    // ── 无障碍服务自动恢复（需 WRITE_SECURE_SETTINGS 权限，通过 ADB 授予）──
    private volatile long lastAccessibilityRecoverAt = 0;
    private static final long ACCESSIBILITY_RECOVER_COOLDOWN = 5_000; // 恢复操作冷却 5 秒

    // ── 手机屏幕截图（MediaProjection，需要用户显式授权）──
    public static final String ACTION_START_PHONE_SCREEN = "start_phone_screen_projection";
    public static final String ACTION_STOP_PHONE_SCREEN = "stop_phone_screen_projection";
    public static final String ACTION_TEST_ACCESSIBILITY_SCREEN = "test_accessibility_screen";
    public static final String EXTRA_RESULT_CODE = "result_code";
    public static final String EXTRA_RESULT_DATA = "result_data";
    private final Object phoneScreenLock = new Object();
    private MediaProjectionManager projectionManager;
    private MediaProjection mediaProjection;
    private VirtualDisplay phoneScreenDisplay;
    private ImageReader phoneScreenReader;
    private volatile boolean phoneScreenEnabled = false;
    private volatile long lastPhoneCaptureAt = 0;

    // ── 步数计数 ──
    // 使用 TYPE_STEP_COUNTER（硬件累计步数，低功耗），搭载定位线程 10 分钟上报
    // 凌晨 5:00 重置（逻辑日期以 5:00 为分界，适应晚睡作息）
    // 重启检测：currentCounter < lastKnownCounter 时把上一 boot 周期走的步数补偿到 rebootOffset
    private SensorManager sensorManager;
    private Sensor stepSensor;
    private volatile float latestStepCounter = -1;  // 传感器最新值（开机累计）
    private volatile int serverStepRestore = -1;    // 服务端恢复的步数（重装 APK 后使用）
    private volatile boolean stepRestorePending = false; // 正在从服务端恢复步数
    private Handler mainHandler;  // 主线程 Handler，传感器回调需要 Looper
    private static final String PREF_STEP_DAY_START = "step_day_start_counter";
    private static final String PREF_STEP_REBOOT_OFFSET = "step_reboot_offset";
    private static final String PREF_STEP_LAST_KNOWN = "step_last_known_counter";
    private static final String PREF_STEP_RESET_DATE = "step_reset_logical_date";
    private static final int STEP_RESET_HOUR = 5;  // 凌晨 5 点重置

    // ══════════════════════════════════════════════════════════
    //  生命周期
    // ══════════════════════════════════════════════════════════

    @Override
    public void onCreate() {
        super.onCreate();
        Log.i(TAG, "=== onCreate ===");
        createNotificationChannels();
        mainHandler = new Handler(Looper.getMainLooper());
        phoneCameraController = new PhoneCameraController(this);
        android.content.SharedPreferences phoneCameraPrefs =
                getSharedPreferences("phone_camera_arm", MODE_PRIVATE);
        phoneCameraArmPersistence = new PhoneCameraArmPersistence(
                new PhoneCameraArmPersistence.Store() {
                    @Override
                    public boolean getBoolean(String key, boolean fallback) {
                        return phoneCameraPrefs.getBoolean(key, fallback);
                    }

                    @Override
                    public String getString(String key, String fallback) {
                        return phoneCameraPrefs.getString(key, fallback);
                    }

                    @Override
                    public float getFloat(String key, float fallback) {
                        return phoneCameraPrefs.getFloat(key, fallback);
                    }

                    @Override
                    public void put(
                            boolean armed,
                            String facing,
                            float zoom
                    ) {
                        phoneCameraPrefs.edit()
                                .putBoolean("armed", armed)
                                .putString("facing", facing)
                                .putFloat("zoom", zoom)
                                .apply();
                    }
                });
        phoneCameraArmPersistence.restoreInto(phoneCameraState);

        PowerManager pm = (PowerManager) getSystemService(Context.POWER_SERVICE);
        if (pm != null) {
            wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "AionChat:Push");
            wakeLock.acquire();
            Log.i(TAG, "WakeLock acquired");
        }

        WifiManager wm = (WifiManager) getApplicationContext().getSystemService(Context.WIFI_SERVICE);
        if (wm != null) {
            wifiLock = wm.createWifiLock(WifiManager.WIFI_MODE_FULL_LOW_LATENCY, "AionChat:Wifi");
            wifiLock.acquire();
            Log.i(TAG, "WifiLock acquired");
        }

        client = new OkHttpClient.Builder()
                .pingInterval(30, TimeUnit.SECONDS)
                .readTimeout(0, TimeUnit.SECONDS)
                .connectTimeout(10, TimeUnit.SECONDS)
                .addInterceptor(chain -> {
                    Request original = chain.request();
                    if (!ConnectionEndpoint.isCloudflareHost(original.url().host())) {
                        return chain.proceed(original);
                    }
                    String cookie = getCloudflareCookie();
                    if (cookie == null) return chain.proceed(original);
                    return chain.proceed(original.newBuilder()
                            .header("Cookie", cookie)
                            .build());
                })
                .build();

        miBandRuntime = MiBandRuntime.get(this);
        miBandRuntime.setSampleSink(new MiBandHealthUploader(client, this::getHttpBase));
        miBandCommandListener = new MiBandRuntime.Listener() {
            @Override public void onStatus(MiBandStatus status) {
                if (status.authenticated) drainMiBandCommands();
            }
            @Override public void onSamples(java.util.List<com.aion.chat.miband.MiBandProtocol.ActivitySample> samples) {}
        };
        miBandRuntime.addListener(miBandCommandListener);

        registerNetworkCallback();
        initStepCounter();
        com.aion.chat.supervision.AppSupervisionRuntime supervisionRuntime =
                com.aion.chat.supervision.AppSupervisionRuntime.start(this);
        supervisionRuntime.setSyncListener(this::postAppSupervisionState);
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        boolean endpointChanged = false;
        String action = null;
        if (intent != null) {
            action = intent.getStringExtra("action");

            String url = intent.getStringExtra("url");
            if (url != null) {
                String normalizedPageUrl = ConnectionEndpoint.normalizePageUrl(url);
                getSharedPreferences(PREFS, MODE_PRIVATE)
                        .edit()
                        .putString(PushServiceStartPolicy.PREF_LAST_ACTIVE_URL, normalizedPageUrl)
                        .apply();
                String ws = ConnectionEndpoint.toWebSocketUrl(normalizedPageUrl);
                if (ws.equals(serverUrl) && wsConnected.get()) {
                    Log.d(TAG, "Already connected to " + serverUrl);
                    if (action == null) return START_STICKY;
                }
                endpointChanged = serverUrl != null && !ws.equals(serverUrl);
                if (endpointChanged) resetWebSocketForEndpointChange();
                serverUrl = ws;
            }

            if (PushServiceStartPolicy.ACTION_SET_FOREGROUND.equals(action)) {
                isForegroundActive = intent.getBooleanExtra("active", false);
                // WebView takes over playback while the page is foregrounded.
                if (isForegroundActive) stopMusic();
                Log.d(TAG, "foreground=" + isForegroundActive);
                if (PushServiceStartPolicy.canReturnAfterLightweightAction(
                        action, serverUrl, isHeartbeatThreadAlive())) {
                    return START_STICKY;
                }
                Log.i(TAG, "Foreground action cold-started service; continuing bootstrap");
            }
            if (ACTION_RELEASE_RING_FOR_PAGE.equals(action)) {
                releaseRingForPageConnection();
                if (serverUrl != null && isHeartbeatThreadAlive()) {
                    return START_STICKY;
                }
                Log.i(TAG, "Ring handoff action cold-started service; continuing bootstrap");
            }
            if (ACTION_ACQUIRE_RING_FOR_BACKGROUND.equals(action)) {
                requestRingBackgroundConnection();
                if (serverUrl != null && isHeartbeatThreadAlive()
                        && ringSyncThread != null && ringSyncThread.isAlive()) {
                    return START_STICKY;
                }
                Log.i(TAG, "Ring background acquire cold-started service; continuing bootstrap");
            }
            if (ACTION_RING_FEATURE_CHANGED.equals(action)) {
                onRingFeatureSettingChanged();
                if (serverUrl != null && isHeartbeatThreadAlive()
                        && ringSyncThread != null && ringSyncThread.isAlive()) {
                    return START_STICKY;
                }
                Log.i(TAG, "Ring feature action cold-started service; continuing bootstrap");
            }
            if (ACTION_MI_BAND_SETTINGS_CHANGED.equals(action)) {
                wakeMiBandScheduler();
                if (serverUrl != null && isHeartbeatThreadAlive()
                        && miBandSyncThread != null && miBandSyncThread.isAlive()) {
                    return START_STICKY;
                }
                Log.i(TAG, "Mi Band setting action cold-started service; continuing bootstrap");
            }
            if (ACTION_REFRESH_CLOUDFLARE_AUTH.equals(action)) {
                if (PushServiceStartPolicy.canReturnAfterLightweightAction(
                        action, serverUrl, isHeartbeatThreadAlive())) {
                    if (isCloudflareServer() && !wsConnected.get()) {
                        reconnectDelay = 3000;
                        connectWebSocket();
                    }
                    return START_STICKY;
                }
                Log.i(TAG, "Cloudflare auth action cold-started service; continuing bootstrap");
            }
            if (ACTION_START_PHONE_SCREEN.equals(action)) {
                int resultCode = intent.getIntExtra(EXTRA_RESULT_CODE, 0);
                Intent resultData = intent.getParcelableExtra(EXTRA_RESULT_DATA);
                startPhoneScreenProjection(resultCode, resultData);
                // 不提前返回：如果这是 Service 首次启动，还需要继续初始化 URL、前台服务和 WebSocket。
            }
            if (ACTION_STOP_PHONE_SCREEN.equals(action)) {
                stopPhoneScreenProjection();
                return START_STICKY;
            }
            if (ACTION_TEST_ACCESSIBILITY_SCREEN.equals(action)) {
                requestAccessibilityPhoneScreen("manual_test", true);
                return START_STICKY;
            }
            if (ACTION_ARM_PHONE_CAMERA.equals(action)) {
                phoneCameraState.arm(
                        intent.getStringExtra(EXTRA_PHONE_CAMERA_FACING),
                        intent.getFloatExtra(EXTRA_PHONE_CAMERA_ZOOM, 1f)
                );
                phoneCameraArmPersistence.rememberArmed(
                        phoneCameraState.getFacing(),
                        phoneCameraState.getZoom());
                postPhoneCameraArmState(true);
            }
            if (ACTION_DISARM_PHONE_CAMERA.equals(action)) {
                cancelActivePhoneCameraCapture();
                phoneCameraState.disarm();
                phoneCameraArmPersistence.rememberDisarmed();
                postPhoneCameraArmState(false);
            }
        }

        if (serverUrl == null) {
            SharedPreferences prefs = getSharedPreferences(PREFS, MODE_PRIVATE);
            String saved = prefs.getString(PREF_SAVED_URL, DEFAULT_PAGE_URL);
            String lastActive = prefs.getString(
                    PushServiceStartPolicy.PREF_LAST_ACTIVE_URL, "");
            String fallback = PushServiceStartPolicy.chooseFallbackPageUrl(
                    lastActive, saved, DEFAULT_PAGE_URL);
            String normalized = ConnectionEndpoint.normalizePageUrl(fallback);
            SharedPreferences.Editor editor = prefs.edit()
                    .putString(PushServiceStartPolicy.PREF_LAST_ACTIVE_URL, normalized);
            if (fallback.equals(saved) && !normalized.equals(saved)) {
                editor.putString(PREF_SAVED_URL, normalized);
            }
            editor.apply();
            serverUrl = ConnectionEndpoint.toWebSocketUrl(normalized);
        }

        Log.i(TAG, "onStartCommand url=" + serverUrl);

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            // Android 14+: 需要声明所有用到的前台服务类型
            int serviceType = ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC;
            if (phoneScreenEnabled || mediaProjection != null) {
                serviceType |= ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION;
            }
            if (ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_FINE_LOCATION)
                    == PackageManager.PERMISSION_GRANTED) {
                serviceType |= ServiceInfo.FOREGROUND_SERVICE_TYPE_LOCATION;
            }
            if (ContextCompat.checkSelfPermission(this, Manifest.permission.BLUETOOTH_CONNECT)
                    == PackageManager.PERMISSION_GRANTED) {
                serviceType |= ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE;
            }
            if (phoneCameraState.isArmed()
                    && ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA)
                    == PackageManager.PERMISSION_GRANTED) {
                serviceType |= ServiceInfo.FOREGROUND_SERVICE_TYPE_CAMERA;
            }
            startForeground(NOTIF_FOREGROUND, buildKeepAlive("连接中..."), serviceType);
        } else {
            startForeground(NOTIF_FOREGROUND, buildKeepAlive("连接中..."));
        }

        shouldRun = true;
        startHeartbeatThread();
        startLocationThread();
        startActivityThread();
        startRingSyncThread();
        startMiBandSyncThread();
        if (endpointChanged) connectWebSocket();
        return START_STICKY;
    }

    private boolean isHeartbeatThreadAlive() {
        return heartbeatThread != null && heartbeatThread.isAlive();
    }

    private synchronized void resetWebSocketForEndpointChange() {
        wsGeneration.incrementAndGet();
        wsConnected.set(false);
        wsConnecting.set(false);
        WebSocket old = webSocket;
        webSocket = null;
        if (old != null) {
            try { old.cancel(); } catch (Exception ignored) {}
        }
    }

    @Nullable @Override
    public IBinder onBind(Intent intent) { return null; }

    @Override
    public void onDestroy() {
        Log.i(TAG, "=== onDestroy ===");
        shouldRun = false;
        com.aion.chat.supervision.AppSupervisionRuntime runtime =
                com.aion.chat.supervision.AppSupervisionRuntime.get();
        if (runtime != null) runtime.setSyncListener(null);
        wsGeneration.incrementAndGet();
        if (heartbeatThread != null) heartbeatThread.interrupt();
        if (locationThread != null) locationThread.interrupt();
        if (activityThread != null) activityThread.interrupt();
        if (ringSyncThread != null) ringSyncThread.interrupt();
        if (ringBackgroundSync != null) ringBackgroundSync.close();
        if (miBandSyncThread != null) miBandSyncThread.interrupt();
        if (miBandRuntime != null && miBandCommandListener != null) {
            miBandRuntime.removeListener(miBandCommandListener);
        }
        if (miBandRuntime != null) miBandRuntime.disconnect();
        stopEsp32Bridge();
        stopPhoneScreenProjection();
        phoneCameraState.disarm();
        cancelActivePhoneCameraCapture();
        if (phoneCameraController != null) phoneCameraController.close();
        phoneCameraStateSync.shutdownNow();
        unregisterScreenReceiver();
        if (sensorManager != null) sensorManager.unregisterListener(stepListener);
        if (webSocket != null) try { webSocket.cancel(); } catch (Exception ignored) {}
        if (client != null) client.dispatcher().executorService().shutdown();
        stopMusic();
        stopPhoneCameraAlert();
        if (wakeLock != null && wakeLock.isHeld()) wakeLock.release();
        if (wifiLock != null && wifiLock.isHeld()) wifiLock.release();
        unregisterNetworkCallback();
        super.onDestroy();
    }

    @Override
    public void onTaskRemoved(Intent rootIntent) {
        Log.w(TAG, "Task removed → schedule restart");
        Intent ri = new Intent(getApplicationContext(), AionPushService.class);
        ri.setPackage(getPackageName());
        PendingIntent pi = PendingIntent.getService(getApplicationContext(), 1, ri,
                PendingIntent.FLAG_ONE_SHOT | PendingIntent.FLAG_IMMUTABLE);
        AlarmManager am = (AlarmManager) getSystemService(Context.ALARM_SERVICE);
        if (am != null) {
            am.setExactAndAllowWhileIdle(AlarmManager.ELAPSED_REALTIME_WAKEUP,
                    SystemClock.elapsedRealtime() + 3000, pi);
        }
        super.onTaskRemoved(rootIntent);
    }

    // ══════════════════════════════════════════════════════════
    //  网络变化监听 — 网络恢复时立即触发重连
    // ══════════════════════════════════════════════════════════

    private void registerNetworkCallback() {
        connectivityManager = (ConnectivityManager) getSystemService(Context.CONNECTIVITY_SERVICE);
        if (connectivityManager == null) return;

        networkCallback = new ConnectivityManager.NetworkCallback() {
            @Override
            public void onAvailable(Network network) {
                Log.i(TAG, "★ Network available, connected=" + wsConnected.get());
                if (!wsConnected.get() && shouldRun) {
                    reconnectDelay = 3000;
                    connectWebSocket();
                }
            }
            @Override
            public void onLost(Network network) {
                Log.w(TAG, "★ Network lost");
                wsConnected.set(false);
                updateKeepAlive("网络断开，等待恢复...");
            }
        };

        NetworkRequest req = new NetworkRequest.Builder()
                .addCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
                .build();
        connectivityManager.registerNetworkCallback(req, networkCallback);
        Log.i(TAG, "NetworkCallback registered");
    }

    private void unregisterNetworkCallback() {
        if (connectivityManager != null && networkCallback != null) {
            try { connectivityManager.unregisterNetworkCallback(networkCallback); }
            catch (Exception ignored) {}
        }
    }

    // ══════════════════════════════════════════════════════════
    //  心跳线程 — 纯 Java Thread
    // ══════════════════════════════════════════════════════════

    private synchronized void startHeartbeatThread() {
        if (heartbeatThread != null && heartbeatThread.isAlive()) return;

        heartbeatThread = new Thread(() -> {
            Log.i(TAG, "♥ Heartbeat started tid=" + Thread.currentThread().getId());

            if (!wsConnected.get()) connectWebSocket();

            while (shouldRun) {
                try { Thread.sleep(HEARTBEAT_MS); }
                catch (InterruptedException e) { break; }
                if (!shouldRun) break;

                try {
                    com.aion.chat.supervision.AppSupervisionRuntime supervisionRuntime =
                            com.aion.chat.supervision.AppSupervisionRuntime.get();
                    if (supervisionRuntime != null) {
                        supervisionRuntime.checkpointWatchdog();
                    }
                    if (wsConnected.get() && webSocket != null) {
                        boolean sent = webSocket.send("{\"type\":\"ping\"}");
                        long elapsed = (lastMessageTime > 0)
                                ? (System.currentTimeMillis() - lastMessageTime) / 1000 : 0;
                        Log.d(TAG, "♥ ping=" + sent + " msgs=" + msgReceived + " idle=" + elapsed + "s");

                        if (!sent) {
                            Log.w(TAG, "♥ ping failed → reconnect");
                            wsConnected.set(false);
                            connectWebSocket();
                        } else if (lastMessageTime > 0
                                && System.currentTimeMillis() - lastMessageTime > HEALTH_TIMEOUT) {
                            Log.w(TAG, "♥ health timeout → reconnect");
                            wsConnected.set(false);
                            connectWebSocket();
                        }
                    } else if (!wsConnected.get()) {
                        Log.i(TAG, "♥ not connected → reconnect");
                        connectWebSocket();
                    }
                } catch (Exception e) {
                    Log.e(TAG, "♥ error: " + e.getMessage());
                }
            }
            Log.i(TAG, "♥ Heartbeat exiting");
        }, "AionHeartbeat");
        heartbeatThread.setDaemon(false);
        heartbeatThread.start();
    }

    // ══════════════════════════════════════════════════════════
    //  定位上报线程 — 每隔 N 分钟获取 GPS 坐标并 POST 到服务器
    // ══════════════════════════════════════════════════════════

    private synchronized void startLocationThread() {
        if (locationThread != null && locationThread.isAlive()) return;

        locationThread = new Thread(() -> {
            Log.i(TAG, "📍 Location thread started");
            // 首次等 15 秒让 WS 和 GPS 稳定
            try { Thread.sleep(15000); } catch (InterruptedException e) { return; }

            while (shouldRun) {
                try {
                    // 权限可能在服务启动后才授予，重试初始化步数传感器
                    if (latestStepCounter < 0) initStepCounter();

                    // 先检查服务端定位功能是否启用
                    checkLocationEnabled();
                    if (locationEnabled) {
                        requestLocationOnce();
                    } else {
                        Log.d(TAG, "📍 server location disabled, idle");
                    }
                } catch (Exception e) {
                    Log.e(TAG, "📍 error: " + e.getMessage());
                }

                long interval = locationEnabled ? locationInterval : LOCATION_INTERVAL_DISABLED;
                try { Thread.sleep(interval); }
                catch (InterruptedException e) { break; }
            }
            Log.i(TAG, "📍 Location thread exiting");
        }, "AionLocation");
        locationThread.setDaemon(false);
        locationThread.start();
    }

    private void checkLocationEnabled() {
        if (serverUrl == null) return;
        String httpBase = serverUrl
                .replace("ws://", "http://")
                .replace("wss://", "https://")
                .replace("/ws", "");
        try {
            Request req = new Request.Builder()
                    .url(httpBase + "/api/location/config")
                    .get().build();
            try (Response resp = client.newCall(req).execute()) {
                if (resp.isSuccessful() && resp.body() != null) {
                    JSONObject cfg = new JSONObject(resp.body().string());
                    // active = enabled && 不在静默时段（服务端计算）
                    locationEnabled = cfg.optBoolean("active", false);
                }
            }
        } catch (Exception e) {
            Log.d(TAG, "📍 check config failed: " + e.getMessage());
        }
    }

    private void requestLocationOnce() {
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_FINE_LOCATION)
                != PackageManager.PERMISSION_GRANTED) {
            Log.w(TAG, "📍 No location permission");
            return;
        }

        if (locationManager == null) {
            locationManager = (LocationManager) getSystemService(Context.LOCATION_SERVICE);
        }
        if (locationManager == null) return;

        // 优先尝试 GPS，备用 Network
        Location loc = null;
        try {
            loc = locationManager.getLastKnownLocation(LocationManager.GPS_PROVIDER);
        } catch (Exception ignored) {}
        if (loc == null || System.currentTimeMillis() - loc.getTime() > 10 * 60_000) {
            try {
                loc = locationManager.getLastKnownLocation(LocationManager.NETWORK_PROVIDER);
            } catch (Exception ignored) {}
        }

        // 如果缓存的位置太旧（>10分钟），请求一次实时定位
        if (loc == null || System.currentTimeMillis() - loc.getTime() > 10 * 60_000) {
            requestFreshLocation();
            return;
        }

        lastKnownLocation = loc;
        postLocationToServer(loc);
    }

    private void requestFreshLocation() {
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_FINE_LOCATION)
                != PackageManager.PERMISSION_GRANTED) return;
        if (locationManager == null) return;

        // 注意: LocationListener 回调发生在 Looper 线程，这里用主线程 Looper
        try {
            String provider = locationManager.isProviderEnabled(LocationManager.GPS_PROVIDER)
                    ? LocationManager.GPS_PROVIDER : LocationManager.NETWORK_PROVIDER;

            locationManager.requestSingleUpdate(provider, new LocationListener() {
                @Override
                public void onLocationChanged(Location location) {
                    lastKnownLocation = location;
                    postLocationToServer(location);
                }
                @Override public void onStatusChanged(String p, int s, Bundle e) {}
                @Override public void onProviderEnabled(String p) {}
                @Override public void onProviderDisabled(String p) {}
            }, getMainLooper());
        } catch (Exception e) {
            Log.e(TAG, "📍 requestSingleUpdate failed: " + e.getMessage());
        }
    }

    private void postLocationToServer(Location loc) {
        if (loc == null || serverUrl == null) return;

        // 从 wsUrl 推断 HTTP API 地址
        String httpBase = serverUrl
                .replace("ws://", "http://")
                .replace("wss://", "https://")
                .replace("/ws", "");

        String apiUrl = httpBase + "/api/location/heartbeat";

        try {
            JSONObject body = new JSONObject();
            body.put("lng", loc.getLongitude());
            body.put("lat", loc.getLatitude());
            body.put("accuracy", loc.getAccuracy());
            body.put("is_gcj02", false);  // Android 原生 GPS 输出 WGS84

            // 搭载步数数据
            int steps = getTodaySteps();
            if (steps >= 0) {
                body.put("steps", steps);
                body.put("step_logical_date", getLogicalDate());
            }
            // 传感器诊断信息一并上报，方便服务端排查
            boolean hasPerm = ContextCompat.checkSelfPermission(this,
                    Manifest.permission.ACTIVITY_RECOGNITION) == PackageManager.PERMISSION_GRANTED;
            String stepDiag = "steps=" + steps
                    + " sensorVal=" + latestStepCounter
                    + " sensorObj=" + (stepSensor != null)
                    + " perm=" + hasPerm;
            body.put("step_diag", stepDiag);
            Log.i(TAG, "\uD83D\uDC63 " + stepDiag);

            MediaType JSON = MediaType.get("application/json; charset=utf-8");
            RequestBody reqBody = RequestBody.create(body.toString(), JSON);
            Request req = new Request.Builder().url(apiUrl).post(reqBody).build();

            // 同步请求（已在后台线程）
            try (Response resp = client.newCall(req).execute()) {
                String respBody = resp.body() != null ? resp.body().string() : "";
                Log.i(TAG, "📍 posted loc (" + String.format("%.4f,%.4f", loc.getLongitude(), loc.getLatitude())
                        + " acc=" + (int) loc.getAccuracy() + "m) → " + resp.code());
            }
        } catch (Exception e) {
            Log.e(TAG, "📍 post failed: " + e.getMessage());
        }
    }

    private synchronized void startRingSyncThread() {
        if (ringSyncThread != null && ringSyncThread.isAlive()) return;
        ringSyncThread = new Thread(() -> {
            Log.i(TAG, "💍 Ring sync thread started");
            if (isRingFeatureEnabled()) runRingBackgroundSyncOnce();
            while (shouldRun) {
                if (!waitUntilRingFeatureEnabled()) break;
                if (consumeRingAcquireRequest()) {
                    runRingBackgroundAcquireOnce();
                    continue;
                }
                long delay = computeNextRingSyncDelayMs();
                Log.i(TAG, "💍 next background ring sync in " + (delay / 1000) + "s");
                try {
                    synchronized (ringSyncSignal) {
                        if (!ringAcquireRequested && shouldRun) {
                            ringSyncSignal.wait(delay);
                        }
                    }
                } catch (InterruptedException e) {
                    break;
                }
                if (!shouldRun) break;
                if (!isRingFeatureEnabled()) continue;
                if (consumeRingAcquireRequest()) {
                    runRingBackgroundAcquireOnce();
                    continue;
                }
                runRingBackgroundSyncOnce();
            }
            if (ringBackgroundSync != null) {
                ringBackgroundSync.close();
                ringBackgroundSync = null;
            }
            Log.i(TAG, "💍 Ring sync thread exiting");
        }, "AionRingSync");
        ringSyncThread.setDaemon(false);
        ringSyncThread.start();
    }

    private boolean waitUntilRingFeatureEnabled() {
        synchronized (ringSyncSignal) {
            while (shouldRun && !isRingFeatureEnabled()) {
                try {
                    Log.i(TAG, "💍 ring feature disabled; waiting without scheduled BLE wakeups");
                    ringSyncSignal.wait();
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                    return false;
                }
            }
        }
        return shouldRun;
    }

    private boolean isRingFeatureEnabled() {
        return getSharedPreferences(RING_PREFS_NAME, MODE_PRIVATE)
                .getBoolean(KEY_RING_ENABLED, true);
    }

    private void onRingFeatureSettingChanged() {
        boolean enabled = isRingFeatureEnabled();
        if (!enabled) {
            ringAcquireRequested = false;
            RingBackgroundSync sync = ringBackgroundSync;
            if (sync != null) sync.cancelForFeatureDisabled();
        }
        synchronized (ringSyncSignal) {
            ringSyncSignal.notifyAll();
        }
        Log.i(TAG, "💍 ring feature enabled=" + enabled);
    }

    private boolean consumeRingAcquireRequest() {
        synchronized (ringSyncSignal) {
            if (!ringAcquireRequested) return false;
            ringAcquireRequested = false;
            return true;
        }
    }

    private void requestRingBackgroundConnection() {
        if (!isRingFeatureEnabled()) return;
        synchronized (ringSyncSignal) {
            ringAcquireRequested = true;
            ringSyncSignal.notifyAll();
        }
        Log.i(TAG, "💍 connected health page released GATT; waking background BLE owner");
    }

    private void runRingBackgroundAcquireOnce() {
        if (!isRingFeatureEnabled()) return;
        try {
            if (ringBackgroundSync == null) {
                ringBackgroundSync = new RingBackgroundSync();
            }
            ringBackgroundSync.acquireConnectionForBackground();
        } catch (Exception e) {
            Log.e(TAG, "💍 immediate background acquire failed: " + e.getMessage());
        }
    }

    private void runRingBackgroundSyncOnce() {
        if (!isRingFeatureEnabled()) return;
        try {
            if (ringBackgroundSync == null) {
                ringBackgroundSync = new RingBackgroundSync();
            }
            ringBackgroundSync.syncComprehensiveSnapshotOnce();
        } catch (Exception e) {
            Log.e(TAG, "💍 sync failed: " + e.getMessage());
        }
    }

    private void releaseRingForPageConnection() {
        RingBackgroundSync sync = ringBackgroundSync;
        if (sync == null) return;
        Log.i(TAG, "💍 health page requested BLE ownership; releasing background GATT");
        sync.cancelForPageConnection();
    }

    private long computeNextRingSyncDelayMs() {
        Calendar cal = Calendar.getInstance();
        cal.set(Calendar.SECOND, 0);
        cal.set(Calendar.MILLISECOND, 0);
        int minute = cal.get(Calendar.MINUTE);
        int targetMinute = (minute / 10) * 10 + RING_SYNC_OFFSET_MINUTE;
        if (minute > targetMinute || (minute == targetMinute && Calendar.getInstance().get(Calendar.SECOND) > 0)) {
            targetMinute += 10;
        }
        if (targetMinute >= 60) {
            cal.add(Calendar.HOUR_OF_DAY, 1);
            targetMinute -= 60;
        }
        cal.set(Calendar.MINUTE, targetMinute);
        long delay = cal.getTimeInMillis() - System.currentTimeMillis();
        if (delay <= 0) delay += RING_SYNC_INTERVAL;
        if (delay < 1000) delay = 1000;
        return delay;
    }

    private synchronized void startMiBandSyncThread() {
        if (miBandSyncThread != null && miBandSyncThread.isAlive()) return;
        miBandSyncThread = new Thread(() -> {
            Log.i(TAG, "⌚ Mi Band sync thread started");
            while (shouldRun) {
                try {
                    if (miBandRuntime == null || !miBandRuntime.hasConfig()) {
                        waitForMiBand(60_000L);
                        continue;
                    }
                    if (!miBandRuntime.status().authenticated) {
                        miBandRuntime.autoConnect();
                        waitForMiBandReady(20_000L);
                    }
                    if (miBandRuntime.status().authenticated
                            && !miBandRuntime.status().realtime
                            && !miBandRuntime.status().syncing) {
                        miBandRuntime.syncNow();
                    }
                    long delay = MiBandSyncSchedule.nextDelayMillis(
                            Calendar.getInstance(), miBandRuntime.settings());
                    waitForMiBand(delay < 0 ? 60_000L : delay);
                } catch (InterruptedException stopped) {
                    Thread.currentThread().interrupt();
                    break;
                } catch (Exception error) {
                    Log.w(TAG, "⌚ Mi Band scheduler: " + error.getMessage());
                    try { waitForMiBand(30_000L); }
                    catch (InterruptedException stopped) {
                        Thread.currentThread().interrupt();
                        break;
                    }
                }
            }
            Log.i(TAG, "⌚ Mi Band sync thread exiting");
        }, "AionMiBandSync");
        miBandSyncThread.setDaemon(false);
        miBandSyncThread.start();
    }

    private void waitForMiBandReady(long timeoutMillis) throws InterruptedException {
        long deadline = System.currentTimeMillis() + timeoutMillis;
        while (shouldRun && System.currentTimeMillis() < deadline) {
            if (miBandRuntime.status().authenticated) return;
            Thread.sleep(500L);
        }
    }

    private void waitForMiBand(long delayMillis) throws InterruptedException {
        synchronized (miBandSyncSignal) {
            if (shouldRun) miBandSyncSignal.wait(Math.max(1_000L, delayMillis));
        }
    }

    private void wakeMiBandScheduler() {
        synchronized (miBandSyncSignal) {
            miBandSyncSignal.notifyAll();
        }
    }

    private class RingBackgroundSync {
        private static final String PREFS_NAME = "aion_ring_ble";
        private static final String KEY_DEVICE_ADDRESS = "device_address";
        private static final String KEY_DEVICE_NAME = "device_name";
        private static final String KEY_SYNC_FAIL_COUNT = "bg_sync_fail_count";
        private static final String KEY_NEXT_SYNC_ATTEMPT_AT = "bg_next_sync_attempt_at";
        private static final String KEY_LAST_SYNC_FAILURE = "bg_last_sync_failure";
        private static final String KEY_PAGE_CONNECTED = "page_connection_active";
        private static final String KEY_PAGE_CONNECTED_AT = "page_connection_active_at";
        private static final long PAGE_CONNECTION_STALE_MS = 15 * 60_000L;
        private static final long HEALTH_HISTORY_TIMEOUT_SECONDS = 15;
        private static final long HEALTH_ACCUMULATE_IDLE_MS = 5_000L;
        private static final int MAX_RING_CONNECT_ATTEMPTS = 2;
        private static final long RING_CONNECT_RETRY_DELAY_MS = 1_500L;
        private static final int DT_SETTING_TIME = 0x0100;
        private static final int DT_SETTING_HEART_MONITOR = 0x010C;
        private static final int DT_GET_DEVICE_INFO = 0x0201;
        private static final int DT_GET_CHIP_SCHEME = 0x021B;
        private static final int DT_GET_POWER = 0x0225;
        private static final int DT_HEALTH_ALL = 0x0509;
        private static final int DT_HEALTH_ALL_ACK = 0x0518;
        private static final int DT_HEALTH_BLOCK = 0x0580;
        private static final int HEART_MONITOR_INTERVAL_MIN = 10;

        private final UUID cccdUuid = UUID.fromString("00002902-0000-1000-8000-00805f9b34fb");
        private final RingBleService[] services = new RingBleService[] {
                new RingBleService("be940000-7333-be46-b7ae-689e71722bd5", "be940001-7333-be46-b7ae-689e71722bd5", "be940003-7333-be46-b7ae-689e71722bd5", "Main"),
                new RingBleService("6e400001-b5a3-f393-e0a9-e50e24dcca9e", "6e400002-b5a3-f393-e0a9-e50e24dcca9e", "6e400003-b5a3-f393-e0a9-e50e24dcca9e", "UART"),
                new RingBleService("0000ae00-0000-1000-8000-00805f9b34fb", "0000ae01-0000-1000-8000-00805f9b34fb", "0000ae02-0000-1000-8000-00805f9b34fb", "JieLi")
        };

        private BluetoothAdapter adapter;
        private BluetoothLeScanner scanner;
        private BluetoothGatt gatt;
        private BluetoothGattCharacteristic writeChar;
        private BluetoothDevice currentDevice;
        private CountDownLatch connectLatch;
        private CountDownLatch writeLatch;
        private volatile int lastWriteStatus = BluetoothGatt.GATT_FAILURE;
        private CountDownLatch healthLatch;
        private final ArrayList<BluetoothGattCharacteristic> notificationChars = new ArrayList<>();
        private int notificationIndex = 0;
        private final Object payloadLock = new Object();
        private final ArrayList<byte[]> healthPayloads = new ArrayList<>();
        private byte[] reassemblyData;
        private volatile boolean connected = false;
        private volatile boolean healthDone = false;
        private int healthRequestGeneration = 0;
        private int healthPayloadVersion = 0;
        private int healthPayloadCount = 0;
        private int healthPayloadBytes = 0;
        private String healthFinishReason = "";
        private volatile BluetoothDevice scanMatch;
        private volatile CountDownLatch scanLatch;
        private volatile String scanTargetAddress = "";
        private volatile String scanTargetName = "";
        private final AtomicInteger pageTakeoverGeneration = new AtomicInteger(0);
        private String deviceName = "";

        RingBackgroundSync() {
            BluetoothManager bm = (BluetoothManager) getSystemService(Context.BLUETOOTH_SERVICE);
            if (bm != null) adapter = bm.getAdapter();
        }

        void acquireConnectionForBackground() {
            SharedPreferences prefs = getSharedPreferences(PREFS_NAME, MODE_PRIVATE);
            String savedAddress = prefs.getString(KEY_DEVICE_ADDRESS, "");
            String savedName = prefs.getString(KEY_DEVICE_NAME, "");
            String httpBase = getHttpBase();
            if ((savedAddress == null || savedAddress.isEmpty())
                    && (savedName == null || savedName.isEmpty())) {
                Log.d(TAG, "💍 immediate handoff skipped: no saved ring device");
                return;
            }
            if (!hasBluetoothPermissionsForRing()) {
                Log.w(TAG, "💍 immediate handoff skipped: bluetooth permissions missing");
                return;
            }
            int operationGeneration = pageTakeoverGeneration.get();
            try {
                BluetoothDevice device = connectKnownSavedDevice(
                        savedAddress, savedName, operationGeneration);
                if (isPageTakeoverRequested(operationGeneration)) {
                    Log.i(TAG, "💍 immediate background handoff yielded to health page");
                    return;
                }
                if (device == null) {
                    recordRingSyncFailure(prefs, "ring_not_found_during_handoff");
                    Log.w(TAG, "💍 immediate handoff could not find the released ring");
                    if (httpBase != null) {
                        postRingDiag(httpBase, "handoff_ring_not_found",
                                "页面释放连接后未能立即扫描到戒指" + backoffDiagSuffix(prefs),
                                0, 0, 0);
                    }
                    return;
                }
                syncTimeAndMonitorSetting();
                recordRingSyncSuccess(prefs);
                Log.i(TAG, "💍 background BLE owner acquired and will keep GATT connected");
                if (httpBase != null) {
                    postRingDiag(httpBase, "handoff_connected",
                            "健康页离开后，后台原生服务已立即接管并保持戒指连接",
                            1, 0, 0);
                }
            } catch (Exception e) {
                if (isPageTakeoverRequested(operationGeneration)) {
                    Log.i(TAG, "💍 immediate background handoff yielded to health page");
                    return;
                }
                recordRingSyncFailure(prefs,
                        "handoff_" + e.getClass().getSimpleName() + ": " + e.getMessage());
                if (!isReadyConnection()) close();
                if (httpBase != null) {
                    postRingDiag(httpBase, "handoff_failed",
                            "后台立即接管戒指失败：" + e.getMessage() + backoffDiagSuffix(prefs),
                            0, 0, 0);
                }
                throw e;
            } finally {
                useBalancedConnectionPriority();
            }
        }

        void syncComprehensiveSnapshotOnce() {
            String httpBase = getHttpBase();
            if (httpBase == null) {
                Log.d(TAG, "💍 no server url yet");
                return;
            }
            SharedPreferences prefs = getSharedPreferences(PREFS_NAME, MODE_PRIVATE);
            String savedAddress = prefs.getString(KEY_DEVICE_ADDRESS, "");
            String savedName = prefs.getString(KEY_DEVICE_NAME, "");
            if ((savedAddress == null || savedAddress.isEmpty()) && (savedName == null || savedName.isEmpty())) {
                Log.d(TAG, "💍 no saved ring device");
                postRingDiag(httpBase, "no_saved_device", "没有保存过戒指设备，后台无法自动同步", 0, 0, 0);
                return;
            }
            if (shouldSkipRingSyncForPageConnection(prefs, httpBase)) return;
            if (!isReadyConnection() && shouldSkipRingSyncForBackoff(prefs, httpBase)) return;
            if (!hasBluetoothPermissionsForRing()) {
                Log.w(TAG, "💍 bluetooth permissions missing");
                postRingDiag(httpBase, "bluetooth_permission_missing", "蓝牙权限缺失，后台无法连接戒指", 0, 0, 0);
                return;
            }
            int operationGeneration = pageTakeoverGeneration.get();
            try {
                BluetoothDevice device = refreshBackgroundConnectionForScheduledSync(
                        savedAddress, savedName, operationGeneration);
                if (isPageTakeoverRequested(operationGeneration)) {
                    Log.i(TAG, "💍 background sync yielded to health page during connection");
                    return;
                }
                if (device == null) {
                    Log.w(TAG, "💍 saved ring not found");
                    recordRingSyncFailure(prefs, "ring_not_found");
                    postRingDiag(httpBase, "ring_not_found", "没有扫描到已保存戒指 savedName=" + savedName + backoffDiagSuffix(prefs), 0, 0, 0);
                    return;
                }
                syncTimeAndMonitorSetting();
                RingHealthSnapshot snapshot = requestComprehensiveSnapshot();
                if (snapshot == null) {
                    recordRingSyncSuccess(prefs);
                    Log.i(TAG, "💍 no comprehensive health records");
                    postRingDiag(httpBase, "no_records", "已连接戒指，但本次没有读到新的综合健康数据", 0, 0, 0);
                    return;
                }
                postRingSnapshot(httpBase, snapshot);
                recordRingSyncSuccess(prefs);
                Log.i(TAG, "💍 comprehensive health snapshot synced");
                postRingDiag(
                        httpBase,
                        "ok",
                        "后台综合健康同步完成"
                                + "; steps=" + snapshot.steps
                                + "; heartRate=" + snapshot.heartRate
                                + "; bloodPressure=" + snapshot.systolicBp + "/" + snapshot.diastolicBp
                                + "; spo2=" + snapshot.spo2
                                + "; " + healthCompletionDiag(),
                        1,
                        1,
                        snapshot.measuredAt
                );
            } catch (Exception e) {
                if (isPageTakeoverRequested(operationGeneration)) {
                    Log.i(TAG, "💍 background sync yielded to health page");
                    return;
                }
                recordRingSyncFailure(prefs, e.getClass().getSimpleName() + ": " + e.getMessage());
                postRingDiag(httpBase, "sync_failed", "后台综合健康同步失败：" + e.getMessage() + backoffDiagSuffix(prefs), 0, 0, 0);
                throw e;
            } finally {
                useBalancedConnectionPriority();
            }
        }

        private BluetoothDevice ensureBackgroundConnection(
                String savedAddress, String savedName, int operationGeneration) {
            if (connected && writeChar != null && gatt != null) {
                Log.i(TAG, "💍 reusing ready background GATT");
                try {
                    gatt.requestConnectionPriority(BluetoothGatt.CONNECTION_PRIORITY_HIGH);
                } catch (Exception ignored) {}
                return currentDevice;
            }
            BluetoothDevice device = resolveSavedDevice(
                    savedAddress, savedName, operationGeneration);
            if (device == null || isPageTakeoverRequested(operationGeneration)) return null;
            connectSavedDeviceWithRetry(
                    device, savedAddress, savedName, operationGeneration);
            return currentDevice;
        }

        private BluetoothDevice refreshBackgroundConnectionForScheduledSync(
                String savedAddress, String savedName, int operationGeneration) {
            if (isReadyConnection()) {
                Log.i(TAG, "💍 refreshing held background GATT before scheduled data request");
                close();
                // Closing the held client makes the ring advertise immediately;
                // a brief pause lets Android finish unregistering the old GATT.
                sleepQuiet(350);
            }
            return connectKnownSavedDevice(savedAddress, savedName, operationGeneration);
        }

        private BluetoothDevice connectKnownSavedDevice(
                String savedAddress, String savedName, int operationGeneration) {
            if (adapter != null && adapter.isEnabled()
                    && savedAddress != null && !savedAddress.isEmpty()) {
                try {
                    BluetoothDevice knownDevice = adapter.getRemoteDevice(savedAddress);
                    Log.i(TAG, "💍 direct connecting known saved ring address");
                    connectSavedDeviceWithRetry(
                            knownDevice, savedAddress, savedName, operationGeneration);
                    return currentDevice;
                } catch (IllegalArgumentException e) {
                    Log.w(TAG, "💍 invalid saved ring address; falling back to scan");
                }
            }
            return ensureBackgroundConnection(savedAddress, savedName, operationGeneration);
        }

        private boolean isReadyConnection() {
            return connected && writeChar != null && gatt != null;
        }

        String status() {
            return "ready=" + isReadyConnection()
                    + ", connected=" + connected
                    + ", gatt=" + (gatt != null)
                    + ", writeChar=" + (writeChar != null)
                    + ", device=" + deviceName;
        }

        private void useBalancedConnectionPriority() {
            BluetoothGatt activeGatt = gatt;
            if (!connected || activeGatt == null) return;
            try {
                activeGatt.requestConnectionPriority(BluetoothGatt.CONNECTION_PRIORITY_BALANCED);
            } catch (Exception ignored) {}
        }

        private boolean shouldSkipRingSyncForPageConnection(SharedPreferences prefs, String httpBase) {
            long now = System.currentTimeMillis();
            long connectedAt = prefs.getLong(KEY_PAGE_CONNECTED_AT, 0);
            long ageMs = connectedAt > 0 ? now - connectedAt : Long.MAX_VALUE;
            if (!prefs.getBoolean(KEY_PAGE_CONNECTED, false)
                    || connectedAt <= 0
                    || ageMs > PAGE_CONNECTION_STALE_MS) {
                return false;
            }
            Log.i(TAG, "💍 skip background ring sync, health page owns connection");
            postRingDiag(
                    httpBase,
                    "page_connection_active",
                    "健康页正在保持戒指连接，交给页面原生定时同步"
                            + "; pageConnectedAgeMs=" + ageMs
                            + "; pageConnectedAtMs=" + connectedAt,
                    0,
                    0,
                    0
            );
            return true;
        }

        private boolean shouldSkipRingSyncForBackoff(SharedPreferences prefs, String httpBase) {
            long now = System.currentTimeMillis();
            long nextAttemptAt = prefs.getLong(KEY_NEXT_SYNC_ATTEMPT_AT, 0);
            if (nextAttemptAt <= now) return false;
            int failCount = prefs.getInt(KEY_SYNC_FAIL_COUNT, 0);
            long waitMs = nextAttemptAt - now;
            String reason = prefs.getString(KEY_LAST_SYNC_FAILURE, "");
            Log.i(TAG, "💍 skip background ring sync, backoff " + (waitMs / 1000) + "s");
            postRingDiag(
                    httpBase,
                    "backoff",
                    "后台戒指同步暂停以保护电量"
                            + "; failCount=" + failCount
                            + "; nextAttemptAtMs=" + nextAttemptAt
                            + "; waitMs=" + waitMs
                            + "; lastFailure=" + reason,
                    0,
                    0,
                    0
            );
            return true;
        }

        private void recordRingSyncSuccess(SharedPreferences prefs) {
            prefs.edit()
                    .remove(KEY_SYNC_FAIL_COUNT)
                    .remove(KEY_NEXT_SYNC_ATTEMPT_AT)
                    .remove(KEY_LAST_SYNC_FAILURE)
                    .apply();
        }

        private void recordRingSyncFailure(SharedPreferences prefs, String reason) {
            int failCount = prefs.getInt(KEY_SYNC_FAIL_COUNT, 0) + 1;
            long failedAt = System.currentTimeMillis();
            long nextAttemptAt = RingSyncSchedule.alignFailureRetryAt(
                    failedAt,
                    RING_SYNC_OFFSET_MINUTE,
                    RING_SYNC_INTERVAL);
            prefs.edit()
                    .putInt(KEY_SYNC_FAIL_COUNT, failCount)
                    .putLong(KEY_NEXT_SYNC_ATTEMPT_AT, nextAttemptAt)
                    .putString(KEY_LAST_SYNC_FAILURE, reason == null ? "" : reason)
                    .apply();
        }

        private String backoffDiagSuffix(SharedPreferences prefs) {
            return "; failCount=" + prefs.getInt(KEY_SYNC_FAIL_COUNT, 0)
                    + "; nextAttemptAtMs=" + prefs.getLong(KEY_NEXT_SYNC_ATTEMPT_AT, 0)
                    + "; lastFailure=" + prefs.getString(KEY_LAST_SYNC_FAILURE, "");
        }

        private BluetoothDevice resolveSavedDevice(
                String savedAddress, String savedName, int operationGeneration) {
            if (isPageTakeoverRequested(operationGeneration)) return null;
            if (adapter == null || !adapter.isEnabled()) return null;
            scanner = adapter.getBluetoothLeScanner();
            if (scanner != null) {
                scanTargetAddress = savedAddress == null ? "" : savedAddress;
                scanTargetName = savedName == null ? "" : savedName;
                scanMatch = null;
                scanLatch = new CountDownLatch(1);
                try {
                    scanner.startScan(scanCallback);
                    if (isPageTakeoverRequested(operationGeneration)) return null;
                    scanLatch.await(12, TimeUnit.SECONDS);
                } catch (Exception e) {
                    Log.w(TAG, "💍 scan failed: " + e.getMessage());
                } finally {
                    try { scanner.stopScan(scanCallback); } catch (Exception ignored) {}
                }
                if (scanMatch != null) return scanMatch;
            }
            return null;
        }

        private void connectSavedDeviceWithRetry(
                BluetoothDevice initialDevice,
                String savedAddress,
                String savedName,
                int operationGeneration) {
            RuntimeException lastError = null;
            BluetoothDevice device = initialDevice;
            for (int attempt = 1; attempt <= MAX_RING_CONNECT_ATTEMPTS; attempt++) {
                if (isPageTakeoverRequested(operationGeneration)) return;
                if (device == null) {
                    lastError = new IllegalStateException("saved ring not found");
                } else {
                    try {
                        Log.i(TAG, "💍 background ring connect attempt " + attempt
                                + "/" + MAX_RING_CONNECT_ATTEMPTS);
                        connect(device, operationGeneration);
                        return;
                    } catch (RuntimeException e) {
                        if (isPageTakeoverRequested(operationGeneration)) return;
                        lastError = e;
                        Log.w(TAG, "💍 background ring connect attempt " + attempt
                                + " failed: " + e.getMessage());
                    }
                }
                close();
                if (attempt < MAX_RING_CONNECT_ATTEMPTS) {
                    sleepQuiet(RING_CONNECT_RETRY_DELAY_MS);
                    device = resolveSavedDevice(
                            savedAddress, savedName, operationGeneration);
                }
            }
            if (lastError != null) throw lastError;
            throw new IllegalStateException("ring connect failed");
        }

        private final ScanCallback scanCallback = new ScanCallback() {
            @Override
            public void onScanResult(int callbackType, ScanResult result) {
                BluetoothDevice dev = result.getDevice();
                String name = getDeviceName(dev);
                if (!matchesSavedBackgroundDevice(dev, name)) return;
                scanMatch = dev;
                CountDownLatch latch = scanLatch;
                if (latch != null) latch.countDown();
            }
        };

        private boolean matchesSavedBackgroundDevice(BluetoothDevice dev, String name) {
            String address = "";
            try { address = dev.getAddress(); } catch (Exception ignored) {}
            if (!scanTargetAddress.isEmpty()
                    && scanTargetAddress.equalsIgnoreCase(address)) return true;
            return !scanTargetName.isEmpty() && scanTargetName.equals(name);
        }

        private void connect(BluetoothDevice device, int operationGeneration) {
            if (isPageTakeoverRequested(operationGeneration)) return;
            close();
            currentDevice = device;
            deviceName = getDeviceName(device);
            connectLatch = new CountDownLatch(1);
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
                gatt = device.connectGatt(AionPushService.this, false, gattCallback, BluetoothDevice.TRANSPORT_LE);
            } else {
                gatt = device.connectGatt(AionPushService.this, false, gattCallback);
            }
            if (isPageTakeoverRequested(operationGeneration)) {
                close();
                return;
            }
            try {
                boolean ready = connectLatch.await(20, TimeUnit.SECONDS);
                if (isPageTakeoverRequested(operationGeneration)) return;
                if (!ready || !connected || writeChar == null) {
                    throw new IllegalStateException("ring connect timeout");
                }
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                throw new IllegalStateException("ring connect interrupted");
            }
            try { Thread.sleep(500); } catch (InterruptedException e) { Thread.currentThread().interrupt(); }
        }

        private final BluetoothGattCallback gattCallback = new BluetoothGattCallback() {
            @Override
            public void onConnectionStateChange(BluetoothGatt g, int status, int newState) {
                if (g != gatt) return;
                if (status != BluetoothGatt.GATT_SUCCESS) {
                    connected = false;
                    CountDownLatch latch = connectLatch;
                    if (latch != null) latch.countDown();
                    return;
                }
                if (newState == BluetoothProfile.STATE_CONNECTED) {
                    try { g.requestConnectionPriority(BluetoothGatt.CONNECTION_PRIORITY_HIGH); } catch (Exception ignored) {}
                    mainHandler.postDelayed(g::discoverServices, 700);
                } else if (newState == BluetoothProfile.STATE_DISCONNECTED) {
                    connected = false;
                }
            }

            @Override
            public void onServicesDiscovered(BluetoothGatt g, int status) {
                if (g != gatt) return;
                if (status != BluetoothGatt.GATT_SUCCESS) {
                    CountDownLatch latch = connectLatch;
                    if (latch != null) latch.countDown();
                    return;
                }
                setupRingService(g);
            }

            @Override
            public void onCharacteristicChanged(BluetoothGatt g, BluetoothGattCharacteristic c) {
                if (g != gatt) return;
                processIncoming(c.getValue());
            }

            @Override
            public void onCharacteristicChanged(BluetoothGatt g, BluetoothGattCharacteristic c, byte[] value) {
                if (g != gatt) return;
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                    processIncoming(value);
                }
            }

            @Override
            public void onCharacteristicWrite(BluetoothGatt g, BluetoothGattCharacteristic c, int status) {
                if (g != gatt) return;
                lastWriteStatus = status;
                CountDownLatch latch = writeLatch;
                if (latch != null) latch.countDown();
            }

            @Override
            public void onDescriptorWrite(BluetoothGatt g, BluetoothGattDescriptor d, int status) {
                if (g != gatt) return;
                if (status != BluetoothGatt.GATT_SUCCESS) {
                    failNotificationSetup("descriptor status=" + status);
                    return;
                }
                notificationIndex++;
                enableNextNotification(g);
            }
        };

        private void setupRingService(BluetoothGatt g) {
            BluetoothGattService matchedService = null;
            RingBleService matched = null;
            for (RingBleService svc : services) {
                matchedService = g.getService(svc.service);
                if (matchedService != null) {
                    matched = svc;
                    break;
                }
            }
            if (matchedService == null || matched == null) {
                CountDownLatch latch = connectLatch;
                if (latch != null) latch.countDown();
                return;
            }
            writeChar = matchedService.getCharacteristic(matched.write);
            if (writeChar == null) {
                CountDownLatch latch = connectLatch;
                if (latch != null) latch.countDown();
                return;
            }
            if ((writeChar.getProperties() & BluetoothGattCharacteristic.PROPERTY_WRITE) != 0) {
                writeChar.setWriteType(BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT);
            } else {
                writeChar.setWriteType(BluetoothGattCharacteristic.WRITE_TYPE_NO_RESPONSE);
            }
            notificationChars.clear();
            notificationIndex = 0;
            addNotificationCharacteristic(matchedService.getCharacteristic(matched.notify));
            addNotificationCharacteristic(writeChar);
            for (BluetoothGattCharacteristic ch : matchedService.getCharacteristics()) {
                addNotificationCharacteristic(ch);
            }
            if (notificationChars.isEmpty()) {
                failNotificationSetup("no notify characteristics");
                return;
            }
            Log.i(TAG, "💍 background ring service found: " + matched.name
                    + "; notifyChars=" + notificationChars.size());
            enableNextNotification(g);
        }

        private void addNotificationCharacteristic(BluetoothGattCharacteristic ch) {
            if (ch == null) return;
            int props = ch.getProperties();
            if ((props & BluetoothGattCharacteristic.PROPERTY_NOTIFY) == 0
                    && (props & BluetoothGattCharacteristic.PROPERTY_INDICATE) == 0) return;
            for (BluetoothGattCharacteristic existing : notificationChars) {
                if (existing.getUuid().equals(ch.getUuid())) return;
            }
            notificationChars.add(ch);
        }

        private void enableNextNotification(BluetoothGatt g) {
            if (g != gatt) return;
            if (notificationIndex >= notificationChars.size()) {
                finishNotificationSetup();
                return;
            }
            BluetoothGattCharacteristic ch = notificationChars.get(notificationIndex);
            Log.i(TAG, "💍 subscribing notify " + (notificationIndex + 1)
                    + "/" + notificationChars.size() + " " + ch.getUuid());
            enableNotify(g, ch);
        }

        private void finishNotificationSetup() {
            connected = true;
            Log.i(TAG, "💍 background ring service ready; all notifications subscribed");
            CountDownLatch latch = connectLatch;
            if (latch != null) latch.countDown();
        }

        private void failNotificationSetup(String reason) {
            connected = false;
            Log.w(TAG, "💍 notification setup failed: " + reason);
            CountDownLatch latch = connectLatch;
            if (latch != null) latch.countDown();
        }

        @SuppressWarnings("deprecation")
        private void enableNotify(BluetoothGatt g, BluetoothGattCharacteristic ch) {
            try {
                boolean notificationEnabled = g.setCharacteristicNotification(ch, true);
                if (!notificationEnabled) {
                    failNotificationSetup("setCharacteristicNotification returned false");
                    return;
                }
                BluetoothGattDescriptor desc = ch.getDescriptor(cccdUuid);
                if (desc == null) {
                    notificationIndex++;
                    enableNextNotification(g);
                    return;
                }
                byte[] value = (ch.getProperties() & BluetoothGattCharacteristic.PROPERTY_INDICATE) != 0
                        ? BluetoothGattDescriptor.ENABLE_INDICATION_VALUE
                        : BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE;
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                    int result = g.writeDescriptor(desc, value);
                    if (result != android.bluetooth.BluetoothStatusCodes.SUCCESS) {
                        failNotificationSetup("writeDescriptor result=" + result);
                    }
                } else {
                    desc.setValue(value);
                    if (!g.writeDescriptor(desc)) {
                        failNotificationSetup("writeDescriptor returned false");
                    }
                }
            } catch (Exception e) {
                failNotificationSetup(e.getClass().getSimpleName() + ": " + e.getMessage());
            }
        }

        private void syncTimeAndMonitorSetting() {
            sleepQuiet(350);
            Calendar now = Calendar.getInstance();
            int dow = now.get(Calendar.DAY_OF_WEEK) - Calendar.MONDAY;
            if (dow < 0) dow = 6;
            byte[] timePayload = new byte[] {
                    (byte) (now.get(Calendar.YEAR) & 0xFF),
                    (byte) ((now.get(Calendar.YEAR) >> 8) & 0xFF),
                    (byte) (now.get(Calendar.MONTH) + 1),
                    (byte) now.get(Calendar.DAY_OF_MONTH),
                    (byte) now.get(Calendar.HOUR_OF_DAY),
                    (byte) now.get(Calendar.MINUTE),
                    (byte) now.get(Calendar.SECOND),
                    (byte) dow
            };
            writePacket(DT_SETTING_TIME, timePayload);
            sleepQuiet(500);
            writePacket(DT_GET_DEVICE_INFO, new byte[] {(byte) 0xFF, 0x46});
            sleepQuiet(500);
            writePacket(DT_GET_CHIP_SCHEME, new byte[0]);
            sleepQuiet(500);
            writePacket(DT_GET_POWER, new byte[0]);
            sleepQuiet(500);
            writePacket(DT_SETTING_HEART_MONITOR, new byte[] {1, HEART_MONITOR_INTERVAL_MIN});
            sleepQuiet(350);
        }

        private RingHealthSnapshot requestComprehensiveSnapshot() {
            CountDownLatch requestLatch;
            synchronized (payloadLock) {
                healthPayloads.clear();
                reassemblyData = null;
                healthDone = false;
                healthRequestGeneration++;
                healthPayloadVersion = 0;
                healthPayloadCount = 0;
                healthPayloadBytes = 0;
                healthFinishReason = "";
                requestLatch = new CountDownLatch(1);
                healthLatch = requestLatch;
            }
            writePacket(DT_HEALTH_ALL, new byte[0]);
            boolean completed = false;
            try { completed = requestLatch.await(HEALTH_HISTORY_TIMEOUT_SECONDS, TimeUnit.SECONDS); }
            catch (InterruptedException e) { Thread.currentThread().interrupt(); }
            if (!completed || !healthDone) {
                throw new IllegalStateException(
                        "comprehensive health history timeout; " + healthCompletionDiag());
            }
            return parseComprehensiveSnapshot();
        }

        private void processIncoming(byte[] raw) {
            if (raw == null || raw.length < 4) return;
            byte[] packet;
            byte[] rest = null;
            synchronized (payloadLock) {
                if (reassemblyData != null) {
                    byte[] combined = new byte[reassemblyData.length + raw.length];
                    System.arraycopy(reassemblyData, 0, combined, 0, reassemblyData.length);
                    System.arraycopy(raw, 0, combined, reassemblyData.length, raw.length);
                    raw = combined;
                    reassemblyData = null;
                }
                int declaredLen = ((raw[2] & 0xFF) | ((raw[3] & 0xFF) << 8));
                if (declaredLen <= 0 || raw.length < declaredLen) {
                    reassemblyData = raw;
                    return;
                }
                packet = new byte[declaredLen];
                System.arraycopy(raw, 0, packet, 0, declaredLen);
                if (raw.length > declaredLen) {
                    rest = new byte[raw.length - declaredLen];
                    System.arraycopy(raw, declaredLen, rest, 0, rest.length);
                }
            }
            processPacket(packet);
            if (rest != null) processIncoming(rest);
        }

        private void processPacket(byte[] pkt) {
            if (pkt.length < 6) return;
            int dataType = ((pkt[0] & 0xFF) << 8) | (pkt[1] & 0xFF);
            int totalLen = (pkt[2] & 0xFF) | ((pkt[3] & 0xFF) << 8);
            int payloadLen = totalLen - 6;
            if (payloadLen < 0 || pkt.length < totalLen) return;
            byte[] payload = new byte[payloadLen];
            if (payloadLen > 0) System.arraycopy(pkt, 4, payload, 0, payloadLen);
            if (dataType == DT_HEALTH_ALL) {
                if (payload.length >= 2) {
                    int count = (payload[0] & 0xFF) | ((payload[1] & 0xFF) << 8);
                    if (count == 0) finishHealthHistory("zero_count");
                }
                return;
            }
            if (dataType == DT_HEALTH_ALL_ACK) {
                if (payload.length == 0) {
                    finishHealthHistory("empty_ack");
                } else {
                    int requestGeneration;
                    int payloadVersion;
                    synchronized (payloadLock) {
                        if (healthDone) return;
                        healthPayloads.add(payload);
                        healthPayloadCount++;
                        healthPayloadBytes += payload.length;
                        healthPayloadVersion++;
                        requestGeneration = healthRequestGeneration;
                        payloadVersion = healthPayloadVersion;
                    }
                    scheduleHealthIdleFinish(requestGeneration, payloadVersion);
                }
                return;
            }
            if (dataType == DT_HEALTH_BLOCK) finishHealthHistory("block");
        }

        private void scheduleHealthIdleFinish(int requestGeneration, int payloadVersion) {
            mainHandler.postDelayed(() -> {
                synchronized (payloadLock) {
                    if (requestGeneration != healthRequestGeneration
                            || payloadVersion != healthPayloadVersion
                            || healthDone) return;
                    finishHealthHistory("idle");
                }
            }, HEALTH_ACCUMULATE_IDLE_MS);
        }

        private void finishHealthHistory(String reason) {
            CountDownLatch latch;
            synchronized (payloadLock) {
                if (healthDone) return;
                healthDone = true;
                healthFinishReason = reason == null ? "" : reason;
                latch = healthLatch;
            }
            if (latch != null) latch.countDown();
        }

        private String healthCompletionDiag() {
            synchronized (payloadLock) {
                return "payloadCount=" + healthPayloadCount
                        + "; payloadBytes=" + healthPayloadBytes
                        + "; finishReason=" + healthFinishReason;
            }
        }

        private RingHealthSnapshot parseComprehensiveSnapshot() {
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            synchronized (payloadLock) {
                for (byte[] p : healthPayloads) out.write(p, 0, p.length);
            }
            return RingHealthSnapshot.latestFromPayload(out.toByteArray(), localEpoch2000Seconds());
        }

        private void postRingSnapshot(String httpBase, RingHealthSnapshot snapshot) {
            try {
                JSONObject existingRing = fetchExistingRingSnapshot(httpBase);
                JSONObject raw = parseExistingRingRaw(existingRing.optString("raw_json", ""));
                raw.put("all", snapshot.toJson());

                JSONObject body = new JSONObject();
                String resolvedDeviceName = deviceName == null || deviceName.isEmpty()
                        ? existingRing.optString("device_name", "")
                        : deviceName;
                body.put("device_name", resolvedDeviceName);
                body.put("heart_rate", valueOrNull(snapshot.heartRate));
                body.put("systolic_bp", valueOrNull(snapshot.systolicBp));
                body.put("diastolic_bp", valueOrNull(snapshot.diastolicBp));
                body.put("spo2", valueOrNull(snapshot.spo2));
                body.put("hrv", valueOrNull(snapshot.hrv));
                body.put("measured_at", snapshot.measuredAt);
                Object sleepStart = existingRing.opt("sleep_start_at");
                Object sleepEnd = existingRing.opt("sleep_end_at");
                if (hasJsonValue(sleepStart) && hasJsonValue(sleepEnd)) {
                    JSONObject sleep = new JSONObject();
                    sleep.put("start_at", sleepStart);
                    sleep.put("end_at", sleepEnd);
                    putExistingValue(sleep, "total_min", existingRing.opt("sleep_total_min"));
                    putExistingValue(sleep, "deep_min", existingRing.opt("sleep_deep_min"));
                    putExistingValue(sleep, "light_min", existingRing.opt("sleep_light_min"));
                    putExistingValue(sleep, "rem_min", existingRing.opt("sleep_rem_min"));
                    putExistingValue(sleep, "wake_min", existingRing.opt("sleep_wake_min"));
                    putExistingValue(sleep, "wake_count", existingRing.opt("sleep_wake_count"));
                    body.put("sleep", sleep);
                }
                body.put("raw", raw);

                MediaType JSON = MediaType.get("application/json; charset=utf-8");
                RequestBody reqBody = RequestBody.create(body.toString(), JSON);
                Request req = new Request.Builder()
                        .url(httpBase + "/api/health/ring/latest")
                        .post(reqBody)
                        .build();
                try (Response resp = client.newCall(req).execute()) {
                    if (!resp.isSuccessful()) {
                        throw new IllegalStateException("ring snapshot upload failed http=" + resp.code());
                    }
                    Log.i(TAG, "💍 posted comprehensive health snapshot → " + resp.code());
                }
            } catch (Exception e) {
                if (e instanceof RuntimeException) throw (RuntimeException) e;
                throw new IllegalStateException("ring snapshot upload failed: " + e.getMessage(), e);
            }
        }

        private JSONObject fetchExistingRingSnapshot(String httpBase) {
            Request req = new Request.Builder()
                    .url(httpBase + "/api/health/summary")
                    .get()
                    .build();
            try (Response resp = client.newCall(req).execute()) {
                if (!resp.isSuccessful()) {
                    throw new IllegalStateException("health summary fetch failed http=" + resp.code());
                }
                String responseBody = resp.body() == null ? "" : resp.body().string();
                JSONObject ring = new JSONObject(responseBody).optJSONObject("ring");
                return ring == null ? new JSONObject() : ring;
            } catch (Exception e) {
                if (e instanceof RuntimeException) throw (RuntimeException) e;
                throw new IllegalStateException("health summary fetch failed: " + e.getMessage(), e);
            }
        }

        private JSONObject parseExistingRingRaw(String rawJson) {
            if (rawJson == null || rawJson.trim().isEmpty()) return new JSONObject();
            try { return new JSONObject(rawJson); }
            catch (Exception ignored) { return new JSONObject(); }
        }

        private Object valueOrNull(int value) {
            return value > 0 ? value : JSONObject.NULL;
        }

        private boolean hasJsonValue(Object value) {
            return value != null && value != JSONObject.NULL;
        }

        private void putExistingValue(JSONObject target, String key, Object value) throws Exception {
            if (hasJsonValue(value)) target.put(key, value);
        }

        private void postRingDiag(String httpBase, String status, String message, int total, int uploaded, double latestMeasuredAt) {
            if (httpBase == null) return;
            try {
                JSONObject body = new JSONObject();
                body.put("status", status);
                body.put("info", message
                        + "; total=" + total
                        + "; uploaded=" + uploaded
                        + "; latestMeasuredAt=" + latestMeasuredAt
                        + "; device=" + deviceName);
                MediaType JSON = MediaType.get("application/json; charset=utf-8");
                RequestBody reqBody = RequestBody.create(body.toString(), JSON);
                Request req = new Request.Builder()
                        .url(httpBase + "/api/health/ring/diag-report")
                        .post(reqBody)
                        .build();
                try (Response resp = client.newCall(req).execute()) {
                    Log.i(TAG, "💍 diag posted " + status + " → " + resp.code());
                }
            } catch (Exception e) {
                Log.e(TAG, "💍 diag post failed: " + e.getMessage());
            }
        }

        @SuppressWarnings("deprecation")
        private void writePacket(int dataType, byte[] payload) {
            if (gatt == null || writeChar == null) throw new IllegalStateException("ring not connected");
            byte[] pkt = buildPacket(dataType, payload);
            writeLatch = new CountDownLatch(1);
            lastWriteStatus = BluetoothGatt.GATT_FAILURE;
            boolean ok;
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                int result = gatt.writeCharacteristic(writeChar, pkt, writeChar.getWriteType());
                ok = result == android.bluetooth.BluetoothStatusCodes.SUCCESS;
            } else {
                writeChar.setValue(pkt);
                ok = gatt.writeCharacteristic(writeChar);
            }
            if (!ok) throw new IllegalStateException("write failed 0x" + Integer.toHexString(dataType));
            try {
                boolean completed = writeLatch.await(3, TimeUnit.SECONDS);
                if (!completed) {
                    throw new IllegalStateException(
                            "write callback timeout 0x" + Integer.toHexString(dataType));
                }
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                throw new IllegalStateException(
                        "write interrupted 0x" + Integer.toHexString(dataType));
            }
            if (lastWriteStatus != BluetoothGatt.GATT_SUCCESS) {
                throw new IllegalStateException(
                        "write callback failed 0x" + Integer.toHexString(dataType)
                                + " status=" + lastWriteStatus);
            }
            Log.i(TAG, "💍 write ok 0x" + Integer.toHexString(dataType));
        }

        private byte[] buildPacket(int dataType, byte[] payload) {
            int payloadLen = payload == null ? 0 : payload.length;
            int totalLen = payloadLen + 6;
            byte[] pkt = new byte[totalLen];
            pkt[0] = (byte) ((dataType >> 8) & 0xFF);
            pkt[1] = (byte) (dataType & 0xFF);
            pkt[2] = (byte) (totalLen & 0xFF);
            pkt[3] = (byte) ((totalLen >> 8) & 0xFF);
            if (payloadLen > 0) System.arraycopy(payload, 0, pkt, 4, payloadLen);
            int crc = crc16(pkt, totalLen - 2);
            pkt[totalLen - 2] = (byte) (crc & 0xFF);
            pkt[totalLen - 1] = (byte) ((crc >> 8) & 0xFF);
            return pkt;
        }

        private int crc16(byte[] data, int len) {
            int crc = 0xFFFF;
            for (int i = 0; i < len; i++) {
                crc = ((crc << 8) & 0xFF00) | ((crc >> 8) & 0x00FF);
                crc ^= data[i] & 0xFF;
                crc ^= (crc & 0xFF) >> 4;
                crc ^= (crc << 12) & 0xFFFF;
                crc ^= ((crc & 0xFF) << 5) & 0xFFFF;
                crc &= 0xFFFF;
            }
            return crc;
        }

        private double localEpoch2000Seconds() {
            Calendar cal = Calendar.getInstance();
            cal.clear();
            cal.set(2000, Calendar.JANUARY, 1, 0, 0, 0);
            return cal.getTimeInMillis() / 1000.0;
        }

        private boolean hasBluetoothPermissionsForRing() {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                return ContextCompat.checkSelfPermission(AionPushService.this, Manifest.permission.BLUETOOTH_SCAN) == PackageManager.PERMISSION_GRANTED
                        && ContextCompat.checkSelfPermission(AionPushService.this, Manifest.permission.BLUETOOTH_CONNECT) == PackageManager.PERMISSION_GRANTED;
            }
            return true;
        }

        private boolean looksLikeRing(ScanResult result, String name) {
            if (name != null) {
                String upper = name.toUpperCase(Locale.US);
                if (upper.contains("SMART RING") || upper.contains("YCBT") || upper.startsWith("DFU")) return true;
            }
            ScanRecord record = result.getScanRecord();
            if (record == null || record.getServiceUuids() == null) return false;
            for (android.os.ParcelUuid uuid : record.getServiceUuids()) {
                for (RingBleService svc : services) {
                    if (svc.service.equals(uuid.getUuid())) return true;
                }
            }
            return false;
        }

        private String getDeviceName(BluetoothDevice dev) {
            try {
                String name = dev.getName();
                return name == null ? "" : name;
            } catch (Exception e) {
                return "";
            }
        }

        private void sleepQuiet(long ms) {
            try { Thread.sleep(ms); }
            catch (InterruptedException e) { Thread.currentThread().interrupt(); }
        }

        private boolean isPageTakeoverRequested(int operationGeneration) {
            return operationGeneration != pageTakeoverGeneration.get();
        }

        void cancelForPageConnection() {
            pageTakeoverGeneration.incrementAndGet();
            try {
                if (scanner != null) scanner.stopScan(scanCallback);
            } catch (Exception ignored) {}
            close();
            CountDownLatch scan = scanLatch;
            CountDownLatch connect = connectLatch;
            CountDownLatch write = writeLatch;
            CountDownLatch health = healthLatch;
            if (scan != null) scan.countDown();
            if (connect != null) connect.countDown();
            if (write != null) write.countDown();
            if (health != null) health.countDown();
        }

        void cancelForFeatureDisabled() {
            cancelForPageConnection();
        }

        void close() {
            connected = false;
            BluetoothGatt closingGatt = gatt;
            gatt = null;
            try {
                if (closingGatt != null) {
                    closingGatt.disconnect();
                    closingGatt.close();
                }
            } catch (Exception ignored) {}
            writeChar = null;
            currentDevice = null;
        }

        private class RingBleService {
            final UUID service;
            final UUID write;
            final UUID notify;
            final String name;
            RingBleService(String service, String write, String notify, String name) {
                this.service = UUID.fromString(service);
                this.write = UUID.fromString(write);
                this.notify = UUID.fromString(notify);
                this.name = name;
            }
        }

    }

    // ══════════════════════════════════════════════════════════
    //  WebSocket 连接 — synchronized 防并发
    // ══════════════════════════════════════════════════════════

    private synchronized void connectWebSocket() {
        if (wsConnected.get()) return;
        if (serverUrl == null) { Log.e(TAG, "url=null"); return; }

        // Access authentication comes from the WebView login. Do not follow an
        // Access redirect as a WebSocket handshake and do not bypass Access.
        if (isCloudflareServer() && getCloudflareCookie() == null) {
            Log.i(TAG, "Cloudflare Access session not available yet");
            updateKeepAlive("等待 Cloudflare 安全登录…");
            return;
        }
        if (!wsConnecting.compareAndSet(false, true)) return;

        final int gen = wsGeneration.incrementAndGet();

        WebSocket old = webSocket;
        webSocket = null;
        if (old != null) try { old.cancel(); } catch (Exception ignored) {}

        Log.i(TAG, ">>> connect gen=" + gen + " → " + serverUrl);
        updateKeepAlive("连接中...");

        try {
            Request req = new Request.Builder().url(serverUrl).build();
            webSocket = client.newWebSocket(req, new WebSocketListener() {

                @Override
                public void onOpen(WebSocket ws, Response resp) {
                    if (gen != wsGeneration.get()) { ws.cancel(); return; }
                    Log.i(TAG, ">>> OPEN gen=" + gen);
                    wsConnecting.set(false);
                    wsConnected.set(true);
                    reconnectDelay = 3000;
                    msgReceived = 0;
                    lastMessageTime = System.currentTimeMillis();
                    try {
                        JSONObject registration = new JSONObject();
                        registration.put("type", "register_client");
                        registration.put("client_id", getPhoneCameraClientId());
                        ws.send(registration.toString());
                        if (phoneCameraState.isArmed()) {
                            postPhoneCameraArmState(true);
                        }
                    } catch (Exception error) {
                        Log.w(TAG, "phone camera registration failed", error);
                    }
                    updateKeepAlive("在线 ✨");
                    fetchPendingMiBandCommands();
                    syncAppSupervisionRuntimeConfig();
                    fetchPendingAppSupervisionCommands();
                }

                @Override
                public void onMessage(WebSocket ws, String text) {
                    if (gen != wsGeneration.get()) return;
                    lastMessageTime = System.currentTimeMillis();
                    handleMessage(text);
                }

                @Override
                public void onFailure(WebSocket ws, Throwable t, Response resp) {
                    if (gen != wsGeneration.get()) return;
                    String err = t != null ? t.getMessage() : "unknown";
                    Log.w(TAG, ">>> FAIL gen=" + gen + ": " + err);
                    wsConnecting.set(false);
                    wsConnected.set(false);
                    reconnectDelay = Math.min(reconnectDelay * 2, MAX_RECONNECT_DELAY);
                    if (isCloudflareServer() && resp != null
                            && (resp.code() == 302 || resp.code() == 401 || resp.code() == 403)) {
                        updateKeepAlive("Cloudflare 登录已过期，请打开 App 重新登录");
                    } else {
                        updateKeepAlive("连接失败: " + err);
                    }
                    // 不在这里阻塞或重连！心跳线程会处理
                }

                @Override
                public void onClosed(WebSocket ws, int code, String reason) {
                    if (gen != wsGeneration.get()) return;
                    Log.i(TAG, ">>> CLOSED gen=" + gen + " code=" + code);
                    wsConnecting.set(false);
                    wsConnected.set(false);
                    updateKeepAlive("连接关闭(" + code + ")");
                }
            });
        } catch (Exception e) {
            wsConnecting.set(false);
            Log.e(TAG, "connect error: " + e.getMessage());
            reconnectDelay = Math.min(reconnectDelay * 2, MAX_RECONNECT_DELAY);
        }
    }

    // ══════════════════════════════════════════════════════════
    //  消息 → 通知
    // ══════════════════════════════════════════════════════════

    private void offerBandCommand(JSONObject data) {
        if (data == null || !"vibrate".equals(data.optString("action", ""))) return;
        String id = data.optString("id", "").trim();
        String pattern = data.optString("pattern", "").trim().toLowerCase(Locale.US);
        String note = data.optString("note", "").trim();
        String senderName = data.optString("sender_name", "").trim();
        long expiresAtMillis = (long) (data.optDouble("expires_at", 0) * 1000.0);
        if (!miBandCommandInbox.offer(id, pattern, note, senderName, expiresAtMillis)) return;
        Log.i(TAG, "⌚ queued band command " + id + " pattern=" + pattern);
        drainMiBandCommands();
        if (miBandRuntime != null && !miBandRuntime.status().authenticated) {
            miBandRuntime.manualReconnect();
        }
    }

    private void drainMiBandCommands() {
        if (miBandRuntime == null) return;
        MiBandCommandInbox.Command command = miBandCommandInbox.nextReady(
                System.currentTimeMillis(), miBandRuntime.status().authenticated);
        if (command == null) return;
        MiBandRuntime.Completion completion = success -> {
            miBandCommandInbox.complete(command.id, success);
            if (success) {
                ackMiBandCommand(command.id);
                if (mainHandler != null) mainHandler.post(this::drainMiBandCommands);
            } else if (miBandRuntime != null) {
                miBandRuntime.manualReconnect();
            }
        };
        if (!command.note.isEmpty()) {
            miBandRuntime.sendNote(
                    command.pattern, command.senderName, command.note, completion);
        } else {
            miBandRuntime.vibrate(command.pattern, completion);
        }
    }

    private void fetchPendingMiBandCommands() {
        if (!miBandCommandFetchActive.compareAndSet(false, true)) return;
        new Thread(() -> {
            try {
                String base = getHttpBase();
                if (base == null || base.isEmpty()) return;
                Request request = new Request.Builder()
                        .url(base + "/api/health/mi-band/commands/pending")
                        .get()
                        .build();
                try (Response response = client.newCall(request).execute()) {
                    if (!response.isSuccessful()) return;
                    String raw = response.body() != null ? response.body().string() : "{}";
                    JSONArray items = new JSONObject(raw).optJSONArray("items");
                    if (items == null) return;
                    for (int index = 0; index < items.length(); index++) {
                        JSONObject item = items.optJSONObject(index);
                        if (item != null) offerBandCommand(item);
                    }
                }
            } catch (Exception error) {
                Log.w(TAG, "⌚ pending command fetch failed: " + error.getMessage());
            } finally {
                miBandCommandFetchActive.set(false);
                drainMiBandCommands();
            }
        }, "AionMiBandCommandFetch").start();
    }

    private void ackMiBandCommand(String commandId) {
        new Thread(() -> {
            try {
                String base = getHttpBase();
                if (base == null || base.isEmpty()) return;
                Request request = new Request.Builder()
                        .url(base + "/api/health/mi-band/commands/" + commandId + "/ack")
                        .post(RequestBody.create("", MediaType.get("application/json; charset=utf-8")))
                        .build();
                try (Response response = client.newCall(request).execute()) {
                    if (!response.isSuccessful()) {
                        Log.w(TAG, "⌚ command ack failed HTTP " + response.code());
                    }
                }
            } catch (Exception error) {
                Log.w(TAG, "⌚ command ack failed: " + error.getMessage());
            }
        }, "AionMiBandCommandAck").start();
    }

    private void postAppSupervisionState(
            String eventType, String triggerGroupId, long checkpointMs) {
        new Thread(() -> {
            try {
                com.aion.chat.supervision.AppSupervisionRuntime runtime =
                        com.aion.chat.supervision.AppSupervisionRuntime.get();
                String base = getHttpBase();
                if (runtime == null || base == null || base.isEmpty()) return;
                JSONObject payload = runtime.buildStatePayload(
                        eventType, triggerGroupId, checkpointMs);
                RequestBody body = RequestBody.create(
                        payload.toString(), MediaType.get("application/json; charset=utf-8"));
                Request request = new Request.Builder()
                        .url(base + "/api/app-supervision/state")
                        .post(body)
                        .build();
                try (Response response = client.newCall(request).execute()) {
                    if (!response.isSuccessful()) {
                        Log.w(TAG, "App supervision state upload HTTP " + response.code());
                        return;
                    }
                    String raw = response.body() == null ? "{}" : response.body().string();
                    boolean enabled = new JSONObject(raw).optBoolean(
                            "featureEnabled", runtime.engine().isFeatureEnabled());
                    if (enabled != runtime.engine().isFeatureEnabled()) {
                        mainHandler.post(() -> runtime.setFeatureEnabled(enabled));
                    }
                }
            } catch (Exception error) {
                Log.w(TAG, "App supervision state upload failed: " + error.getMessage());
            }
        }, "AionAppSupervisionState").start();
    }

    private void syncAppSupervisionRuntimeConfig() {
        new Thread(() -> {
            try {
                String base = getHttpBase();
                if (base == null || base.isEmpty()) return;
                Request request = new Request.Builder()
                        .url(base + "/api/app-supervision/runtime-config")
                        .get()
                        .build();
                try (Response response = client.newCall(request).execute()) {
                    if (!response.isSuccessful()) return;
                    String raw = response.body() == null ? "{}" : response.body().string();
                    boolean enabled = new JSONObject(raw).optBoolean("featureEnabled", false);
                    java.util.Map<String, String> roleLabels =
                            com.aion.chat.supervision.AppSupervisionRoleCatalog
                                    .fromRuntimeConfig(raw);
                    mainHandler.post(() -> {
                        com.aion.chat.supervision.AppSupervisionRuntime runtime =
                                com.aion.chat.supervision.AppSupervisionRuntime.get();
                        if (runtime != null) {
                            runtime.setRoleLabels(roleLabels);
                            if (runtime.engine().isFeatureEnabled() != enabled) {
                                runtime.setFeatureEnabled(enabled);
                            }
                        }
                    });
                }
            } catch (Exception error) {
                Log.w(TAG, "App supervision config sync failed: " + error.getMessage());
            }
        }, "AionAppSupervisionConfig").start();
    }

    private void fetchPendingAppSupervisionCommands() {
        if (!appSupervisionCommandFetchActive.compareAndSet(false, true)) return;
        new Thread(() -> {
            try {
                String base = getHttpBase();
                if (base == null || base.isEmpty()) return;
                Request request = new Request.Builder()
                        .url(base + "/api/app-supervision/commands/pending")
                        .get()
                        .build();
                try (Response response = client.newCall(request).execute()) {
                    if (!response.isSuccessful()) return;
                    String raw = response.body() == null ? "{}" : response.body().string();
                    JSONArray commands = new JSONObject(raw).optJSONArray("commands");
                    if (commands == null) return;
                    for (int index = 0; index < commands.length(); index++) {
                        JSONObject command = commands.optJSONObject(index);
                        if (command != null) dispatchAppSupervisionCommand(command);
                    }
                }
            } catch (Exception error) {
                Log.w(TAG, "App supervision pending fetch failed: " + error.getMessage());
            } finally {
                appSupervisionCommandFetchActive.set(false);
            }
        }, "AionAppSupervisionPending").start();
    }

    private void dispatchAppSupervisionCommand(JSONObject data) {
        if (data == null) return;
        mainHandler.post(() -> applyAppSupervisionCommandOnMain(data));
    }

    private void applyAppSupervisionCommandOnMain(JSONObject data) {
        String commandId = data.optString("commandId", "").trim();
        if (commandId.isEmpty()) return;
        StoredCommandResult stored = findStoredAppSupervisionResult(commandId);
        if (stored != null) {
            ackAppSupervisionCommand(commandId, stored.success, stored.reason);
            return;
        }
        com.aion.chat.supervision.AppSupervisionRuntime runtime =
                com.aion.chat.supervision.AppSupervisionRuntime.get();
        if (runtime == null) return;
        String action = data.optString("action", "");
        String groupId = data.optString("groupId", "");
        int minutes = data.optInt("minutes", 0);
        String roleId = data.optString("roleId", "");
        String message = data.optString("message", "");
        long expiresWallMs = Math.round(data.optDouble("expiresAt", 0.0) * 1000.0);
        com.aion.chat.supervision.AppSupervisionRuntime.CommandResult result =
                runtime.applyAiCommand(action, groupId, minutes, roleId, message,
                        commandId, expiresWallMs);
        storeAppSupervisionResult(commandId, result.isSuccess(), result.getReason());
        ackAppSupervisionCommand(commandId, result.isSuccess(), result.getReason());
    }

    private void ackAppSupervisionCommand(
            String commandId, boolean success, String reason) {
        new Thread(() -> {
            try {
                String base = getHttpBase();
                if (base == null || base.isEmpty()) return;
                JSONObject value = new JSONObject();
                value.put("commandId", commandId);
                value.put("success", success);
                value.put("reason", reason == null ? "" : reason);
                Request request = new Request.Builder()
                        .url(base + "/api/app-supervision/commands/ack")
                        .post(RequestBody.create(value.toString(),
                                MediaType.get("application/json; charset=utf-8")))
                        .build();
                try (Response response = client.newCall(request).execute()) {
                    if (!response.isSuccessful()) {
                        Log.w(TAG, "App supervision ack HTTP " + response.code());
                    }
                }
            } catch (Exception error) {
                Log.w(TAG, "App supervision ack failed: " + error.getMessage());
            }
        }, "AionAppSupervisionAck").start();
    }

    private synchronized StoredCommandResult findStoredAppSupervisionResult(String commandId) {
        String raw = getSharedPreferences(PREFS, MODE_PRIVATE)
                .getString(PREF_APP_SUPERVISION_RESULTS, "");
        if (raw == null || raw.isEmpty()) return null;
        for (String line : raw.split("\\n")) {
            String[] fields = line.split("\\t", 3);
            if (fields.length >= 2 && commandId.equals(fields[0])) {
                String reason = "";
                if (fields.length == 3 && !fields[2].isEmpty()) {
                    try {
                        reason = new String(Base64.decode(fields[2], Base64.NO_WRAP),
                                java.nio.charset.StandardCharsets.UTF_8);
                    } catch (Exception ignored) {}
                }
                return new StoredCommandResult("1".equals(fields[1]), reason);
            }
        }
        return null;
    }

    private synchronized void storeAppSupervisionResult(
            String commandId, boolean success, String reason) {
        SharedPreferences preferences = getSharedPreferences(PREFS, MODE_PRIVATE);
        String raw = preferences.getString(PREF_APP_SUPERVISION_RESULTS, "");
        ArrayList<String> lines = new ArrayList<>();
        if (raw != null && !raw.isEmpty()) {
            for (String line : raw.split("\\n")) {
                if (!line.startsWith(commandId + "\t") && !line.isEmpty()) lines.add(line);
            }
        }
        String encodedReason = Base64.encodeToString(
                String.valueOf(reason == null ? "" : reason).getBytes(
                        java.nio.charset.StandardCharsets.UTF_8),
                Base64.NO_WRAP);
        lines.add(commandId + "\t" + (success ? "1" : "0") + "\t" + encodedReason);
        while (lines.size() > 256) lines.remove(0);
        preferences.edit().putString(
                PREF_APP_SUPERVISION_RESULTS,
                android.text.TextUtils.join("\n", lines)).commit();
    }

    private static final class StoredCommandResult {
        final boolean success;
        final String reason;
        StoredCommandResult(boolean success, String reason) {
            this.success = success;
            this.reason = reason;
        }
    }

    private void handleMessage(String text) {
        try {
            JSONObject json = new JSONObject(text);
            String type = json.optString("type", "");

            if ("pong".equals(type) || "ping".equals(type)) return;

            msgReceived++;
            Log.d(TAG, "MSG #" + msgReceived + " type=" + type);

            JSONObject data = json.optJSONObject("data");

            switch (type) {
                case "phone_camera_capture": {
                    dispatchPhoneCameraCapture(data);
                    break;
                }
                case "mi_band_command": {
                    offerBandCommand(data);
                    break;
                }
                case "app_supervision_command": {
                    dispatchAppSupervisionCommand(data);
                    break;
                }
                case "capability_config_changed": {
                    if (data != null && "app_supervision".equals(data.optString("key", ""))) {
                        boolean enabled = data.optBoolean("enabled", false);
                        mainHandler.post(() -> {
                            com.aion.chat.supervision.AppSupervisionRuntime runtime =
                                    com.aion.chat.supervision.AppSupervisionRuntime.get();
                            if (runtime != null) runtime.setFeatureEnabled(enabled);
                        });
                    }
                    break;
                }
                case "schedule_alarm": {
                    String c = data != null ? data.optString("content", "闹铃") : "闹铃";
                    showNotif(CH_ALARM, "⏰ 闹铃", c, true);
                    break;
                }
                case "monitor_alert": {
                    String c = data != null ? data.optString("content", "监控提醒") : "监控提醒";
                    boolean nativePhoneCapture = data != null
                            && data.optBoolean("phone_camera_native_capture", false);
                    showNotif(CH_ALARM, "👁 监控", c, true, nativePhoneCapture);
                    if (!nativePhoneCapture) {
                        schedulePhoneScreenCapture("monitor_alert");
                    }
                    break;
                }
                case "cam_check": {
                    schedulePhoneScreenCapture("cam_check");
                    break;
                }
                case "music": {
                    // 后台自动播放音乐（前台由 WebView JS 处理）
                    if (!isForegroundActive && data != null) {
                        JSONArray cards = data.optJSONArray("cards");
                        if (cards != null && cards.length() > 0) {
                            JSONObject firstCard = cards.optJSONObject(0);
                            if (firstCard != null) {
                                int songId = firstCard.optInt("id", 0);
                                if (songId > 0) {
                                    playMusicStream(songId);
                                }
                            }
                        }
                    }
                    break;
                }
                case "msg_created": {
                    if (data != null) {
                        String role = data.optString("role", "");
                        if ("assistant".equals(role)) {
                            String c = data.optString("content", "");
                            if (c.length() > 100) c = c.substring(0, 100) + "...";
                            String sender = data.optString("sender", "AI");
                            if (sender.isEmpty()) sender = "AI";
                            else sender = sender.substring(0, 1).toUpperCase() + sender.substring(1);
                            showNotif(CH_MESSAGE, "💬 " + sender, c, true);
                        }
                    }
                    break;
                }
                case "chatroom_msg_created": {
                    if (data != null) {
                        String sender = data.optString("sender", "");
                        if (!"user".equals(sender) && !"system".equals(sender) && !sender.isEmpty()) {
                            String c = data.optString("content", "");
                            if (c.length() > 100) c = c.substring(0, 100) + "...";
                            sender = sender.substring(0, 1).toUpperCase() + sender.substring(1);
                            showNotif(CH_MESSAGE, "💬 " + sender, c, true);
                        }
                    }
                    break;
                }
                case "esp32_bridge": {
                    if (data != null) {
                        boolean active = data.optBoolean("active", false);
                        if (active) {
                            String captureUrl = data.optString("url", "");
                            if (!captureUrl.isEmpty()) {
                                startEsp32Bridge(captureUrl);
                            }
                        } else {
                            stopEsp32Bridge();
                        }
                    }
                    break;
                }
                case "request_location_sync": {
                    // 服务端请求立即上报位置+步数
                    Log.i(TAG, "📍 Force sync requested via WS");
                    new Thread(() -> {
                        try {
                            if (latestStepCounter < 0) initStepCounter();
                            requestLocationOnce();
                        } catch (Exception e) {
                            Log.e(TAG, "📍 Force sync error: " + e.getMessage());
                        }
                    }, "ForceSyncLocation").start();
                    break;
                }
                case "request_step_diag": {
                    // 诊断步数传感器状态，在主线程执行
                    mainHandler.post(() -> {
                        try {
                            boolean hasPerm = ContextCompat.checkSelfPermission(
                                    AionPushService.this,
                                    Manifest.permission.ACTIVITY_RECOGNITION)
                                    == PackageManager.PERMISSION_GRANTED;
                            SharedPreferences dp = getSharedPreferences("aion_prefs", MODE_PRIVATE);
                            String diagInfo = "perm=" + hasPerm
                                    + " sensorObj=" + (stepSensor != null)
                                    + " latestVal=" + latestStepCounter
                                    + " dayStart=" + dp.getFloat(PREF_STEP_DAY_START, -1)
                                    + " offset=" + dp.getFloat(PREF_STEP_REBOOT_OFFSET, 0)
                                    + " lastKnown=" + dp.getFloat(PREF_STEP_LAST_KNOWN, -1)
                                    + " resetDate=" + dp.getString(PREF_STEP_RESET_DATE, "")
                                    + " todaySteps=" + getTodaySteps();
                            Log.i(TAG, "\uD83D\uDC63 DIAG: " + diagInfo);
                            // 尝试重新初始化
                            if (stepSensor == null) initStepCounter();
                            // 通过 HTTP POST 发给服务端（不走 WS，更可靠）
                            String httpBase = serverUrl
                                    .replace("ws://", "http://")
                                    .replace("wss://", "https://")
                                    .replace("/ws", "");
                            new Thread(() -> {
                                try {
                                    JSONObject body = new JSONObject();
                                    body.put("info", diagInfo);
                                    MediaType JSON_T = MediaType.get("application/json; charset=utf-8");
                                    RequestBody reqBody = RequestBody.create(body.toString(), JSON_T);
                                    Request req = new Request.Builder()
                                            .url(httpBase + "/api/location/step-diag-report")
                                            .post(reqBody).build();
                                    try (Response resp = client.newCall(req).execute()) {
                                        Log.i(TAG, "\uD83D\uDC63 diag posted: " + resp.code());
                                    }
                                } catch (Exception e) {
                                    Log.e(TAG, "\uD83D\uDC63 diag post failed: " + e.getMessage());
                                }
                            }, "StepDiag").start();
                        } catch (Exception e) {
                            Log.e(TAG, "\uD83D\uDC63 diag error: " + e.getMessage());
                        }
                    });
                    break;
                }
                case "request_ring_diag": {
                    SharedPreferences rp = getSharedPreferences("aion_ring_ble", MODE_PRIVATE);
                    boolean hasPerm = hasBluetoothPermissionsForRing();
                    boolean btEnabled = false;
                    try {
                        BluetoothManager bm = (BluetoothManager) getSystemService(Context.BLUETOOTH_SERVICE);
                        BluetoothAdapter ad = bm != null ? bm.getAdapter() : null;
                        btEnabled = ad != null && ad.isEnabled();
                    } catch (Exception ignored) {}
                    String info = "build=" + BuildConfig.VERSION_NAME
                            + "(" + BuildConfig.VERSION_CODE + ")"
                            + "; bluetoothEnabled=" + btEnabled
                            + "; bluetoothPerm=" + hasPerm
                            + "; ringThreadAlive=" + (ringSyncThread != null && ringSyncThread.isAlive())
                            + "; backgroundGatt=" + (ringBackgroundSync == null
                                    ? "not_initialized" : ringBackgroundSync.status())
                            + "; savedName=" + rp.getString("device_name", "")
                            + "; savedAddress=" + rp.getString("device_address", "")
                            + "; lastUploadedMs=" + rp.getLong("bg_last_heart_measured_at", 0)
                            + "; bgFailCount=" + rp.getInt("bg_sync_fail_count", 0)
                            + "; nextAttemptAtMs=" + rp.getLong("bg_next_sync_attempt_at", 0)
                            + "; lastFailure=" + rp.getString("bg_last_sync_failure", "")
                            + "; wsConnected=" + wsConnected.get()
                            + "; foreground=" + isForegroundActive;
                    Log.i(TAG, "💍 DIAG: " + info);
                    postRingDiagNow("diag", info);
                    break;
                }
            }
        } catch (Exception e) {
            Log.w(TAG, "parse error: " + e.getMessage());
        }
    }

    private boolean isCloudflareServer() {
        if (serverUrl == null) return false;
        try {
            return ConnectionEndpoint.isCloudflareHost(new java.net.URI(serverUrl).getHost());
        } catch (Exception ignored) {
            return false;
        }
    }

    private String getCloudflareCookie() {
        try {
            String cookie = CookieManager.getInstance()
                    .getCookie(ConnectionEndpoint.CLOUDFLARE_COOKIE_URL);
            if (ConnectionEndpoint.hasCloudflareAccessCookie(cookie)) return cookie;
        } catch (Exception e) {
            Log.w(TAG, "Unable to read Cloudflare Access session: "
                    + e.getClass().getSimpleName());
        }
        return null;
    }

    private boolean hasBluetoothPermissionsForRing() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            return ContextCompat.checkSelfPermission(this, Manifest.permission.BLUETOOTH_SCAN) == PackageManager.PERMISSION_GRANTED
                    && ContextCompat.checkSelfPermission(this, Manifest.permission.BLUETOOTH_CONNECT) == PackageManager.PERMISSION_GRANTED;
        }
        return true;
    }

    private void postRingDiagNow(String status, String info) {
        String httpBase = getHttpBase();
        if (httpBase == null) return;
        new Thread(() -> {
            try {
                JSONObject body = new JSONObject();
                body.put("status", status);
                body.put("info", info);
                MediaType JSON_T = MediaType.get("application/json; charset=utf-8");
                RequestBody reqBody = RequestBody.create(body.toString(), JSON_T);
                Request req = new Request.Builder()
                        .url(httpBase + "/api/health/ring/diag-report")
                        .post(reqBody)
                        .build();
                try (Response resp = client.newCall(req).execute()) {
                    Log.i(TAG, "💍 diag now posted: " + resp.code());
                }
            } catch (Exception e) {
                Log.e(TAG, "💍 diag now post failed: " + e.getMessage());
            }
        }, "RingDiag").start();
    }

    // ══════════════════════════════════════════════════════════
    //  ESP32-CAM 桥接（手机从 ESP32 拉帧 → 上传服务器）
    // ══════════════════════════════════════════════════════════

    private void startEsp32Bridge(String captureUrl) {
        stopEsp32Bridge();
        esp32CaptureUrl = captureUrl;
        esp32BridgeActive = true;
        esp32BridgeThread = new Thread(() -> {
            Log.i(TAG, "📷 ESP32 bridge started: " + captureUrl);
            int failCount = 0;
            while (esp32BridgeActive && shouldRun) {
                try {
                    // 从 ESP32 拉一帧 JPEG
                    Request req = new Request.Builder()
                            .url(esp32CaptureUrl)
                            .get()
                            .build();
                    byte[] jpgBytes;
                    try (Response resp = client.newCall(req).execute()) {
                        if (!resp.isSuccessful() || resp.body() == null) {
                            failCount++;
                            if (failCount % 10 == 0) {
                                Log.w(TAG, "📷 ESP32 fetch failed " + failCount + " times");
                            }
                            Thread.sleep(Math.min(5000, 1000 + failCount * 500L));
                            continue;
                        }
                        jpgBytes = resp.body().bytes();
                    }
                    if (jpgBytes.length < 100) {
                        failCount++;
                        Thread.sleep(1000);
                        continue;
                    }

                    // 上传到服务器
                    String httpBase = serverUrl
                            .replace("ws://", "http://")
                            .replace("wss://", "https://")
                            .replace("/ws", "");
                    RequestBody body = RequestBody.create(jpgBytes,
                            MediaType.get("image/jpeg"));
                    Request upload = new Request.Builder()
                            .url(httpBase + "/api/cam/esp32/frame")
                            .post(body)
                            .build();
                    try (Response uploadResp = client.newCall(upload).execute()) {
                        if (uploadResp.isSuccessful()) {
                            failCount = 0;
                        } else {
                            failCount++;
                        }
                    }
                    // 正常 ~1fps
                    Thread.sleep(1000);
                } catch (InterruptedException e) {
                    break;
                } catch (Exception e) {
                    failCount++;
                    if (failCount % 10 == 0) {
                        Log.e(TAG, "📷 ESP32 bridge error: " + e.getMessage());
                    }
                    try { Thread.sleep(Math.min(5000, 1000 + failCount * 500L)); }
                    catch (InterruptedException ie) { break; }
                }
            }
            Log.i(TAG, "📷 ESP32 bridge stopped");
        }, "Esp32Bridge");
        esp32BridgeThread.setDaemon(true);
        esp32BridgeThread.start();
    }

    private void stopEsp32Bridge() {
        esp32BridgeActive = false;
        if (esp32BridgeThread != null && esp32BridgeThread.isAlive()) {
            esp32BridgeThread.interrupt();
            try { esp32BridgeThread.join(3000); } catch (InterruptedException ignored) {}
        }
        esp32BridgeThread = null;
    }

    // ══════════════════════════════════════════════════════════
    //  原生音乐播放（后台 WebView 冻结时由 MediaPlayer 接管）
    // ══════════════════════════════════════════════════════════

    private void playMusicStream(int songId) {
        // ws://host:port/ws → http://host:port
        String httpBase = serverUrl.replace("ws://", "http://").replace("wss://", "https://");
        if (httpBase.endsWith("/ws")) httpBase = httpBase.substring(0, httpBase.length() - 3);
        String streamUrl = httpBase + "/api/music/stream/" + songId;
        Log.i(TAG, "♪ Playing music: " + streamUrl);

        stopMusic();

        try {
            mediaPlayer = new MediaPlayer();
            mediaPlayer.setAudioAttributes(new AudioAttributes.Builder()
                    .setContentType(AudioAttributes.CONTENT_TYPE_MUSIC)
                    .setUsage(AudioAttributes.USAGE_ALARM)  // 走闹钟音频流，可穿透勿扰模式
                    .build());
            mediaPlayer.setDataSource(streamUrl);
            mediaPlayer.setOnPreparedListener(MediaPlayer::start);
            mediaPlayer.setOnCompletionListener(mp -> {
                Log.i(TAG, "♪ Music finished");
                mp.release();
                if (mediaPlayer == mp) mediaPlayer = null;
            });
            mediaPlayer.setOnErrorListener((mp, what, extra) -> {
                Log.e(TAG, "♪ MediaPlayer error: " + what + "/" + extra);
                mp.release();
                if (mediaPlayer == mp) mediaPlayer = null;
                return true;
            });
            mediaPlayer.prepareAsync();
        } catch (Exception e) {
            Log.e(TAG, "♪ Music play error: " + e.getMessage());
            if (mediaPlayer != null) {
                try { mediaPlayer.release(); } catch (Exception ignored) {}
                mediaPlayer = null;
            }
        }
    }

    private void stopMusic() {
        if (mediaPlayer != null) {
            try {
                if (mediaPlayer.isPlaying()) mediaPlayer.stop();
                mediaPlayer.release();
            } catch (Exception ignored) {}
            mediaPlayer = null;
        }
    }

    private long startPhoneCameraAlert() {
        stopPhoneCameraAlert();
        MediaPlayer player = new MediaPlayer();
        try (AssetFileDescriptor asset = getAssets().openFd(
                "public/AionMonitoralart.mp3")) {
            player.setAudioAttributes(new AudioAttributes.Builder()
                    .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                    .setUsage(AudioAttributes.USAGE_NOTIFICATION_EVENT)
                    .build());
            player.setDataSource(
                    asset.getFileDescriptor(),
                    asset.getStartOffset(),
                    asset.getLength());
            player.setOnCompletionListener(this::releasePhoneCameraAlert);
            player.setOnErrorListener((failed, what, extra) -> {
                Log.e(TAG, "phone camera alert failed: " + what + "/" + extra);
                releasePhoneCameraAlert(failed);
                return true;
            });
            player.prepare();
            synchronized (phoneCameraAlertLock) {
                phoneCameraAlertPlayer = player;
            }
            player.start();
            long startedElapsedMs = SystemClock.elapsedRealtime();
            long captureTargetElapsedMs =
                    startedElapsedMs + PHONE_CAMERA_SHUTTER_OFFSET_MS;
            Log.i(TAG, "phone camera alert started=" + startedElapsedMs
                    + " captureTargetElapsedMs=" + captureTargetElapsedMs);
            return captureTargetElapsedMs;
        } catch (Exception error) {
            Log.e(TAG, "phone camera alert unavailable; capture continues", error);
            try { player.release(); } catch (Exception ignored) {}
            synchronized (phoneCameraAlertLock) {
                if (phoneCameraAlertPlayer == player) {
                    phoneCameraAlertPlayer = null;
                }
            }
            return 0L;
        }
    }

    private void stopPhoneCameraAlert() {
        releasePhoneCameraAlert(null);
    }

    private void releasePhoneCameraAlert(MediaPlayer expected) {
        MediaPlayer player;
        synchronized (phoneCameraAlertLock) {
            if (expected != null && phoneCameraAlertPlayer != expected) return;
            player = phoneCameraAlertPlayer;
            phoneCameraAlertPlayer = null;
        }
        if (player != null) {
            try {
                if (player.isPlaying()) player.stop();
            } catch (Exception ignored) {
            }
            try { player.release(); } catch (Exception ignored) {}
        }
    }

    private void showNotif(String ch, String title, String text, boolean high) {
        showNotif(ch, title, text, high, false);
    }

    private void showNotif(
            String ch,
            String title,
            String text,
            boolean high,
            boolean silent
    ) {
        NotificationManager nm = getSystemService(NotificationManager.class);
        if (nm == null) return;

        Log.i(TAG, "NOTIFY " + title + ": " + text);

        Intent i = new Intent(this, LauncherActivity.class);
        i.setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        PendingIntent pi = PendingIntent.getActivity(this, notifCounter, i,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);

        NotificationCompat.Builder b = new NotificationCompat.Builder(this, ch)
                .setSmallIcon(R.mipmap.ic_launcher)
                .setContentTitle(title)
                .setContentText(text)
                .setStyle(new NotificationCompat.BigTextStyle().bigText(text))
                .setPriority(high ? NotificationCompat.PRIORITY_HIGH : NotificationCompat.PRIORITY_DEFAULT)
                .setContentIntent(pi)
                .setAutoCancel(true)
                .setCategory(high ? NotificationCompat.CATEGORY_ALARM : NotificationCompat.CATEGORY_MESSAGE)
                .setVisibility(NotificationCompat.VISIBILITY_PUBLIC);

        if (silent) {
            b.setSilent(true);
        }
        if (high) {
            if (!silent) b.setDefaults(NotificationCompat.DEFAULT_ALL);
            b.setFullScreenIntent(pi, true);  // 锁屏时亮屏弹出
        }

        nm.notify(NOTIF_MSG_BASE + (notifCounter++ % 50), b.build());
    }

    // ══════════════════════════════════════════════════════════
    //  手机屏幕截图 — MediaProjection 授权后按监控提示抓取一帧
    // ══════════════════════════════════════════════════════════

    private void startPhoneScreenProjection(int resultCode, Intent resultData) {
        if (resultCode == 0 || resultData == null) {
            Log.w(TAG, "📱 screen projection missing result");
            return;
        }
        synchronized (phoneScreenLock) {
            stopPhoneScreenProjectionLocked();
            try {
                if (projectionManager == null) {
                    projectionManager = (MediaProjectionManager) getSystemService(Context.MEDIA_PROJECTION_SERVICE);
                }
                if (projectionManager == null) {
                    Log.w(TAG, "📱 MediaProjectionManager unavailable");
                    postPhoneScreenSkip("projection_manager_unavailable", false);
                    return;
                }

                // Android 14+ 要求 MediaProjection 会话运行在 mediaProjection 类型的前台服务中。
                // 用户授权已在 ActivityResult 中完成，这里先把服务类型提升，再创建投影实例和虚拟显示。
                updateForegroundForProjection();
                mediaProjection = projectionManager.getMediaProjection(resultCode, resultData);
                if (mediaProjection == null) {
                    Log.w(TAG, "📱 MediaProjection unavailable");
                    postPhoneScreenSkip("projection_unavailable", false);
                    return;
                }
                mediaProjection.registerCallback(new MediaProjection.Callback() {
                    @Override
                    public void onStop() {
                        synchronized (phoneScreenLock) {
                            stopPhoneScreenProjectionLocked();
                        }
                    }
                }, mainHandler);

                DisplayMetrics dm = getResources().getDisplayMetrics();
                int rawW = Math.max(1, dm.widthPixels);
                int rawH = Math.max(1, dm.heightPixels);
                float scale = Math.min(1f, 1080f / Math.max(rawW, rawH));
                int capW = Math.max(1, Math.round(rawW * scale));
                int capH = Math.max(1, Math.round(rawH * scale));

                phoneScreenReader = ImageReader.newInstance(capW, capH, PixelFormat.RGBA_8888, 2);
                phoneScreenDisplay = mediaProjection.createVirtualDisplay(
                        "AionPhoneScreen",
                        capW,
                        capH,
                        dm.densityDpi,
                        DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR,
                        phoneScreenReader.getSurface(),
                        null,
                        mainHandler
                );
                phoneScreenEnabled = true;
                Log.i(TAG, "📱 phone screen projection ready " + capW + "x" + capH);
            } catch (Exception e) {
                Log.e(TAG, "📱 start projection failed: " + e.getMessage());
                postPhoneScreenSkip("projection_start_failed:" + e.getClass().getSimpleName(), false);
                stopPhoneScreenProjectionLocked();
            }
        }
    }

    private void stopPhoneScreenProjection() {
        synchronized (phoneScreenLock) {
            stopPhoneScreenProjectionLocked();
        }
    }

    private void stopPhoneScreenProjectionLocked() {
        phoneScreenEnabled = false;
        if (phoneScreenDisplay != null) {
            try { phoneScreenDisplay.release(); } catch (Exception ignored) {}
            phoneScreenDisplay = null;
        }
        if (phoneScreenReader != null) {
            try { phoneScreenReader.close(); } catch (Exception ignored) {}
            phoneScreenReader = null;
        }
        if (mediaProjection != null) {
            MediaProjection oldProjection = mediaProjection;
            mediaProjection = null;
            try { oldProjection.stop(); } catch (Exception ignored) {}
        }
    }

    private void updateForegroundForProjection() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            int serviceType = ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC
                    | ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION;
            if (ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_FINE_LOCATION)
                    == PackageManager.PERMISSION_GRANTED) {
                serviceType |= ServiceInfo.FOREGROUND_SERVICE_TYPE_LOCATION;
            }
            if (ContextCompat.checkSelfPermission(this, Manifest.permission.BLUETOOTH_CONNECT)
                    == PackageManager.PERMISSION_GRANTED) {
                serviceType |= ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE;
            }
            startForeground(NOTIF_FOREGROUND, buildKeepAlive("在线 ✨ · 手机屏幕监督已开启"), serviceType);
        } else {
            startForeground(NOTIF_FOREGROUND, buildKeepAlive("在线 ✨ · 手机屏幕监督已开启"));
        }
    }

    private void schedulePhoneScreenCapture(String reason) {
        schedulePhoneScreenSnapshot(reason, 4200, true);
    }

    private void schedulePhoneScreenSnapshot(String reason, long delayMs, boolean forceAccessibilityFallback) {
        schedulePhoneScreenSnapshot(
                reason, delayMs, forceAccessibilityFallback, 0L);
    }

    private void schedulePhoneScreenSnapshotAt(
            String reason,
            long captureTargetElapsedMs,
            boolean forceAccessibilityFallback
    ) {
        long delayMs = captureTargetElapsedMs > 0L
                ? Math.max(0L, captureTargetElapsedMs - SystemClock.elapsedRealtime())
                : 0L;
        schedulePhoneScreenSnapshot(
                reason,
                delayMs,
                forceAccessibilityFallback,
                captureTargetElapsedMs);
    }

    private void schedulePhoneScreenSnapshot(
            String reason,
            long delayMs,
            boolean forceAccessibilityFallback,
            long captureTargetElapsedMs
    ) {
        if (System.currentTimeMillis() - lastPhoneCaptureAt < 3000) return;
        lastPhoneCaptureAt = System.currentTimeMillis();
        new Thread(() -> {
            try { Thread.sleep(Math.max(0, delayMs)); } catch (InterruptedException ignored) {}
            if (captureTargetElapsedMs > 0L) {
                long triggeredElapsedMs = SystemClock.elapsedRealtime();
                Log.i(TAG, "phone screen capture target="
                        + captureTargetElapsedMs
                        + " triggered=" + triggeredElapsedMs
                        + " deltaMs="
                        + (triggeredElapsedMs - captureTargetElapsedMs));
            }
            captureAndUploadPhoneScreen(reason, forceAccessibilityFallback);
        }, "PhoneScreenCapture").start();
    }

    private boolean isPhoneUnlockedForCapture() {
        try {
            PowerManager pm = (PowerManager) getSystemService(Context.POWER_SERVICE);
            if (pm != null && Build.VERSION.SDK_INT >= Build.VERSION_CODES.KITKAT_WATCH && !pm.isInteractive()) {
                return false;
            }
            KeyguardManager kg = (KeyguardManager) getSystemService(Context.KEYGUARD_SERVICE);
            if (kg != null) {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M && kg.isDeviceLocked()) return false;
                if (kg.isKeyguardLocked()) return false;
            }
        } catch (Exception e) {
            Log.w(TAG, "📱 lock state check failed: " + e.getMessage());
            return false;
        }
        return screenOn;
    }

    private boolean requestAccessibilityPhoneScreen(String reason, boolean force) {
        boolean enabledInSettings = AionAccessibilityService.isEnabledInSettings(this);
        String httpBase = getHttpBase();
        boolean accepted = AionAccessibilityService.captureLatest(
                this,
                lastReportedApp,
                reason,
                force,
                force ? 500 : 900,
                httpBase
        );
        Log.i(TAG, "📱 accessibility capture request reason=" + reason
                + " enabled=" + enabledInSettings
                + " accepted=" + accepted
                + " httpBase=" + httpBase
                + " app=" + lastReportedApp);
        if (!accepted) {
            postPhoneScreenSkip(enabledInSettings
                    ? "accessibility_not_connected"
                    : "accessibility_not_enabled", false);
        }
        return accepted;
    }

    private void captureAndUploadPhoneScreen(String reason, boolean forceAccessibilityFallback) {
        if (!phoneScreenEnabled || phoneScreenReader == null) {
            requestAccessibilityPhoneScreen("fallback_" + reason, forceAccessibilityFallback);
            return;
        }
        if (!isPhoneUnlockedForCapture()) {
            postPhoneScreenSkip("locked", true);
            return;
        }

        Image image = null;
        Bitmap bitmap = null;
        Bitmap cropped = null;
        Bitmap scaled = null;
        try {
            synchronized (phoneScreenLock) {
                if (phoneScreenReader == null) return;
                image = phoneScreenReader.acquireLatestImage();
            }
            if (image == null) {
                try { Thread.sleep(250); } catch (InterruptedException ignored) {}
                synchronized (phoneScreenLock) {
                    if (phoneScreenReader == null) return;
                    image = phoneScreenReader.acquireLatestImage();
                }
            }
            if (image == null) {
                postPhoneScreenSkip("no_frame", false);
                return;
            }

            int width = image.getWidth();
            int height = image.getHeight();
            Image.Plane plane = image.getPlanes()[0];
            ByteBuffer buffer = plane.getBuffer();
            int pixelStride = plane.getPixelStride();
            int rowStride = plane.getRowStride();
            int rowPadding = rowStride - pixelStride * width;
            int paddedWidth = width + rowPadding / pixelStride;

            bitmap = Bitmap.createBitmap(paddedWidth, height, Bitmap.Config.ARGB_8888);
            bitmap.copyPixelsFromBuffer(buffer);
            cropped = Bitmap.createBitmap(bitmap, 0, 0, width, height);

            float scale = Math.min(1f, 1080f / Math.max(width, height));
            if (scale < 1f) {
                int sw = Math.max(1, Math.round(width * scale));
                int sh = Math.max(1, Math.round(height * scale));
                scaled = Bitmap.createScaledBitmap(cropped, sw, sh, true);
            } else {
                scaled = cropped;
            }

            ByteArrayOutputStream out = new ByteArrayOutputStream();
            scaled.compress(Bitmap.CompressFormat.JPEG, 82, out);
            String b64 = Base64.encodeToString(out.toByteArray(), Base64.NO_WRAP);
            uploadPhoneScreenBase64(b64, reason);
        } catch (Exception e) {
            Log.e(TAG, "📱 capture failed: " + e.getMessage());
            if (!requestAccessibilityPhoneScreen("fallback_capture_failed_" + reason, forceAccessibilityFallback)) {
                postPhoneScreenSkip("capture_failed", false);
            }
        } finally {
            if (image != null) try { image.close(); } catch (Exception ignored) {}
            if (bitmap != null) bitmap.recycle();
            if (cropped != null && cropped != scaled) cropped.recycle();
            if (scaled != null) scaled.recycle();
        }
    }

    private String getPhoneCameraClientId() {
        String deviceId = Settings.Secure.getString(
                getContentResolver(), Settings.Secure.ANDROID_ID);
        if (deviceId == null || deviceId.trim().isEmpty()) {
            deviceId = "unknown-device";
        }
        return "android-push:" + deviceId;
    }

    private String getPhoneCameraHttpBase() {
        String current = getHttpBase();
        if (current != null) return current;
        String saved = getSharedPreferences(PREFS, MODE_PRIVATE)
                .getString(PushServiceStartPolicy.PREF_LAST_ACTIVE_URL, DEFAULT_PAGE_URL);
        return ConnectionEndpoint.normalizePageUrl(saved)
                .replaceFirst("/(?:chat|camera|settings)(?:[/?#].*)?$", "");
    }

    private void postPhoneCameraArmState(boolean armed) {
        phoneCameraStateSync.execute(() -> {
            String httpBase = getPhoneCameraHttpBase();
            if (httpBase == null || client == null) return;
            try {
                JSONObject body = new JSONObject();
                body.put("client_id", getPhoneCameraClientId());
                if (armed) {
                    body.put("facing", phoneCameraState.getFacing());
                    body.put("zoom", phoneCameraState.getZoom());
                    body.put("capabilities", phoneCameraController.getCapabilities());
                }
                MediaType jsonType = MediaType.get("application/json; charset=utf-8");
                Request request = new Request.Builder()
                        .url(httpBase + (armed
                                ? "/api/phone-camera/arm"
                                : "/api/phone-camera/disarm"))
                        .post(RequestBody.create(body.toString(), jsonType))
                        .build();
                try (Response response = client.newCall(request).execute()) {
                    Log.i(TAG, "phone camera " + (armed ? "armed" : "disarmed")
                            + " -> " + response.code());
                }
            } catch (Exception error) {
                Log.w(TAG, "phone camera state sync failed", error);
            }
        });
    }

    private void dispatchPhoneCameraCapture(JSONObject data) {
        if (data == null || phoneCameraController == null) return;
        String requestId = data.optString("request_id", "").trim();
        long deadlineMs = Math.round(data.optDouble("deadline_at", 0) * 1000.0);
        PhoneCameraState.Decision decision = phoneCameraState.begin(
                requestId, deadlineMs, System.currentTimeMillis());
        if (decision != PhoneCameraState.Decision.ACCEPTED) {
            Log.d(TAG, "phone camera request ignored: " + decision + " " + requestId);
            return;
        }
        String facing = PhoneCameraImagePolicy.normalizeFacing(
                data.optString("facing", phoneCameraState.getFacing()));
        float zoom = (float) data.optDouble("zoom", phoneCameraState.getZoom());
        long captureTargetElapsedMs = startPhoneCameraAlert();
        schedulePhoneScreenSnapshotAt(
                "phone_camera_capture",
                captureTargetElapsedMs,
                true);
        int generation = phoneCameraCaptureGeneration.incrementAndGet();
        attemptPhoneCameraCapture(
                requestId,
                deadlineMs,
                facing,
                zoom,
                1,
                generation,
                captureTargetElapsedMs);
    }

    private void attemptPhoneCameraCapture(
            String requestId,
            long deadlineMs,
            String facing,
            float zoom,
            int attempt,
            int generation,
            long captureTargetElapsedMs
    ) {
        if (!isPhoneCameraCaptureActive(generation)) return;
        if (!PhoneCameraPreviewCoordinator.shared().pauseForEvent(750L)) {
            handlePhoneCameraAttemptFailure(
                    requestId,
                    deadlineMs,
                    facing,
                    zoom,
                    attempt,
                    generation,
                    captureTargetElapsedMs,
                    "preview_release_timeout");
            return;
        }
        long timeoutMs = PhoneCameraRetryPolicy.captureTimeoutMs(
                System.currentTimeMillis(), deadlineMs);
        if (timeoutMs <= 0L) {
            finishPhoneCameraCaptureFailure(
                    requestId, "capture_deadline_exhausted", generation);
            return;
        }
        phoneCameraController.capture(
                facing,
                zoom,
                new PhoneCameraController.CaptureCallback() {
                    @Override
                    public void onSuccess(byte[] jpeg, JSONObject metadata) {
                        if (!isPhoneCameraCaptureActive(generation)) return;
                        cancelPhoneCameraRetry();
                        PhoneCameraPreviewCoordinator.shared().finishEvent();
                        new Thread(
                                () -> uploadPhoneCameraCapture(requestId, jpeg, metadata),
                                "PhoneCameraUpload"
                        ).start();
                    }

                    @Override
                    public void onFailure(String error) {
                        handlePhoneCameraAttemptFailure(
                                requestId,
                                deadlineMs,
                                facing,
                                zoom,
                                attempt,
                                generation,
                                captureTargetElapsedMs,
                                error);
                    }
                },
                timeoutMs,
                captureTargetElapsedMs
        );
    }

    private void handlePhoneCameraAttemptFailure(
            String requestId,
            long deadlineMs,
            String facing,
            float zoom,
            int attempt,
            int generation,
            long captureTargetElapsedMs,
            String error
    ) {
        if (!isPhoneCameraCaptureActive(generation)) return;
        long now = System.currentTimeMillis();
        if (PhoneCameraRetryPolicy.shouldRetry(
                error, attempt, now, deadlineMs)) {
            long delayMs = PhoneCameraRetryPolicy.delayMs(attempt);
            Log.i(TAG, "phone camera busy; retry " + requestId
                    + " attempt=" + (attempt + 1)
                    + " delayMs=" + delayMs
                    + " error=" + error);
            Runnable retry = () -> {
                phoneCameraRetryRunnable = null;
                if (!isPhoneCameraCaptureActive(generation)) return;
                attemptPhoneCameraCapture(
                        requestId,
                        deadlineMs,
                        facing,
                        zoom,
                        attempt + 1,
                        generation,
                        captureTargetElapsedMs);
            };
            cancelPhoneCameraRetry();
            phoneCameraRetryRunnable = retry;
            mainHandler.postDelayed(retry, delayMs);
            return;
        }
        finishPhoneCameraCaptureFailure(requestId, error, generation);
    }

    private void finishPhoneCameraCaptureFailure(
            String requestId,
            String error,
            int generation
    ) {
        if (!isPhoneCameraCaptureActive(generation)) return;
        cancelPhoneCameraRetry();
        PhoneCameraPreviewCoordinator.shared().finishEvent();
        new Thread(
                () -> postPhoneCameraFailure(requestId, error),
                "PhoneCameraFailure"
        ).start();
    }

    private boolean isPhoneCameraCaptureActive(int generation) {
        return shouldRun && phoneCameraCaptureGeneration.get() == generation;
    }

    private void cancelPhoneCameraRetry() {
        Runnable retry = phoneCameraRetryRunnable;
        phoneCameraRetryRunnable = null;
        if (retry != null) mainHandler.removeCallbacks(retry);
    }

    private void cancelActivePhoneCameraCapture() {
        phoneCameraCaptureGeneration.incrementAndGet();
        cancelPhoneCameraRetry();
        if (phoneCameraController != null) phoneCameraController.close();
        PhoneCameraPreviewCoordinator.shared().finishEvent();
    }

    private void uploadPhoneCameraCapture(
            String requestId,
            byte[] jpeg,
            JSONObject metadata
    ) {
        try {
            Request request = new Request.Builder()
                    .url(getPhoneCameraHttpBase() + "/api/phone-camera/upload")
                    .header("X-Phone-Camera-Request-Id", requestId)
                    .header("X-Phone-Camera-Metadata", metadata.toString())
                    .post(RequestBody.create(jpeg, MediaType.get("image/jpeg")))
                    .build();
            try (Response response = client.newCall(request).execute()) {
                Log.i(TAG, "phone camera upload " + requestId + " -> " + response.code());
                if (!response.isSuccessful()) {
                    throw new java.io.IOException("upload_http_" + response.code());
                }
            }
        } catch (Exception error) {
            Log.w(TAG, "phone camera upload failed", error);
            postPhoneCameraFailure(requestId, "upload_failed");
            return;
        } finally {
            phoneCameraState.complete(requestId);
        }
    }

    private void postPhoneCameraFailure(String requestId, String errorMessage) {
        try {
            JSONObject body = new JSONObject();
            body.put("request_id", requestId);
            body.put("error", errorMessage);
            body.put("metadata", new JSONObject()
                    .put("facing", phoneCameraState.getFacing())
                    .put("zoom", phoneCameraState.getZoom()));
            Request request = new Request.Builder()
                    .url(getPhoneCameraHttpBase() + "/api/phone-camera/failure")
                    .post(RequestBody.create(
                            body.toString(),
                            MediaType.get("application/json; charset=utf-8")))
                    .build();
            try (Response response = client.newCall(request).execute()) {
                Log.i(TAG, "phone camera failure " + requestId + " -> " + response.code());
            }
        } catch (Exception postError) {
            Log.w(TAG, "phone camera failure report failed", postError);
        } finally {
            phoneCameraState.complete(requestId);
        }
    }

    private String getHttpBase() {
        if (serverUrl == null) return null;
        return serverUrl.replace("ws://", "http://")
                .replace("wss://", "https://")
                .replace("/ws", "");
    }

    private void uploadPhoneScreenBase64(String b64, String reason) {
        String httpBase = getHttpBase();
        if (httpBase == null) return;
        try {
            JSONObject body = new JSONObject();
            body.put("image_base64", b64);
            body.put("timestamp", System.currentTimeMillis() / 1000.0);
            body.put("app", lastReportedApp);
            body.put("locked", false);
            body.put("reason", reason);
            body.put("source", "mediaprojection");
            MediaType JSON_TYPE = MediaType.get("application/json; charset=utf-8");
            RequestBody reqBody = RequestBody.create(body.toString(), JSON_TYPE);
            Request req = new Request.Builder()
                    .url(httpBase + "/api/phone-screen/upload")
                    .post(reqBody)
                    .build();
            try (Response resp = client.newCall(req).execute()) {
                Log.i(TAG, "📱 phone screen uploaded → " + resp.code());
            }
        } catch (Exception e) {
            Log.e(TAG, "📱 phone screen upload failed: " + e.getMessage());
        }
    }

    private void postPhoneScreenSkip(String reason, boolean locked) {
        new Thread(() -> postPhoneScreenSkipOnBackground(reason, locked), "PhoneScreenSkip").start();
    }

    private void postPhoneScreenSkipOnBackground(String reason, boolean locked) {
        String httpBase = getHttpBase();
        if (httpBase == null || client == null) return;
        try {
            JSONObject body = new JSONObject();
            body.put("reason", reason);
            body.put("app", lastReportedApp);
            body.put("locked", locked);
            MediaType JSON_TYPE = MediaType.get("application/json; charset=utf-8");
            RequestBody reqBody = RequestBody.create(body.toString(), JSON_TYPE);
            Request req = new Request.Builder()
                    .url(httpBase + "/api/phone-screen/skip")
                    .post(reqBody)
                    .build();
            try (Response resp = client.newCall(req).execute()) {
                Log.d(TAG, "📱 phone screen skipped " + reason + " → " + resp.code());
            }
        } catch (Exception e) {
            Log.d(TAG, "📱 phone screen skip report failed: " + e.getClass().getSimpleName() + ":" + e.getMessage());
        }
    }

    // ══════════════════════════════════════════════════════════
    //  通知渠道
    // ══════════════════════════════════════════════════════════

    private void createNotificationChannels() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return;
        NotificationManager nm = getSystemService(NotificationManager.class);
        if (nm == null) return;
        String appName = getString(R.string.app_name);

        NotificationChannel c1 = new NotificationChannel(CH_KEEPALIVE, appName + " 保活",
                NotificationManager.IMPORTANCE_LOW);
        c1.setShowBadge(false);
        nm.createNotificationChannel(c1);

        NotificationChannel c2 = new NotificationChannel(CH_MESSAGE, appName + " 消息横幅",
                NotificationManager.IMPORTANCE_HIGH);
        c2.enableVibration(true);
        c2.setLockscreenVisibility(Notification.VISIBILITY_PUBLIC);
        nm.createNotificationChannel(c2);

        NotificationChannel c3 = new NotificationChannel(CH_ALARM, "闹铃与监控",
                NotificationManager.IMPORTANCE_HIGH);
        c3.enableVibration(true);
        c3.setLockscreenVisibility(Notification.VISIBILITY_PUBLIC);
        nm.createNotificationChannel(c3);
    }

    private Notification buildKeepAlive(String text) {
        Intent i = new Intent(this, LauncherActivity.class);
        i.setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        PendingIntent pi = PendingIntent.getActivity(this, 0, i,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        return new NotificationCompat.Builder(this, CH_KEEPALIVE)
                .setSmallIcon(R.mipmap.ic_launcher)
                .setContentTitle(getString(R.string.app_name))
                .setContentText(text)
                .setContentIntent(pi)
                .setOngoing(true)
                .setPriority(NotificationCompat.PRIORITY_LOW)
                .build();
    }

    private void updateKeepAlive(String text) {
        NotificationManager nm = getSystemService(NotificationManager.class);
        if (nm != null) nm.notify(NOTIF_FOREGROUND, buildKeepAlive(text));
    }

    // ══════════════════════════════════════════════════════════
    //  活动上报线程 — UsageStatsManager 检测前台应用
    // ══════════════════════════════════════════════════════════

    private synchronized void startActivityThread() {
        if (activityThread != null && activityThread.isAlive()) return;

        // 注册屏幕开关广播
        registerScreenReceiver();

        activityThread = new Thread(() -> {
            Log.i(TAG, "📱 Activity thread started");
            // 等待 20 秒让服务稳定
            try { Thread.sleep(20000); } catch (InterruptedException e) { return; }

            while (shouldRun) {
                try {
                    if (screenOn && hasUsageStatsPermission()) {
                        reportForegroundApp();
                    } else {
                        Log.d(TAG, "📱 Usage access permission not granted");
                    }
                } catch (Exception e) {
                    Log.e(TAG, "📱 activity error: " + e.getMessage());
                }

                // 每轮检测无障碍服务，被系统关闭时自动恢复
                try { Thread.sleep(ACTIVITY_INTERVAL); }
                catch (InterruptedException e) { break; }
            }
            Log.i(TAG, "📱 Activity thread exiting");
        }, "AionActivity");
        activityThread.setDaemon(false);
        activityThread.start();
    }

    private boolean hasUsageStatsPermission() {
        try {
            UsageStatsManager usm = (UsageStatsManager) getSystemService(Context.USAGE_STATS_SERVICE);
            if (usm == null) return false;
            long now = System.currentTimeMillis();
            java.util.List<UsageStats> stats = usm.queryUsageStats(
                    UsageStatsManager.INTERVAL_DAILY, now - 60_000, now);
            return stats != null && !stats.isEmpty();
        } catch (Exception e) {
            return false;
        }
    }

    private void reportForegroundApp() {
        UsageStatsManager usm = (UsageStatsManager) getSystemService(Context.USAGE_STATS_SERVICE);
        if (usm == null) return;

        long now = System.currentTimeMillis();

        // 方案一：UsageEvents（更可靠，能在后台获取真实的前台切换事件）
        String pkgName = null;
        try {
            UsageEvents events = usm.queryEvents(now - 120_000, now);
            UsageEvents.Event event = new UsageEvents.Event();
            while (events.hasNextEvent()) {
                events.getNextEvent(event);
                // ACTIVITY_RESUMED (=1 on older / =2) 表示 Activity 进入前台
                if (event.getEventType() == UsageEvents.Event.ACTIVITY_RESUMED
                        || event.getEventType() == 1) {
                    pkgName = event.getPackageName();
                }
            }
        } catch (Exception e) {
            Log.d(TAG, "📱 UsageEvents failed, fallback to queryUsageStats: " + e.getMessage());
        }

        // 方案二：如果 UsageEvents 没结果，fallback 到 queryUsageStats
        if (pkgName == null) {
            java.util.List<UsageStats> stats = usm.queryUsageStats(
                    UsageStatsManager.INTERVAL_DAILY, now - 120_000, now);
            if (stats != null && !stats.isEmpty()) {
                UsageStats recent = null;
                for (UsageStats s : stats) {
                    if (recent == null || s.getLastTimeUsed() > recent.getLastTimeUsed()) {
                        recent = s;
                    }
                }
                if (recent != null) pkgName = recent.getPackageName();
            }
        }

        if (pkgName == null) return;

        // 仅过滤自身
        if (pkgName.equals(getPackageName())) {
            return;
        }

        // 每次轮询都上报（服务端摘要层负责合并去重）
        lastReportedApp = pkgName;
        lastReportedTime = now;

        // 直接发送包名，服务端做名称翻译（避免 vivo ROM 中文编码乱码）
        postActivityToServer(pkgName);
    }

    private void postActivityToServer(String pkgName) {
        if (serverUrl == null) return;

        String httpBase = serverUrl
                .replace("ws://", "http://")
                .replace("wss://", "https://")
                .replace("/ws", "");

        try {
            JSONObject body = new JSONObject();
            body.put("device", "phone");
            body.put("app", pkgName);
            body.put("title", pkgName);
            body.put("timestamp", System.currentTimeMillis() / 1000.0);

            MediaType JSON_TYPE = MediaType.get("application/json; charset=utf-8");
            RequestBody reqBody = RequestBody.create(body.toString(), JSON_TYPE);
            Request req = new Request.Builder()
                    .url(httpBase + "/api/activity/report")
                    .post(reqBody)
                    .build();

            try (Response resp = client.newCall(req).execute()) {
                Log.i(TAG, "📱 reported activity: " + pkgName + " → " + resp.code());
            }
        } catch (Exception e) {
            Log.e(TAG, "📱 activity report failed: " + e.getMessage());
        }
    }

    // ══════════════════════════════════════════════════════════
    //  无障碍服务自动恢复 — 被 ROM 安全策略关闭后自动重新开启
    //  需要 WRITE_SECURE_SETTINGS 权限（通过 ADB 一次性授予）：
    //  adb shell pm grant com.aion.chat android.permission.WRITE_SECURE_SETTINGS
    // ══════════════════════════════════════════════════════════

    private void checkAndRecoverAccessibility() {
        // 检查无障碍服务实例是否存活
        if (AionAccessibilityService.isReady()) return;

        // 只有用户曾主动开启过无障碍服务才自动恢复，未开过的不强制
        boolean userOptedIn = getSharedPreferences("aion_prefs", MODE_PRIVATE)
                .getBoolean("accessibility_user_opted_in", false);
        if (!userOptedIn) return;

        // 冷却期内不重复操作
        long now = System.currentTimeMillis();
        if (now - lastAccessibilityRecoverAt < ACCESSIBILITY_RECOVER_COOLDOWN) return;
        lastAccessibilityRecoverAt = now;

        // 检查是否有 WRITE_SECURE_SETTINGS 权限
        boolean hasPermission = (checkCallingOrSelfPermission(
                "android.permission.WRITE_SECURE_SETTINGS") == PackageManager.PERMISSION_GRANTED);
        if (!hasPermission) {
            Log.d(TAG, "♻️ No WRITE_SECURE_SETTINGS, cannot auto-recover accessibility");
            return;
        }

        try {
            String targetComponent = new android.content.ComponentName(
                    this, AionAccessibilityService.class).flattenToString();

            // 读取当前已启用的无障碍服务列表
            String current = Settings.Secure.getString(
                    getContentResolver(), Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES);

            // 如果列表中已经没有我们的服务，重新写入
            if (current == null || !current.contains(targetComponent)) {
                String newValue = (current == null || current.isEmpty())
                        ? targetComponent
                        : current + ":" + targetComponent;
                Settings.Secure.putString(getContentResolver(),
                        Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES, newValue);
                Settings.Secure.putString(getContentResolver(),
                        "accessibility_enabled", "1");
                Log.i(TAG, "♻️ Accessibility service re-enabled by WRITE_SECURE_SETTINGS");
            } else {
                // 设置里有但实例没启动，尝试先移除再添加来触发系统重新绑定
                String without = current.replace(targetComponent, "")
                        .replace("::", ":").replaceAll("^:|:$", "");
                Settings.Secure.putString(getContentResolver(),
                        Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES, without);
                try { Thread.sleep(500); } catch (InterruptedException ignored) {}
                String restored = without.isEmpty()
                        ? targetComponent
                        : without + ":" + targetComponent;
                Settings.Secure.putString(getContentResolver(),
                        Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES, restored);
                Log.i(TAG, "♻️ Accessibility service toggled to force rebind");
            }
        } catch (SecurityException e) {
            Log.w(TAG, "♻️ WRITE_SECURE_SETTINGS permission revoked: " + e.getMessage());
        } catch (Exception e) {
            Log.e(TAG, "♻️ accessibility recover failed: " + e.getMessage());
        }
    }

    // ══════════════════════════════════════════════════════════
    //  屏幕开关监听 — 锁屏/亮屏时立即上报
    // ══════════════════════════════════════════════════════════

    private void registerScreenReceiver() {
        if (screenReceiver != null) return;
        screenReceiver = new BroadcastReceiver() {
            @Override
            public void onReceive(Context context, Intent intent) {
                if (intent == null || intent.getAction() == null) return;
                switch (intent.getAction()) {
                    case Intent.ACTION_SCREEN_OFF:
                        Log.i(TAG, "📱 Screen OFF");
                        screenOn = false;
                        com.aion.chat.supervision.AppSupervisionRuntime runtime =
                                com.aion.chat.supervision.AppSupervisionRuntime.get();
                        if (runtime != null) runtime.onScreenOff();
                        lastReportedApp = "__screen_off__";
                        // 在后台线程发送，避免阻塞广播
                        new Thread(() -> {
                            postActivityToServer("screen_off");
                            postPhoneScreenSkip("screen_off", true);
                        }, "ScreenOff").start();
                        break;
                    case Intent.ACTION_SCREEN_ON:
                        Log.i(TAG, "📱 Screen ON");
                        screenOn = true;
                        com.aion.chat.supervision.AppSupervisionRuntime screenOnRuntime =
                                com.aion.chat.supervision.AppSupervisionRuntime.get();
                        if (screenOnRuntime != null) screenOnRuntime.onScreenOn();
                        lastReportedApp = "__screen_on__";
                        new Thread(() -> {
                            postActivityToServer("screen_on");
                        }, "ScreenOn").start();
                        break;
                    case Intent.ACTION_USER_PRESENT:
                        com.aion.chat.supervision.AppSupervisionRuntime userPresentRuntime =
                                com.aion.chat.supervision.AppSupervisionRuntime.get();
                        if (userPresentRuntime != null) userPresentRuntime.onUserPresent();
                        break;
                }
            }
        };
        IntentFilter filter = new IntentFilter();
        filter.addAction(Intent.ACTION_SCREEN_OFF);
        filter.addAction(Intent.ACTION_SCREEN_ON);
        filter.addAction(Intent.ACTION_USER_PRESENT);
        registerReceiver(screenReceiver, filter);
        Log.i(TAG, "📱 Screen receiver registered");
    }

    private void unregisterScreenReceiver() {
        if (screenReceiver != null) {
            try { unregisterReceiver(screenReceiver); } catch (Exception ignored) {}
            screenReceiver = null;
        }
    }

    // ══════════════════════════════════════════════════════════
    //  步数计数 — TYPE_STEP_COUNTER 传感器 + 重启补偿 + 5:00 重置
    // ══════════════════════════════════════════════════════════

    /**
     * 获取当前"逻辑日期"字符串（以凌晨 5:00 为分界）。
     * 例如：若当前时间是 2026-05-15 03:00，逻辑上仍属于 "2026-05-14"。
     */
    private String getLogicalDate() {
        Calendar cal = Calendar.getInstance();
        if (cal.get(Calendar.HOUR_OF_DAY) < STEP_RESET_HOUR) {
            cal.add(Calendar.DATE, -1);
        }
        return new SimpleDateFormat("yyyy-MM-dd", Locale.US).format(cal.getTime());
    }

    private void initStepCounter() {
        if (sensorManager == null) {
            sensorManager = (SensorManager) getSystemService(Context.SENSOR_SERVICE);
        }
        if (sensorManager == null) {
            Log.w(TAG, "\uD83D\uDC63 SensorManager not available");
            return;
        }
        if (stepSensor != null) return;  // 已经注册过了
        stepSensor = sensorManager.getDefaultSensor(Sensor.TYPE_STEP_COUNTER);
        if (stepSensor == null) {
            Log.w(TAG, "\uD83D\uDC63 No step counter sensor on this device");
            return;
        }
        // 重装 APK 后 SharedPreferences 丢失，尝试从服务端恢复步数基线
        SharedPreferences prefs = getSharedPreferences("aion_prefs", MODE_PRIVATE);
        if (prefs.getFloat(PREF_STEP_DAY_START, -1) < 0) {
            stepRestorePending = true;
            restoreStepStateFromServer();
        }
        // 传感器回调必须在有 Looper 的线程上注册，用主线程 Handler
        sensorManager.registerListener(stepListener, stepSensor,
                SensorManager.SENSOR_DELAY_NORMAL, mainHandler);
        Log.i(TAG, "\uD83D\uDC63 Step counter sensor registered (mainHandler)");
    }

    /**
     * 从服务端恢复步数状态（重装 APK 后 SharedPreferences 丢失时调用）
     */
    private void restoreStepStateFromServer() {
        if (serverUrl == null) {
            stepRestorePending = false;
            return;
        }
        new Thread(() -> {
            try {
                String httpBase = serverUrl.replace("ws://", "http://")
                        .replace("wss://", "https://")
                        .replace("/ws", "");
                String apiUrl = httpBase + "/api/location/step-state";
                Request req = new Request.Builder().url(apiUrl).get().build();
                try (Response resp = client.newCall(req).execute()) {
                    String body = resp.body() != null ? resp.body().string() : "";
                    JSONObject json = new JSONObject(body);
                    int steps = json.optInt("steps", -1);
                    String date = json.optString("logical_date", "");
                    if (steps > 0 && date.equals(getLogicalDate())) {
                        serverStepRestore = steps;
                        Log.i(TAG, "\uD83D\uDC63 Restored step state from server: " + steps + " steps for " + date);
                    } else {
                        Log.i(TAG, "\uD83D\uDC63 No matching step state on server (steps=" + steps + " date=" + date + " today=" + getLogicalDate() + ")");
                    }
                }
            } catch (Exception e) {
                Log.w(TAG, "\uD83D\uDC63 Failed to restore step state: " + e.getMessage());
            } finally {
                stepRestorePending = false;
            }
        }).start();
    }

    private final SensorEventListener stepListener = new SensorEventListener() {
        @Override
        public void onSensorChanged(SensorEvent event) {
            if (event.sensor.getType() != Sensor.TYPE_STEP_COUNTER) return;
            float currentCounter = event.values[0];
            latestStepCounter = currentCounter;

            SharedPreferences prefs = getSharedPreferences("aion_prefs", MODE_PRIVATE);
            String savedDate = prefs.getString(PREF_STEP_RESET_DATE, "");
            String logicalDate = getLogicalDate();

            float dayStart = prefs.getFloat(PREF_STEP_DAY_START, -1);
            float lastKnown = prefs.getFloat(PREF_STEP_LAST_KNOWN, -1);
            float rebootOffset = prefs.getFloat(PREF_STEP_REBOOT_OFFSET, 0);

            // 首次启动或跨逻辑日 → 重置
            if (!logicalDate.equals(savedDate) || dayStart < 0) {
                // 等待服务端恢复完成（重装 APK 场景）
                if (dayStart < 0 && stepRestorePending) {
                    Log.d(TAG, "\uD83D\uDC63 Waiting for server step restore...");
                    return;
                }
                // 重装 APK 后从服务端恢复的步数作为 rebootOffset
                float restoreOffset = 0;
                if (dayStart < 0 && serverStepRestore > 0) {
                    restoreOffset = serverStepRestore;
                    serverStepRestore = -1;
                    Log.i(TAG, "\uD83D\uDC63 Using server-restored steps as offset: " + (int) restoreOffset);
                }
                Log.i(TAG, "\uD83D\uDC63 Step reset for logical day " + logicalDate
                        + " (was " + savedDate + ") restoreOffset=" + (int) restoreOffset);
                prefs.edit()
                        .putFloat(PREF_STEP_DAY_START, currentCounter)
                        .putFloat(PREF_STEP_REBOOT_OFFSET, restoreOffset)
                        .putFloat(PREF_STEP_LAST_KNOWN, currentCounter)
                        .putString(PREF_STEP_RESET_DATE, logicalDate)
                        .apply();
                return;
            }

            // 重启检测：传感器值小于上次记录值 → 手机重启了
            if (lastKnown >= 0 && currentCounter < lastKnown) {
                float rescued = lastKnown - dayStart;
                rebootOffset += rescued;
                dayStart = 0;  // TYPE_STEP_COUNTER 重启后从 0 开始
                Log.i(TAG, "\uD83D\uDC63 Reboot detected! rescued=" + (int) rescued
                        + " newOffset=" + (int) rebootOffset);
                prefs.edit()
                        .putFloat(PREF_STEP_DAY_START, dayStart)
                        .putFloat(PREF_STEP_REBOOT_OFFSET, rebootOffset)
                        .putFloat(PREF_STEP_LAST_KNOWN, currentCounter)
                        .apply();
                return;
            }

            // 正常更新 lastKnown
            prefs.edit().putFloat(PREF_STEP_LAST_KNOWN, currentCounter).apply();
        }

        @Override
        public void onAccuracyChanged(Sensor sensor, int accuracy) {}
    };

    /**
     * 获取今日步数。返回 -1 表示传感器不可用。
     */
    private int getTodaySteps() {
        if (latestStepCounter < 0) return -1;

        SharedPreferences prefs = getSharedPreferences("aion_prefs", MODE_PRIVATE);
        String savedDate = prefs.getString(PREF_STEP_RESET_DATE, "");
        String logicalDate = getLogicalDate();

        // 跨日但传感器回调还没触发重置，先算旧日步数返回 0 也行
        // 但更安全的做法是在这里也做重置
        if (!logicalDate.equals(savedDate)) {
            prefs.edit()
                    .putFloat(PREF_STEP_DAY_START, latestStepCounter)
                    .putFloat(PREF_STEP_REBOOT_OFFSET, 0)
                    .putFloat(PREF_STEP_LAST_KNOWN, latestStepCounter)
                    .putString(PREF_STEP_RESET_DATE, logicalDate)
                    .apply();
            return 0;
        }

        float dayStart = prefs.getFloat(PREF_STEP_DAY_START, latestStepCounter);
        float rebootOffset = prefs.getFloat(PREF_STEP_REBOOT_OFFSET, 0);
        int steps = (int) ((latestStepCounter - dayStart) + rebootOffset);
        return Math.max(steps, 0);
    }
}
