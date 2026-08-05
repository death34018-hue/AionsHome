package com.aion.chat.homecoming;

import android.content.ContentValues;
import android.content.Context;
import android.database.Cursor;
import android.database.sqlite.SQLiteDatabase;

import java.util.ArrayList;
import java.util.List;

final class HomecomingReturnPackageCoordinator
        implements HomecomingReturnController.PackagePort {
    private final HomecomingDatabase database;
    private final HomecomingReturnPackageRepository repository;
    private final HomecomingReturnPackageBuilder builder;
    private final String deviceId;

    HomecomingReturnPackageCoordinator(Context context) {
        database = new HomecomingDatabase(context);
        repository = new HomecomingReturnPackageRepository(context);
        deviceId = HomecomingBackupScheduler.getOrCreateDeviceId(context);
        HomecomingKeyStore keys = new HomecomingKeyStore(context, deviceId);
        builder = new HomecomingReturnPackageBuilder(
                this::loadPendingOperations,
                epochId -> baseSnapshotId(),
                repository,
                keys::signReturnPayload,
                deviceId);
    }

    @Override
    public HomecomingReturnPackageRepository.ReturnPackage freezeAndBuild(
            String epochId, long now) throws Exception {
        SQLiteDatabase writable = database.getWritableDatabase();
        ContentValues epoch = new ContentValues();
        epoch.put("epoch_id", epochId);
        epoch.put("device_id", deviceId);
        epoch.put("started_at", now);
        epoch.put("frozen_at", now);
        writable.insertWithOnConflict(
                "homecoming_epoch", null, epoch, SQLiteDatabase.CONFLICT_IGNORE);
        ContentValues frozen = new ContentValues();
        frozen.put("frozen_at", now);
        writable.update(
                "homecoming_epoch", frozen, "epoch_id=?", new String[]{epochId});
        if (loadPendingOperations(epochId).isEmpty()) return null;
        return builder.freezeAndBuild(epochId, now);
    }

    @Override
    public List<HomecomingReturnPackageRepository.ReturnPackage>
            pendingInSequence() throws Exception {
        return repository.pendingInSequence();
    }

    @Override
    public void confirm(
            HomecomingReturnPackageRepository.ReturnPackage value,
            HomecomingReturnClient.Receipt receipt,
            long now) {
        repository.confirm(
                value.packageId,
                new HomecomingReturnPackageRepository.Receipt(
                        receipt.packageId,
                        receipt.importSessionId,
                        receipt.acceptedHighestDeviceSeq,
                        receipt.resultSummarySha256),
                now);
    }

    private List<HomecomingOperationJournal.Operation> loadPendingOperations(
            String epochId) {
        ArrayList<HomecomingOperationJournal.Operation> result = new ArrayList<>();
        try (Cursor cursor = database.getReadableDatabase().rawQuery(
                "SELECT op_id,epoch_id,device_id,device_seq,entity_type,"
                        + "entity_id,action,base_revision,payload_json,created_at "
                        + "FROM operation_journal "
                        + "WHERE epoch_id=? AND status='pending' ORDER BY device_seq",
                new String[]{epochId})) {
            while (cursor.moveToNext()) {
                result.add(new HomecomingOperationJournal.Operation(
                        cursor.getString(0),
                        cursor.getString(1),
                        cursor.getString(2),
                        cursor.getLong(3),
                        cursor.getString(4),
                        cursor.getString(5),
                        cursor.getString(6),
                        cursor.getString(7),
                        cursor.getString(8),
                        cursor.getLong(9)));
            }
        }
        return result;
    }

    private String baseSnapshotId() {
        try (Cursor cursor = database.getReadableDatabase().rawQuery(
                "SELECT value FROM snapshot_meta WHERE key='snapshot_id'",
                null)) {
            if (!cursor.moveToFirst()) {
                throw new IllegalStateException(
                        "imported base snapshot metadata is unavailable");
            }
            String snapshotId = cursor.getString(0);
            if (snapshotId == null || snapshotId.trim().isEmpty()) {
                throw new IllegalStateException(
                        "imported base snapshot metadata is invalid");
            }
            return snapshotId.trim();
        }
    }
}
