package com.aion.chat.homecoming;

import org.json.JSONObject;
import org.junit.Test;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.List;

import static org.junit.Assert.assertArrayEquals;
import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertThrows;
import static org.junit.Assert.assertTrue;

public class HomecomingReturnPackageBuilderTest {
    @Test
    public void sameFrozenOperationsReuseStoredImmutableEnvelope() throws Exception {
        MemoryPackages packages = new MemoryPackages();
        HomecomingReturnPackageBuilder builder = builder(
                packages,
                Arrays.asList(
                        operation("op-one", 7L, "message", "create", "{\"text\":\"一\"}"),
                        operation("op-two", 8L, "memory", "update", "{\"content\":\"二\"}")));

        HomecomingReturnPackageRepository.ReturnPackage first =
                builder.freezeAndBuild("epoch-one", 1_000L);
        HomecomingReturnPackageRepository.ReturnPackage second =
                builder.freezeAndBuild("epoch-one", 2_000L);

        assertEquals(first.packageId, second.packageId);
        assertArrayEquals(first.compressedEnvelope, second.compressedEnvelope);
        assertEquals(1, packages.saved.size());
        assertEquals(7L, first.firstDeviceSeq);
        assertEquals(8L, first.highestDeviceSeq);
        assertEquals(2, first.operationCount);
    }

    @Test
    public void packageHashCoversCanonicalPayloadButNotSignature() throws Exception {
        MemoryPackages packages = new MemoryPackages();
        HomecomingReturnPackageBuilder builder = builder(
                packages,
                Collections.singletonList(
                        operation("op-one", 1L, "message", "create", "{\"text\":\"内容\"}")));

        HomecomingReturnPackageRepository.ReturnPackage value =
                builder.freezeAndBuild("epoch-one", 1_000L);
        JSONObject envelope = HomecomingReturnPackageBuilder.readEnvelope(
                value.compressedEnvelope);
        byte[] canonical = HomecomingSnapshotStore.canonicalJson(
                envelope.getJSONObject("payload")).getBytes(StandardCharsets.UTF_8);
        String expectedHash = hex(MessageDigest.getInstance("SHA-256").digest(canonical));

        assertEquals(expectedHash, envelope.getString("payload_sha256"));
        assertEquals("SHA256withRSA/PSS",
                envelope.getString("signature_algorithm"));
        assertTrue(envelope.getString("signature_b64").length() > 10);
    }

    @Test
    public void rejectsSequenceGapsAndUnknownOperationKinds() {
        MemoryPackages packages = new MemoryPackages();
        assertThrows(IllegalStateException.class, () -> builder(
                packages,
                Arrays.asList(
                        operation("op-one", 1L, "message", "create", "{}"),
                        operation("op-three", 3L, "message", "create", "{}")))
                .freezeAndBuild("epoch-one", 1_000L));
        assertThrows(IllegalArgumentException.class, () -> builder(
                packages,
                Collections.singletonList(
                        operation("op-one", 1L, "camera_frame", "create", "{}")))
                .freezeAndBuild("epoch-one", 1_000L));
    }

    private static HomecomingReturnPackageBuilder builder(
            MemoryPackages packages,
            List<HomecomingOperationJournal.Operation> operations) {
        return new HomecomingReturnPackageBuilder(
                epoch -> operations,
                epoch -> "snapshot-one",
                packages,
                bytes -> ("signed:" + new String(bytes, StandardCharsets.UTF_8))
                        .getBytes(StandardCharsets.UTF_8),
                "android:test-device");
    }

    private static HomecomingOperationJournal.Operation operation(
            String id, long sequence, String entity, String action, String payload) {
        return new HomecomingOperationJournal.Operation(
                id, "epoch-one", "android:test-device", sequence,
                entity, id, action, "", payload, 100L + sequence);
    }

    private static String hex(byte[] value) {
        StringBuilder result = new StringBuilder();
        for (byte item : value) result.append(String.format("%02x", item & 0xff));
        return result.toString();
    }

    private static final class MemoryPackages
            implements HomecomingReturnPackageBuilder.PackagePort {
        final List<HomecomingReturnPackageRepository.ReturnPackage> saved =
                new ArrayList<>();

        @Override public HomecomingReturnPackageRepository.ReturnPackage reusable(
                String epochId, long highestSequence) {
            for (HomecomingReturnPackageRepository.ReturnPackage value : saved) {
                if (value.epochId.equals(epochId)
                        && value.highestDeviceSeq == highestSequence) {
                    return value;
                }
            }
            return null;
        }

        @Override public HomecomingReturnPackageRepository.ReturnPackage save(
                HomecomingReturnPackageRepository.ReturnPackage value) {
            saved.add(value);
            return value;
        }
    }
}
