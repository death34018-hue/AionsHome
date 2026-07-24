package com.aion.chat.miband;

import org.junit.Test;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

public class MiBandScanResultTest {
    @Test
    public void mergeKeepsAddressAndUsesLatestUsefulValues() {
        MiBandScanResult first = new MiBandScanResult("aa:bb:cc:dd:ee:ff", "", -82);
        MiBandScanResult merged = first.merge(new MiBandScanResult(
                "AA:BB:CC:DD:EE:FF", "Xiaomi Smart Band 7", -46));

        assertEquals("AA:BB:CC:DD:EE:FF", merged.address);
        assertEquals("Xiaomi Smart Band 7", merged.name);
        assertEquals(-46, merged.rssi);
    }

    @Test
    public void ranksLikelyBandsBeforeUnknownDevicesThenBySignal() {
        List<MiBandScanResult> values = new ArrayList<>(Arrays.asList(
                new MiBandScanResult("00:00:00:00:00:01", "Headphones", -20),
                new MiBandScanResult("00:00:00:00:00:02", "Mi Smart Band 7", -70),
                new MiBandScanResult("00:00:00:00:00:03", "Xiaomi Band", -40),
                new MiBandScanResult("00:00:00:00:00:04", "", -10)));

        values.sort(MiBandScanResult.rankComparator());

        assertEquals("Xiaomi Band", values.get(0).name);
        assertEquals("Mi Smart Band 7", values.get(1).name);
        assertEquals("", values.get(2).name);
        assertEquals("Headphones", values.get(3).name);
    }

    @Test
    public void filtersOrdinaryBluetoothDevicesButKeepsBandSignals() {
        assertFalse(MiBandScanResult.isLikelyBand("Living Room TV", false, false));
        assertFalse(MiBandScanResult.isLikelyBand("Wireless Headphones", false, false));
        assertTrue(MiBandScanResult.isLikelyBand("Xiaomi Smart Band 7", false, false));
        assertTrue(MiBandScanResult.isLikelyBand("", true, false));
        assertTrue(MiBandScanResult.isLikelyBand("", false, true));
    }
}
