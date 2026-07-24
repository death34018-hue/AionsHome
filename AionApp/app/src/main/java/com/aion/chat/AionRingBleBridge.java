package com.aion.chat;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.app.AlertDialog;
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
import android.content.pm.PackageManager;
import android.os.Build;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;
import android.util.SparseArray;
import android.webkit.JavascriptInterface;
import android.webkit.WebView;

import java.util.ArrayList;
import java.util.Calendar;
import java.util.HashMap;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;

/**
 * 智能戒指 BLE 桥接：给 health.html 提供扫描、连接、写入和通知回包。
 * 前端负责 YCBT 协议封包和解析，原生层只做 WebView 不能稳定完成的 BLE 通道。
 */
@SuppressLint("MissingPermission")
public class AionRingBleBridge {

    private static final String TAG = "AionRingBle";
    private static final String PREFS_NAME = "aion_ring_ble";
    private static final String KEY_DEVICE_ADDRESS = "device_address";
    private static final String KEY_DEVICE_NAME = "device_name";
    private static final String KEY_SYNC_FAIL_COUNT = "bg_sync_fail_count";
    private static final String KEY_NEXT_SYNC_ATTEMPT_AT = "bg_next_sync_attempt_at";
    private static final String KEY_LAST_SYNC_FAILURE = "bg_last_sync_failure";
    private static final String KEY_PAGE_CONNECTED = "page_connection_active";
    private static final String KEY_PAGE_CONNECTED_AT = "page_connection_active_at";
    private static final String KEY_RING_ENABLED = "ring_enabled";
    private static final int AUTO_SYNC_OFFSET_MINUTE = 2;
    private static final UUID CCCD_UUID = UUID.fromString("00002902-0000-1000-8000-00805f9b34fb");
    private static final int[] RING_COMPANY_IDS = {0x7810, 0x7811, 0x7812, 0x7813, 0xFEC5};

    private static class RingService {
        final UUID service;
        final UUID write;
        final UUID notify;
        final String name;
        RingService(String service, String write, String notify, String name) {
            this.service = UUID.fromString(service);
            this.write = UUID.fromString(write);
            this.notify = UUID.fromString(notify);
            this.name = name;
        }
    }

    private static final RingService[] SERVICES = new RingService[] {
            new RingService("be940000-7333-be46-b7ae-689e71722bd5", "be940001-7333-be46-b7ae-689e71722bd5", "be940003-7333-be46-b7ae-689e71722bd5", "Main"),
            new RingService("6e400001-b5a3-f393-e0a9-e50e24dcca9e", "6e400002-b5a3-f393-e0a9-e50e24dcca9e", "6e400003-b5a3-f393-e0a9-e50e24dcca9e", "UART"),
            new RingService("0000ae00-0000-1000-8000-00805f9b34fb", "0000ae01-0000-1000-8000-00805f9b34fb", "0000ae02-0000-1000-8000-00805f9b34fb", "JieLi")
    };

    private final WebView webView;
    private final Context context;
    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    private final ExecutorService writeExecutor = Executors.newSingleThreadExecutor();

    private BluetoothAdapter adapter;
    private BluetoothLeScanner scanner;
    private BluetoothGatt gatt;
    private BluetoothDevice currentDevice;
    private BluetoothGattCharacteristic writeChar;
    private volatile boolean connected = false;
    private volatile boolean gattConnected = false;
    private volatile boolean scanning = false;
    private final Runnable autoSyncRunnable = new Runnable() {
        @Override
        public void run() {
            if (!isRingEnabled() || !connected) return;
            markPageConnectionActive();
            callJs("ringNativeBle.onLog('原生定时触发戒指同步')");
            callJs("syncRingHistory(true)");
            scheduleNextAutoSync();
        }
    };
    private volatile CountDownLatch writeLatch;
    private BluetoothDevice firstCandidate = null;
    private final Map<Integer, BluetoothDevice> candidateDevices = new HashMap<>();
    private final Map<String, Integer> candidateIds = new HashMap<>();
    private final Map<Integer, String> candidateLabels = new HashMap<>();
    private int nextCandidateId = 1;
    private String deviceName = "";
    private String lastStage = "idle";
    private int discoverAttempt = 0;
    private int gattConnectAttempt = 0;
    private boolean legacyGattMode = false;
    private boolean autoConnectScan = false;
    private boolean pickerScan = false;
    private volatile boolean healthPageVisible = false;
    private int backgroundHandoffGeneration = 0;
    private int scanSession = 0;
    private int autoConnectFailCount = 0;       // 自动连接失败计数
    private static final int MAX_AUTO_CONNECT_FAIL = 3;  // 最多自动连接尝试次数
    private AlertDialog scanDialog = null;
    private final java.util.Queue<Runnable> gattOpQueue = new java.util.LinkedList<>();
    private volatile boolean gattOpBusy = false;
    private static final int MAX_GATT_RETRY = 3;
    private static final int GATT_STATUS_BUSY = 133;

    public AionRingBleBridge(WebView webView, Context context) {
        this.webView = webView;
        this.context = context;
        BluetoothManager bm = (BluetoothManager) context.getSystemService(Context.BLUETOOTH_SERVICE);
        if (bm != null) adapter = bm.getAdapter();
    }

    @JavascriptInterface
    public boolean isRingEnabled() {
        return context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
                .getBoolean(KEY_RING_ENABLED, true);
    }

    @JavascriptInterface
    public void setRingEnabled(boolean enabled) {
        context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
                .edit()
                .putBoolean(KEY_RING_ENABLED, enabled)
                .apply();
        if (!enabled) {
            backgroundHandoffGeneration++;
            mainHandler.post(() -> disconnectInternal(true));
        }
        try {
            Intent intent = new Intent(context, AionPushService.class);
            intent.putExtra("action", AionPushService.ACTION_RING_FEATURE_CHANGED);
            context.startService(intent);
        } catch (Exception e) {
            Log.w(TAG, "Unable to notify ring feature setting change", e);
        }
    }

    @JavascriptInterface
    public void connect() {
        autoConnectFailCount = 0;  // 手动连接时重置自动连接失败计数
        callJs("ringNativeBle.onLog('原生层 connect() 已收到')");
        startRingScan(false, true);
    }

    @JavascriptInterface
    public void autoConnect() {
        String savedAddress = getSavedAddress();
        String savedName = getSavedName();
        if ((savedAddress == null || savedAddress.isEmpty()) && (savedName == null || savedName.isEmpty())) {
            callJs("ringNativeBle.onLog('还没有保存过戒指设备，请先手动选择一次')");
            return;
        }
        if (autoConnectFailCount >= MAX_AUTO_CONNECT_FAIL) {
            callJs("ringNativeBle.onLog('自动连接已尝试 " + autoConnectFailCount + " 次未成功，已停止自动尝试，请手动点击连接')");
            return;
        }
        callJs("ringNativeBle.onLog('尝试自动连接已保存戒指: " + escapeJs(savedName.isEmpty() ? savedAddress : savedName) + " (第" + (autoConnectFailCount + 1) + "次)')");
        startRingScan(true, false);
    }

    @JavascriptInterface
    public boolean hasSavedDevice() {
        String savedAddress = getSavedAddress();
        String savedName = getSavedName();
        return (savedAddress != null && !savedAddress.isEmpty()) || (savedName != null && !savedName.isEmpty());
    }

    @JavascriptInterface
    public void forgetSavedDevice() {
        context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
                .edit()
                .remove(KEY_DEVICE_ADDRESS)
                .remove(KEY_DEVICE_NAME)
                .remove(KEY_SYNC_FAIL_COUNT)
                .remove(KEY_NEXT_SYNC_ATTEMPT_AT)
                .remove(KEY_LAST_SYNC_FAILURE)
                .apply();
        callJs("ringNativeBle.onLog('已忘记保存的戒指设备')");
    }

    private void startRingScan(boolean autoConnectSaved, boolean showPicker) {
        if (!isRingEnabled()) return;
        if (adapter == null || !adapter.isEnabled()) {
            callJs("ringNativeBle.onError('蓝牙未开启')");
            return;
        }
        if (!hasBluetoothPermissions()) {
            callJs("ringNativeBle.onLog('蓝牙权限未授予，正在请求权限...')");
            requestBluetoothPermissions();
            return;
        }
        if (connected) {
            callJs("ringNativeBle.onLog('戒指蓝牙已处于连接状态')");
            callJs("ringNativeBle.onConnected('" + escapeJs(deviceName) + "')");
            return;
        }
        if (gatt != null && gattConnected && !showPicker) {
            callJs("ringNativeBle.onLog('已有GATT连接，重新发现服务')");
            discoverServices(gatt);
            return;
        }
        if (scanning) {
            callJs("ringNativeBle.onLog('正在扫描中，请稍等')");
            return;
        }
        // A previous connection attempt can own the peripheral even though it
        // never reached the ready state. Close it before trying to discover the
        // ring again, otherwise the ring may stop advertising and appear lost.
        releaseUnreadyGattBeforeScan();
        scanner = adapter.getBluetoothLeScanner();
        if (scanner == null) {
            callJs("ringNativeBle.onError('无法获取BLE扫描器')");
            return;
        }
        // Claim ownership before scanning so the background service cannot
        // start a second GATT client during the page's connection handshake.
        markPageConnectionActive();
        requestBackgroundRingRelease();
        scanning = true;
        autoConnectScan = autoConnectSaved;
        pickerScan = showPicker;
        int session = ++scanSession;
        lastStage = "scanning";
        firstCandidate = null;
        candidateDevices.clear();
        candidateIds.clear();
        candidateLabels.clear();
        nextCandidateId = 1;
        callJs("ringNativeBle.onLog('" + (autoConnectSaved ? "正在搜索已保存戒指..." : "正在搜索智能戒指候选设备...") + "')");
        if (pickerScan) showScanningDialog(session);
        try {
            scanner.startScan(scanCb);
        } catch (Exception e) {
            scanning = false;
            dismissScanDialog();
            callJs("ringNativeBle.onError('扫描失败: " + escapeJs(e.getMessage()) + "')");
            return;
        }
        if (pickerScan) {
            mainHandler.postDelayed(() -> {
                if (session == scanSession && scanning && pickerScan && !candidateDevices.isEmpty()) {
                    stopScan();
                    showCandidatePicker();
                }
            }, 3500);
        }
        mainHandler.postDelayed(() -> {
            if (session == scanSession && scanning) {
                BluetoothDevice candidate = firstCandidate;
                stopScan();
                if (autoConnectScan && candidate != null) {
                    callJs("ringNativeBle.onLog('未匹配到厂商标识，尝试连接 Smart Ring 候选设备')");
                    connectGatt(candidate);
                } else if (pickerScan && !candidateDevices.isEmpty()) {
                    showCandidatePicker();
                } else if (pickerScan) {
                    showNoDeviceDialog();
                } else if (!candidateDevices.isEmpty()) {
                    callJs("ringNativeBle.onLog('已找到戒指候选设备，请在列表里点选一次，之后会自动连接')");
                } else {                    if (autoConnectScan) autoConnectFailCount++;                    callJs("ringNativeBle.onError('自动匹配未找到 Smart Ring，请让戒指靠近手机并确认绿灯闪烁')");
                }
            }
        }, 12000);
    }

    private void releaseUnreadyGattBeforeScan() {
        BluetoothGatt staleGatt = gatt;
        if (staleGatt == null || connected) return;
        gatt = null;
        gattConnected = false;
        writeChar = null;
        currentDevice = null;
        discoverAttempt = 0;
        gattConnectAttempt = 0;
        lastStage = "releasing_stale_gatt";
        synchronized (gattOpQueue) {
            gattOpQueue.clear();
            gattOpBusy = false;
        }
        try { staleGatt.disconnect(); } catch (Exception ignored) {}
        try { staleGatt.close(); } catch (Exception ignored) {}
        callJs("ringNativeBle.onLog('已释放上一次未完成的戒指连接，重新扫描')");
    }

    private void requestBackgroundRingRelease() {
        try {
            Intent intent = new Intent(context, AionPushService.class);
            intent.putExtra("action", AionPushService.ACTION_RELEASE_RING_FOR_PAGE);
            context.startService(intent);
        } catch (Exception e) {
            Log.w(TAG, "Unable to request background ring handoff", e);
        }
    }

    private void requestBackgroundRingAcquire() {
        if (!isRingEnabled()) return;
        try {
            Intent intent = new Intent(context, AionPushService.class);
            intent.putExtra("action", AionPushService.ACTION_ACQUIRE_RING_FOR_BACKGROUND);
            context.startService(intent);
        } catch (Exception e) {
            Log.w(TAG, "Unable to request immediate background ring handoff", e);
        }
    }

    @JavascriptInterface
    public void disconnect() {
        disconnectInternal(true);
    }

    /**
     * The WebView cannot reliably execute health-page JavaScript after the app
     * is backgrounded. Release its GATT client before the foreground service
     * is told to take over scheduled synchronization.
     */
    public void releaseForBackgroundSync() {
        if (!isRingEnabled()) {
            disconnectInternal(true);
            return;
        }
        handoffConnectedPageToBackground();
    }

    /** Ask the top-level chat page to report whether its persistent health iframe is visible. */
    public void resumeHealthPageConnection() {
        if (!isRingEnabled()) return;
        if (!hasSavedDevice()) return;
        mainHandler.post(() -> webView.evaluateJavascript(
                "(function(){try{if(typeof window.syncHealthRingPageVisibility==='function'){"
                        + "window.syncHealthRingPageVisibility();}}catch(e){}})()",
                null));
    }

    /**
     * Persistent feature iframes are hidden with CSS instead of being unloaded,
     * so pagehide/visibilitychange cannot be used as the BLE ownership signal.
     */
    @JavascriptInterface
    public void setHealthPageVisible(boolean visible) {
        healthPageVisible = visible;
        if (!isRingEnabled()) {
            backgroundHandoffGeneration++;
            disconnectInternal(true);
            return;
        }
        if (!visible) {
            handoffConnectedPageToBackground();
            return;
        }
        backgroundHandoffGeneration++;
        if (!hasSavedDevice()) return;
        if (connected) {
            requestBackgroundRingRelease();
            markPageConnectionActive();
            scheduleNextAutoSync();
            callJs("ringNativeBle.onConnected('" + escapeJs(deviceName) + "')");
            return;
        }
        callJs("ringNativeBle.onDisconnected();"
                + "w.setTimeout(function(){w.reconnectSavedRingIfNeeded();},250)");
    }

    private void handoffConnectedPageToBackground() {
        boolean hadReadyConnection = connected && writeChar != null;
        healthPageVisible = false;
        int handoffGeneration = ++backgroundHandoffGeneration;
        Log.i(TAG, "health page hidden; ready=" + hadReadyConnection
                + ", handing BLE ownership to background service");
        if (hadReadyConnection) {
            // Keep the old client alive briefly so the service can register its
            // own GATT client against the known connected address. This avoids
            // relying on an immediate advertising window after disconnect.
            stopAutoSync();
            clearPageConnectionActive();
            requestBackgroundRingAcquire();
            mainHandler.postDelayed(() -> {
                if (handoffGeneration != backgroundHandoffGeneration || healthPageVisible) return;
                disconnectInternal(true);
            }, 3_500L);
            return;
        }
        disconnectInternal(true);
    }

    public void close() {
        disconnectInternal(false);
    }

    private void disconnectInternal(boolean notifyJs) {
        stopScan();
        dismissScanDialog();
        connected = false;
        gattConnected = false;
        autoConnectScan = false;
        pickerScan = false;
        lastStage = "disconnected";
        writeChar = null;
        if (gatt != null) {
            try {
                gatt.disconnect();
                gatt.close();
            } catch (Exception ignored) {}
            gatt = null;
        }
        stopAutoSync();
        clearPageConnectionActive();
        if (notifyJs) callJs("ringNativeBle.onDisconnected()");
    }

    @JavascriptInterface
    public boolean isConnected() {
        return connected;
    }

    @JavascriptInterface
    public void connectCandidate(int id) {
        if (!isRingEnabled()) return;
        BluetoothDevice dev = candidateDevices.get(id);
        if (dev == null) {
            callJs("ringNativeBle.onError('候选设备不存在或已过期')");
            return;
        }
        stopScan();
        try { deviceName = dev.getName(); } catch (Exception ignored) { deviceName = ""; }
        if (deviceName == null) deviceName = "";
        callJs("ringNativeBle.onLog('正在连接候选设备: " + escapeJs(deviceName.isEmpty() ? ("设备#" + id) : deviceName) + "')");
        saveDevice(dev, deviceName);
        connectGatt(dev);
    }

    @JavascriptInterface
    public String getStatus() {
        boolean bluetoothEnabled = adapter != null && adapter.isEnabled();
        return "adapter=" + (adapter != null)
                + ", ringEnabled=" + isRingEnabled()
                + ", enabled=" + bluetoothEnabled
                + ", scanner=" + (scanner != null)
                + ", scanning=" + scanning
                + ", gattConnected=" + gattConnected
                + ", connected=" + connected
                + ", writeChar=" + (writeChar != null)
                + ", candidates=" + candidateDevices.size()
                + ", stage=" + escapeJs(lastStage)
                + ", device=" + escapeJs(deviceName);
    }

    @JavascriptInterface
    public void write(final String hexPacket) {
        if (!isRingEnabled()) return;
        if (!connected || writeChar == null || gatt == null) return;
        writeExecutor.execute(() -> writeInternal(hexPacket));
    }

    private final ScanCallback scanCb = new ScanCallback() {
        @Override
        public void onScanResult(int callbackType, ScanResult result) {
            if (!scanning) return;
            BluetoothDevice dev = result.getDevice();
            String name = getDeviceName(dev);
            boolean smartRingName = name.toUpperCase().contains("SMART RING");
            boolean likely = looksLikeRing(result, dev);
            if (smartRingName || likely) rememberCandidate(result, dev);
            if (smartRingName || firstCandidate == null && hasKnownRingService(result)) {
                firstCandidate = dev;
            }
            if (!smartRingName && !likely) return;
            if (autoConnectScan && matchesSavedDevice(dev, name)) {
                stopScan();
                deviceName = name;
                callJs("ringNativeBle.onLog('找到已保存戒指: " + escapeJs(deviceName.isEmpty() ? "智能戒指" : deviceName) + "')");
                connectGatt(dev);
            }
        }

        @Override
        public void onScanFailed(int errorCode) {
            scanning = false;
            callJs("ringNativeBle.onError('扫描失败，错误码: " + errorCode + "')");
        }
    };

    private int rememberCandidate(ScanResult result, BluetoothDevice dev) {
        String key;
        try { key = dev.getAddress(); } catch (Exception e) { key = String.valueOf(dev.hashCode()); }
        Integer existing = candidateIds.get(key);
        if (existing != null) return existing;
        if (candidateDevices.size() >= 24) return -1;
        int id = nextCandidateId++;
        candidateIds.put(key, id);
        candidateDevices.put(id, dev);

        String name = getDeviceName(dev);
        if (name == null || name.trim().isEmpty()) name = "未知设备";
        boolean likely = looksLikeRing(result, dev);
        int rssi = result.getRssi();
        candidateLabels.put(id, name + (likely ? " · 可能是戒指" : "") + "  " + rssi + " dBm");
        callJs("ringNativeBle.onScanDevice(" + id + ", '" + escapeJs(name) + "', " + rssi + ", " + likely + ")");
        return id;
    }

    private void showScanningDialog(int session) {
        if (!(context instanceof Activity)) return;
        mainHandler.post(() -> {
            if (session != scanSession || !pickerScan) return;
            dismissScanDialog();
            Activity activity = (Activity) context;
            if (activity.isFinishing()) return;
            scanDialog = new AlertDialog.Builder(activity)
                    .setTitle("选择蓝牙戒指")
                    .setMessage("正在搜索附近的 Smart Ring...")
                    .setNegativeButton("取消", (d, which) -> {
                        stopScan();
                        pickerScan = false;
                    })
                    .create();
            scanDialog.setOnCancelListener(d -> {
                stopScan();
                pickerScan = false;
            });
            scanDialog.show();
        });
    }

    private void showCandidatePicker() {
        if (!(context instanceof Activity)) {
            callJs("ringNativeBle.onLog('已找到戒指候选设备，请在页面列表里点选')");
            return;
        }
        mainHandler.post(() -> {
            dismissScanDialog();
            if (candidateDevices.isEmpty()) {
                showNoDeviceDialog();
                return;
            }
            Activity activity = (Activity) context;
            if (activity.isFinishing()) return;
            ArrayList<Integer> ids = new ArrayList<>();
            ArrayList<String> labels = new ArrayList<>();
            for (Map.Entry<Integer, BluetoothDevice> entry : candidateDevices.entrySet()) {
                ids.add(entry.getKey());
                String label = candidateLabels.get(entry.getKey());
                labels.add(label == null ? ("戒指设备 #" + entry.getKey()) : label);
            }
            scanDialog = new AlertDialog.Builder(activity)
                    .setTitle("选择蓝牙戒指")
                    .setItems(labels.toArray(new String[0]), (d, which) -> {
                        pickerScan = false;
                        int id = ids.get(which);
                        callJs("ringNativeBle.onLog('已在弹窗中选择戒指 #" + id + "')");
                        connectCandidate(id);
                    })
                    .setNegativeButton("取消", (d, which) -> {
                        pickerScan = false;
                        callJs("ringNativeBle.onLog('已取消选择戒指')");
                    })
                    .create();
            scanDialog.setOnCancelListener(d -> {
                pickerScan = false;
                callJs("ringNativeBle.onLog('已取消选择戒指')");
            });
            scanDialog.show();
        });
    }

    private void showNoDeviceDialog() {
        if (!(context instanceof Activity)) {
            callJs("ringNativeBle.onError('未找到 Smart Ring，请让戒指靠近手机并确认绿灯闪烁')");
            return;
        }
        mainHandler.post(() -> {
            dismissScanDialog();
            Activity activity = (Activity) context;
            if (activity.isFinishing()) return;
            scanDialog = new AlertDialog.Builder(activity)
                    .setTitle("没有找到戒指")
                    .setMessage("请让戒指靠近手机，确认戒指绿灯闪烁后再试一次。")
                    .setPositiveButton("重试", (d, which) -> startRingScan(false, true))
                    .setNegativeButton("取消", (d, which) -> {
                        pickerScan = false;
                        callJs("ringNativeBle.onError('未找到 Smart Ring')");
                    })
                    .create();
            scanDialog.show();
        });
    }

    private void dismissScanDialog() {
        if (scanDialog != null) {
            try { scanDialog.dismiss(); } catch (Exception ignored) {}
            scanDialog = null;
        }
    }

    private void saveDevice(BluetoothDevice dev, String name) {
        String address = "";
        try { address = dev.getAddress(); } catch (Exception ignored) {}
        if (name == null) name = "";
        context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
                .edit()
                .putString(KEY_DEVICE_ADDRESS, address == null ? "" : address)
                .putString(KEY_DEVICE_NAME, name)
                .apply();
        callJs("ringNativeBle.onLog('已保存戒指设备，之后可自动连接')");
    }

    private String getSavedAddress() {
        String value = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
                .getString(KEY_DEVICE_ADDRESS, "");
        return value == null ? "" : value;
    }

    private String getSavedName() {
        String value = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
                .getString(KEY_DEVICE_NAME, "");
        return value == null ? "" : value;
    }

    private boolean matchesSavedDevice(BluetoothDevice dev, String name) {
        String savedAddress = getSavedAddress();
        String savedName = getSavedName();
        String address = "";
        try { address = dev.getAddress(); } catch (Exception ignored) {}
        if (!savedAddress.isEmpty() && savedAddress.equals(address)) return true;
        if (!savedName.isEmpty() && name != null && savedName.equals(name)) return true;
        return savedAddress.isEmpty() && savedName.isEmpty();
    }

    private boolean looksLikeRing(ScanResult result, BluetoothDevice dev) {
        String name = null;
        try { name = dev.getName(); } catch (Exception ignored) {}
        if (name != null) {
            String upper = name.toUpperCase();
            if (upper.startsWith("DFU") || upper.contains("SMART RING") || upper.contains("YCBT")) return true;
        }
        ScanRecord record = result.getScanRecord();
        if (record == null) return false;
        if (hasKnownRingService(result)) return true;
        SparseArray<byte[]> mfg = record.getManufacturerSpecificData();
        for (int id : RING_COMPANY_IDS) {
            if (mfg.get(id) != null) return true;
        }
        return false;
    }

    private boolean hasKnownRingService(ScanResult result) {
        ScanRecord record = result.getScanRecord();
        if (record == null || record.getServiceUuids() == null) return false;
        for (android.os.ParcelUuid uuid : record.getServiceUuids()) {
            for (RingService svc : SERVICES) {
                if (svc.service.equals(uuid.getUuid())) return true;
            }
        }
        return false;
    }

    private void stopScan() {
        if (!scanning) return;
        scanning = false;
        try { if (scanner != null) scanner.stopScan(scanCb); } catch (Exception ignored) {}
    }

    private void connectGatt(BluetoothDevice dev) {
        if (!isRingEnabled()) return;
        gattConnectAttempt = 0;
        connectGatt(dev, false);
    }

    private void connectGatt(BluetoothDevice dev, boolean legacyMode) {
        if (!isRingEnabled()) return;
        try {
            stopScan();
            connected = false;
            synchronized (gattOpQueue) { gattOpQueue.clear(); gattOpBusy = false; }
            gattConnected = false;
            writeChar = null;
            currentDevice = dev;
            discoverAttempt = 0;
            legacyGattMode = legacyMode;
            gattConnectAttempt++;
            lastStage = "connectGatt";
            if (gatt != null) {
                try {
                    gatt.disconnect();
                    refreshDeviceCache(gatt);
                    gatt.close();
                } catch (Exception ignored) {}
                gatt = null;
            }
            deviceName = getDeviceName(dev);
            callJs("ringNativeBle.onLog('正在建立 GATT 连接: " + escapeJs(deviceName.isEmpty() ? "Smart Ring" : deviceName) + "，模式=" + (legacyMode ? "legacy" : "le") + "')");
            if (legacyMode || Build.VERSION.SDK_INT < Build.VERSION_CODES.M) {
                gatt = dev.connectGatt(context, false, gattCb);
            } else {
                gatt = dev.connectGatt(context, false, gattCb, BluetoothDevice.TRANSPORT_LE);
            }
        } catch (Exception e) {
            lastStage = "connectGatt_error";
            callJs("ringNativeBle.onError('连接失败: " + escapeJs(e.getMessage()) + "')");
        }
    }

    @SuppressWarnings("deprecation")
    private final BluetoothGattCallback gattCb = new BluetoothGattCallback() {
        @Override
        public void onConnectionStateChange(BluetoothGatt g, int status, int newState) {
            if (g != gatt) {
                try { g.close(); } catch (Exception ignored) {}
                return;
            }
            callJs("ringNativeBle.onLog('GATT状态变化: status=" + status + ", state=" + newState + "')");
            if (status != BluetoothGatt.GATT_SUCCESS) {
                connected = false;
                gattConnected = false;
                writeChar = null;
                stopAutoSync();
                clearPageConnectionActive();
                try { g.close(); } catch (Exception ignored) {}
                // GATT 133 / 62 是 Android 常见临时失败，重试可恢复
                if ((status == GATT_STATUS_BUSY || status == 62) && gattConnectAttempt < MAX_GATT_RETRY && currentDevice != null) {
                    lastStage = "gatt_retry_" + gattConnectAttempt;
                    callJs("ringNativeBle.onLog('GATT 状态码 " + status + "，第 " + gattConnectAttempt + " 次重试...')");
                    mainHandler.postDelayed(() -> connectGatt(currentDevice, gattConnectAttempt >= 2), 800);
                    return;
                }
                lastStage = "gatt_status_" + status;
                if (autoConnectScan) autoConnectFailCount++;
                callJs("ringNativeBle.onError('GATT连接异常，状态码: " + status + "')");
                return;
            }
            if (newState == BluetoothProfile.STATE_CONNECTED) {
                gattConnected = true;
                lastStage = "gatt_connected";
                callJs("ringNativeBle.onLog('GATT已连接，准备发现服务')");
                try { g.requestConnectionPriority(BluetoothGatt.CONNECTION_PRIORITY_HIGH); } catch (Exception ignored) {}
                mainHandler.postDelayed(() -> {
                    refreshDeviceCache(g);
                    discoverServices(g);
                }, legacyGattMode ? 1200 : 700);
            } else if (newState == BluetoothProfile.STATE_DISCONNECTED) {
                connected = false;
                gattConnected = false;
                writeChar = null;
                lastStage = "disconnected";
                stopAutoSync();
                clearPageConnectionActive();
                callJs("ringNativeBle.onDisconnected()");
                try { g.close(); } catch (Exception ignored) {}
            }
        }

        @Override
        public void onServicesDiscovered(BluetoothGatt g, int status) {
            if (g != gatt) return;
            discoverAttempt = 0;
            callJs("ringNativeBle.onLog('服务发现完成: status=" + status + ", count=" + g.getServices().size() + "')");
            if (status != BluetoothGatt.GATT_SUCCESS) {
                if (!legacyGattMode && currentDevice != null) {
                    retryWithLegacyGatt(g, "服务发现状态异常，换 legacy GATT 重连");
                    return;
                }
                lastStage = "service_status_" + status;
                callJs("ringNativeBle.onError('服务发现失败')");
                return;
            }
            if (g.getServices().isEmpty() && !legacyGattMode && currentDevice != null) {
                retryWithLegacyGatt(g, "服务列表为空，换 legacy GATT 重连");
                return;
            }
            setupServices(g);
        }

        @Override
        public void onMtuChanged(BluetoothGatt g, int mtu, int status) {
            callJs("ringNativeBle.onLog('MTU状态: mtu=" + mtu + ", status=" + status + "')");
        }

        @Override
        public void onCharacteristicChanged(BluetoothGatt g, BluetoothGattCharacteristic c) {
            callJs("ringNativeBle.onData('" + bytesToHex(c.getValue()) + "')");
        }

        @Override
        public void onCharacteristicChanged(BluetoothGatt g, BluetoothGattCharacteristic c, byte[] value) {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                callJs("ringNativeBle.onData('" + bytesToHex(value) + "')");
            }
        }

        @Override
        public void onCharacteristicWrite(BluetoothGatt g, BluetoothGattCharacteristic c, int status) {
            CountDownLatch l = writeLatch;
            if (l != null) l.countDown();
        }

        @Override
        public void onDescriptorWrite(BluetoothGatt g, BluetoothGattDescriptor d, int status) {
            callJs("ringNativeBle.onLog('描述符写入完成: " + d.getCharacteristic().getUuid() + ", status=" + status + "')");
            processNextGattOp();
        }
    };

    @SuppressWarnings("deprecation")
    private void writeInternal(String hexPacket) {
        try {
            byte[] value = hexToBytes(hexPacket);
            writeLatch = new CountDownLatch(1);
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                gatt.writeCharacteristic(writeChar, value, writeChar.getWriteType());
            } else {
                writeChar.setValue(value);
                gatt.writeCharacteristic(writeChar);
            }
            writeLatch.await(3, TimeUnit.SECONDS);
        } catch (Exception e) {
            Log.e(TAG, "write error", e);
            callJs("ringNativeBle.onError('发送失败: " + escapeJs(e.getMessage()) + "')");
        }
    }

    private byte[] hexToBytes(String hex) {
        int len = hex.length();
        byte[] out = new byte[len / 2];
        for (int i = 0; i < len; i += 2) {
            out[i / 2] = (byte) ((Character.digit(hex.charAt(i), 16) << 4)
                    + Character.digit(hex.charAt(i + 1), 16));
        }
        return out;
    }

    private String bytesToHex(byte[] bytes) {
        if (bytes == null) return "";
        StringBuilder sb = new StringBuilder(bytes.length * 2);
        for (byte b : bytes) sb.append(String.format("%02x", b & 0xFF));
        return sb.toString();
    }

    private void callJs(String js) {
        mainHandler.post(() -> {
            // health.html 在 chat.html 的 iframe 中加载，evaluateJavascript 只在主框架执行，
            // 必须遍历所有 iframe 找到 ringNativeBle 所在的窗口
            String script =
                    "(function(){" +
                    "var _f=function(w){try{if(typeof w.ringNativeBle!=='undefined'){w." + js + "}}catch(e){}};" +
                    "_f(window);" +
                    "try{var fs=document.querySelectorAll('iframe');" +
                    "for(var i=0;i<fs.length;i++){try{_f(fs[i].contentWindow)}catch(e){}}}catch(e){}" +
                    "})()";
            webView.evaluateJavascript(script, null);
        });
    }

    private void discoverServices(BluetoothGatt g) {
        lastStage = "discover_services";
        discoverAttempt++;
        boolean started = false;
        try { started = g.discoverServices(); } catch (Exception ignored) {}
        final int attempt = discoverAttempt;
        callJs("ringNativeBle.onLog('开始发现服务: " + started + " (第" + attempt + "次)')");
        mainHandler.postDelayed(() -> {
            if (!connected && gattConnected && "discover_services".equals(lastStage) && attempt == discoverAttempt) {
                int count = 0;
                try { count = g.getServices().size(); } catch (Exception ignored) {}
                callJs("ringNativeBle.onLog('服务发现等待超时，缓存服务数: " + count + "')");
                if (count > 0) {
                    setupServices(g);
                } else if (attempt < 2) {
                    discoverServices(g);
                } else if (!legacyGattMode && currentDevice != null) {
                    retryWithLegacyGatt(g, "服务发现未返回，刷新缓存后使用 legacy GATT 重连");
                } else {
                    lastStage = "discover_timeout";
                    callJs("ringNativeBle.onError('服务发现超时，请关闭戒指官方App或重新点连接')");
                    try {
                        g.disconnect();
                    } catch (Exception ignored) {}
                }
            }
        }, 3500);
    }

    private void setupServices(BluetoothGatt g) {
        BluetoothGattService matchedService = null;
        RingService matched = null;
        for (RingService svc : SERVICES) {
            matchedService = g.getService(svc.service);
            if (matchedService != null) {
                matched = svc;
                break;
            }
        }
        if (matchedService == null || matched == null) {
            callJs("ringNativeBle.onLog('预设服务未命中，尝试从实际服务中自动选择通道')");
            for (BluetoothGattService service : g.getServices()) {
                BluetoothGattCharacteristic writable = null;
                boolean hasNotify = false;
                for (BluetoothGattCharacteristic ch : service.getCharacteristics()) {
                    int props = ch.getProperties();
                    if (writable == null && ((props & BluetoothGattCharacteristic.PROPERTY_WRITE) != 0
                            || (props & BluetoothGattCharacteristic.PROPERTY_WRITE_NO_RESPONSE) != 0)) {
                        writable = ch;
                    }
                    if ((props & BluetoothGattCharacteristic.PROPERTY_NOTIFY) != 0
                            || (props & BluetoothGattCharacteristic.PROPERTY_INDICATE) != 0) {
                        hasNotify = true;
                    }
                }
                if (writable != null && hasNotify) {
                    matchedService = service;
                    writeChar = writable;
                    matched = new RingService(service.getUuid().toString(), writable.getUuid().toString(), writable.getUuid().toString(), "Auto");
                    break;
                }
            }
            if (matchedService == null || writeChar == null || matched == null) {
                if (!legacyGattMode && currentDevice != null) {
                    retryWithLegacyGatt(g, "未找到预设服务，换 legacy GATT 重连");
                    return;
                }
                lastStage = "service_not_found";
                callJs("ringNativeBle.onError('未找到可用戒指BLE通道，实际服务: " + escapeJs(describeServices(g)) + "')");
                return;
            }
        } else {
            writeChar = matchedService.getCharacteristic(matched.write);
        }
        lastStage = "service_" + matched.name;
        callJs("ringNativeBle.onLog('找到戒指服务: " + matched.name + "')");
        if (writeChar == null) {
            lastStage = "write_char_not_found";
            callJs("ringNativeBle.onError('未找到写入特征，实际服务: " + escapeJs(describeServices(g)) + "')");
            return;
        }
        if ((writeChar.getProperties() & BluetoothGattCharacteristic.PROPERTY_WRITE) != 0) {
            writeChar.setWriteType(BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT);
        } else {
            writeChar.setWriteType(BluetoothGattCharacteristic.WRITE_TYPE_NO_RESPONSE);
        }
        java.util.HashSet<UUID> notificationStarted = new java.util.HashSet<>();
        BluetoothGattCharacteristic notifyChar = matchedService.getCharacteristic(matched.notify);
        if (notifyChar != null) {
            enableNotification(g, notifyChar);
            notificationStarted.add(notifyChar.getUuid());
        }
        if (!notificationStarted.contains(writeChar.getUuid())) {
            int writeProps = writeChar.getProperties();
            if ((writeProps & BluetoothGattCharacteristic.PROPERTY_NOTIFY) != 0
                    || (writeProps & BluetoothGattCharacteristic.PROPERTY_INDICATE) != 0) {
                enableNotification(g, writeChar);
                notificationStarted.add(writeChar.getUuid());
            }
        }
        for (BluetoothGattCharacteristic ch : matchedService.getCharacteristics()) {
            if (notificationStarted.contains(ch.getUuid())) continue;
            int props = ch.getProperties();
            if ((props & BluetoothGattCharacteristic.PROPERTY_NOTIFY) != 0
                    || (props & BluetoothGattCharacteristic.PROPERTY_INDICATE) != 0) {
                enableNotification(g, ch);
                notificationStarted.add(ch.getUuid());
            }
        }
        connected = true;
        gattConnected = true;
        lastStage = "ready";
        autoConnectFailCount = 0;  // 连接成功，重置失败计数
        clearBackgroundFailureBackoff();
        markPageConnectionActive();
        scheduleNextAutoSync();
        callJs("ringNativeBle.onConnected('" + escapeJs(deviceName) + "')");
        callJs("ringNativeBle.onLog('使用 " + matched.name + " 通道')");
    }

    private void clearBackgroundFailureBackoff() {
        context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
                .edit()
                .remove(KEY_SYNC_FAIL_COUNT)
                .remove(KEY_NEXT_SYNC_ATTEMPT_AT)
                .remove(KEY_LAST_SYNC_FAILURE)
                .apply();
    }

    private void markPageConnectionActive() {
        context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
                .edit()
                .putBoolean(KEY_PAGE_CONNECTED, true)
                .putLong(KEY_PAGE_CONNECTED_AT, System.currentTimeMillis())
                .apply();
    }

    private void clearPageConnectionActive() {
        context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
                .edit()
                .remove(KEY_PAGE_CONNECTED)
                .remove(KEY_PAGE_CONNECTED_AT)
                .apply();
    }

    private void scheduleNextAutoSync() {
        if (!isRingEnabled() || !connected) return;
        mainHandler.removeCallbacks(autoSyncRunnable);
        long delay = computeNextAutoSyncDelayMs();
        mainHandler.postDelayed(autoSyncRunnable, delay);
    }

    private void stopAutoSync() {
        mainHandler.removeCallbacks(autoSyncRunnable);
    }

    private long computeNextAutoSyncDelayMs() {
        Calendar cal = Calendar.getInstance();
        int minute = cal.get(Calendar.MINUTE);
        int second = cal.get(Calendar.SECOND);
        int millisecond = cal.get(Calendar.MILLISECOND);
        int targetMinute = (minute / 10) * 10 + AUTO_SYNC_OFFSET_MINUTE;
        if (minute > targetMinute
                || (minute == targetMinute && (second > 0 || millisecond > 0))) {
            targetMinute += 10;
        }
        cal.set(Calendar.SECOND, 0);
        cal.set(Calendar.MILLISECOND, 0);
        if (targetMinute >= 60) {
            cal.add(Calendar.HOUR_OF_DAY, 1);
            targetMinute -= 60;
        }
        cal.set(Calendar.MINUTE, targetMinute);
        long delay = cal.getTimeInMillis() - System.currentTimeMillis();
        return Math.max(delay, 1000);
    }

    @SuppressWarnings("deprecation")
    private void enableNotification(BluetoothGatt g, BluetoothGattCharacteristic ch) {
        int props = ch.getProperties();
        if ((props & BluetoothGattCharacteristic.PROPERTY_NOTIFY) == 0
                && (props & BluetoothGattCharacteristic.PROPERTY_INDICATE) == 0) {
            return;
        }
        try {
            g.setCharacteristicNotification(ch, true);
            BluetoothGattDescriptor desc = ch.getDescriptor(CCCD_UUID);
            if (desc != null) {
                byte[] descValue = (props & BluetoothGattCharacteristic.PROPERTY_INDICATE) != 0
                        ? BluetoothGattDescriptor.ENABLE_INDICATION_VALUE
                        : BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE;
                enqueueGattOp(() -> {
                    try {
                        boolean ok;
                        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                            int result = g.writeDescriptor(desc, descValue);
                            ok = result == android.bluetooth.BluetoothStatusCodes.SUCCESS;
                        } else {
                            desc.setValue(descValue);
                            ok = g.writeDescriptor(desc);
                        }
                        callJs("ringNativeBle.onLog('订阅通知: " + ch.getUuid() + ", ok=" + ok + "')");
                        if (!ok) processNextGattOp();
                    } catch (Exception e) {
                        callJs("ringNativeBle.onLog('订阅通知失败: " + ch.getUuid() + ", " + escapeJs(e.getMessage()) + "')");
                        processNextGattOp();
                    }
                });
            }
        } catch (Exception e) {
            callJs("ringNativeBle.onLog('订阅通知失败: " + ch.getUuid() + ", " + escapeJs(e.getMessage()) + "')");
        }
    }

    private void retryWithLegacyGatt(BluetoothGatt g, String reason) {
        callJs("ringNativeBle.onLog('" + escapeJs(reason) + "')");
        try {
            refreshDeviceCache(g);
            g.disconnect();
            g.close();
        } catch (Exception ignored) {}
        gatt = null;
        connected = false;
        gattConnected = false;
        writeChar = null;
        if (currentDevice != null) {
            mainHandler.postDelayed(() -> connectGatt(currentDevice, true), 800);
        }
    }

    private void refreshDeviceCache(BluetoothGatt g) {
        if (g == null) return;
        try {
            java.lang.reflect.Method refresh = g.getClass().getMethod("refresh");
            refresh.invoke(g);
            callJs("ringNativeBle.onLog('已刷新GATT缓存')");
        } catch (Exception ignored) {}
    }

    private String getDeviceName(BluetoothDevice dev) {
        String name = "";
        try { name = dev.getName(); } catch (Exception ignored) {}
        return name == null ? "" : name;
    }

    private String describeServices(BluetoothGatt g) {
        StringBuilder sb = new StringBuilder();
        for (BluetoothGattService service : g.getServices()) {
            if (sb.length() > 0) sb.append(" | ");
            sb.append(service.getUuid()).append(":");
            for (BluetoothGattCharacteristic ch : service.getCharacteristics()) {
                sb.append(ch.getUuid()).append("(").append(ch.getProperties()).append("),");
            }
        }
        return sb.toString();
    }

    private String escapeJs(String s) {
        return s == null ? "" : s.replace("\\", "\\\\").replace("'", "\\'");
    }

    // ── 运行时蓝牙权限检查（Android 12+ 必须） ──

    private boolean hasBluetoothPermissions() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            return context.checkSelfPermission("android.permission.BLUETOOTH_SCAN")
                        == PackageManager.PERMISSION_GRANTED
                    && context.checkSelfPermission("android.permission.BLUETOOTH_CONNECT")
                        == PackageManager.PERMISSION_GRANTED;
        }
        return true;
    }

    private void requestBluetoothPermissions() {
        if (context instanceof Activity) {
            Activity activity = (Activity) context;
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                activity.requestPermissions(
                        new String[]{
                                "android.permission.BLUETOOTH_SCAN",
                                "android.permission.BLUETOOTH_CONNECT"
                        }, 5001);
            }
        } else {
            callJs("ringNativeBle.onError('无法请求蓝牙权限，请在系统设置中手动允许')");
        }
    }

    // ── GATT 操作队列（Android 一次只能执行一个 GATT 写操作） ──

    private void enqueueGattOp(Runnable op) {
        Runnable toRun = null;
        synchronized (gattOpQueue) {
            if (!gattOpBusy) {
                gattOpBusy = true;
                toRun = op;
            } else {
                gattOpQueue.add(op);
            }
        }
        if (toRun != null) toRun.run();
    }

    private void processNextGattOp() {
        Runnable op;
        synchronized (gattOpQueue) {
            op = gattOpQueue.poll();
            if (op == null) {
                gattOpBusy = false;
                return;
            }
        }
        op.run();
    }
}
