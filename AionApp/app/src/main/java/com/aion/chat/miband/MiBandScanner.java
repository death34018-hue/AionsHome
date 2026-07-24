package com.aion.chat.miband;

import android.bluetooth.BluetoothAdapter;
import android.bluetooth.BluetoothManager;
import android.bluetooth.le.BluetoothLeScanner;
import android.bluetooth.le.ScanCallback;
import android.bluetooth.le.ScanRecord;
import android.bluetooth.le.ScanResult;
import android.content.Context;
import android.os.Handler;
import android.os.Looper;
import android.os.ParcelUuid;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

public final class MiBandScanner {
    public interface Listener {
        void onScanState(boolean scanning, String message);
        void onScanResults(List<MiBandScanResult> results);
    }

    private final BluetoothAdapter adapter;
    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    private final Listener listener;
    private final String savedAddress;
    private final Map<String, MiBandScanResult> devices = new LinkedHashMap<>();
    private BluetoothLeScanner scanner;
    private boolean scanning;

    public MiBandScanner(Context context, Listener listener) {
        BluetoothManager manager = (BluetoothManager) context.getSystemService(Context.BLUETOOTH_SERVICE);
        adapter = manager == null ? null : manager.getAdapter();
        this.listener = listener;
        savedAddress = context.getSharedPreferences(MiBandPreferences.PREFS_NAME, Context.MODE_PRIVATE)
                .getString(MiBandPreferences.KEY_ADDRESS, "");
    }

    public void start() {
        mainHandler.post(() -> {
            if (adapter == null) {
                listener.onScanState(false, "此手机不支持蓝牙低功耗");
                return;
            }
            if (!adapter.isEnabled()) {
                listener.onScanState(false, "请先开启手机蓝牙");
                return;
            }
            stopInternal(false);
            devices.clear();
            scanner = adapter.getBluetoothLeScanner();
            if (scanner == null) {
                listener.onScanState(false, "蓝牙扫描器暂不可用");
                return;
            }
            try {
                scanning = true;
                scanner.startScan(callback);
                listener.onScanState(true, "正在扫描附近手环…");
                mainHandler.postDelayed(() -> stopInternal(true), 8_000L);
            } catch (SecurityException error) {
                scanning = false;
                listener.onScanState(false, "请允许附近设备权限后重试");
            }
        });
    }

    public void stop() { mainHandler.post(() -> stopInternal(true)); }

    private final ScanCallback callback = new ScanCallback() {
        @Override public void onScanResult(int callbackType, ScanResult result) {
            if (result == null || result.getDevice() == null) return;
            try {
                String address = result.getDevice().getAddress();
                if (address == null || address.isEmpty()) return;
                ScanRecord record = result.getScanRecord();
                String name = record == null ? null : record.getDeviceName();
                if (name == null || name.trim().isEmpty()) name = result.getDevice().getName();
                boolean savedDevice = savedAddress != null && savedAddress.equalsIgnoreCase(address);
                boolean knownService = advertisesMiBandService(record);
                if (!MiBandScanResult.isLikelyBand(name, knownService, savedDevice)) return;
                MiBandScanResult latest = new MiBandScanResult(address, name, result.getRssi());
                MiBandScanResult existing = devices.get(latest.address);
                devices.put(latest.address, existing == null ? latest : existing.merge(latest));
                publishResults();
            } catch (SecurityException error) {
                stopInternal(false);
                listener.onScanState(false, "附近设备权限已失效，请重新授权");
            }
        }

        @Override public void onScanFailed(int errorCode) {
            scanning = false;
            listener.onScanState(false, "扫描失败（" + errorCode + "），请稍后重试");
        }
    };

    private static boolean advertisesMiBandService(ScanRecord record) {
        if (record == null || record.getServiceUuids() == null) return false;
        for (ParcelUuid service : record.getServiceUuids()) {
            String value = service == null ? "" : service.toString().toLowerCase(java.util.Locale.US);
            if (value.startsWith("0000fee0-") || value.startsWith("0000fee1-")
                    || value.startsWith("0000fe95-") || value.contains("3512-2118-0009af100700")) {
                return true;
            }
        }
        return false;
    }

    private void publishResults() {
        List<MiBandScanResult> values = new ArrayList<>(devices.values());
        values.sort(MiBandScanResult.rankComparator());
        listener.onScanResults(values);
    }

    private void stopInternal(boolean notify) {
        mainHandler.removeCallbacksAndMessages(null);
        if (scanning && scanner != null) {
            try { scanner.stopScan(callback); } catch (SecurityException ignored) {}
        }
        scanning = false;
        publishResults();
        if (notify) {
            listener.onScanState(false, devices.isEmpty() ? "没有发现设备，请让手环靠近手机后重试" : "扫描完成，请选择手环");
        }
    }
}
