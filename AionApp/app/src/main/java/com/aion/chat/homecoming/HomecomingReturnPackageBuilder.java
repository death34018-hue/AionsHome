package com.aion.chat.homecoming;

import org.bouncycastle.util.encoders.Base64;
import org.json.JSONArray;
import org.json.JSONObject;
import org.json.JSONTokener;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Comparator;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.zip.GZIPInputStream;

public final class HomecomingReturnPackageBuilder {
    private static final int SCHEMA = 1;
    private final OperationSource operations;
    private final SnapshotSource snapshots;
    private final PackagePort packages;
    private final Signer signer;
    private final String deviceId;

    public HomecomingReturnPackageBuilder(
            OperationSource operations,
            SnapshotSource snapshots,
            PackagePort packages,
            Signer signer,
            String deviceId) {
        this.operations = required(operations, "operations");
        this.snapshots = required(snapshots, "snapshots");
        this.packages = required(packages, "packages");
        this.signer = required(signer, "signer");
        this.deviceId = required(deviceId, "deviceId");
    }

    public HomecomingReturnPackageRepository.ReturnPackage freezeAndBuild(
            String epochId, long now) throws Exception {
        String epoch = required(epochId, "epochId");
        List<HomecomingOperationJournal.Operation> source =
                new ArrayList<>(operations.load(epoch));
        if (source.isEmpty()) {
            throw new IllegalStateException("return package has no operations");
        }
        Collections.sort(source, Comparator.comparingLong(value -> value.deviceSeq));
        validate(source, epoch);

        long highest = source.get(source.size() - 1).deviceSeq;
        HomecomingReturnPackageRepository.ReturnPackage reusable =
                packages.reusable(epoch, highest);
        if (reusable != null) return reusable;

        String snapshotId = required(snapshots.baseSnapshotId(epoch), "baseSnapshotId");
        JSONObject payload = payload(epoch, snapshotId, source, now);
        byte[] canonical = HomecomingSnapshotStore.canonicalJson(payload)
                .getBytes(StandardCharsets.UTF_8);
        String hash = HomecomingSnapshotStore.sha256Hex(canonical);
        String packageId = "return-" + hash;
        byte[] signature = signer.sign(signingText(epoch, hash));
        String signatureBase64 = Base64.toBase64String(signature);
        JSONObject envelope = new JSONObject()
                .put("package_id", packageId)
                .put("payload", payload)
                .put("payload_sha256", hash)
                .put("signature_algorithm",
                        HomecomingKeyStore.ReturnSigningCodec.ALGORITHM_LABEL)
                .put("signature_b64", signatureBase64);
        byte[] compressed = HomecomingSnapshotStore.gzip(
                HomecomingSnapshotStore.canonicalJson(envelope)
                        .getBytes(StandardCharsets.UTF_8));
        HomecomingReturnPackageRepository.ReturnPackage value =
                new HomecomingReturnPackageRepository.ReturnPackage(
                        packageId, epoch, deviceId, snapshotId,
                        source.get(0).deviceSeq, highest, source.size(),
                        hash, signatureBase64, compressed, "", "ready", now, now);
        return packages.save(value);
    }

    static JSONObject readEnvelope(byte[] compressed) throws Exception {
        GZIPInputStream gzip = new GZIPInputStream(
                new ByteArrayInputStream(compressed));
        ByteArrayOutputStream output = new ByteArrayOutputStream();
        byte[] buffer = new byte[8192];
        int count;
        while ((count = gzip.read(buffer)) != -1) output.write(buffer, 0, count);
        gzip.close();
        Object parsed = new JSONTokener(
                new String(output.toByteArray(), StandardCharsets.UTF_8)).nextValue();
        if (!(parsed instanceof JSONObject)) {
            throw new IllegalArgumentException("return envelope must be an object");
        }
        return (JSONObject) parsed;
    }

    private JSONObject payload(String epochId, String snapshotId,
            List<HomecomingOperationJournal.Operation> source, long now) throws Exception {
        JSONArray encoded = new JSONArray();
        Map<String, Integer> counts = new HashMap<>();
        for (HomecomingOperationJournal.Operation operation : source) {
            JSONObject operationPayload = parsePayload(operation.payloadJson);
            encoded.put(new JSONObject()
                    .put("op_id", operation.opId)
                    .put("device_seq", operation.deviceSeq)
                    .put("entity_type", operation.entityType)
                    .put("entity_id", operation.entityId)
                    .put("action", operation.action)
                    .put("base_revision", operation.baseRevision)
                    .put("payload", operationPayload)
                    .put("created_at", operation.createdAt));
            Integer current = counts.get(operation.entityType);
            counts.put(operation.entityType, current == null ? 1 : current + 1);
        }
        JSONObject sectionCounts = new JSONObject();
        for (Map.Entry<String, Integer> entry : counts.entrySet()) {
            sectionCounts.put(entry.getKey(), entry.getValue());
        }
        return new JSONObject()
                .put("schema", SCHEMA)
                .put("device_id", deviceId)
                .put("epoch_id", epochId)
                .put("base_snapshot_id", snapshotId)
                .put("created_at", now)
                .put("first_device_seq", source.get(0).deviceSeq)
                .put("highest_device_seq", source.get(source.size() - 1).deviceSeq)
                .put("operation_count", source.size())
                .put("operations", encoded)
                .put("section_counts", sectionCounts);
    }

    private void validate(List<HomecomingOperationJournal.Operation> source, String epochId) {
        long expected = source.get(0).deviceSeq;
        if (expected <= 0) throw new IllegalStateException("device sequence must be positive");
        for (HomecomingOperationJournal.Operation operation : source) {
            if (!epochId.equals(operation.epochId)
                    || !deviceId.equals(operation.deviceId)) {
                throw new IllegalArgumentException("operation binding mismatch");
            }
            if (operation.deviceSeq != expected++) {
                throw new IllegalStateException("device sequence is not contiguous");
            }
            if (!allowed(operation.entityType, operation.action)) {
                throw new IllegalArgumentException("unsupported return operation");
            }
            parsePayload(operation.payloadJson);
        }
    }

    private static boolean allowed(String entity, String action) {
        if ("message".equals(entity)) return "create".equals(action);
        if ("memory".equals(entity)) {
            return "create".equals(action) || "update".equals(action)
                    || "delete".equals(action);
        }
        if ("memory_auto".equals(entity)) return "create".equals(action);
        if ("schedule".equals(entity)) {
            return "create".equals(action) || "delete".equals(action)
                    || "execute".equals(action);
        }
        if ("supervision_event".equals(entity)) return "execute".equals(action);
        return "deferred_control".equals(entity) && "create".equals(action);
    }

    private static JSONObject parsePayload(String raw) {
        try {
            Object value = new JSONTokener(raw).nextValue();
            if (!(value instanceof JSONObject)) {
                throw new IllegalArgumentException("operation payload must be an object");
            }
            return (JSONObject) value;
        } catch (IllegalArgumentException exception) {
            throw exception;
        } catch (Exception exception) {
            throw new IllegalArgumentException("operation payload must be valid JSON", exception);
        }
    }

    private byte[] signingText(String epochId, String hash) {
        return ("schema=" + SCHEMA + "\n"
                + "device_id=" + deviceId + "\n"
                + "epoch_id=" + epochId + "\n"
                + "payload_sha256=" + hash + "\n")
                .getBytes(StandardCharsets.UTF_8);
    }

    private static String required(String value, String label) {
        if (value == null || value.trim().isEmpty()) {
            throw new IllegalArgumentException(label + " is required");
        }
        return value.trim();
    }

    private static <T> T required(T value, String label) {
        if (value == null) throw new IllegalArgumentException(label + " is required");
        return value;
    }

    public interface OperationSource {
        List<HomecomingOperationJournal.Operation> load(String epochId) throws Exception;
    }

    public interface SnapshotSource {
        String baseSnapshotId(String epochId) throws Exception;
    }

    public interface PackagePort {
        HomecomingReturnPackageRepository.ReturnPackage reusable(
                String epochId, long highestSequence) throws Exception;
        HomecomingReturnPackageRepository.ReturnPackage save(
                HomecomingReturnPackageRepository.ReturnPackage value) throws Exception;
    }

    public interface Signer {
        byte[] sign(byte[] bytes) throws Exception;
    }
}
