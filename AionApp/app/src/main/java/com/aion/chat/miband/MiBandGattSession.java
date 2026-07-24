package com.aion.chat.miband;

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
import android.content.Context;
import android.os.Build;
import android.os.Handler;
import android.os.Looper;

import java.io.ByteArrayOutputStream;
import java.security.GeneralSecurityException;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Calendar;
import java.util.Collections;
import java.util.List;
import java.util.TimeZone;
import java.util.UUID;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.zip.CRC32;

@SuppressLint("MissingPermission")
public final class MiBandGattSession {
    private static final UUID CCCD = UUID.fromString("00002902-0000-1000-8000-00805f9b34fb");
    private static final int HEART_RATE_COMMAND = 0x04;
    private static final int HEART_RATE_MODE_STOP = 0x00;
    private static final int HEART_RATE_MODE_START = 0x01;
    private static final int HEART_RATE_MODE_CONTINUE = 0x02;
    private static final byte[] FIND_BAND_START = {0x03};
    private static final byte[] FIND_BAND_STOP = {0x06};

    public interface Listener {
        void onDisconnected(String reason);
        void onHeartRate(int bpm, long measuredAtMillis);
    }

    private final Context context;
    private final Listener listener;
    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    private final Object writeLock = new Object();
    private final Object fetchLock = new Object();
    private MiBandProtocol.ChunkEncoder encoder = new MiBandProtocol.ChunkEncoder(23);
    private MiBandProtocol.ChunkDecoder decoder = new MiBandProtocol.ChunkDecoder();

    private BluetoothGatt gatt;
    private BluetoothGattCharacteristic chunkWrite;
    private BluetoothGattCharacteristic chunkRead;
    private BluetoothGattCharacteristic fetchMetadata;
    private BluetoothGattCharacteristic fetchData;
    private BluetoothGattCharacteristic heartRateMeasurement;
    private BluetoothGattCharacteristic batteryLevel;
    private CountDownLatch serviceLatch;
    private CountDownLatch descriptorLatch;
    private CountDownLatch authLatch;
    private CountDownLatch configLatch;
    private CountDownLatch readLatch;
    private CountDownLatch fetchLatch;
    private CountDownLatch mtuLatch;
    private CountDownLatch characteristicWriteLatch;
    private volatile int characteristicWriteStatus = BluetoothGatt.GATT_SUCCESS;
    private volatile Exception operationError;
    private volatile int battery = -1;
    private volatile boolean connected;
    private volatile boolean authenticated;
    private volatile boolean realtime;
    private int writeHandle;
    private byte[] authKey;

    private int fetchCounter;
    private long fetchStartMillis;
    private Long fetchExpectedCrc;
    private boolean fetchCompletion;
    private int fetchGeneration;
    private final ByteArrayOutputStream fetchBuffer = new ByteArrayOutputStream();
    private List<MiBandProtocol.ActivitySample> fetchedActivity = Collections.emptyList();

    private final Runnable realtimeKeepalive = new Runnable() {
        @Override public void run() {
            if (!realtime || !connected) return;
            try {
                writeChunked(MiBandProtocol.HEART_RATE_ENDPOINT,
                        new byte[] {HEART_RATE_COMMAND, HEART_RATE_MODE_CONTINUE}, false);
            } catch (Exception error) {
                realtime = false;
            }
            if (realtime) mainHandler.postDelayed(this, 1_000L);
        }
    };

    public MiBandGattSession(Context context, Listener listener) {
        this.context = context.getApplicationContext();
        this.listener = listener;
    }

    public boolean isConnected() { return connected; }
    public boolean isAuthenticated() { return authenticated; }
    public boolean isRealtime() { return realtime; }

    public void connect(String address, byte[] configuredAuthKey, long timeoutMillis) throws Exception {
        close();
        authKey = Arrays.copyOf(configuredAuthKey, configuredAuthKey.length);
        decoder = new MiBandProtocol.ChunkDecoder();
        serviceLatch = new CountDownLatch(1);
        operationError = null;
        BluetoothManager manager = (BluetoothManager) context.getSystemService(Context.BLUETOOTH_SERVICE);
        BluetoothAdapter adapter = manager == null ? null : manager.getAdapter();
        if (adapter == null || !adapter.isEnabled()) throw new IllegalStateException("蓝牙未开启");
        BluetoothDevice device = adapter.getRemoteDevice(address);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            gatt = device.connectGatt(context, false, callback, BluetoothDevice.TRANSPORT_LE);
        } else {
            gatt = device.connectGatt(context, false, callback);
        }
        await(serviceLatch, timeoutMillis, "连接或发现服务超时");
        if (operationError != null) throw operationError;
        negotiateMtu();
        enableNotification(chunkRead, true, 5_000L);
        authenticate(12_000L);
        applyLowPowerHeartSettings();
    }

    public int readBattery() throws Exception {
        requireReady();
        readLatch = new CountDownLatch(1);
        operationError = null;
        if (!gatt.readCharacteristic(batteryLevel)) throw new IllegalStateException("无法读取手环电量");
        await(readLatch, 5_000L, "读取电量超时");
        if (operationError != null) throw operationError;
        return battery;
    }

    public List<MiBandProtocol.ActivitySample> fetchActivity(Calendar since) throws Exception {
        requireReady();
        if (realtime) throw new IllegalStateException("实时心率开启时不能同步历史数据");
        enableNotification(fetchMetadata, true, 5_000L);
        enableNotification(fetchData, true, 5_000L);
        synchronized (fetchLock) {
            fetchCounter = -1;
            fetchStartMillis = 0L;
            fetchExpectedCrc = null;
            fetchCompletion = false;
            fetchedActivity = Collections.emptyList();
            fetchBuffer.reset();
            operationError = null;
            fetchLatch = new CountDownLatch(1);
            fetchGeneration++;
        }
        writeNoResponse(fetchMetadata, MiBandProtocol.buildActivityFetch(since));
        await(fetchLatch, 20_000L, "同步 activity 超时");
        if (operationError != null) throw operationError;
        return new ArrayList<>(fetchedActivity);
    }

    public void startRealtime() throws Exception {
        requireReady();
        if (realtime) return;
        enableNotification(heartRateMeasurement, true, 5_000L);
        writeChunked(MiBandProtocol.HEART_RATE_ENDPOINT,
                new byte[] {HEART_RATE_COMMAND, HEART_RATE_MODE_START}, false);
        realtime = true;
        mainHandler.removeCallbacks(realtimeKeepalive);
        mainHandler.postDelayed(realtimeKeepalive, 1_000L);
    }

    public void stopRealtime() throws Exception {
        mainHandler.removeCallbacks(realtimeKeepalive);
        if (!realtime) return;
        realtime = false;
        if (connected && authenticated) {
            writeChunked(MiBandProtocol.HEART_RATE_ENDPOINT,
                    new byte[] {HEART_RATE_COMMAND, HEART_RATE_MODE_STOP}, false);
        }
        if (connected && heartRateMeasurement != null) enableNotification(heartRateMeasurement, false, 5_000L);
    }

    public void vibrate(String pattern) throws Exception {
        requireReady();
        int pulses;
        long onMillis;
        long gapMillis;
        if ("single".equals(pattern)) {
            pulses = 1; onMillis = 350L; gapMillis = 0L;
        } else if ("call".equals(pattern)) {
            pulses = 3; onMillis = 250L; gapMillis = 180L;
        } else {
            throw new IllegalArgumentException("未知震动模式");
        }
        vibratePulses(pulses, onMillis, gapMillis);
    }

    public void sendNote(String pattern, String senderName, String note) throws Exception {
        requireReady();
        if (!"single".equals(pattern) && !"call".equals(pattern)) {
            throw new IllegalArgumentException("未知震动模式");
        }
        int notificationId = (senderName + "\u001f" + note).hashCode();
        writeChunked(
                MiBandProtocol.NOTIFICATION_ENDPOINT,
                MiBandProtocol.buildNotification(notificationId, senderName, note),
                true);
        if ("call".equals(pattern)) {
            Thread.sleep(350L);
            vibratePulses(2, 250L, 180L);
        }
    }

    private void vibratePulses(int pulses, long onMillis, long gapMillis) throws Exception {
        boolean active = false;
        try {
            for (int i = 0; i < pulses; i++) {
                writeChunked(MiBandProtocol.FIND_DEVICE_ENDPOINT, FIND_BAND_START, true);
                active = true;
                Thread.sleep(onMillis);
                writeChunked(MiBandProtocol.FIND_DEVICE_ENDPOINT, FIND_BAND_STOP, true);
                active = false;
                if (gapMillis > 0 && i + 1 < pulses) Thread.sleep(gapMillis);
            }
        } finally {
            if (active && connected) {
                try { writeChunked(MiBandProtocol.FIND_DEVICE_ENDPOINT, FIND_BAND_STOP, true); }
                catch (Exception ignored) {}
            }
        }
    }

    public void close() {
        mainHandler.removeCallbacks(realtimeKeepalive);
        realtime = false;
        authenticated = false;
        connected = false;
        BluetoothGatt old = gatt;
        gatt = null;
        if (old != null) {
            try { old.disconnect(); } catch (Exception ignored) {}
            try { old.close(); } catch (Exception ignored) {}
        }
        chunkWrite = chunkRead = fetchMetadata = fetchData = heartRateMeasurement = batteryLevel = null;
        CountDownLatch pendingWrite = characteristicWriteLatch;
        if (pendingWrite != null) pendingWrite.countDown();
        authKey = null;
    }

    private void authenticate(long timeoutMillis) throws Exception {
        MiBandCrypto.KeyPair pair = MiBandCrypto.generateKeyPair();
        authLatch = new CountDownLatch(1);
        operationError = null;
        byte[] command = new byte[52];
        command[0] = 0x04;
        command[1] = 0x02;
        command[2] = 0x00;
        command[3] = 0x02;
        System.arraycopy(pair.publicKey, 0, command, 4, pair.publicKey.length);
        pendingKeyPair = pair;
        writeChunked(MiBandProtocol.AUTH_ENDPOINT, command, false);
        await(authLatch, timeoutMillis, "手环鉴权超时");
        if (operationError != null) throw operationError;
        if (!authenticated) throw new IllegalStateException("手环鉴权失败，请检查密钥");
    }

    private volatile MiBandCrypto.KeyPair pendingKeyPair;

    private void applyLowPowerHeartSettings() throws Exception {
        byte[][] updates = {
                {0x05, 0x08, 0x02, 0x00, 0x01, 0x01, 0x10, 0x01},
                {0x05, 0x08, 0x02, 0x00, 0x01, 0x11, 0x0b, 0x00},
                {0x05, 0x08, 0x02, 0x00, 0x01, 0x12, 0x0b, 0x00},
                {0x05, 0x08, 0x02, 0x00, 0x01, 0x13, 0x0b, 0x00},
                {0x05, 0x08, 0x02, 0x00, 0x01, 0x31, 0x0b, 0x00},
        };
        for (byte[] update : updates) {
            configLatch = new CountDownLatch(1);
            operationError = null;
            writeChunked(MiBandProtocol.CONFIG_ENDPOINT, update, true);
            await(configLatch, 5_000L, "写入低功耗心率设置超时");
            if (operationError != null) throw operationError;
        }
    }

    private void writeChunked(int endpoint, byte[] payload, boolean encrypted) throws Exception {
        synchronized (writeLock) {
            writeHandle = (writeHandle + 1) & 0xff;
            List<byte[]> frames = encoder.encode(endpoint, payload, writeHandle, encrypted);
            for (byte[] frame : frames) writeNoResponse(chunkWrite, frame);
        }
    }

    private void writeNoResponse(BluetoothGattCharacteristic characteristic, byte[] value) throws Exception {
        BluetoothGatt current = gatt;
        if (current == null || characteristic == null) throw new IllegalStateException("BLE 通道未就绪");
        int writeType = MiBandGattWritePolicy.writeType(characteristic.getProperties());
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            int retries = 0;
            while (true) {
                characteristicWriteStatus = BluetoothGatt.GATT_SUCCESS;
                characteristicWriteLatch = new CountDownLatch(1);
                int result = current.writeCharacteristic(characteristic, value,
                        writeType);
                if (result == BluetoothGatt.GATT_SUCCESS) {
                    if (writeType == BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT) {
                        await(characteristicWriteLatch, 3_000L, "BLE 写入完成超时");
                        if (characteristicWriteStatus != BluetoothGatt.GATT_SUCCESS) {
                            throw new IllegalStateException("BLE 写入回调失败: " + characteristicWriteStatus);
                        }
                    }
                    break;
                }
                if (!MiBandGattWritePolicy.shouldRetry(result, retries)) {
                    throw new IllegalStateException("BLE 写入失败: " + result);
                }
                Thread.sleep(MiBandGattWritePolicy.retryDelayMillis(retries));
                retries++;
            }
        } else {
            characteristicWriteStatus = BluetoothGatt.GATT_SUCCESS;
            characteristicWriteLatch = new CountDownLatch(1);
            characteristic.setWriteType(writeType);
            characteristic.setValue(value);
            if (!current.writeCharacteristic(characteristic)) throw new IllegalStateException("BLE 写入启动失败");
            if (writeType == BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT) {
                await(characteristicWriteLatch, 3_000L, "BLE 写入完成超时");
                if (characteristicWriteStatus != BluetoothGatt.GATT_SUCCESS) {
                    throw new IllegalStateException("BLE 写入回调失败: " + characteristicWriteStatus);
                }
            }
        }
        Thread.sleep(MiBandGattWritePolicy.pacingDelayMillis());
    }

    private void negotiateMtu() throws Exception {
        BluetoothGatt current = gatt;
        if (current == null) throw new IllegalStateException("BLE 连接不存在");
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.LOLLIPOP) return;
        mtuLatch = new CountDownLatch(1);
        if (!current.requestMtu(247)) throw new IllegalStateException("无法请求手环 MTU");
        await(mtuLatch, 5_000L, "协商手环 MTU 超时");
        if (operationError != null) throw operationError;
    }

    private void enableNotification(BluetoothGattCharacteristic characteristic,
                                    boolean enabled, long timeoutMillis) throws Exception {
        if (characteristic == null || gatt == null) throw new IllegalStateException("通知通道不存在");
        if (!gatt.setCharacteristicNotification(characteristic, enabled)) {
            throw new IllegalStateException("无法切换 BLE 通知");
        }
        BluetoothGattDescriptor descriptor = characteristic.getDescriptor(CCCD);
        if (descriptor == null) return;
        descriptorLatch = new CountDownLatch(1);
        operationError = null;
        byte[] value = enabled
                ? BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE
                : BluetoothGattDescriptor.DISABLE_NOTIFICATION_VALUE;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            int result = gatt.writeDescriptor(descriptor, value);
            if (result != BluetoothGatt.GATT_SUCCESS) throw new IllegalStateException("无法写入通知描述符");
        } else {
            descriptor.setValue(value);
            if (!gatt.writeDescriptor(descriptor)) throw new IllegalStateException("无法写入通知描述符");
        }
        await(descriptorLatch, timeoutMillis, "订阅 BLE 通知超时");
        if (operationError != null) throw operationError;
    }

    private void requireReady() {
        if (!connected || !authenticated || gatt == null) throw new IllegalStateException("手环尚未连接并鉴权");
    }

    private static void await(CountDownLatch latch, long timeout, String message) throws Exception {
        if (latch == null || !latch.await(timeout, TimeUnit.MILLISECONDS)) throw new IllegalStateException(message);
    }

    private BluetoothGattCharacteristic find(UUID uuid) {
        if (gatt == null) return null;
        for (BluetoothGattService service : gatt.getServices()) {
            BluetoothGattCharacteristic value = service.getCharacteristic(uuid);
            if (value != null) return value;
        }
        return null;
    }

    private void requireChannels() {
        chunkWrite = find(UUID.fromString(MiBandProtocol.CHUNKED_WRITE_UUID));
        chunkRead = find(UUID.fromString(MiBandProtocol.CHUNKED_READ_UUID));
        fetchMetadata = find(UUID.fromString(MiBandProtocol.FETCH_METADATA_UUID));
        fetchData = find(UUID.fromString(MiBandProtocol.FETCH_DATA_UUID));
        heartRateMeasurement = find(UUID.fromString(MiBandProtocol.HEART_RATE_MEASUREMENT_UUID));
        batteryLevel = find(UUID.fromString(MiBandProtocol.BATTERY_LEVEL_UUID));
        if (chunkWrite == null || chunkRead == null || fetchMetadata == null || fetchData == null
                || heartRateMeasurement == null || batteryLevel == null) {
            throw new IllegalStateException("手环缺少必要的 BLE 服务");
        }
    }

    private void handleChunk(byte[] frame) {
        try {
            MiBandProtocol.DecodedMessage message = decoder.feed(frame);
            if (message == null) return;
            if (message.needsAck) writeNoResponse(chunkRead, message.ack);
            if (message.endpoint == MiBandProtocol.AUTH_ENDPOINT) handleAuth(message.payload);
            else if (message.endpoint == MiBandProtocol.CONFIG_ENDPOINT) handleConfig(message.payload);
        } catch (Exception error) {
            operationError = error;
            if (authLatch != null) authLatch.countDown();
            if (configLatch != null) configLatch.countDown();
        }
    }

    private void handleAuth(byte[] payload) throws Exception {
        if (payload.length < 3 || payload[0] != 0x10) return;
        int command = payload[1] & 0xff;
        int status = payload[2] & 0xff;
        if (command == 0x04 && status == 0x01) {
            if (payload.length < 67 || pendingKeyPair == null || authKey == null) {
                throw new IllegalArgumentException("无效的手环鉴权挑战");
            }
            MiBandCrypto.AuthResponse response = MiBandCrypto.buildAuthResponse(
                    authKey, pendingKeyPair,
                    Arrays.copyOfRange(payload, 3, 19),
                    Arrays.copyOfRange(payload, 19, 67));
            encoder.setEncryptionParameters(response.encryptedSequence, response.sessionKey);
            decoder.setEncryptionParameters(response.sessionKey);
            writeChunked(MiBandProtocol.AUTH_ENDPOINT, response.command, false);
        } else if (command == 0x05) {
            authenticated = status == 0x01;
            if (!authenticated) operationError = new IllegalStateException("手环鉴权失败，请检查密钥");
            pendingKeyPair = null;
            if (authLatch != null) authLatch.countDown();
        }
    }

    private void handleConfig(byte[] payload) {
        if (payload.length >= 2 && payload[0] == 0x06) {
            if (payload[1] != 0x00 && payload[1] != 0x01) {
                operationError = new IllegalStateException("手环拒绝健康设置: " + (payload[1] & 0xff));
            }
            if (configLatch != null) configLatch.countDown();
        }
    }

    private void handleFetchData(byte[] payload) {
        synchronized (fetchLock) {
            if (fetchLatch == null || payload.length == 0) return;
            int counter = payload[0] & 0xff;
            int expected = (fetchCounter + 1) & 0xff;
            if (counter != expected) {
                operationError = new IllegalStateException("activity 包序号错误");
                fetchLatch.countDown();
                return;
            }
            fetchCounter = counter;
            fetchBuffer.write(payload, 1, payload.length - 1);
            if (fetchCompletion) scheduleFetchFinish();
        }
    }

    private void handleFetchMetadata(byte[] payload) {
        synchronized (fetchLock) {
            if (fetchLatch == null || payload.length < 3 || payload[0] != 0x10) return;
            try {
                int command = payload[1] & 0xff;
                int status = payload[2] & 0xff;
                if (status != 0x01) throw new IllegalStateException("手环拒绝 activity 请求");
                if (command == 0x01) {
                    if (payload.length != 15 && payload.length != 16) throw new IllegalArgumentException("无效 activity 元信息");
                    long expected = readU32(payload, 3);
                    fetchStartMillis = parseZeppTime(payload, 7);
                    if (expected > 0) writeNoResponse(fetchMetadata, new byte[] {0x02});
                    else {
                        fetchedActivity = Collections.emptyList();
                        writeNoResponse(fetchMetadata, new byte[] {0x03, 0x09});
                    }
                } else if (command == 0x02) {
                    fetchCompletion = true;
                    fetchExpectedCrc = payload.length >= 7 ? readU32(payload, 3) : null;
                    scheduleFetchFinish();
                } else if (command == 0x03) {
                    fetchLatch.countDown();
                }
            } catch (Exception error) {
                operationError = error;
                fetchLatch.countDown();
            }
        }
    }

    private void scheduleFetchFinish() {
        final int generation = ++fetchGeneration;
        mainHandler.postDelayed(() -> {
            synchronized (fetchLock) {
                if (generation != fetchGeneration || fetchLatch == null || fetchLatch.getCount() == 0) return;
                try {
                    byte[] raw = fetchBuffer.toByteArray();
                    if (fetchExpectedCrc != null) {
                        CRC32 crc = new CRC32();
                        crc.update(raw);
                        if (crc.getValue() != fetchExpectedCrc) throw new IllegalArgumentException("activity CRC 校验失败");
                    }
                    fetchedActivity = MiBandProtocol.parseActivity(fetchStartMillis, raw);
                    writeNoResponse(fetchMetadata, new byte[] {0x03, 0x09});
                } catch (Exception error) {
                    operationError = error;
                    fetchLatch.countDown();
                }
            }
        }, 80L);
    }

    private static long parseZeppTime(byte[] data, int offset) {
        int year = (data[offset] & 0xff) | ((data[offset + 1] & 0xff) << 8);
        int quarterHours = data[offset + 7];
        TimeZone zone = TimeZone.getTimeZone(String.format("GMT%+03d:%02d",
                quarterHours / 4, Math.abs(quarterHours % 4) * 15));
        Calendar value = Calendar.getInstance(zone);
        value.clear();
        value.set(year, (data[offset + 2] & 0xff) - 1, data[offset + 3] & 0xff,
                data[offset + 4] & 0xff, data[offset + 5] & 0xff, data[offset + 6] & 0xff);
        return value.getTimeInMillis();
    }

    private static long readU32(byte[] value, int offset) {
        return (long) (value[offset] & 0xff)
                | ((long) (value[offset + 1] & 0xff) << 8)
                | ((long) (value[offset + 2] & 0xff) << 16)
                | ((long) (value[offset + 3] & 0xff) << 24);
    }

    private final BluetoothGattCallback callback = new BluetoothGattCallback() {
        @Override public void onConnectionStateChange(BluetoothGatt current, int status, int newState) {
            if (current != gatt) return;
            if (status == BluetoothGatt.GATT_SUCCESS && newState == BluetoothProfile.STATE_CONNECTED) {
                connected = true;
                if (!current.discoverServices()) {
                    operationError = new IllegalStateException("无法发现手环服务");
                    serviceLatch.countDown();
                }
            } else if (newState == BluetoothProfile.STATE_DISCONNECTED) {
                boolean wasConnected = connected;
                connected = false;
                authenticated = false;
                realtime = false;
                mainHandler.removeCallbacks(realtimeKeepalive);
                if (serviceLatch != null) serviceLatch.countDown();
                if (authLatch != null) authLatch.countDown();
                if (wasConnected && listener != null) listener.onDisconnected("蓝牙连接已断开");
            }
        }

        @Override public void onServicesDiscovered(BluetoothGatt current, int status) {
            if (current != gatt) return;
            try {
                if (status != BluetoothGatt.GATT_SUCCESS) throw new IllegalStateException("发现手环服务失败: " + status);
                requireChannels();
            } catch (Exception error) {
                operationError = error;
            }
            if (serviceLatch != null) serviceLatch.countDown();
        }

        @Override public void onDescriptorWrite(BluetoothGatt current, BluetoothGattDescriptor descriptor, int status) {
            if (current != gatt) return;
            if (status != BluetoothGatt.GATT_SUCCESS) operationError = new IllegalStateException("订阅通知失败: " + status);
            if (descriptorLatch != null) descriptorLatch.countDown();
        }

        @Override public void onMtuChanged(BluetoothGatt current, int mtu, int status) {
            if (current != gatt) return;
            if (status == BluetoothGatt.GATT_SUCCESS && mtu >= 70) {
                encoder = new MiBandProtocol.ChunkEncoder(mtu);
            } else {
                operationError = new IllegalStateException("手环 MTU 过小: " + mtu);
            }
            if (mtuLatch != null) mtuLatch.countDown();
        }

        @Override public void onCharacteristicRead(BluetoothGatt current, BluetoothGattCharacteristic characteristic,
                                                   byte[] value, int status) {
            if (current != gatt) return;
            if (status == BluetoothGatt.GATT_SUCCESS && value != null && value.length == 1) battery = value[0] & 0xff;
            else operationError = new IllegalStateException("读取手环电量失败: " + status);
            if (readLatch != null) readLatch.countDown();
        }

        @Override public void onCharacteristicWrite(BluetoothGatt current,
                                                    BluetoothGattCharacteristic characteristic, int status) {
            if (current != gatt) return;
            characteristicWriteStatus = status;
            CountDownLatch pending = characteristicWriteLatch;
            if (pending != null) pending.countDown();
        }

        @SuppressWarnings("deprecation")
        @Override public void onCharacteristicRead(BluetoothGatt current, BluetoothGattCharacteristic characteristic, int status) {
            onCharacteristicRead(current, characteristic, characteristic.getValue(), status);
        }

        @Override public void onCharacteristicChanged(BluetoothGatt current,
                                                       BluetoothGattCharacteristic characteristic, byte[] value) {
            if (current != gatt || value == null) return;
            UUID uuid = characteristic.getUuid();
            if (uuid.equals(UUID.fromString(MiBandProtocol.CHUNKED_READ_UUID))) handleChunk(value);
            else if (uuid.equals(UUID.fromString(MiBandProtocol.FETCH_DATA_UUID))) handleFetchData(value);
            else if (uuid.equals(UUID.fromString(MiBandProtocol.FETCH_METADATA_UUID))) handleFetchMetadata(value);
            else if (uuid.equals(UUID.fromString(MiBandProtocol.HEART_RATE_MEASUREMENT_UUID))) {
                try {
                    int bpm = MiBandProtocol.parseHeartRate(value);
                    if (bpm >= 20 && bpm <= 240 && listener != null) listener.onHeartRate(bpm, System.currentTimeMillis());
                } catch (IllegalArgumentException ignored) {}
            }
        }

        @SuppressWarnings("deprecation")
        @Override public void onCharacteristicChanged(BluetoothGatt current, BluetoothGattCharacteristic characteristic) {
            onCharacteristicChanged(current, characteristic, characteristic.getValue());
        }
    };
}
