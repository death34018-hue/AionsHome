package com.aion.chat;

import android.annotation.SuppressLint;
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
import android.bluetooth.le.ScanResult;
import android.content.Context;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;
import android.webkit.JavascriptInterface;
import android.webkit.WebView;

import java.lang.ref.WeakReference;
import java.util.Locale;
import java.util.ArrayList;
import java.util.List;
import java.util.Comparator;
import java.util.function.BooleanSupplier;
import java.util.UUID;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.TimeUnit;

/**
 * Android BLE bridge for the two independent toy profiles.
 * SOSEXY retains its original packet framing; SAVKOM uses the candidate SL278H/K
 * FFE0/FFE1 protocol and executes durations, HOLD and SEQ locally on Android.
 */
@SuppressLint("MissingPermission")
public final class BleBridge {
    private static final String TAG = "AionBle";
    private static final UUID CCCD_UUID = uuid(0x2902);
    private static WeakReference<BleBridge> activeBridge = new WeakReference<>(null);

    private enum ToyProfile {
        SOSEXY("sosexy", "SOSEXY", uuid(0xee01), uuid(0xee03), uuid(0xee02)),
        SVAKOM("svakom", "SL278", uuid(0xffe0), uuid(0xffe1), uuid(0xffe2)),
        ANKNI("ankni", "ANKNI", uuid(0xdddd), uuid(0xddd1), uuid(0xddd2));

        final String key;
        final String namePrefix;
        final UUID serviceUuid;
        final UUID writeUuid;
        final UUID notifyUuid;

        ToyProfile(String key, String namePrefix, UUID serviceUuid, UUID writeUuid, UUID notifyUuid) {
            this.key = key;
            this.namePrefix = namePrefix;
            this.serviceUuid = serviceUuid;
            this.writeUuid = writeUuid;
            this.notifyUuid = notifyUuid;
        }

        boolean acceptsName(String name) {
            if (this == SVAKOM) return SvakomProtocol.acceptsDeviceName(name);
            return name != null && name.toUpperCase(Locale.US).startsWith(namePrefix);
        }

        static ToyProfile fromKey(String value) {
            if (value != null && value.trim().equalsIgnoreCase(SVAKOM.key)) return SVAKOM;
            if (value != null && value.trim().equalsIgnoreCase(ANKNI.key)) return ANKNI;
            return SOSEXY;
        }
    }

    private final WebView webView;
    private final Context context;
    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    private final ThreadPoolExecutor writeExecutor = new ThreadPoolExecutor(
            1, 1, 0L, TimeUnit.MILLISECONDS, new LinkedBlockingQueue<>());
    private final SvakomWriteQueue svakomWriteQueue = new SvakomWriteQueue(writeExecutor);
    private final SvakomCommandScheduler svakomScheduler;
    private final AnkniScheduler ankniScheduler;
    private final List<BluetoothGattCharacteristic> ankniTargets = new ArrayList<>();
    private volatile int ankniWriteGeneration;
    private final Object ankniWriteLock = new Object();
    private volatile long ankniFrameSerial;

    private BluetoothAdapter adapter;
    private BluetoothLeScanner scanner;
    private BluetoothGatt gatt;
    private BluetoothGattCharacteristic writeCharacteristic;
    private volatile ToyProfile selectedProfile = ToyProfile.SOSEXY;
    private volatile ToyProfile activeProfile;
    private volatile String deviceName = "";
    private volatile boolean connected;
    private volatile boolean scanning;
    private volatile CountDownLatch writeLatch;
    private volatile int writeStatus = BluetoothGatt.GATT_FAILURE;

    public BleBridge(WebView webView, Context context) {
        this.webView = webView;
        this.context = context.getApplicationContext();
        BluetoothManager manager = (BluetoothManager) context.getSystemService(Context.BLUETOOTH_SERVICE);
        if (manager != null) adapter = manager.getAdapter();
        svakomScheduler = new SvakomCommandScheduler(
                this::enqueueSvakomRaw,
                new SvakomCommandScheduler.Clock() {
                    @Override public long now() { return android.os.SystemClock.elapsedRealtime(); }
                    @Override public SvakomCommandScheduler.Cancellation schedule(Runnable runnable, long delayMs) {
                        mainHandler.postDelayed(runnable, delayMs);
                        return () -> mainHandler.removeCallbacks(runnable);
                    }
                },
                new SvakomCommandScheduler.Listener() {
                    @Override public void onAction(String action) {
                        callJs("toyNativeBle.onNativeAction('" + escapeJs(action) + "')");
                    }
                    @Override public void onError(String message) { reportError(message); }
                });
        ankniScheduler = new AnkniScheduler(this::enqueueAnkniRaw,
                (runnable, delay) -> { mainHandler.postDelayed(runnable, delay); return () -> mainHandler.removeCallbacks(runnable); },
                new AnkniScheduler.Listener() {
                    @Override public void onAction(String action) {
                        callJs("toyNativeBle.onNativeAction('" + escapeJs(action) + "')");
                    }
                    @Override public void onReplace() { invalidateAnkniWrites(); }
                });
        activeBridge = new WeakReference<>(this);
    }

    @JavascriptInterface
    public void selectProfile(String profile) {
        ToyProfile next = ToyProfile.fromKey(profile);
        if (next == selectedProfile) return;
        if (connected || scanning) disconnect();
        selectedProfile = next;
        callJs("toyNativeBle.onLog('已选择 " + next.namePrefix + "')");
    }

    @JavascriptInterface
    public void connect() {
        if (adapter == null || !adapter.isEnabled()) {
            reportError("蓝牙未开启");
            return;
        }
        if (connected || scanning) return;
        scanner = adapter.getBluetoothLeScanner();
        if (scanner == null) {
            reportError("无法获取 BLE 扫描器");
            return;
        }
        final ToyProfile requested = selectedProfile;
        scanning = true;
        callJs("toyNativeBle.onLog('正在寻找 " + requested.namePrefix + "…')");
        try {
            scanner.startScan(scanCallback);
        } catch (Exception error) {
            scanning = false;
            reportError("扫描失败: " + safeMessage(error));
            return;
        }
        mainHandler.postDelayed(() -> {
            if (scanning && selectedProfile == requested) {
                stopScan();
                reportError("未找到 " + requested.namePrefix + " 设备");
            }
        }, 10000L);
    }

    @JavascriptInterface
    public void disconnect() {
        stopScan();
        if (connected) {
            final BluetoothGatt closing = gatt;
            if (activeProfile == ToyProfile.SOSEXY) {
                writeExecutor.getQueue().clear();
                writeExecutor.execute(() -> sendSosexyInternal("03000111000003110000071100"));
            } else emergencyStop();
            writeExecutor.execute(() -> mainHandler.post(() -> { if (gatt == closing) closeGatt(); }));
        } else closeGatt();
    }

    @JavascriptInterface public int getAnkniControlVersion() { return 3; }
    @JavascriptInterface public synchronized boolean executeAnkniCommand(String command) {
        if (!readyFor(ToyProfile.ANKNI)) return false;
        try {
            AnkniProtocol.parse(command);
            ankniScheduler.execute(command);
            return true;
        } catch (RuntimeException error) { reportError(safeMessage(error)); return false; }
    }
    @JavascriptInterface public synchronized boolean configureAnkni(boolean swap, String prefix) {
        try { AnkniProtocol.prefix(prefix); invalidateAnkniWrites(); ankniScheduler.configure(swap,prefix); return true; }
        catch (RuntimeException error) { reportError(safeMessage(error)); return false; }
    }
    @JavascriptInterface public String getAnkniWriteTargets() {
        org.json.JSONArray rows = new org.json.JSONArray();
        synchronized (ankniTargets) {
            for (BluetoothGattCharacteristic c : ankniTargets) rows.put(c.getService().getUuid() + " / " + c.getUuid());
        }
        return rows.toString();
    }
    @JavascriptInterface public boolean selectAnkniWriteTarget(int index) {
        synchronized (ankniTargets) {
            if (!readyFor(ToyProfile.ANKNI) || index < 0 || index >= ankniTargets.size()) return false;
            // Finish STOP on the old target before changing it.
            emergencyStop();
            BluetoothGattCharacteristic next = ankniTargets.get(index);
            BluetoothGatt expected = gatt;
            writeExecutor.execute(() -> { if (gatt == expected && readyFor(ToyProfile.ANKNI)) setAnkniTarget(next); });
            return true;
        }
    }
    private void setAnkniTarget(BluetoothGattCharacteristic c) {
        c.setWriteType((c.getProperties() & BluetoothGattCharacteristic.PROPERTY_WRITE_NO_RESPONSE) != 0
                ? BluetoothGattCharacteristic.WRITE_TYPE_NO_RESPONSE : BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT);
        writeCharacteristic = c;
    }
    private int ankniRank(BluetoothGattCharacteristic c) {
        int[] preferred = {0xddd1,0xffe1,0xfee1,0xae01,0xdd11};
        for(int i=0;i<preferred.length;i++) if(c.getUuid().equals(uuid(preferred[i]))) return i;
        return 999;
    }
    private void discoverAnkni(BluetoothGatt connection) {
        synchronized (ankniTargets) {
            ankniTargets.clear();
            for(BluetoothGattService service:connection.getServices()) {
                for(BluetoothGattCharacteristic c:service.getCharacteristics()) {
                    if((c.getProperties() & (BluetoothGattCharacteristic.PROPERTY_WRITE | BluetoothGattCharacteristic.PROPERTY_WRITE_NO_RESPONSE)) != 0) ankniTargets.add(c);
                }
            }
            ankniTargets.sort(Comparator.comparingInt(this::ankniRank));
            if(ankniTargets.isEmpty()) {reportError("ANKNI 没有可写特征");closeGatt();return;}
            setAnkniTarget(ankniTargets.get(0));
        }
        connected = true;
        callJs("toyNativeBle.onConnected('ankni','" + escapeJs(deviceName) + "')");
    }
    private void invalidateAnkniWrites() {
        synchronized (ankniWriteLock) {
            ankniWriteGeneration++;
            CountDownLatch pending = writeLatch;
            if (pending != null) pending.countDown();
        }
    }
    private boolean enqueueAnkniRaw(String hex) {
        if (!readyFor(ToyProfile.ANKNI)) return false;
        final int token = ankniWriteGeneration;
        final BluetoothGatt target = gatt;
        final boolean motionFrame = (hex.startsWith("AA0803") && !hex.equals("AA0803000000B5"))
                || ((hex.startsWith("AA0F02") || hex.startsWith("AA0A02")) && !hex.substring(6,10).equals("0000"));
        final long frame = motionFrame ? ++ankniFrameSerial : ankniFrameSerial;
        writeExecutor.execute(() -> {
            if (motionFrame && frame != ankniFrameSerial) return;
            if(token != ankniWriteGeneration || target != gatt || !readyFor(ToyProfile.ANKNI)) return;
            try {
                if(!writeAwait(hexToBytes(hex), () -> token == ankniWriteGeneration && target == gatt && readyFor(ToyProfile.ANKNI))) failAnkniWrite(token, target, hex, "ANKNI 写入失败");
            } catch(Exception error) { failAnkniWrite(token, target, hex, "ANKNI 写入失败: " + safeMessage(error)); }
        });
        return true;
    }

    private synchronized void failAnkniWrite(int token, BluetoothGatt target, String hex, String message) {
        // The timer can start a waiting plan; check and stop atomically with it.
        synchronized (ankniScheduler) {
            if (token != ankniWriteGeneration || target != gatt || !readyFor(ToyProfile.ANKNI)) return;
            // A failed first STOP must not discard the second classic STOP.
            boolean stopping = hex.equals("AA0803000000B5") || hex.equals("AA0F020000BB") || hex.equals("AA0A020000B6");
            if (!stopping) {
                invalidateAnkniWrites();
                ankniScheduler.stop();
            }
        }
        reportError(message);
    }

    @JavascriptInterface public boolean isConnected() { return connected; }
    /** Prevent a new independent-control page from driving an old linked scheduler. */
    @JavascriptInterface public int getSvakomControlVersion() { return 3; }
    @JavascriptInterface public String getProfile() {
        ToyProfile profile = activeProfile != null ? activeProfile : selectedProfile;
        return profile.key;
    }
    @JavascriptInterface public String getDeviceName() { return deviceName; }

    @JavascriptInterface
    public String getDiagnostics() {
        ToyProfile profile = activeProfile != null ? activeProfile : selectedProfile;
        return "{\"profile\":\"" + escapeJson(profile.key)
                + "\",\"deviceName\":\"" + escapeJson(deviceName)
                + "\",\"connected\":" + connected
                + ",\"service\":\"" + profile.serviceUuid
                + "\",\"write\":\"" + profile.writeUuid + "\"}";
    }

    /** Compatibility entry point: framed SOSEXY command or raw SAVKOM packet. */
    @JavascriptInterface
    public void sendData(String hex) {
        if (!isValidHex(hex)) {
            reportError("无效的十六进制指令");
            return;
        }
        if (!connected || writeCharacteristic == null) {
            reportError("玩具尚未连接");
            return;
        }
        if (activeProfile == ToyProfile.ANKNI) { reportError("ANKNI 请使用独立指令接口"); return; }
        if (activeProfile == ToyProfile.SVAKOM) enqueueSvakomRaw(hex);
        else writeExecutor.execute(() -> sendSosexyInternal(hex));
    }

    /** Semantic SAVKOM command. Android owns all timers and Timeline execution. */
    @JavascriptInterface
    public void executeCommand(String command) {
        if (!readyFor(ToyProfile.SVAKOM)) {
            reportError("SAVKOM 尚未连接或协议未确认");
            return;
        }
        try {
            svakomScheduler.execute(command);
        } catch (RuntimeException error) {
            reportError(safeMessage(error));
        }
    }

    @JavascriptInterface
    public boolean setSvakomVibrationLevel(int level) {
        if (!readyFor(ToyProfile.SVAKOM)) return false;
        try {
            svakomScheduler.updateVibrationLevel(level);
            return true;
        } catch (RuntimeException error) {
            reportError(safeMessage(error));
            return false;
        }
    }

    @JavascriptInterface
    public synchronized void emergencyStop() {
        if (readyFor(ToyProfile.ANKNI)) { invalidateAnkniWrites(); ankniScheduler.stop(); return; }
        if (!readyFor(ToyProfile.SVAKOM)) return;
        writeExecutor.getQueue().clear();
        try {
            svakomScheduler.stop();
        } catch (RuntimeException error) {
            reportError("停止失败: " + safeMessage(error));
        }
    }

    /** Used by app-level emergency paths without depending on a particular WebView page. */
    public static void emergencyStopActiveBridge() {
        BleBridge bridge = activeBridge.get();
        if (bridge != null) bridge.emergencyStop();
    }

    private final ScanCallback scanCallback = new ScanCallback() {
        @Override public void onScanResult(int callbackType, ScanResult result) {
            if (!scanning) return;
            BluetoothDevice candidate = result.getDevice();
            String name = "";
            try { name = candidate.getName(); } catch (Exception ignored) {}
            ToyProfile requested = selectedProfile;
            if (!requested.acceptsName(name)) return;
            stopScan();
            deviceName = name == null ? requested.namePrefix : name;
            activeProfile = requested;
            callJs("toyNativeBle.onLog('找到 " + escapeJs(deviceName) + "，正在校验服务…')");
            connectGatt(candidate);
        }

        @Override public void onScanFailed(int errorCode) {
            scanning = false;
            reportError("BLE 扫描失败: " + errorCode);
        }
    };

    private void stopScan() {
        if (!scanning) return;
        scanning = false;
        try { if (scanner != null) scanner.stopScan(scanCallback); } catch (Exception ignored) {}
    }

    private void connectGatt(BluetoothDevice device) {
        try {
            gatt = device.connectGatt(context, false, gattCallback, BluetoothDevice.TRANSPORT_LE);
        } catch (Exception error) {
            reportError("连接失败: " + safeMessage(error));
            closeGatt();
        }
    }

    @SuppressWarnings("deprecation")
    private final BluetoothGattCallback gattCallback = new BluetoothGattCallback() {
        @Override public void onConnectionStateChange(BluetoothGatt callbackGatt, int status, int newState) {
            if (newState == BluetoothProfile.STATE_CONNECTED) {
                if (!callbackGatt.discoverServices()) reportError("无法发现蓝牙服务");
                return;
            }
            if (newState == BluetoothProfile.STATE_DISCONNECTED) {
                if (callbackGatt == gatt) {
                    connected = false;
                    writeCharacteristic = null;
                    svakomScheduler.cancelPending();
        invalidateAnkniWrites();
        ankniScheduler.cancel();
                    callJs("toyNativeBle.onDisconnected()");
                    try { callbackGatt.close(); } catch (Exception ignored) {}
                    gatt = null;
                    activeProfile = null;
                } else {
                    try { callbackGatt.close(); } catch (Exception ignored) {}
                }
            }
        }

        @Override public void onServicesDiscovered(BluetoothGatt callbackGatt, int status) {
            ToyProfile profile = activeProfile;
            if (callbackGatt != gatt || profile == null) return;
            if (status != BluetoothGatt.GATT_SUCCESS) {
                reportError("服务发现失败: " + status);
                closeGatt();
                return;
            }
            if (profile == ToyProfile.ANKNI) { discoverAnkni(callbackGatt); return; }
            BluetoothGattService service = callbackGatt.getService(profile.serviceUuid);
            if (service == null) {
                reportError(deviceName + " 未提供预期服务 " + profile.serviceUuid + "，协议尚未确认");
                closeGatt();
                return;
            }
            BluetoothGattCharacteristic characteristic = service.getCharacteristic(profile.writeUuid);
            if (characteristic == null) {
                reportError(deviceName + " 未提供预期写入特征 " + profile.writeUuid + "，协议尚未确认");
                closeGatt();
                return;
            }
            int properties = characteristic.getProperties();
            if ((properties & BluetoothGattCharacteristic.PROPERTY_WRITE) != 0) {
                characteristic.setWriteType(BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT);
            } else if ((properties & BluetoothGattCharacteristic.PROPERTY_WRITE_NO_RESPONSE) != 0) {
                characteristic.setWriteType(BluetoothGattCharacteristic.WRITE_TYPE_NO_RESPONSE);
            } else {
                reportError("预期特征不可写，已阻止发送");
                closeGatt();
                return;
            }
            writeCharacteristic = characteristic;
            subscribeNotifications(callbackGatt, service, profile.notifyUuid);
            connected = true;
            callJs("toyNativeBle.onConnected('" + profile.key + "','" + escapeJs(deviceName) + "')");
        }

        @Override public void onCharacteristicWrite(BluetoothGatt callbackGatt,
                                                       BluetoothGattCharacteristic characteristic,
                                                       int status) {
            writeStatus = status;
            CountDownLatch latch = writeLatch;
            if (latch != null) latch.countDown();
        }
    };

    @SuppressWarnings("deprecation")
    private void subscribeNotifications(BluetoothGatt callbackGatt,
                                        BluetoothGattService service,
                                        UUID notifyUuid) {
        BluetoothGattCharacteristic notify = service.getCharacteristic(notifyUuid);
        if (notify == null) return;
        try {
            callbackGatt.setCharacteristicNotification(notify, true);
            BluetoothGattDescriptor descriptor = notify.getDescriptor(CCCD_UUID);
            if (descriptor != null) {
                descriptor.setValue(BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE);
                callbackGatt.writeDescriptor(descriptor);
            }
        } catch (Exception error) {
            Log.w(TAG, "notification subscription failed", error);
        }
    }

    private boolean enqueueSvakomRaw(String hex) {
        if (!readyFor(ToyProfile.SVAKOM) || !isValidHex(hex)) return false;
        svakomWriteQueue.enqueue(hex, () -> {
            try {
                if (!writeAwait(hexToBytes(hex))) reportError("SAVKOM 写入失败: " + hex);
            } catch (Exception error) {
                reportError("SAVKOM 写入失败: " + safeMessage(error));
            }
        });
        return true;
    }

    private void sendSosexyInternal(String hexCommand) {
        if (!readyFor(ToyProfile.SOSEXY)) return;
        try {
            byte[] data = hexToBytes("00" + hexCommand);
            final int chunkSize = 18;
            int chunkCount = Math.max(1, (data.length + chunkSize - 1) / chunkSize);
            int random = (int) (Math.random() * 255);
            for (int index = 0; index < chunkCount; index += 1) {
                int start = index * chunkSize;
                int end = Math.min(start + chunkSize, data.length);
                byte[] packet = new byte[2 + end - start];
                packet[0] = (byte) random;
                packet[1] = (byte) (index + 1);
                System.arraycopy(data, start, packet, 2, end - start);
                if (!writeAwait(packet)) {
                    reportError("SOSEXY 写入超时");
                    return;
                }
            }
            if (data.length > 0 && data.length % chunkSize == 0) {
                writeAwait(new byte[]{(byte) random, (byte) (chunkCount + 1)});
            }
        } catch (Exception error) {
            reportError("SOSEXY 写入失败: " + safeMessage(error));
        }
    }

    @SuppressWarnings("deprecation")
    private boolean writeAwait(byte[] value) throws InterruptedException {
        return writeAwait(value, () -> true);
    }

    @SuppressWarnings("deprecation")
    private boolean writeAwait(byte[] value, BooleanSupplier valid) throws InterruptedException {
        BluetoothGatt currentGatt = gatt;
        BluetoothGattCharacteristic currentCharacteristic = writeCharacteristic;
        if (!connected || currentGatt == null || currentCharacteristic == null) return false;
        for (int attempt = 0; attempt < 3; attempt += 1) {
            if (!valid.getAsBoolean() || currentGatt != gatt || currentCharacteristic != writeCharacteristic) return false;
            writeStatus = BluetoothGatt.GATT_FAILURE;
            boolean noResponse = currentCharacteristic.getWriteType()
                    == BluetoothGattCharacteristic.WRITE_TYPE_NO_RESPONSE;
            CountDownLatch latch = new CountDownLatch(1);
            writeLatch = latch;
            currentCharacteristic.setValue(value);
            boolean accepted;
            try { accepted = currentGatt.writeCharacteristic(currentCharacteristic); }
            catch (Exception error) { accepted = false; }
            if (!accepted) {
                Thread.sleep(80L);
                continue;
            }
            if (noResponse) {
                Thread.sleep(60L);
                return true;
            }
            if (latch.await(2, TimeUnit.SECONDS) && writeStatus == BluetoothGatt.GATT_SUCCESS) return true;
        }
        return false;
    }

    private boolean readyFor(ToyProfile profile) {
        return connected && activeProfile == profile && gatt != null && writeCharacteristic != null;
    }

    private void closeGatt() {
        stopScan();
        connected = false;
        writeCharacteristic = null;
        svakomScheduler.cancelPending();
        invalidateAnkniWrites();
        ankniScheduler.cancel();
        writeExecutor.getQueue().clear();
        BluetoothGatt current = gatt;
        gatt = null;
        activeProfile = null;
        if (current != null) {
            try { current.disconnect(); } catch (Exception ignored) {}
            try { current.close(); } catch (Exception ignored) {}
        }
        callJs("toyNativeBle.onDisconnected()");
    }

    private void reportError(String message) {
        callJs("toyNativeBle.onError('" + escapeJs(message) + "')");
    }

    private void callJs(String expression) {
        mainHandler.post(() -> webView.evaluateJavascript(
                "typeof toyNativeBle!=='undefined'&&" + expression, null));
    }

    private static UUID uuid(int shortUuid) {
        return UUID.fromString(String.format(Locale.US,
                "0000%04x-0000-1000-8000-00805f9b34fb", shortUuid));
    }

    private static boolean isValidHex(String value) {
        return value != null && !value.isEmpty() && (value.length() & 1) == 0
                && value.matches("[0-9a-fA-F]+");
    }

    private static byte[] hexToBytes(String hex) {
        byte[] output = new byte[hex.length() / 2];
        for (int index = 0; index < hex.length(); index += 2) {
            output[index / 2] = (byte) Integer.parseInt(hex.substring(index, index + 2), 16);
        }
        return output;
    }

    private static String safeMessage(Throwable error) {
        String message = error == null ? "未知错误" : error.getMessage();
        return message == null || message.trim().isEmpty() ? error.getClass().getSimpleName() : message;
    }

    private static String escapeJs(String value) {
        return value == null ? "" : value.replace("\\", "\\\\")
                .replace("'", "\\'").replace("\r", "\\r").replace("\n", "\\n");
    }

    private static String escapeJson(String value) {
        return value == null ? "" : value.replace("\\", "\\\\")
                .replace("\"", "\\\"").replace("\r", "\\r").replace("\n", "\\n");
    }
}
