package com.aion.chat.homecoming;

import android.content.Context;
import android.content.SharedPreferences;
import android.database.Cursor;
import android.database.sqlite.SQLiteDatabase;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;

final class HomecomingRuntime {
    private final HomecomingDatabase database;
    private final HomecomingRouteVault vault;
    private final HomecomingChatRepository chats;
    private final HomecomingMemoryRepository memories;
    private final HomecomingChatEngine engine;
    private final java.util.concurrent.ConcurrentHashMap<
            String, HomecomingGroupTurnCoordinator> groupTurns =
            new java.util.concurrent.ConcurrentHashMap<>();
    private final HomecomingModelGateway gateway;
    private final HomecomingIdentityRepository identities;
    private final HomecomingContextBuilder contextBuilder;
    private final HomecomingTtsEngine tts;
    private final HomecomingScheduleRepository schedules;
    private final HomecomingAlarmRegistrar alarms;
    private final HomecomingScheduleCommandHandler scheduleCommands;
    private final HomecomingSupervisionAdapter supervision;
    private final HomecomingSupervisionRepository supervisionEvents;
    private final HomecomingSupervisionCommandHandler supervisionCommands;
    private final HomecomingControlDispatcher controlDispatcher;
    private final HomecomingNotificationController notifications;
    private final SharedPreferences preferences;
    private final String epochId;
    private final String deviceId;

    HomecomingRuntime(Context context, HomecomingModeStore modeStore) throws Exception {
        if (!modeStore.isActive()) {
            throw new IllegalStateException("Homecoming mode is inactive");
        }
        database = new HomecomingDatabase(context);
        deviceId = HomecomingBackupScheduler.getOrCreateDeviceId(context);
        epochId = modeStore.currentEpoch();
        HomecomingKeyStore keys = new HomecomingKeyStore(context, deviceId);
        vault = keys.openRouteVault();
        chats = new HomecomingChatRepository(database, epochId, deviceId);
        memories = new HomecomingMemoryRepository(database, epochId, deviceId);
        identities = new HomecomingIdentityRepository(database);
        supervision = new HomecomingSupervisionAdapter(context);
        supervisionEvents = new HomecomingSupervisionRepository(
                database, epochId, deviceId);
        contextBuilder = new HomecomingContextBuilder(
                identities,
                memories,
                chats::listMessages,
                48_000,
                new HomecomingSupervisionContext(
                        supervision));
        gateway = new HomecomingModelGateway(
                vault, new HomecomingModelGateway.OkHttpSseTransport());
        schedules = new HomecomingScheduleRepository(database, epochId, deviceId);
        alarms = new HomecomingAlarmRegistrar(context, epochId);
        scheduleCommands = new HomecomingScheduleCommandHandler(schedules, alarms);
        supervisionCommands = new HomecomingSupervisionCommandHandler(
                supervision::applyCommand, epochId);
        controlDispatcher = new HomecomingControlDispatcher(
                scheduleCommands::apply,
                supervisionCommands::apply,
                new HomecomingControlDispatcher.ResultPort() {
                    @Override public void defer(
                            String requestId,
                            HomecomingControlParser.ControlEvent event,
                            long now) {
                        chats.recordDeferredControls(
                                requestId,
                                java.util.Collections.singletonList(event),
                                now);
                    }

                    @Override public void system(
                            String resultRequestId,
                            String timelineId,
                            String text,
                            long now) {
                        chats.commitSystemMessage(
                                resultRequestId, timelineId, text, now);
                    }
                },
                (timelineId, ownerId) -> identities
                        .identityForTimeline(timelineId, ownerId)
                        .companionName);
        engine = new HomecomingChatEngine(
                chats,
                contextBuilder,
                gateway,
                controlDispatcher);
        HomecomingMediaCache media = new HomecomingMediaCache(
                new File(context.getCacheDir(), "homecoming-media"));
        tts = new HomecomingTtsEngine(
                new HomecomingTtsNetworkSynthesizer(vault),
                new HomecomingAudioPlayer(media),
                180);
        notifications = new HomecomingNotificationController(context);
        preferences = context.getSharedPreferences(
                HomecomingModeStore.PREFERENCES_NAME, Context.MODE_PRIVATE);
        new HomecomingScheduleReconciler(
                schedules, alarms).reconcile(System.currentTimeMillis());
    }

    String bootstrapJson() throws Exception {
        HomecomingIdentityRepository identities = new HomecomingIdentityRepository(database);
        HomecomingIdentityRepository.Identity main =
                identities.identityForTimeline("main_private", "main");
        HomecomingIdentityRepository.Identity second =
                identities.identityForTimeline("companion_private", "second");
        JSONArray routes = new JSONArray();
        for (HomecomingKeyStore.RouteDescriptor descriptor : vault.listDescriptors()) {
            routes.put(descriptor.toJson());
        }
        JSONObject identity = new JSONObject()
                .put("userName", main.userName)
                .put("mainName", main.companionName)
                .put("secondName", second.companionName);
        JSONObject routePreferences = new JSONObject();
        for (String ownerId : new String[]{"main", "second"}) {
            routePreferences.put(ownerId, new JSONObject()
                    .put("routeId", preferences.getString(
                            "background_route_" + ownerId, ""))
                    .put("modelId", preferences.getString(
                            "background_model_" + ownerId, "")));
        }
        return new JSONObject()
                .put("identity", identity)
                .put("routes", routes)
                .put("routePreferences", routePreferences)
                .put("ttsEnabled", preferences.getBoolean("homecoming_tts_enabled", true))
                .put("scheduleExactness", alarms.exactness())
                .put("pendingScheduleCount", schedules.listActive().size())
                .toString();
    }

    void setRoutePreference(
            String ownerId, String routeId, String modelId) {
        if (!"main".equals(ownerId) && !"second".equals(ownerId)) {
            throw new IllegalArgumentException("unsupported route owner");
        }
        vault.resolve(routeId).model(modelId);
        preferences.edit()
                .putString("background_route_" + ownerId, routeId)
                .putString("background_model_" + ownerId, modelId)
                .apply();
    }

    String supervisionStatusJson() throws Exception {
        HomecomingIdentityRepository.Identity main =
                identities.identityForTimeline("main_private", "main");
        HomecomingIdentityRepository.Identity second =
                identities.identityForTimeline("companion_private", "second");
        return supervisionStatus(
                supervision.snapshot(),
                main.companionName,
                second.companionName).toString();
    }

    static JSONObject supervisionStatus(
            HomecomingSupervisionAdapter.Snapshot snapshot,
            String mainName,
            String secondName) throws Exception {
        JSONObject output = new JSONObject()
                .put("enabled", snapshot != null && snapshot.enabled)
                .put("readiness", snapshot == null
                        ? "unavailable" : snapshot.readiness);
        JSONArray projected = new JSONArray();
        JSONArray groups = snapshot == null
                ? null : snapshot.state.optJSONArray("groups");
        if (groups != null) {
            for (int index = 0; index < groups.length(); index++) {
                JSONObject group = groups.optJSONObject(index);
                if (group == null) continue;
                String roleId = group.optString("roleId", "")
                        .trim().toLowerCase();
                String roleLabel = "connor".equals(roleId)
                        ? secondName
                        : "aion".equals(roleId) ? mainName : "";
                JSONArray checkpoints = group.optJSONArray(
                        "checkpointsMinutes");
                projected.put(new JSONObject()
                        .put("groupId", group.optString("groupId", ""))
                        .put("displayName", group.optString("displayName", ""))
                        .put("roleLabel", roleLabel == null ? "" : roleLabel)
                        .put("roundUsageMs", group.optLong("roundUsageMs", 0L))
                        .put("checkpointsMinutes", checkpoints == null
                                ? new JSONArray() : new JSONArray(
                                        checkpoints.toString()))
                        .put("effectiveState",
                                group.optString("effectiveState", "UNKNOWN"))
                        .put("lockActive", !group.isNull("lock"))
                        .put("temporaryUnlockActive",
                                !group.isNull("temporaryUnlock")));
            }
        }
        output.put("groups", projected);
        return output;
    }

    String messagesJson(String timelineId, long beforeCreatedAt, int limit) throws Exception {
        validateTimeline(timelineId);
        int bounded = Math.max(1, Math.min(200, limit));
        long before = beforeCreatedAt <= 0 ? Long.MAX_VALUE : beforeCreatedAt;
        ArrayList<MessageProjection> rows = new ArrayList<>();
        SQLiteDatabase readable = database.getReadableDatabase();
        try (Cursor cursor = readable.rawQuery(
                "SELECT payload_json FROM message_snapshot WHERE timeline_id=?",
                new String[]{timelineId})) {
            while (cursor.moveToNext()) {
                JSONObject value = new JSONObject(cursor.getString(0));
                long createdAt = (long) value.optDouble("created_at", 0);
                if (createdAt >= before) continue;
                String sender = value.optString("sender", "");
                rows.add(new MessageProjection(
                        value.optString("id", ""),
                        sender,
                        value.optString("content", ""),
                        createdAt,
                        !"user".equals(sender)));
            }
        }
        try (Cursor cursor = readable.rawQuery(
                "SELECT id,sender_id,text_content,created_at,role FROM chat_message "
                        + "WHERE timeline_id=? AND epoch_id=? AND created_at<?",
                new String[]{timelineId, epochId, String.valueOf(before)})) {
            while (cursor.moveToNext()) {
                rows.add(new MessageProjection(
                        cursor.getString(0),
                        cursor.getString(1),
                        cursor.getString(2),
                        cursor.getLong(3),
                        isAssistantProjection(cursor.getString(4))));
            }
        }
        rows.sort(Comparator.comparingLong(row -> row.createdAt));
        if (rows.size() > bounded) {
            rows.subList(0, rows.size() - bounded).clear();
        }
        JSONArray output = new JSONArray();
        for (MessageProjection row : rows) output.put(row.toJson());
        return output.toString();
    }

    void send(String requestId, String timelineId, String responderOwner,
            String text, String routeId, String modelId, String imageDataUrl,
            EventSink sink) {
        validateTimeline(timelineId);
        vault.resolve(routeId).model(modelId);
        if ("group".equals(timelineId)) {
            sendGroup(
                    requestId, responderOwner, text,
                    routeId, modelId, imageDataUrl, sink);
            return;
        }
        preferences.edit()
                .putString("background_route_" + responderOwner, routeId)
                .putString("background_model_" + responderOwner, modelId)
                .apply();
        engine.send(new HomecomingChatEngine.ChatCommand(
                requestId, timelineId, responderOwner, "user", text,
                routeId, modelId, imageDataUrl, ""), new HomecomingChatEngine.Observer() {
            @Override public void onChunk(String chunk) {
                sink.emit(event("chunk", requestId, chunk, ""));
            }
            @Override public void onComplete(String messageId, String completeText) {
                sink.emit(event("complete", requestId, completeText, messageId));
                if (preferences.getBoolean("homecoming_tts_enabled", true)) {
                    try {
                        HomecomingRouteVault.Service service = vault.resolveService("tts");
                        String voice = "second".equals(responderOwner)
                                ? service.secondVoice : service.mainVoice;
                        tts.enqueue(timelineId, messageId, voice, completeText);
                    } catch (RuntimeException ignored) {
                    }
                }
            }
            @Override public void onFailure(String code) {
                sink.emit(event("failure", requestId, "", code));
            }
        });
    }

    private void sendGroup(
            String requestId,
            String selectedOwner,
            String text,
            String routeId,
            String modelId,
            String imageDataUrl,
            EventSink sink) {
        preferences.edit()
                .putString("background_route_" + selectedOwner, routeId)
                .putString("background_model_" + selectedOwner, modelId)
                .putString("group_fallback_route", routeId)
                .putString("group_fallback_model", modelId)
                .putString("group_pending_text_" + requestId, text)
                .putString("group_pending_image_" + requestId, imageDataUrl)
                .apply();
        HomecomingGroupTurnCoordinator coordinator =
                new HomecomingGroupTurnCoordinator(
                        identities.groupReplyOrder(requestId),
                        new HomecomingGroupTurnCoordinator.ReplyPort() {
                            @Override public void start(
                                    String parentRequestId,
                                    String childRequestId,
                                    String ownerId,
                                    boolean commitUser,
                                    HomecomingGroupTurnCoordinator.ReplyCompletion completion) {
                                RouteChoice route = groupRoute(
                                        ownerId, routeId, modelId);
                                engine.send(new HomecomingChatEngine.ChatCommand(
                                        childRequestId, "group", ownerId, "user",
                                        text, route.routeId, route.modelId,
                                        commitUser ? imageDataUrl : "", "",
                                        commitUser),
                                        new HomecomingChatEngine.Observer() {
                                    @Override public void onChunk(String chunk) {
                                        sink.emit(ownerEvent(
                                                "group_chunk", requestId,
                                                ownerId, chunk, ""));
                                    }
                                    @Override public void onComplete(
                                            String messageId, String completeText) {
                                        playTts(
                                                "group", ownerId,
                                                messageId, completeText);
                                        completion.onComplete(
                                                messageId, completeText);
                                    }
                                    @Override public void onFailure(String code) {
                                        completion.onFailure(code);
                                    }
                                });
                            }

                            @Override public void cancel(String parentRequestId) {
                                engine.stop(parentRequestId);
                                engine.stop(parentRequestId + ":main");
                                engine.stop(parentRequestId + ":second");
                            }
                        });
        groupTurns.put(requestId, coordinator);
        coordinator.start(requestId, new HomecomingGroupTurnCoordinator.Observer() {
            @Override public void onReplyComplete(
                    String ownerId, String messageId, String completeText) {
                sink.emit(ownerEvent(
                        "group_reply_complete", requestId,
                        ownerId, completeText, messageId));
            }
            @Override public void onReplyFailure(String ownerId, String code) {
                sink.emit(ownerEvent(
                        "group_reply_failure", requestId,
                        ownerId, "", code));
            }
            @Override public void onTurnComplete() {
                groupTurns.remove(requestId);
                preferences.edit()
                        .remove("group_pending_text_" + requestId)
                        .remove("group_pending_image_" + requestId)
                        .apply();
                sink.emit(event("group_complete", requestId, "", ""));
            }
        });
    }

    private RouteChoice groupRoute(
            String ownerId, String fallbackRoute, String fallbackModel) {
        String preferredRoute = preferences.getString(
                "background_route_" + ownerId, "");
        String preferredModel = preferences.getString(
                "background_model_" + ownerId, "");
        if (preferredRoute != null && !preferredRoute.isEmpty()
                && preferredModel != null && !preferredModel.isEmpty()) {
            try {
                vault.resolve(preferredRoute).model(preferredModel);
                return new RouteChoice(preferredRoute, preferredModel);
            } catch (RuntimeException ignored) {
            }
        }
        try {
            vault.resolve(fallbackRoute).model(fallbackModel);
            return new RouteChoice(fallbackRoute, fallbackModel);
        } catch (RuntimeException ignored) {
            return backgroundRoute(ownerId);
        }
    }

    private void playTts(
            String timelineId, String ownerId,
            String messageId, String text) {
        if (!preferences.getBoolean("homecoming_tts_enabled", true)) return;
        try {
            HomecomingRouteVault.Service service = vault.resolveService("tts");
            String voice = "second".equals(ownerId)
                    ? service.secondVoice : service.mainVoice;
            tts.enqueue(timelineId, messageId, voice, text);
        } catch (RuntimeException ignored) {
        }
    }

    void fireSchedule(
            String scheduleId,
            long triggerAt,
            HomecomingTriggerEngine.Completion completion) {
        HomecomingScheduleRepository.Schedule schedule = schedules.find(scheduleId);
        if (schedule == null) {
            completion.onFailure("unknown_schedule");
            return;
        }
        RouteChoice route;
        try {
            route = backgroundRoute(schedule.ownerId);
        } catch (RuntimeException exception) {
            completion.onFailure("no_portable_route");
            return;
        }
        HomecomingTriggerEngine triggerEngine = new HomecomingTriggerEngine(
                new HomecomingTriggerEngine.SchedulePort() {
                    @Override public HomecomingScheduleRepository.Schedule find(String id) {
                        return schedules.find(id);
                    }
                    @Override public HomecomingScheduleRepository.ExecutionClaim claim(
                            String id, long at, long now) {
                        return schedules.claimExecution(id, at, now);
                    }
                    @Override public void complete(
                            String executionId, String messageId, long now) {
                        schedules.completeExecution(executionId, messageId, now);
                    }
                    @Override public void fail(
                            String executionId, String diagnostic, long now) {
                        schedules.failExecution(executionId, diagnostic, now);
                    }
                },
                (value, trigger, now) -> contextBuilder.build(
                        value.timelineId, value.ownerId, trigger, now, ""),
                (requestId, routeId, modelId, messages, observer) -> {
                    try {
                        gateway.stream(new HomecomingModelGateway.ChatRequest(
                                requestId, routeId, modelId, messages, ""), observer);
                    } catch (Exception exception) {
                        throw new IllegalStateException(
                                "background model request failed", exception);
                    }
                },
                (value, executionId, completeText, now) ->
                        chats.commitAssistantMessage(
                                "schedule:" + executionId,
                                value.timelineId,
                                value.ownerId,
                                completeText,
                                now).id,
                (executionId, value, controls, now) -> {
                    controlDispatcher.apply(
                            "schedule:" + executionId,
                            value.timelineId,
                            value.ownerId,
                            controls,
                            now);
                },
                System::currentTimeMillis);
        triggerEngine.execute(
                scheduleId,
                triggerAt,
                route.routeId,
                route.modelId,
                new HomecomingTriggerEngine.Completion() {
                    @Override public void onComplete(String messageId, String text) {
                        try {
                            HomecomingIdentityRepository.Identity identity =
                                    identities.identityForTimeline(
                                            schedule.timelineId, schedule.ownerId);
                            notifications.post(
                                    schedule, identity.companionName, text);
                        } catch (RuntimeException ignored) {
                        }
                        if (preferences.getBoolean("homecoming_tts_enabled", true)) {
                            try {
                                HomecomingRouteVault.Service service =
                                        vault.resolveService("tts");
                                String voice = "second".equals(schedule.ownerId)
                                        ? service.secondVoice : service.mainVoice;
                                tts.enqueue(
                                        schedule.timelineId, messageId, voice, text);
                            } catch (RuntimeException ignored) {
                            }
                        }
                        completion.onComplete(messageId, text);
                    }
                    @Override public void onFailure(String code) {
                        completion.onFailure(code);
                    }
                    @Override public void onDuplicate() {
                        completion.onDuplicate();
                    }
                });
    }

    void fireSupervisionEvent(
            String eventId,
            HomecomingSupervisionTriggerEngine.Completion completion) {
        HomecomingSupervisionRepository.Event event =
                supervisionEvents.find(eventId);
        if (event == null) {
            completion.onFailure("unknown_supervision_event");
            return;
        }
        String ownerId = "connor".equalsIgnoreCase(event.roleId)
                ? "second" : "main";
        String timelineId = "second".equals(ownerId)
                ? "companion_private" : "main_private";
        RouteChoice route;
        try {
            route = backgroundRoute(ownerId);
        } catch (RuntimeException exception) {
            completion.onFailure("no_portable_route");
            return;
        }
        HomecomingSupervisionTriggerEngine triggerEngine =
                new HomecomingSupervisionTriggerEngine(
                        new HomecomingSupervisionTriggerEngine.EventPort() {
                            @Override public HomecomingSupervisionRepository.Event find(
                                    String id) {
                                return supervisionEvents.find(id);
                            }
                            @Override public HomecomingSupervisionRepository.Claim claim(
                                    String id, long now) {
                                return supervisionEvents.claim(id, now);
                            }
                            @Override public void complete(
                                    String id, String messageId,
                                    String resultText, long now) {
                                supervisionEvents.complete(
                                        id, messageId, resultText, now);
                            }
                            @Override public void fail(
                                    String id, String diagnostic, long now) {
                                supervisionEvents.fail(id, diagnostic, now);
                            }
                        },
                        (value, targetOwner, targetTimeline, trigger, now) ->
                                contextBuilder.build(
                                        targetTimeline,
                                        targetOwner,
                                        trigger,
                                        now,
                                        value.payloadJson),
                        (requestId, routeId, modelId, messages, observer) -> {
                            try {
                                gateway.stream(
                                        new HomecomingModelGateway.ChatRequest(
                                                requestId,
                                                routeId,
                                                modelId,
                                                messages,
                                                ""),
                                        observer);
                            } catch (Exception exception) {
                                throw new IllegalStateException(
                                        "background model request failed", exception);
                            }
                        },
                        (value, targetOwner, targetTimeline, text, now) ->
                                chats.commitAssistantMessage(
                                        "supervision:" + value.eventId,
                                        targetTimeline,
                                        targetOwner,
                                        text,
                                        now).id,
                        (value, targetOwner, targetTimeline, controls, now) ->
                                controlDispatcher.apply(
                                        "supervision:" + value.eventId,
                                        targetTimeline,
                                        targetOwner,
                                        controls,
                                        now),
                        System::currentTimeMillis);
        triggerEngine.execute(
                eventId,
                route.routeId,
                route.modelId,
                new HomecomingSupervisionTriggerEngine.Completion() {
                    @Override public void onComplete(String messageId, String text) {
                        try {
                            String configuredName = identities.identityForTimeline(
                                    timelineId, ownerId).companionName;
                            notifications.post(event.eventId, configuredName, text);
                        } catch (RuntimeException ignored) {
                        }
                        if (preferences.getBoolean("homecoming_tts_enabled", true)) {
                            try {
                                HomecomingRouteVault.Service service =
                                        vault.resolveService("tts");
                                String voice = "second".equals(ownerId)
                                        ? service.secondVoice : service.mainVoice;
                                tts.enqueue(timelineId, messageId, voice, text);
                            } catch (RuntimeException ignored) {
                            }
                        }
                        completion.onComplete(messageId, text);
                    }

                    @Override public void onFailure(String code) {
                        completion.onFailure(code);
                    }

                    @Override public void onDuplicate() {
                        completion.onDuplicate();
                    }
                });
    }

    void summarizeMemories(
            String ownerId,
            String routeId,
            String fallbackModelId,
            EventSink sink) {
        try {
            String modelId = configuredSummaryModel(ownerId, routeId, fallbackModelId);
            HomecomingMemorySummarizer summarizer =
                    HomecomingMemorySummarizer.forRuntime(
                            database, identities, gateway, vault,
                            epochId, deviceId, routeId, modelId);
            summarizer.summarizeOwner(ownerId, System.currentTimeMillis(), result ->
                    sink.emit(event(
                            "summary_result",
                            "",
                            result.status,
                            String.valueOf(result.createdMemories))));
        } catch (Exception exception) {
            sink.emit(event("summary_result", "", "summary_failed", "0"));
        }
    }

    void summarizeAllMemories(EventSink sink) {
        JSONObject results = new JSONObject();
        for (String ownerId : new String[]{"main", "second"}) {
            try {
                RouteChoice route = backgroundRoute(ownerId);
                final HomecomingMemorySummarizer.Result[] captured = {null};
                String modelId = configuredSummaryModel(
                        ownerId, route.routeId, route.modelId);
                HomecomingMemorySummarizer summarizer =
                        HomecomingMemorySummarizer.forRuntime(
                                database, identities, gateway, vault,
                                epochId, deviceId, route.routeId, modelId);
                summarizer.summarizeOwner(
                        ownerId, System.currentTimeMillis(),
                        value -> captured[0] = value);
                HomecomingMemorySummarizer.Result value = captured[0];
                results.put(ownerId, value == null
                        ? summaryResultJson("summary_failed", 0, 0)
                        : summaryResultJson(
                                value.status,
                                value.processedMessages,
                                value.createdMemories));
            } catch (Exception exception) {
                try {
                    results.put(ownerId, summaryResultJson(
                            "summary_failed", 0, 0));
                } catch (Exception ignored) {
                }
            }
        }
        try {
            sink.emit(new JSONObject()
                    .put("type", "summary_all_result")
                    .put("results", results));
        } catch (Exception exception) {
            sink.emit(event("summary_all_result", "", "summary_failed", "0"));
        }
    }

    private static JSONObject summaryResultJson(
            String status, int processed, int created) throws Exception {
        return new JSONObject()
                .put("status", status)
                .put("processed", processed)
                .put("created", created);
    }

    void stop(String requestId) {
        HomecomingGroupTurnCoordinator group = groupTurns.remove(requestId);
        if (group != null) group.stop(requestId);
        engine.stop(requestId);
    }

    void freeze() {
        gateway.cancelAll();
        for (String timeline : new String[]{
                "main_private", "companion_private", "group"}) {
            tts.cancelConversation(timeline);
        }
    }

    void setTtsEnabled(boolean enabled) {
        preferences.edit().putBoolean("homecoming_tts_enabled", enabled).apply();
        if (!enabled) {
            for (String timeline : new String[]{
                    "main_private", "companion_private", "group"}) {
                tts.cancelConversation(timeline);
            }
        }
    }

    void replayTts(String messageId) {
        SQLiteDatabase readable = database.getReadableDatabase();
        try (Cursor cursor = readable.rawQuery(
                "SELECT timeline_id,sender_id,text_content FROM chat_message WHERE id=?",
                new String[]{messageId})) {
            if (!cursor.moveToFirst()) return;
            HomecomingRouteVault.Service service = vault.resolveService("tts");
            String sender = cursor.getString(1);
            String voice = "second".equals(sender)
                    ? service.secondVoice : service.mainVoice;
            tts.enqueue(cursor.getString(0), messageId, voice, cursor.getString(2));
        }
    }

    String memoriesJson(String owner, String query) throws Exception {
        JSONArray values = new JSONArray();
        for (HomecomingMemoryRepository.Memory memory : memories.recall(owner, query, 100)) {
            values.put(memoryJson(memory));
        }
        return values.toString();
    }

    String createMemory(String owner, String content, String keywords) throws Exception {
        return memoryJson(memories.create(
                owner, content, keywords, System.currentTimeMillis())).toString();
    }

    String updateMemory(
            String owner, String id, String content, String baseHash) throws Exception {
        return memoryJson(memories.update(
                owner, id, content, baseHash, System.currentTimeMillis())).toString();
    }

    boolean deleteMemory(String owner, String id, String baseHash) {
        memories.delete(owner, id, baseHash, System.currentTimeMillis());
        return true;
    }

    String schedulesJson() throws Exception {
        JSONArray values = new JSONArray();
        for (HomecomingScheduleRepository.Schedule schedule : schedules.listActive()) {
            values.put(scheduleJson(schedule));
        }
        return values.toString();
    }

    String createSchedule(
            String type,
            long triggerAt,
            String content,
            String ownerId,
            String timelineId) throws Exception {
        HomecomingScheduleRepository.Schedule created = schedules.create(
                type,
                triggerAt,
                content,
                ownerId,
                timelineId,
                System.currentTimeMillis());
        String registrationStatus = "registered";
        try {
            alarms.register(created);
        } catch (RuntimeException exception) {
            registrationStatus = "registration_failed";
        }
        return scheduleJson(created)
                .put("registrationStatus", registrationStatus)
                .toString();
    }

    boolean deleteSchedule(String id) {
        schedules.delete(id, System.currentTimeMillis());
        try {
            alarms.cancel(id);
        } catch (RuntimeException ignored) {
        }
        return true;
    }

    private static JSONObject scheduleJson(
            HomecomingScheduleRepository.Schedule schedule) throws Exception {
        return new JSONObject()
                .put("id", schedule.id)
                .put("type", schedule.type)
                .put("triggerAt", schedule.triggerAt)
                .put("content", schedule.content)
                .put("ownerId", schedule.ownerId)
                .put("timelineId", schedule.timelineId)
                .put("status", schedule.status)
                .put("updatedAt", schedule.updatedAt);
    }

    private static JSONObject memoryJson(HomecomingMemoryRepository.Memory memory)
            throws Exception {
        return new JSONObject()
                .put("id", memory.id)
                .put("ownerId", memory.ownerId)
                .put("content", memory.content)
                .put("keywords", memory.keywords)
                .put("baseHash", memory.baseHash)
                .put("updatedAt", memory.updatedAt);
    }

    private static JSONObject event(
            String type, String requestId, String text, String value) {
        try {
            return new JSONObject()
                    .put("type", type)
                    .put("requestId", requestId)
                    .put("text", text)
                    .put("value", value);
        } catch (Exception exception) {
            throw new IllegalStateException("could not encode runtime event", exception);
        }
    }

    private static JSONObject ownerEvent(
            String type, String requestId, String ownerId,
            String text, String value) {
        try {
            return event(type, requestId, text, value)
                    .put("ownerId", ownerId);
        } catch (Exception exception) {
            throw new IllegalStateException("could not encode owner event", exception);
        }
    }

    private String configuredSummaryModel(
            String ownerId, String routeId, String fallbackModelId) {
        String configured = "";
        try (Cursor cursor = database.getReadableDatabase().rawQuery(
                "SELECT payload_json FROM runtime_snapshot WHERE key='runtime'", null)) {
            if (cursor.moveToFirst()) {
                JSONObject runtimeState = new JSONObject(cursor.getString(0));
                JSONObject settings = runtimeState.optJSONObject("settings");
                if (settings != null) {
                    configured = settings.optString(
                            "main".equals(ownerId)
                                    ? "main_memory_model" : "memory_model",
                            "");
                }
            }
        } catch (Exception ignored) {
        }
        if (!configured.isEmpty()) {
            try {
                vault.resolve(routeId).model(configured);
                return configured;
            } catch (RuntimeException ignored) {
            }
        }
        vault.resolve(routeId).model(fallbackModelId);
        return fallbackModelId;
    }

    private RouteChoice backgroundRoute(String ownerId) {
        String preferredRoute = preferences.getString(
                "background_route_" + ownerId, "");
        String preferredModel = preferences.getString(
                "background_model_" + ownerId, "");
        if (preferredRoute != null && !preferredRoute.isEmpty()
                && preferredModel != null && !preferredModel.isEmpty()) {
            try {
                vault.resolve(preferredRoute).model(preferredModel);
                return new RouteChoice(preferredRoute, preferredModel);
            } catch (RuntimeException ignored) {
            }
        }
        for (HomecomingKeyStore.RouteDescriptor descriptor : vault.listDescriptors()) {
            if (!descriptor.modelKeys.isEmpty()) {
                return new RouteChoice(
                        descriptor.routeId, descriptor.modelKeys.get(0));
            }
        }
        throw new IllegalStateException("no portable Homecoming route");
    }

    private static void validateTimeline(String value) {
        if (!"main_private".equals(value)
                && !"companion_private".equals(value)
                && !"group".equals(value)) {
            throw new IllegalArgumentException("unsupported timeline");
        }
    }

    static boolean isAssistantProjection(String role) {
        return !"user".equals(role);
    }

    interface EventSink {
        void emit(JSONObject event);
    }

    private static final class RouteChoice {
        final String routeId;
        final String modelId;
        RouteChoice(String routeId, String modelId) {
            this.routeId = routeId;
            this.modelId = modelId;
        }
    }

    private static final class MessageProjection {
        final String id;
        final String sender;
        final String text;
        final long createdAt;
        final boolean assistant;
        MessageProjection(String id, String sender, String text,
                long createdAt, boolean assistant) {
            this.id = id;
            this.sender = sender;
            this.text = text;
            this.createdAt = createdAt;
            this.assistant = assistant;
        }
        JSONObject toJson() throws Exception {
            return new JSONObject()
                    .put("id", id)
                    .put("sender", sender)
                    .put("text", text)
                    .put("createdAt", createdAt)
                    .put("assistant", assistant);
        }
    }
}
