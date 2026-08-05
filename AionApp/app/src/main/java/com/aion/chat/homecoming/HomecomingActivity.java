package com.aion.chat.homecoming;

import android.annotation.SuppressLint;
import android.content.Intent;
import android.content.SharedPreferences;
import android.database.Cursor;
import android.database.sqlite.SQLiteDatabase;
import android.graphics.Bitmap;
import android.net.Uri;
import android.os.Bundle;
import android.os.Build;
import android.provider.MediaStore;
import android.util.Base64;
import android.util.Log;
import android.webkit.WebResourceRequest;
import android.webkit.WebChromeClient;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Toast;

import androidx.appcompat.app.AppCompatActivity;

import com.aion.chat.AionPushService;
import com.aion.chat.LauncherActivity;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicBoolean;

public final class HomecomingActivity extends AppCompatActivity {
    public static final String EXTRA_SHOW_CONFIRMATION = "show_confirmation";
    public static final String EXTRA_OPEN_RETURN = "open_return";
    private static final String ASSET_URL =
            "file:///android_asset/homecoming/index.html";
    private static final String KEY_PORTABLE_ROUTE_COUNT = "portable_route_count";
    private static final int REQUEST_PICK_IMAGE = 4101;
    private static final int REQUEST_CAPTURE_IMAGE = 4102;
    private static final int MAX_IMAGE_BYTES = 16 * 1024 * 1024;

    private HomecomingModeStore modeStore;
    private HomecomingDatabase database;
    private WebView webView;
    private HomecomingRuntime runtime;
    private ExecutorService runtimeExecutor = Executors.newSingleThreadExecutor();
    private final ExecutorService returnExecutor = Executors.newSingleThreadExecutor();
    private final AtomicBoolean returnInFlight = new AtomicBoolean(false);
    private HomecomingReturnPackageCoordinator returnPackages;
    private HomecomingReturnController returnController;
    private volatile String returnPhase = "idle";
    private volatile String returnFailure = "";
    private volatile boolean canReturnWithoutSync;
    private volatile Map<String, Integer> returnCounts = Collections.emptyMap();
    private boolean returnPanelOpen;
    private String pendingImageDataUrl = "";

    @SuppressLint({"SetJavaScriptEnabled", "AddJavascriptInterface"})
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        modeStore = new HomecomingModeStore(this);
        database = new HomecomingDatabase(this);
        returnPackages = new HomecomingReturnPackageCoordinator(this);
        recoverInterruptedReturn();
        returnController = createReturnController();
        returnPanelOpen = getIntent().getBooleanExtra(EXTRA_OPEN_RETURN, false)
                || modeStore.isFrozen();

        webView = new WebView(this);
        setContentView(webView);
        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setAllowFileAccess(true);
        settings.setAllowContentAccess(false);
        settings.setAllowFileAccessFromFileURLs(false);
        settings.setAllowUniversalAccessFromFileURLs(false);
        settings.setBlockNetworkLoads(true);
        settings.setDomStorageEnabled(false);
        settings.setDatabaseEnabled(false);
        webView.setWebChromeClient(new WebChromeClient());
        webView.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                return !request.getUrl().toString().startsWith(
                        "file:///android_asset/homecoming/");
            }

            @Override
            public boolean shouldOverrideUrlLoading(WebView view, String url) {
                return url == null || !url.startsWith(
                        "file:///android_asset/homecoming/");
            }
        });
        webView.addJavascriptInterface(new HomecomingBridge(this), "HomecomingNative");
        webView.loadUrl(ASSET_URL);
        if (modeStore.isActive()) {
            initializeRuntime();
        }
    }

    String readinessJson() {
        try {
            int routeCount = homecomingPreferences().getInt(
                    KEY_PORTABLE_ROUTE_COUNT, 0);
            SQLiteDatabase readable = database.getReadableDatabase();
            String snapshotId = metaString(readable, "snapshot_id");
            long storedSchema = metaLong(readable, "schema");
            long createdAt = metaLong(readable, "created_at");
            String readinessEpoch = modeStore.currentEpoch();
            if (readinessEpoch == null || readinessEpoch.isEmpty()) {
                readinessEpoch = "readiness";
            }
            HomecomingAlarmRegistrar alarmReadiness =
                    new HomecomingAlarmRegistrar(this, readinessEpoch);
            java.util.LinkedHashMap<String, String> permissionStates =
                    new java.util.LinkedHashMap<>();
            permissionStates.put(
                    "schedule_exactness", alarmReadiness.exactness());
            int pendingSchedules = modeStore.isActive()
                    ? new HomecomingScheduleRepository(
                            database,
                            modeStore.currentEpoch(),
                            HomecomingBackupScheduler.getOrCreateDeviceId(this))
                            .listActive().size()
                    : count(readable, "schedule_snapshot", null);
            HomecomingReadiness readiness = new HomecomingReadiness(
                    !snapshotId.isEmpty() && routeCount > 0,
                    createdAt,
                    homecomingPreferences().getLong("last_checked_at", 0L),
                    count(readable, "memory_snapshot", "owner_id='main'"),
                    count(readable, "memory_snapshot", "owner_id='second'"),
                    messageCount(readable, "main_private"),
                    messageCount(readable, "companion_private"),
                    messageCount(readable, "group"),
                    pendingSchedules,
                    routeCount,
                    storedSchema <= 0
                            ? HomecomingContract.SCHEMA_VERSION
                            : (int) storedSchema,
                    permissionStates,
                    snapshotId.isEmpty() ? "尚无可用灾备快照" : "");
            return readiness.toJson().toString();
        } catch (Exception exception) {
            return new HomecomingReadiness(
                    false, 0L, 0L, 0, 0, 0, 0, 0, 0, 0,
                    HomecomingContract.SCHEMA_VERSION,
                    Collections.<String, String>emptyMap(),
                    "归巢数据暂不可读").toJson().toString();
        }
    }

    void requestRefresh() {
        Toast.makeText(
                this,
                "归巢备份由正常模式独立维护",
                Toast.LENGTH_SHORT).show();
        notifyPageState();
    }

    void confirmEnter() {
        String snapshotId = metaString(
                database.getReadableDatabase(), "snapshot_id");
        int routeCount = homecomingPreferences().getInt(
                KEY_PORTABLE_ROUTE_COUNT, 0);
        if (snapshotId.isEmpty() || routeCount < 1) {
            Toast.makeText(
                    this,
                    "尚无完整快照或可用云端线路，不能进入归巢",
                    Toast.LENGTH_LONG).show();
            notifyPageState();
            return;
        }
        modeStore.activate();
        stopService(new Intent(this, AionPushService.class));
        startHomecomingService();
        initializeRuntime();
        notifyPageState();
    }

    void cancelEnter() {
        if (!modeStore.isActive() && !modeStore.isFrozen()) {
            finish();
        }
    }

    private void startHomecomingService() {
        Intent service = new Intent(this, HomecomingForegroundService.class)
                .setAction(HomecomingForegroundService.ACTION_START);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            startForegroundService(service);
        } else {
            startService(service);
        }
    }

    void requestFoundationReturn() {
        returnPanelOpen = true;
        returnPhase = modeStore.isActive() ? "ready" : returnPhase;
        notifyPageState();
    }

    void startReturnSync() {
        returnPanelOpen = true;
        returnFailure = "";
        canReturnWithoutSync = false;
        if (!returnInFlight.compareAndSet(false, true)) return;
        notifyPageState();
        returnExecutor.execute(() -> {
            try {
                returnController.synchronize(returnRoute(), returnObserver());
            } finally {
                returnInFlight.set(false);
                runOnUiThread(this::notifyPageState);
            }
        });
    }

    void retryReturnSync() {
        startReturnSync();
    }

    void returnWithoutSync() {
        if (returnInFlight.get()) return;
        returnController.archiveAndReturn(returnRoute());
    }

    String returnStateJson() {
        try {
            List<HomecomingReturnPackageRepository.ReturnPackage> pending =
                    returnPackages.pendingInSequence();
            JSONObject counts = new JSONObject();
            for (Map.Entry<String, Integer> entry : returnCounts.entrySet()) {
                counts.put(entry.getKey(), entry.getValue());
            }
            String mode = modeStore.isActive() ? "active"
                    : modeStore.isFreezing() ? "freezing"
                    : modeStore.isReturning() ? "returning"
                    : modeStore.isFrozen() ? "frozen" : "inactive";
            return new JSONObject()
                    .put("open", returnPanelOpen || !pending.isEmpty())
                    .put("mode", mode)
                    .put("phase", returnPhase)
                    .put("failure", returnFailure)
                    .put("pendingPackageCount", pending.size())
                    .put("pendingPackageId", modeStore.pendingPackageId())
                    .put("counts", counts)
                    .put("inFlight", returnInFlight.get())
                    .put("canRetry", !returnInFlight.get() && !returnFailure.isEmpty())
                    .put("canReturnWithoutSync",
                            canReturnWithoutSync && !pending.isEmpty())
                    .toString();
        } catch (Exception exception) {
            return "{\"open\":true,\"mode\":\"frozen\","
                    + "\"phase\":\"failed\",\"failure\":\"local_package_unavailable\","
                    + "\"pendingPackageCount\":0,\"counts\":{},"
                    + "\"inFlight\":false,\"canRetry\":true,"
                    + "\"canReturnWithoutSync\":false}";
        }
    }

    boolean isHomecomingActive() {
        return modeStore.isActive();
    }

    String bootstrapJson() {
        if (runtime == null) {
            return "{\"ready\":false}";
        }
        try {
            return runtime.bootstrapJson();
        } catch (Exception exception) {
            return "{\"ready\":false}";
        }
    }

    String messagesJson(String timelineId, long beforeCreatedAt, int limit) {
        if (runtime == null) return "[]";
        try {
            return runtime.messagesJson(timelineId, beforeCreatedAt, limit);
        } catch (Exception exception) {
            return "[]";
        }
    }

    String supervisionStatusJson() {
        if (runtime == null) {
            return "{\"enabled\":false,\"readiness\":\"unavailable\",\"groups\":[]}";
        }
        try {
            return runtime.supervisionStatusJson();
        } catch (Exception exception) {
            return "{\"enabled\":false,\"readiness\":\"unavailable\",\"groups\":[]}";
        }
    }

    void sendMessage(String requestId, String timelineId, String responderOwner,
            String text, String routeId, String modelId) {
        HomecomingRuntime current = runtime;
        if (current == null) {
            emitRuntimeEvent(errorEvent(requestId, "runtime_not_ready"));
            return;
        }
        final String image;
        synchronized (this) {
            image = pendingImageDataUrl;
            pendingImageDataUrl = "";
        }
        runtimeExecutor.execute(() -> {
            try {
                current.send(
                        requestId, timelineId, responderOwner, text,
                        routeId, modelId, image, this::emitRuntimeEvent);
            } catch (Exception exception) {
                emitRuntimeEvent(errorEvent(requestId, "request_rejected"));
            }
        });
    }

    void stopMessage(String requestId) {
        HomecomingRuntime current = runtime;
        if (current != null) current.stop(requestId);
    }

    void setTtsEnabled(boolean enabled) {
        HomecomingRuntime current = runtime;
        if (current != null) current.setTtsEnabled(enabled);
    }

    void replayTts(String messageId) {
        HomecomingRuntime current = runtime;
        if (current != null) runtimeExecutor.execute(() -> current.replayTts(messageId));
    }

    String memoriesJson(String ownerId, String query) {
        if (runtime == null) return "[]";
        try {
            return runtime.memoriesJson(ownerId, query);
        } catch (Exception exception) {
            return "[]";
        }
    }

    void summarizeMemories(String ownerId, String routeId, String modelId) {
        HomecomingRuntime current = runtime;
        if (current == null) return;
        runtimeExecutor.execute(() ->
                current.summarizeMemories(
                        ownerId, routeId, modelId, this::emitRuntimeEvent));
    }

    void setRoutePreference(String ownerId, String routeId, String modelId) {
        HomecomingRuntime current = runtime;
        if (current == null) return;
        try {
            current.setRoutePreference(ownerId, routeId, modelId);
        } catch (RuntimeException ignored) {
        }
    }

    void summarizeAllMemories() {
        HomecomingRuntime current = runtime;
        if (current == null) return;
        runtimeExecutor.execute(() ->
                current.summarizeAllMemories(this::emitRuntimeEvent));
    }

    String createMemory(String ownerId, String content, String keywords) {
        if (runtime == null) return "{}";
        try {
            return runtime.createMemory(ownerId, content, keywords);
        } catch (Exception exception) {
            return "{}";
        }
    }

    String updateMemory(
            String ownerId, String memoryId, String content, String baseHash) {
        if (runtime == null) return "{}";
        try {
            return runtime.updateMemory(ownerId, memoryId, content, baseHash);
        } catch (Exception exception) {
            return "{}";
        }
    }

    boolean deleteMemory(String ownerId, String memoryId, String baseHash) {
        if (runtime == null) return false;
        try {
            return runtime.deleteMemory(ownerId, memoryId, baseHash);
        } catch (Exception exception) {
            return false;
        }
    }

    String schedulesJson() {
        if (runtime == null) return "[]";
        try {
            return runtime.schedulesJson();
        } catch (Exception exception) {
            return "[]";
        }
    }

    String createSchedule(
            String type,
            long triggerAt,
            String content,
            String ownerId,
            String timelineId) {
        if (runtime == null) return "{}";
        try {
            return runtime.createSchedule(
                    type, triggerAt, content, ownerId, timelineId);
        } catch (Exception exception) {
            return "{}";
        }
    }

    boolean deleteSchedule(String id) {
        if (runtime == null) return false;
        try {
            return runtime.deleteSchedule(id);
        } catch (Exception exception) {
            return false;
        }
    }

    void pickImage() {
        Intent intent = new Intent(Intent.ACTION_GET_CONTENT);
        intent.setType("image/*");
        startActivityForResult(
                Intent.createChooser(intent, "选择图片"), REQUEST_PICK_IMAGE);
    }

    void captureImage() {
        Intent intent = new Intent(MediaStore.ACTION_IMAGE_CAPTURE);
        if (intent.resolveActivity(getPackageManager()) != null) {
            startActivityForResult(intent, REQUEST_CAPTURE_IMAGE);
        }
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (resultCode != RESULT_OK || data == null) return;
        try {
            byte[] bytes;
            String mime = "image/jpeg";
            if (requestCode == REQUEST_PICK_IMAGE) {
                Uri uri = data.getData();
                if (uri == null) return;
                String resolved = getContentResolver().getType(uri);
                if (resolved != null && resolved.startsWith("image/")) mime = resolved;
                try (InputStream input = getContentResolver().openInputStream(uri)) {
                    bytes = readCapped(input, MAX_IMAGE_BYTES);
                }
            } else if (requestCode == REQUEST_CAPTURE_IMAGE) {
                Object raw = data.getExtras() == null ? null : data.getExtras().get("data");
                if (!(raw instanceof Bitmap)) return;
                ByteArrayOutputStream output = new ByteArrayOutputStream();
                ((Bitmap) raw).compress(Bitmap.CompressFormat.JPEG, 90, output);
                bytes = output.toByteArray();
            } else {
                return;
            }
            synchronized (this) {
                pendingImageDataUrl = "data:" + mime + ";base64,"
                        + Base64.encodeToString(bytes, Base64.NO_WRAP);
            }
            emitRuntimeEvent(simpleEvent("media_ready", "", "图片已准备"));
        } catch (Exception exception) {
            emitRuntimeEvent(simpleEvent("media_failure", "", "图片读取失败"));
        }
    }

    private void initializeRuntime() {
        if (runtimeExecutor.isShutdown()) {
            runtimeExecutor = Executors.newSingleThreadExecutor();
        }
        try {
            runtime = new HomecomingRuntime(this, modeStore);
        } catch (Exception exception) {
            runtime = null;
            Toast.makeText(this, "归巢运行组件暂不可用", Toast.LENGTH_LONG).show();
        }
    }

    private void recoverInterruptedReturn() {
        if (!modeStore.isFreezing()) return;
        try {
            List<HomecomingReturnPackageRepository.ReturnPackage> pending =
                    returnPackages.pendingInSequence();
            if (pending.isEmpty()) {
                modeStore.resumeActive();
            } else {
                modeStore.markFrozen(pending.get(0).packageId);
            }
        } catch (Exception exception) {
            modeStore.resumeActive();
        }
    }

    private HomecomingReturnController createReturnController() {
        HomecomingReturnController.ModePort mode =
                new HomecomingReturnController.ModePort() {
                    @Override public boolean isActive() {
                        return modeStore.isActive();
                    }

                    @Override public boolean isFrozen() {
                        return modeStore.isFrozen();
                    }

                    @Override public String currentEpoch() {
                        return modeStore.currentEpoch();
                    }

                    @Override public void beginFreezing() {
                        modeStore.beginFreezing();
                        freezeRuntime();
                    }

                    @Override public void resumeActive() {
                        modeStore.resumeActive();
                    }

                    @Override public void markFrozen(String packageId) {
                        modeStore.markFrozen(packageId);
                    }

                    @Override public void markReturning(String packageId) {
                        modeStore.markReturning(packageId);
                    }

                    @Override public void deactivate() {
                        try {
                            if (returnPackages.pendingInSequence().isEmpty()) {
                                modeStore.deactivateAfterSuccessfulReturn();
                            } else {
                                modeStore.deactivateAfterPackageSaved();
                            }
                        } catch (Exception exception) {
                            modeStore.deactivateAfterPackageSaved();
                        }
                    }
                };
        return new HomecomingReturnController(
                mode, returnPackages, new HomecomingReturnClient(),
                System::currentTimeMillis);
    }

    private void freezeRuntime() {
        stopService(new Intent(this, HomecomingForegroundService.class));
        HomecomingRuntime current = runtime;
        runtime = null;
        if (current != null) current.freeze();
        runtimeExecutor.shutdownNow();
    }

    private HomecomingReturnController.ReturnRoute returnRoute() {
        return new HomecomingReturnController.ReturnRoute() {
            @Override public String serverPageUrl() {
                return homecomingPreferences().getString(
                        HomecomingBackupScheduler.KEY_LAST_SERVER_BASE, "");
            }

            @Override public void stopHomecoming() {
                runOnUiThread(() -> stopService(
                        new Intent(HomecomingActivity.this,
                                HomecomingForegroundService.class)));
            }

            @Override public void resumeHomecoming() {
                runOnUiThread(() -> {
                    initializeRuntime();
                    startHomecomingService();
                    notifyPageState();
                });
            }

            @Override public void openNormal() {
                runOnUiThread(() -> {
                    Intent intent = new Intent(
                            HomecomingActivity.this, LauncherActivity.class);
                    intent.putExtra(
                            LauncherActivity.EXTRA_FORCE_ADDRESS_PICKER, true);
                    startActivity(intent);
                    finish();
                });
            }
        };
    }

    private HomecomingReturnController.Observer returnObserver() {
        return new HomecomingReturnController.Observer() {
            @Override public void onPhase(String phase) {
                returnPhase = phase;
                returnFailure = "";
                runOnUiThread(HomecomingActivity.this::notifyPageState);
            }

            @Override public void onFailure(
                    String code, boolean allowReturnWithoutSync) {
                returnPhase = "failed";
                returnFailure = code == null ? "return_sync_failed" : code;
                canReturnWithoutSync = allowReturnWithoutSync;
                runOnUiThread(HomecomingActivity.this::notifyPageState);
            }

            @Override public void onCounts(Map<String, Integer> counts) {
                returnCounts = counts == null
                        ? Collections.emptyMap()
                        : Collections.unmodifiableMap(
                                new LinkedHashMap<>(counts));
                runOnUiThread(HomecomingActivity.this::notifyPageState);
            }

            @Override public void onDiagnostic(
                    String stage, Throwable failure) {
                Log.e("HomecomingReturn", stage, failure);
            }
        };
    }

    private void emitRuntimeEvent(JSONObject event) {
        runOnUiThread(() -> webView.evaluateJavascript(
                "window.HomecomingPage&&window.HomecomingPage.onNativeEvent("
                        + event.toString() + ")", null));
    }

    private static JSONObject errorEvent(String requestId, String code) {
        return simpleEvent("failure", requestId, code);
    }

    private static JSONObject simpleEvent(String type, String requestId, String value) {
        try {
            return new JSONObject()
                    .put("type", type)
                    .put("requestId", requestId)
                    .put("value", value);
        } catch (Exception exception) {
            throw new IllegalStateException("could not encode UI event", exception);
        }
    }

    private static byte[] readCapped(InputStream input, int cap) throws Exception {
        if (input == null) throw new IllegalArgumentException("image stream is missing");
        ByteArrayOutputStream output = new ByteArrayOutputStream();
        byte[] buffer = new byte[8192];
        int total = 0;
        int count;
        while ((count = input.read(buffer)) != -1) {
            total += count;
            if (total > cap) throw new IllegalArgumentException("image is too large");
            output.write(buffer, 0, count);
        }
        return output.toByteArray();
    }

    private void notifyPageState() {
        webView.evaluateJavascript(
                "window.HomecomingPage&&window.HomecomingPage.render()", null);
    }

    private SharedPreferences homecomingPreferences() {
        return getSharedPreferences(HomecomingModeStore.PREFERENCES_NAME, MODE_PRIVATE);
    }

    private static int messageCount(SQLiteDatabase database, String timeline) {
        return count(database, "message_snapshot",
                "timeline_id='" + timeline.replace("'", "''") + "'");
    }

    private static int count(SQLiteDatabase database, String table, String where) {
        String sql = "SELECT COUNT(*) FROM " + table
                + (where == null ? "" : " WHERE " + where);
        try (Cursor cursor = database.rawQuery(sql, null)) {
            return cursor.moveToFirst() ? cursor.getInt(0) : 0;
        }
    }

    private static long metaLong(SQLiteDatabase database, String key) {
        try (Cursor cursor = database.rawQuery(
                "SELECT value FROM snapshot_meta WHERE key=?", new String[]{key})) {
            if (!cursor.moveToFirst()) {
                return 0L;
            }
            try {
                return (long) Double.parseDouble(cursor.getString(0));
            } catch (NumberFormatException ignored) {
                return 0L;
            }
        }
    }

    private static String metaString(SQLiteDatabase database, String key) {
        try (Cursor cursor = database.rawQuery(
                "SELECT value FROM snapshot_meta WHERE key=?",
                new String[]{key})) {
            if (!cursor.moveToFirst()) {
                return "";
            }
            String value = cursor.getString(0);
            return value == null ? "" : value.trim();
        }
    }

    @Override
    protected void onDestroy() {
        runtimeExecutor.shutdownNow();
        returnController.cancel();
        returnExecutor.shutdownNow();
        if (webView != null) webView.destroy();
        super.onDestroy();
    }
}
