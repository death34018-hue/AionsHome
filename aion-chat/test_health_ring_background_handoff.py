import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read_text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def extract_block(source: str, marker: str) -> str:
    start = source.index(marker)
    brace = source.index("{", start)
    depth = 0
    for index in range(brace, len(source)):
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"Could not find end of block after {marker!r}")


class HealthRingBackgroundHandoffTests(unittest.TestCase):
    def test_health_page_exposes_persistent_ring_master_switch(self):
        health = read_text("aion-chat/static/health.html")

        self.assertIn('id="ringFeatureToggle"', health)
        self.assertIn('onchange="toggleRingFeature(this.checked)"', health)
        self.assertIn("async function toggleRingFeature(enabled)", health)
        self.assertIn("window.AionRingBle.setRingEnabled(enabled)", health)
        self.assertIn("await refreshRingFeatureSetting();", health)
        self.assertLess(
            health.index("await refreshRingFeatureSetting();"),
            health.index("检测到手机原生戒指蓝牙桥"),
        )

    def test_native_ring_switch_persists_and_closes_page_ble_immediately(self):
        bridge = read_text(
            "AionApp/app/src/main/java/com/aion/chat/AionRingBleBridge.java"
        )
        self.assertIn("public void setRingEnabled(boolean enabled)", bridge)
        setter = extract_block(bridge, "public void setRingEnabled(boolean enabled)")

        self.assertIn('KEY_RING_ENABLED = "ring_enabled"', bridge)
        self.assertIn("public boolean isRingEnabled()", bridge)
        self.assertIn("putBoolean(KEY_RING_ENABLED, enabled)", setter)
        self.assertIn("disconnectInternal(true)", setter)
        self.assertIn("ACTION_RING_FEATURE_CHANGED", setter)
        self.assertIn("if (!isRingEnabled()) return;", extract_block(bridge, "private void startRingScan("))
        self.assertIn("if (!isRingEnabled()) return;", extract_block(bridge, "public void connectCandidate(int id)"))
        self.assertIn("if (!isRingEnabled()) return;", extract_block(bridge, "public void write(final String hexPacket)"))

    def test_disabled_ring_feature_stops_background_attempts_until_reenabled(self):
        service = read_text(
            "AionApp/app/src/main/java/com/aion/chat/AionPushService.java"
        )
        thread = extract_block(service, "private synchronized void startRingSyncThread()")
        self.assertIn("private void onRingFeatureSettingChanged()", service)
        setting_changed = extract_block(service, "private void onRingFeatureSettingChanged()")
        acquire = extract_block(service, "private void runRingBackgroundAcquireOnce()")
        sync = extract_block(service, "private void runRingBackgroundSyncOnce()")

        self.assertIn('ACTION_RING_FEATURE_CHANGED = "ring_feature_changed"', service)
        self.assertIn('KEY_RING_ENABLED = "ring_enabled"', service)
        self.assertIn("waitUntilRingFeatureEnabled()", thread)
        self.assertIn("cancelForFeatureDisabled()", setting_changed)
        self.assertIn("ringSyncSignal.notifyAll()", setting_changed)
        self.assertIn("if (!isRingFeatureEnabled()) return;", acquire)
        self.assertIn("if (!isRingFeatureEnabled()) return;", sync)

    def test_ring_diagnostic_identifies_the_installed_build(self):
        gradle = read_text("AionApp/app/build.gradle")
        service = read_text(
            "AionApp/app/src/main/java/com/aion/chat/AionPushService.java"
        )
        diag = extract_block(service, 'case "request_ring_diag":')

        self.assertIn("versionCode 23", gradle)
        self.assertIn('versionName "1.22"', gradle)
        self.assertIn("BuildConfig.VERSION_NAME", diag)
        self.assertIn("BuildConfig.VERSION_CODE", diag)
        self.assertIn("backgroundGatt=", diag)
        self.assertIn("ringBackgroundSync.status()", diag)

    def test_app_pause_releases_page_ble_before_background_service_takes_over(self):
        activity = read_text(
            "AionApp/app/src/main/java/com/aion/chat/WebViewActivity.java"
        )
        on_pause = extract_block(activity, "protected void onPause()")

        release = "ringBleBridge.releaseForBackgroundSync();"
        notify = "notifyServiceForeground(false);"
        self.assertIn(release, on_pause)
        self.assertIn(notify, on_pause)
        self.assertLess(on_pause.index(release), on_pause.index(notify))

    def test_app_resume_lets_visible_health_page_reacquire_saved_ring(self):
        activity = read_text(
            "AionApp/app/src/main/java/com/aion/chat/WebViewActivity.java"
        )
        on_resume = extract_block(activity, "protected void onResume()")

        self.assertIn("ringBleBridge.resumeHealthPageConnection();", on_resume)

    def test_native_handoff_immediately_transfers_a_connected_page_gatt(self):
        bridge = read_text(
            "AionApp/app/src/main/java/com/aion/chat/AionRingBleBridge.java"
        )
        self.assertIn("public void releaseForBackgroundSync()", bridge)
        self.assertIn("public void resumeHealthPageConnection()", bridge)
        self.assertIn("public void setHealthPageVisible(boolean visible)", bridge)
        release = extract_block(
            bridge, "public void releaseForBackgroundSync()"
        )
        resume = extract_block(
            bridge, "public void resumeHealthPageConnection()"
        )
        visibility = extract_block(
            bridge, "public void setHealthPageVisible(boolean visible)"
        )

        handoff = extract_block(
            bridge, "private void handoffConnectedPageToBackground()"
        )

        self.assertIn("handoffConnectedPageToBackground();", release)
        self.assertIn("syncHealthRingPageVisibility", resume)
        self.assertIn("handoffConnectedPageToBackground();", visibility)
        self.assertIn("reconnectSavedRingIfNeeded", visibility)
        self.assertIn("boolean hadReadyConnection = connected && writeChar != null;", handoff)
        self.assertIn("if (hadReadyConnection)", handoff)
        self.assertIn("requestBackgroundRingAcquire();", handoff)
        self.assertIn("mainHandler.postDelayed", handoff)
        self.assertIn("disconnectInternal(true);", handoff)
        self.assertLess(
            handoff.index("requestBackgroundRingAcquire();"),
            handoff.index("disconnectInternal(true);"),
        )

    def test_background_acquire_action_wakes_the_single_ring_worker(self):
        service = read_text(
            "AionApp/app/src/main/java/com/aion/chat/AionPushService.java"
        )
        on_start = extract_block(
            service, "public int onStartCommand(Intent intent, int flags, int startId)"
        )
        request = extract_block(
            service, "private void requestRingBackgroundConnection()"
        )
        worker = extract_block(service, "private synchronized void startRingSyncThread()")

        self.assertIn("ACTION_ACQUIRE_RING_FOR_BACKGROUND", service)
        self.assertIn("ACTION_ACQUIRE_RING_FOR_BACKGROUND", on_start)
        self.assertIn("requestRingBackgroundConnection();", on_start)
        self.assertIn("ringAcquireRequested = true;", request)
        self.assertIn("ringSyncSignal.notifyAll();", request)
        self.assertIn("runRingBackgroundAcquireOnce();", worker)

    def test_foreground_service_declares_connected_device_type_for_persistent_ble(self):
        manifest = read_text("AionApp/app/src/main/AndroidManifest.xml")
        service = read_text(
            "AionApp/app/src/main/java/com/aion/chat/AionPushService.java"
        )
        on_start = extract_block(
            service, "public int onStartCommand(Intent intent, int flags, int startId)"
        )
        projection = extract_block(service, "private void updateForegroundForProjection()")

        self.assertIn("android.permission.FOREGROUND_SERVICE_CONNECTED_DEVICE", manifest)
        self.assertIn('android:foregroundServiceType="dataSync|location|connectedDevice|mediaProjection"', manifest)
        self.assertIn("FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE", on_start)
        self.assertIn("FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE", projection)

    def test_immediate_handoff_acquires_once_without_waiting_for_backoff(self):
        service = read_text(
            "AionApp/app/src/main/java/com/aion/chat/AionPushService.java"
        )
        acquire = extract_block(service, "void acquireConnectionForBackground()")

        self.assertIn("connectKnownSavedDevice(", acquire)
        self.assertIn("recordRingSyncSuccess(prefs);", acquire)
        self.assertNotIn("shouldSkipRingSyncForBackoff", acquire)
        self.assertNotIn("KEY_PAGE_CONNECTED", acquire)
        self.assertNotIn("requestComprehensiveSnapshot", acquire)

    def test_scheduled_sync_refreshes_the_held_gatt_then_keeps_the_new_one_open(self):
        service = read_text(
            "AionApp/app/src/main/java/com/aion/chat/AionPushService.java"
        )
        sync_once = extract_block(service, "void syncComprehensiveSnapshotOnce()")
        refresh = extract_block(
            service, "private BluetoothDevice refreshBackgroundConnectionForScheduledSync("
        )

        self.assertIn("refreshBackgroundConnectionForScheduledSync(", sync_once)
        self.assertNotRegex(sync_once, r"finally\s*\{\s*close\(\);")
        self.assertIn("if (isReadyConnection())", refresh)
        self.assertIn("refreshing held background GATT", refresh)
        self.assertIn("connectKnownSavedDevice(", refresh)
        self.assertLess(refresh.index("close();"), refresh.index("connectKnownSavedDevice("))

    def test_known_device_handoff_direct_connects_before_scan_fallback(self):
        service = read_text(
            "AionApp/app/src/main/java/com/aion/chat/AionPushService.java"
        )
        direct = extract_block(service, "private BluetoothDevice connectKnownSavedDevice(")

        self.assertIn("adapter.getRemoteDevice(savedAddress)", direct)
        self.assertIn("connectSavedDeviceWithRetry(", direct)
        self.assertIn("ensureBackgroundConnection(", direct)

    def test_background_write_requires_a_successful_gatt_callback(self):
        service = read_text(
            "AionApp/app/src/main/java/com/aion/chat/AionPushService.java"
        )
        write = extract_block(service, "private void writePacket(")
        callback = extract_block(
            service,
            "public void onCharacteristicWrite(BluetoothGatt g, BluetoothGattCharacteristic c, int status)",
        )

        self.assertIn("lastWriteStatus = status;", callback)
        self.assertIn("boolean completed = writeLatch.await", write)
        self.assertIn("if (!completed)", write)
        self.assertIn("if (lastWriteStatus != BluetoothGatt.GATT_SUCCESS)", write)

    def test_persistent_health_iframe_reports_real_visibility_to_native_bridge(self):
        chat = read_text("aion-chat/static/chat.js")
        sync_visibility = extract_block(
            chat, "function syncHealthRingPageVisibility()"
        )
        open_page = extract_block(chat, "function openSubPage(url)")
        close_page = extract_block(chat, "function closeSubPage(skipReload = false)")

        self.assertIn("subPagePath(currentSubPage) === '/health'", sync_visibility)
        self.assertIn("window.AionRingBle.setHealthPageVisible(visible);", sync_visibility)
        self.assertIn("syncHealthRingPageVisibility();", open_page)
        self.assertIn("syncHealthRingPageVisibility();", close_page)
        self.assertLess(
            open_page.index("currentSubPage = url;"),
            open_page.index("syncHealthRingPageVisibility();"),
        )
        self.assertLess(
            close_page.index("currentSubPage = null;"),
            close_page.index("syncHealthRingPageVisibility();"),
        )

    def test_health_page_claims_ring_before_starting_its_scan(self):
        bridge = read_text(
            "AionApp/app/src/main/java/com/aion/chat/AionRingBleBridge.java"
        )
        start_scan = extract_block(bridge, "private void startRingScan(")

        self.assertIn("releaseUnreadyGattBeforeScan();", start_scan)
        self.assertIn("markPageConnectionActive();", start_scan)
        self.assertIn("requestBackgroundRingRelease();", start_scan)
        self.assertLess(
            start_scan.index("releaseUnreadyGattBeforeScan();"),
            start_scan.index("markPageConnectionActive();"),
        )
        self.assertLess(
            start_scan.index("markPageConnectionActive();"),
            start_scan.index("requestBackgroundRingRelease();"),
        )
        self.assertLess(
            start_scan.index("requestBackgroundRingRelease();"),
            start_scan.index("scanner.startScan(scanCb);"),
        )

    def test_page_scan_closes_its_own_unready_gatt_before_retrying(self):
        bridge = read_text(
            "AionApp/app/src/main/java/com/aion/chat/AionRingBleBridge.java"
        )
        release = extract_block(
            bridge, "private void releaseUnreadyGattBeforeScan()"
        )
        callback = extract_block(
            bridge,
            "public void onConnectionStateChange(BluetoothGatt g, int status, int newState)",
        )

        self.assertIn("staleGatt.disconnect();", release)
        self.assertIn("staleGatt.close();", release)
        self.assertIn("gatt = null;", release)
        self.assertIn("if (g != gatt)", callback)

    def test_page_release_action_cancels_an_inflight_background_gatt(self):
        service = read_text(
            "AionApp/app/src/main/java/com/aion/chat/AionPushService.java"
        )
        on_start = extract_block(
            service, "public int onStartCommand(Intent intent, int flags, int startId)"
        )
        release = extract_block(
            service, "private void releaseRingForPageConnection()"
        )
        cancel = extract_block(
            service, "void cancelForPageConnection()"
        )

        self.assertIn("ACTION_RELEASE_RING_FOR_PAGE", on_start)
        self.assertIn("releaseRingForPageConnection();", on_start)
        self.assertIn("sync.cancelForPageConnection();", release)
        self.assertIn("scanner.stopScan(scanCallback);", cancel)
        self.assertIn("close();", cancel)
        self.assertIn("scanLatch", cancel)
        self.assertIn("connectLatch", cancel)
        self.assertIn("writeLatch", cancel)
        self.assertIn("healthLatch", cancel)
        self.assertGreaterEqual(cancel.count("countDown();"), 4)

    def test_page_takeover_does_not_create_failure_backoff(self):
        service = read_text(
            "AionApp/app/src/main/java/com/aion/chat/AionPushService.java"
        )
        sync_once = extract_block(service, "void syncComprehensiveSnapshotOnce()")
        cancellation = extract_block(
            sync_once, "if (isPageTakeoverRequested(operationGeneration))"
        )

        self.assertIn("return;", cancellation)
        self.assertNotIn("recordRingSyncFailure", cancellation)
        self.assertLess(
            sync_once.index("if (isPageTakeoverRequested(operationGeneration))"),
            sync_once.index("recordRingSyncFailure(prefs, e.getClass()"),
        )

    def test_background_scan_miss_does_not_try_cached_address(self):
        service = read_text(
            "AionApp/app/src/main/java/com/aion/chat/AionPushService.java"
        )
        resolver = extract_block(
            service, "private BluetoothDevice resolveSavedDevice("
        )

        self.assertIn("scanner.startScan(scanCallback);", resolver)
        self.assertIn("if (scanMatch != null) return scanMatch;", resolver)
        self.assertNotIn("adapter.getRemoteDevice(savedAddress)", resolver)

    def test_background_scan_miss_enters_backoff_without_connecting(self):
        service = read_text(
            "AionApp/app/src/main/java/com/aion/chat/AionPushService.java"
        )
        sync_once = extract_block(service, "void syncComprehensiveSnapshotOnce()")
        not_found = extract_block(sync_once, "if (device == null)")

        self.assertIn('recordRingSyncFailure(prefs, "ring_not_found");', not_found)
        self.assertIn("return;", not_found)
        self.assertNotIn("connectSavedDeviceWithRetry", not_found)

    def test_background_reconnect_matches_the_saved_ring_not_any_nearby_ring(self):
        service = read_text(
            "AionApp/app/src/main/java/com/aion/chat/AionPushService.java"
        )
        callback = extract_block(service, "private final ScanCallback scanCallback")

        self.assertIn("matchesSavedBackgroundDevice(dev, name)", callback)
        self.assertNotIn("looksLikeRing(result, name)", callback)

    def test_background_connect_retries_once_after_closing_failed_gatt(self):
        service = read_text(
            "AionApp/app/src/main/java/com/aion/chat/AionPushService.java"
        )
        self.assertIn("private void connectSavedDeviceWithRetry(", service)
        connect_with_retry = extract_block(
            service, "private void connectSavedDeviceWithRetry("
        )

        self.assertIn("MAX_RING_CONNECT_ATTEMPTS", connect_with_retry)
        self.assertIn("close();", connect_with_retry)
        self.assertIn("sleepQuiet", connect_with_retry)

    def test_background_sync_waits_until_ring_is_fully_ready(self):
        service = read_text(
            "AionApp/app/src/main/java/com/aion/chat/AionPushService.java"
        )
        sync_once = extract_block(service, "void syncComprehensiveSnapshotOnce()")
        connect = extract_block(service, "private void connect(BluetoothDevice device,")
        ensure = extract_block(
            service, "private BluetoothDevice ensureBackgroundConnection("
        )
        descriptor_callback = extract_block(
            service,
            "public void onDescriptorWrite(BluetoothGatt g, BluetoothGattDescriptor d, int status)",
        )
        enable_notify = extract_block(service, "private void enableNotify(")

        self.assertLess(
            sync_once.index("refreshBackgroundConnectionForScheduledSync"),
            sync_once.index("syncTimeAndMonitorSetting"),
        )
        self.assertIn("connectSavedDeviceWithRetry", ensure)
        self.assertLess(
            sync_once.index("syncTimeAndMonitorSetting"),
            sync_once.index("requestComprehensiveSnapshot"),
        )
        self.assertIn("connectLatch.await", connect)
        self.assertIn("!connected || writeChar == null", connect)
        self.assertIn("enableNextNotification(g);", descriptor_callback)
        self.assertNotIn("connected = status == BluetoothGatt.GATT_SUCCESS;", descriptor_callback)
        self.assertIn(
            "boolean notificationEnabled = g.setCharacteristicNotification(ch, true);",
            enable_notify,
        )
        self.assertNotIn("connected = true;", enable_notify)

    def test_background_subscribes_every_notify_characteristic_before_ready(self):
        service = read_text(
            "AionApp/app/src/main/java/com/aion/chat/AionPushService.java"
        )
        setup = extract_block(service, "private void setupRingService(")
        next_notify = extract_block(service, "private void enableNextNotification(")
        ready = extract_block(service, "private void finishNotificationSetup()")

        self.assertIn("matchedService.getCharacteristics()", setup)
        self.assertIn("addNotificationCharacteristic", setup)
        self.assertIn("enableNextNotification(g);", setup)
        self.assertIn("enableNotify(g, ch);", next_notify)
        self.assertIn("connected = true;", ready)
        self.assertIn("connectLatch", ready)

    def test_background_protocol_init_matches_the_health_page_handshake(self):
        service = read_text(
            "AionApp/app/src/main/java/com/aion/chat/AionPushService.java"
        )
        sync_once = extract_block(service, "void syncComprehensiveSnapshotOnce()")
        initialize = extract_block(service, "private void syncTimeAndMonitorSetting()")

        self.assertIn("DT_GET_DEVICE_INFO = 0x0201", service)
        self.assertIn("DT_GET_CHIP_SCHEME = 0x021B", service)
        self.assertIn("DT_GET_POWER = 0x0225", service)
        expected = [
            "writePacket(DT_SETTING_TIME",
            "writePacket(DT_GET_DEVICE_INFO",
            "writePacket(DT_GET_CHIP_SCHEME",
            "writePacket(DT_GET_POWER",
            "writePacket(DT_SETTING_HEART_MONITOR",
        ]
        for before, after in zip(expected, expected[1:]):
            self.assertLess(initialize.index(before), initialize.index(after))
        self.assertLess(
            sync_once.index("syncTimeAndMonitorSetting"),
            sync_once.index("requestComprehensiveSnapshot"),
        )

    def test_background_sync_uses_the_same_comprehensive_snapshot_as_health_page(self):
        service = read_text(
            "AionApp/app/src/main/java/com/aion/chat/AionPushService.java"
        )
        sync_once = extract_block(service, "void syncComprehensiveSnapshotOnce()")

        self.assertIn("DT_HEALTH_ALL = 0x0509", service)
        self.assertIn("DT_HEALTH_ALL_ACK = 0x0518", service)
        self.assertIn("requestComprehensiveSnapshot()", sync_once)
        self.assertIn("postRingSnapshot(httpBase", sync_once)
        self.assertNotIn("requestHeartHistory()", sync_once)
        self.assertNotIn("postHeartRate(httpBase", sync_once)

    def test_background_snapshot_upload_preserves_existing_raw_and_sleep_data(self):
        service = read_text(
            "AionApp/app/src/main/java/com/aion/chat/AionPushService.java"
        )
        self.assertIn("private void postRingSnapshot(", service)
        post_snapshot = extract_block(service, "private void postRingSnapshot(")

        self.assertIn("fetchExistingRingSnapshot(httpBase)", post_snapshot)
        self.assertIn('existingRing.optString("raw_json"', post_snapshot)
        self.assertIn('existingRing.opt("sleep_start_at")', post_snapshot)
        self.assertIn('raw.put("all", snapshot.toJson())', post_snapshot)
        self.assertIn('httpBase + "/api/health/ring/latest"', post_snapshot)

    def test_background_comprehensive_chunks_finish_after_five_idle_seconds(self):
        service = read_text(
            "AionApp/app/src/main/java/com/aion/chat/AionPushService.java"
        )

        self.assertIn("HEALTH_ACCUMULATE_IDLE_MS = 5_000L", service)
        self.assertIn("HEALTH_HISTORY_TIMEOUT_SECONDS = 15", service)
        self.assertIn("private void scheduleHealthIdleFinish(", service)
        ack = extract_block(service, "if (dataType == DT_HEALTH_ALL_ACK)")
        idle_finish = extract_block(service, "private void scheduleHealthIdleFinish(")
        request = extract_block(service, "private RingHealthSnapshot requestComprehensiveSnapshot(")

        self.assertIn("healthPayloadVersion++", ack)
        self.assertIn("scheduleHealthIdleFinish(", ack)
        self.assertIn("mainHandler.postDelayed", idle_finish)
        self.assertIn("requestGeneration != healthRequestGeneration", idle_finish)
        self.assertIn("payloadVersion != healthPayloadVersion", idle_finish)
        self.assertIn('finishHealthHistory("idle")', idle_finish)
        self.assertRegex(
            request,
            r"\w+Latch\.await\(HEALTH_HISTORY_TIMEOUT_SECONDS, TimeUnit\.SECONDS\)",
        )


if __name__ == "__main__":
    unittest.main()
