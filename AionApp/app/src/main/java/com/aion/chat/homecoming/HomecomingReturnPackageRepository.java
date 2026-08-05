package com.aion.chat.homecoming;

import android.content.ContentValues;
import android.content.Context;
import android.database.Cursor;
import android.database.sqlite.SQLiteDatabase;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

public final class HomecomingReturnPackageRepository
        implements HomecomingReturnPackageBuilder.PackagePort {
    private final HomecomingDatabase helper;
    private final File root;

    public HomecomingReturnPackageRepository(Context context) {
        this(new HomecomingDatabase(context),
                new File(context.getFilesDir(), "homecoming-returns"));
    }

    HomecomingReturnPackageRepository(HomecomingDatabase helper, File root) {
        this.helper = helper;
        this.root = root;
    }

    @Override
    public synchronized ReturnPackage reusable(String epochId, long highestSequence)
            throws IOException {
        try (Cursor cursor = helper.getReadableDatabase().rawQuery(
                "SELECT package_id,epoch_id,device_id,base_snapshot_id,"
                        + "first_device_seq,highest_device_seq,operation_count,"
                        + "payload_sha256,signature_b64,envelope_path,state,"
                        + "created_at,updated_at FROM return_package "
                        + "WHERE epoch_id=? AND highest_device_seq=? "
                        + "AND state NOT IN ('failed','archived') "
                        + "ORDER BY created_at DESC LIMIT 1",
                new String[]{epochId, Long.toString(highestSequence)})) {
            return cursor.moveToFirst() ? fromCursor(cursor) : null;
        }
    }

    @Override
    public synchronized ReturnPackage save(ReturnPackage value) throws IOException {
        File target = persistEnvelope(value.payloadSha256, value.compressedEnvelope);
        SQLiteDatabase database = helper.getWritableDatabase();
        ContentValues row = values(value, target.getAbsolutePath());
        database.insertOrThrow("return_package", null, row);
        return value.withEnvelopePath(target.getAbsolutePath());
    }

    public synchronized List<ReturnPackage> pendingInSequence() throws IOException {
        ArrayList<ReturnPackage> result = new ArrayList<>();
        try (Cursor cursor = helper.getReadableDatabase().rawQuery(
                "SELECT package_id,epoch_id,device_id,base_snapshot_id,"
                        + "first_device_seq,highest_device_seq,operation_count,"
                        + "payload_sha256,signature_b64,envelope_path,state,"
                        + "created_at,updated_at FROM return_package "
                        + "WHERE state NOT IN ('confirmed','archived') "
                        + "ORDER BY first_device_seq,created_at", null)) {
            while (cursor.moveToNext()) result.add(fromCursor(cursor));
        }
        return Collections.unmodifiableList(result);
    }

    public synchronized void confirm(
            String packageId, Receipt receipt, long now) {
        if (receipt == null || !packageId.equals(receipt.packageId)) {
            throw new IllegalArgumentException("receipt package mismatch");
        }
        SQLiteDatabase database = helper.getWritableDatabase();
        String epochId;
        String deviceId;
        try (Cursor cursor = database.rawQuery(
                "SELECT highest_device_seq,epoch_id,device_id "
                        + "FROM return_package WHERE package_id=?",
                new String[]{packageId})) {
            if (!cursor.moveToFirst()
                    || cursor.getLong(0) != receipt.acceptedHighestDeviceSeq) {
                throw new IllegalArgumentException("receipt sequence mismatch");
            }
            epochId = cursor.getString(1);
            deviceId = cursor.getString(2);
        }
        ContentValues packageUpdate = new ContentValues();
        packageUpdate.put("state", "confirmed");
        packageUpdate.put("server_import_id", receipt.importSessionId);
        packageUpdate.put("result_summary_sha256", receipt.resultSummarySha256);
        packageUpdate.put("updated_at", now);
        if (database.update("return_package", packageUpdate,
                "package_id=?", new String[]{packageId}) != 1) {
            throw new IllegalStateException("return package is unavailable");
        }
        ContentValues operations = new ContentValues();
        operations.put("status", "confirmed");
        operations.put("server_receipt", receipt.importSessionId);
        database.update("operation_journal", operations,
                "epoch_id=? AND device_id=? AND device_seq<=? AND status='pending'",
                new String[]{epochId, deviceId,
                        Long.toString(receipt.acceptedHighestDeviceSeq)});
    }

    private ReturnPackage fromCursor(Cursor cursor) throws IOException {
        String path = cursor.getString(9);
        return new ReturnPackage(
                cursor.getString(0), cursor.getString(1), cursor.getString(2),
                cursor.getString(3), cursor.getLong(4), cursor.getLong(5),
                cursor.getInt(6), cursor.getString(7), cursor.getString(8),
                read(new File(path)), path, cursor.getString(10),
                cursor.getLong(11), cursor.getLong(12));
    }

    private File persistEnvelope(String hash, byte[] envelope) throws IOException {
        if (!root.isDirectory() && !root.mkdirs()) {
            throw new IOException("could not create return package directory");
        }
        File target = new File(root, hash + ".json.gz");
        if (target.isFile()) {
            byte[] existing = read(target);
            if (!java.security.MessageDigest.isEqual(existing, envelope)) {
                throw new IOException("return package hash collision");
            }
            return target;
        }
        File temporary = new File(root, hash + ".tmp");
        FileOutputStream output = new FileOutputStream(temporary);
        try {
            output.write(envelope);
            output.getFD().sync();
        } finally {
            output.close();
        }
        if (!temporary.renameTo(target)) {
            temporary.delete();
            throw new IOException("could not activate return package");
        }
        return target;
    }

    private static ContentValues values(ReturnPackage value, String path) {
        ContentValues row = new ContentValues();
        row.put("package_id", value.packageId);
        row.put("epoch_id", value.epochId);
        row.put("device_id", value.deviceId);
        row.put("base_snapshot_id", value.baseSnapshotId);
        row.put("first_device_seq", value.firstDeviceSeq);
        row.put("highest_device_seq", value.highestDeviceSeq);
        row.put("operation_count", value.operationCount);
        row.put("payload_sha256", value.payloadSha256);
        row.put("signature_b64", value.signatureBase64);
        row.put("envelope_path", path);
        row.put("state", value.state);
        row.put("created_at", value.createdAt);
        row.put("updated_at", value.updatedAt);
        return row;
    }

    private static byte[] read(File file) throws IOException {
        FileInputStream input = new FileInputStream(file);
        try {
            ByteArrayOutputStream output = new ByteArrayOutputStream();
            byte[] buffer = new byte[8192];
            int count;
            while ((count = input.read(buffer)) != -1) output.write(buffer, 0, count);
            return output.toByteArray();
        } finally {
            input.close();
        }
    }

    public static final class ReturnPackage {
        public final String packageId;
        public final String epochId;
        public final String deviceId;
        public final String baseSnapshotId;
        public final long firstDeviceSeq;
        public final long highestDeviceSeq;
        public final int operationCount;
        public final String payloadSha256;
        public final String signatureBase64;
        public final byte[] compressedEnvelope;
        public final String envelopePath;
        public final String state;
        public final long createdAt;
        public final long updatedAt;

        ReturnPackage(String packageId, String epochId, String deviceId,
                String baseSnapshotId, long firstDeviceSeq, long highestDeviceSeq,
                int operationCount, String payloadSha256, String signatureBase64,
                byte[] compressedEnvelope, String envelopePath, String state,
                long createdAt, long updatedAt) {
            this.packageId = packageId;
            this.epochId = epochId;
            this.deviceId = deviceId;
            this.baseSnapshotId = baseSnapshotId;
            this.firstDeviceSeq = firstDeviceSeq;
            this.highestDeviceSeq = highestDeviceSeq;
            this.operationCount = operationCount;
            this.payloadSha256 = payloadSha256;
            this.signatureBase64 = signatureBase64;
            this.compressedEnvelope = compressedEnvelope.clone();
            this.envelopePath = envelopePath;
            this.state = state;
            this.createdAt = createdAt;
            this.updatedAt = updatedAt;
        }

        ReturnPackage withEnvelopePath(String path) {
            return new ReturnPackage(packageId, epochId, deviceId, baseSnapshotId,
                    firstDeviceSeq, highestDeviceSeq, operationCount, payloadSha256,
                    signatureBase64, compressedEnvelope, path, state, createdAt, updatedAt);
        }
    }

    public static final class Receipt {
        public final String packageId;
        public final String importSessionId;
        public final long acceptedHighestDeviceSeq;
        public final String resultSummarySha256;

        public Receipt(String packageId, String importSessionId,
                long acceptedHighestDeviceSeq, String resultSummarySha256) {
            this.packageId = packageId;
            this.importSessionId = importSessionId;
            this.acceptedHighestDeviceSeq = acceptedHighestDeviceSeq;
            this.resultSummarySha256 = resultSummarySha256;
        }
    }
}
