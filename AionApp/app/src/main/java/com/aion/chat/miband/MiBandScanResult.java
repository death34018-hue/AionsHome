package com.aion.chat.miband;

import java.util.Comparator;
import java.util.Locale;

public final class MiBandScanResult {
    public final String address;
    public final String name;
    public final int rssi;

    public MiBandScanResult(String address, String name, int rssi) {
        this.address = address == null ? "" : address.trim().toUpperCase(Locale.US);
        this.name = name == null ? "" : name.trim();
        this.rssi = rssi;
    }

    public MiBandScanResult merge(MiBandScanResult latest) {
        if (latest == null || !address.equalsIgnoreCase(latest.address)) return this;
        String mergedName = latest.name.isEmpty() ? name : latest.name;
        return new MiBandScanResult(address, mergedName, latest.rssi);
    }

    public static boolean isLikelyBand(String name, boolean knownService, boolean savedDevice) {
        if (knownService || savedDevice) return true;
        String lower = name == null ? "" : name.trim().toLowerCase(Locale.US);
        return lower.contains("xiaomi")
                || lower.contains("mi band")
                || lower.contains("miband")
                || lower.contains("smart band")
                || lower.contains("huami band");
    }

    public static Comparator<MiBandScanResult> rankComparator() {
        return Comparator.comparingInt(MiBandScanResult::rank)
                .thenComparing(Comparator.comparingInt((MiBandScanResult value) -> value.rssi).reversed())
                .thenComparing(value -> value.address);
    }

    private int rank() {
        String lower = name.toLowerCase(Locale.US);
        if (lower.contains("band") || lower.contains("xiaomi") || lower.contains("huami")
                || lower.contains("amazfit")) return 0;
        if (name.isEmpty()) return 1;
        return 2;
    }
}
