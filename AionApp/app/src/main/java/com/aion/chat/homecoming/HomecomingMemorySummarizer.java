package com.aion.chat.homecoming;

import android.content.ContentValues;
import android.database.Cursor;
import android.database.sqlite.SQLiteDatabase;
import android.util.Base64;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.IOException;
import java.text.SimpleDateFormat;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashSet;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;
import java.util.Date;
import java.util.Locale;
import java.util.UUID;

import okhttp3.MediaType;
import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.RequestBody;
import okhttp3.Response;

public final class HomecomingMemorySummarizer {
    private final MessageSource source;
    private final SummaryModel model;
    private final MemoryStore store;
    private final EmbeddingProvider embeddings;
    private final int minimumMessages;
    private final int maximumGroupSize;
    private final long idleMillis;
    private final Set<String> runningOwners = new HashSet<>();

    HomecomingMemorySummarizer(
            MessageSource source,
            SummaryModel model,
            MemoryStore store,
            EmbeddingProvider embeddings,
            int minimumMessages,
            int maximumGroupSize,
            long idleMillis) {
        if (minimumMessages < 1 || maximumGroupSize < minimumMessages) {
            throw new IllegalArgumentException("invalid summary group bounds");
        }
        if (idleMillis < 0) {
            throw new IllegalArgumentException("idleMillis must not be negative");
        }
        this.source = source;
        this.model = model;
        this.store = store;
        this.embeddings = embeddings;
        this.minimumMessages = minimumMessages;
        this.maximumGroupSize = maximumGroupSize;
        this.idleMillis = idleMillis;
    }

    static HomecomingMemorySummarizer forRuntime(
            HomecomingDatabase database,
            HomecomingIdentityRepository identities,
            HomecomingModelGateway gateway,
            HomecomingRouteVault vault,
            String epochId,
            String deviceId,
            String routeId,
            String modelKey) {
        SummaryModel summaryModel = (ownerId, messages) -> {
            String timeline = "main".equals(ownerId)
                    ? "main_private" : "companion_private";
            HomecomingIdentityRepository.Identity identity =
                    identities.identityForTimeline(timeline, ownerId);
            String prompt = buildPrompt(
                    identity.companionName,
                    identity.userName,
                    identity.companionPersona,
                    messages);
            final String[] complete = {null};
            final String[] failure = {null};
            gateway.stream(new HomecomingModelGateway.ChatRequest(
                    "summary-" + UUID.randomUUID(),
                    routeId,
                    modelKey,
                    Collections.singletonList(
                            new HomecomingModelGateway.ChatMessage("user", prompt)),
                    ""), new HomecomingModelGateway.StreamObserver() {
                @Override public void onChunk(String text) {
                }
                @Override public void onComplete(String text) {
                    complete[0] = text;
                }
                @Override public void onFailure(String code) {
                    failure[0] = code;
                }
            });
            if (failure[0] != null || complete[0] == null) {
                throw new IOException(
                        failure[0] == null ? "summary model returned no result" : failure[0]);
            }
            return complete[0];
        };
        return new HomecomingMemorySummarizer(
                new SqliteMessageSource(database, epochId),
                summaryModel,
                new SqliteMemoryStore(database, epochId, deviceId),
                new NativeEmbeddingProvider(vault),
                1,
                50,
                0L);
    }

    public void summarizeOwner(String ownerId, long now, Completion completion) {
        String owner = validateOwner(ownerId);
        synchronized (runningOwners) {
            if (!runningOwners.add(owner)) {
                completion.onComplete(new Result("already_running", 0, 0, store.anchor(owner)));
                return;
            }
        }
        try {
            summarize(owner, now, completion);
        } finally {
            synchronized (runningOwners) {
                runningOwners.remove(owner);
            }
        }
    }

    private void summarize(String owner, long now, Completion completion) {
        String anchor = store.anchor(owner);
        List<SourceMessage> messages = source.load(owner, anchor);
        if (messages.size() < minimumMessages) {
            completion.onComplete(new Result(
                    "minimum_not_met", 0, 0, anchor));
            return;
        }
        SourceMessage newest = messages.get(messages.size() - 1);
        if (now - newest.createdAt < idleMillis) {
            completion.onComplete(new Result("idle_not_met", 0, 0, anchor));
            return;
        }

        int processed = 0;
        int created = 0;
        for (List<SourceMessage> group : balancedGroups(messages)) {
            try {
                List<MemoryDraft> drafts = parse(
                        owner, model.summarize(owner, group), group);
                store.commitGroup(
                        owner, group.get(group.size() - 1).id,
                        messageIds(group), drafts, now);
                processed += group.size();
                created += drafts.size();
                anchor = group.get(group.size() - 1).id;
            } catch (Exception exception) {
                completion.onComplete(new Result(
                        "summary_failed", processed, created, anchor));
                return;
            }
        }
        completion.onComplete(new Result("complete", processed, created, anchor));
    }

    private List<List<SourceMessage>> balancedGroups(List<SourceMessage> messages) {
        if (messages.size() <= maximumGroupSize) {
            return Collections.singletonList(messages);
        }
        int groupCount = (messages.size() + maximumGroupSize - 1) / maximumGroupSize;
        int baseSize = messages.size() / groupCount;
        int largerGroups = messages.size() % groupCount;
        ArrayList<List<SourceMessage>> groups = new ArrayList<>();
        int offset = 0;
        for (int index = 0; index < groupCount; index++) {
            int size = baseSize + (index < largerGroups ? 1 : 0);
            groups.add(new ArrayList<>(messages.subList(offset, offset + size)));
            offset += size;
        }
        return groups;
    }

    private List<MemoryDraft> parse(
            String owner, String raw, List<SourceMessage> group) throws Exception {
        String json = extractJson(raw);
        JSONObject root = new JSONObject(json);
        JSONArray values = root.optJSONArray("memories");
        if (values == null) {
            throw new IllegalArgumentException("summary has no memories array");
        }
        Set<String> allowedIds = new HashSet<>();
        for (SourceMessage message : group) allowedIds.add(message.id);
        ArrayList<MemoryDraft> result = new ArrayList<>();
        for (int index = 0; index < values.length(); index++) {
            JSONObject value = values.getJSONObject(index);
            String content = required(value.optString("content", ""), "memory content");
            String type = value.optString("type", "daily");
            if (!"daily".equals(type) && !"important".equals(type)) {
                type = "daily";
            }
            JSONArray keywordValues = value.optJSONArray("keywords");
            ArrayList<String> keywords = new ArrayList<>();
            if (keywordValues != null) {
                for (int i = 0; i < keywordValues.length() && keywords.size() < 8; i++) {
                    String keyword = keywordValues.optString(i, "").trim();
                    if (!keyword.isEmpty() && !keywords.contains(keyword)) {
                        keywords.add(keyword);
                    }
                }
            }
            JSONArray sourceValues = value.optJSONArray("source_message_ids");
            LinkedHashSet<String> sourceIds = new LinkedHashSet<>();
            if (sourceValues != null) {
                for (int i = 0; i < sourceValues.length() && sourceIds.size() < 6; i++) {
                    String id = sourceValues.optString(i, "");
                    if (allowedIds.contains(id)) sourceIds.add(id);
                }
            }
            byte[] embedding;
            try {
                byte[] valueEmbedding = embeddings.embed(content);
                embedding = valueEmbedding == null ? new byte[0] : valueEmbedding;
            } catch (Exception ignored) {
                embedding = new byte[0];
            }
            result.add(new MemoryDraft(
                    owner,
                    content,
                    type,
                    keywords,
                    clamp(value.optDouble("importance", 0.4), 0.0, 1.0),
                    new ArrayList<>(sourceIds),
                    embedding));
        }
        return Collections.unmodifiableList(result);
    }

    private static String extractJson(String raw) {
        String value = raw == null ? "" : raw.trim();
        int start = value.indexOf('{');
        int end = value.lastIndexOf('}');
        if (start < 0 || end < start) {
            throw new IllegalArgumentException("summary is not JSON");
        }
        return value.substring(start, end + 1);
    }

    static String buildPrompt(
            String companionName,
            String userName,
            String persona,
            List<SourceMessage> messages) {
        StringBuilder transcript = new StringBuilder();
        SimpleDateFormat format =
                new SimpleDateFormat("yyyy-MM-dd HH:mm", Locale.ROOT);
        for (SourceMessage message : messages) {
            String sender = "user".equals(message.senderId)
                    ? userName : companionName;
            transcript.append('[')
                    .append(format.format(new Date(message.createdAt)))
                    .append("][id=")
                    .append(message.id)
                    .append("] ")
                    .append(sender)
                    .append(": ")
                    .append(message.text)
                    .append('\n');
        }
        return "你是" + companionName + "。以下人设只用于保持记忆视角：\n"
                + (persona == null ? "" : persona)
                + "\n请从你与" + userName + "的对话中提取可独立召回的原子记忆。"
                + "普通流水内容可以丢弃；不要添加原文没有的事实。"
                + "每条 content 使用绝对日期开头，type 仅可为 daily 或 important，"
                + "source_message_ids 只能引用下方真实 id，最多 6 个。"
                + "只输出 JSON：{\"memories\":[{\"content\":\"\","
                + "\"type\":\"daily\",\"keywords\":[],\"importance\":0.4,"
                + "\"source_message_ids\":[]}]}\n\n"
                + transcript;
    }

    private static double clamp(double value, double minimum, double maximum) {
        return Math.max(minimum, Math.min(maximum, value));
    }

    private static String validateOwner(String ownerId) {
        if (!"main".equals(ownerId) && !"second".equals(ownerId)) {
            throw new IllegalArgumentException("unsupported memory owner");
        }
        return ownerId;
    }

    private static String required(String value, String label) {
        if (value == null || value.trim().isEmpty()) {
            throw new IllegalArgumentException(label + " is required");
        }
        return value.trim();
    }

    interface MessageSource {
        List<SourceMessage> load(String ownerId, String afterMessageId);
    }

    interface SummaryModel {
        String summarize(String ownerId, List<SourceMessage> messages) throws Exception;
    }

    interface MemoryStore {
        String anchor(String ownerId);
        void commitGroup(
                String ownerId, String lastMessageId,
                List<String> sourceMessageIds,
                List<MemoryDraft> drafts, long now) throws Exception;
    }

    interface EmbeddingProvider {
        byte[] embed(String content) throws Exception;
    }

    public interface Completion {
        void onComplete(Result value);
    }

    public static final class SourceMessage {
        public final String id;
        public final String timelineId;
        public final String senderId;
        public final String text;
        public final long createdAt;

        public SourceMessage(
                String id, String timelineId, String senderId,
                String text, long createdAt) {
            this.id = required(id, "message id");
            this.timelineId = required(timelineId, "timeline id");
            this.senderId = required(senderId, "sender id");
            this.text = text == null ? "" : text;
            this.createdAt = createdAt;
        }
    }

    public static final class MemoryDraft {
        public final String ownerId;
        public final String content;
        public final String type;
        public final List<String> keywords;
        public final double importance;
        public final List<String> sourceMessageIds;
        public final byte[] embedding;

        MemoryDraft(
                String ownerId, String content, String type,
                List<String> keywords, double importance,
                List<String> sourceMessageIds, byte[] embedding) {
            this.ownerId = ownerId;
            this.content = content;
            this.type = type;
            this.keywords = Collections.unmodifiableList(new ArrayList<>(keywords));
            this.importance = importance;
            this.sourceMessageIds =
                    Collections.unmodifiableList(new ArrayList<>(sourceMessageIds));
            this.embedding = embedding.clone();
        }
    }

    public static final class Result {
        public final String status;
        public final int processedMessages;
        public final int createdMemories;
        public final String anchorMessageId;

        Result(
                String status, int processedMessages,
                int createdMemories, String anchorMessageId) {
            this.status = status;
            this.processedMessages = processedMessages;
            this.createdMemories = createdMemories;
            this.anchorMessageId = anchorMessageId;
        }
    }

    private static final class SqliteMessageSource implements MessageSource {
        private final HomecomingDatabase helper;
        private final String epochId;

        SqliteMessageSource(HomecomingDatabase helper, String epochId) {
            this.helper = helper;
            this.epochId = required(epochId, "epochId");
        }

        @Override public List<SourceMessage> load(
                String ownerId, String afterMessageId) {
            String privateTimeline = "main".equals(ownerId)
                    ? "main_private" : "companion_private";
            ArrayList<SourceMessage> all = new ArrayList<>();
            try (Cursor cursor = helper.getReadableDatabase().rawQuery(
                    "SELECT id,timeline_id,sender_id,text_content,created_at "
                            + "FROM chat_message WHERE epoch_id=? "
                            + "AND timeline_id IN (?,?) "
                            + "ORDER BY created_at,id",
                    new String[]{epochId, privateTimeline, "group"})) {
                while (cursor.moveToNext()) {
                    all.add(new SourceMessage(
                            cursor.getString(0),
                            cursor.getString(1),
                            cursor.getString(2),
                            cursor.getString(3),
                            cursor.getLong(4)));
                }
            }
            if (afterMessageId == null || afterMessageId.isEmpty()) return all;
            for (int index = 0; index < all.size(); index++) {
                if (afterMessageId.equals(all.get(index).id)) {
                    return new ArrayList<>(all.subList(index + 1, all.size()));
                }
            }
            return all;
        }
    }

    private static final class SqliteMemoryStore implements MemoryStore {
        private final HomecomingDatabase helper;
        private final String epochId;
        private final String deviceId;

        SqliteMemoryStore(
                HomecomingDatabase helper, String epochId, String deviceId) {
            this.helper = helper;
            this.epochId = required(epochId, "epochId");
            this.deviceId = required(deviceId, "deviceId");
        }

        @Override public String anchor(String ownerId) {
            try (Cursor cursor = helper.getReadableDatabase().rawQuery(
                    "SELECT last_message_id FROM summary_anchor "
                            + "WHERE epoch_id=? AND owner_id=?",
                    new String[]{epochId, ownerId})) {
                return cursor.moveToFirst() ? cursor.getString(0) : "";
            }
        }

        @Override public void commitGroup(
                String ownerId,
                String lastMessageId,
                List<String> sourceMessageIds,
                List<MemoryDraft> drafts,
                long now) throws Exception {
            SQLiteDatabase database = helper.getWritableDatabase();
            database.beginTransaction();
            try {
                long nextSequence = nextSequence(database);
                String previousMessageId = anchor(ownerId);
                ArrayList<String> memoryIds = new ArrayList<>();
                for (MemoryDraft draft : drafts) {
                    String memoryId = UUID.randomUUID().toString();
                    memoryIds.add(memoryId);
                    String payload = memoryPayload(memoryId, draft, now);
                    ContentValues row = new ContentValues();
                    row.put("id", memoryId);
                    row.put("owner_id", ownerId);
                    row.put("payload_json", payload);
                    row.put("base_hash", "");
                    row.put("tombstone", 0);
                    row.put("updated_at", now);
                    database.insertOrThrow("memory_local", null, row);

                    HomecomingOperationJournal.Operation operation =
                            automaticMemoryOperation(
                                    epochId, deviceId, memoryId, payload, now);
                    insertOperation(database, operation, nextSequence++);
                }
                ContentValues anchor = new ContentValues();
                anchor.put("epoch_id", epochId);
                anchor.put("owner_id", ownerId);
                anchor.put("last_message_id", lastMessageId);
                anchor.put("updated_at", now);
                database.insertWithOnConflict(
                        "summary_anchor", null, anchor, SQLiteDatabase.CONFLICT_REPLACE);
                HomecomingOperationJournal.Operation checkpoint =
                        summaryCheckpointOperation(
                                epochId, deviceId, ownerId, previousMessageId,
                                lastMessageId, sourceMessageIds, memoryIds, now);
                insertOperation(database, checkpoint, nextSequence);
                database.setTransactionSuccessful();
            } finally {
                database.endTransaction();
            }
        }

        private static String memoryPayload(
                String memoryId, MemoryDraft draft, long now) throws Exception {
            JSONArray keywords = new JSONArray();
            for (String keyword : draft.keywords) keywords.put(keyword);
            JSONArray sourceIds = new JSONArray();
            for (String sourceId : draft.sourceMessageIds) sourceIds.put(sourceId);
            JSONObject value = new JSONObject()
                    .put("id", memoryId)
                    .put("owner_id", draft.ownerId)
                    .put("content", draft.content)
                    .put("type", draft.type)
                    .put("keywords", join(draft.keywords))
                    .put("keyword_items", keywords)
                    .put("importance", draft.importance)
                    .put("source_message_ids", sourceIds)
                    .put("tombstone", false)
                    .put("updated_at", now);
            if (draft.embedding.length > 0) {
                value.put("embedding_base64",
                        Base64.encodeToString(draft.embedding, Base64.NO_WRAP));
            }
            return HomecomingSnapshotStore.canonicalJson(value);
        }

        private static String join(List<String> values) {
            StringBuilder result = new StringBuilder();
            for (String value : values) {
                if (result.length() > 0) result.append(',');
                result.append(value);
            }
            return result.toString();
        }

        private static long nextSequence(SQLiteDatabase database) {
            try (Cursor cursor = database.rawQuery(
                    "SELECT COALESCE(MAX(device_seq),0)+1 FROM operation_journal", null)) {
                return cursor.moveToFirst() ? cursor.getLong(0) : 1L;
            }
        }

        private static void insertOperation(
                SQLiteDatabase database,
                HomecomingOperationJournal.Operation operation,
                long sequence) {
            ContentValues row = new ContentValues();
            row.put("op_id", operation.opId);
            row.put("epoch_id", operation.epochId);
            row.put("device_id", operation.deviceId);
            row.put("device_seq", sequence);
            row.put("entity_type", operation.entityType);
            row.put("entity_id", operation.entityId);
            row.put("action", operation.action);
            row.put("base_revision", operation.baseRevision);
            row.put("payload_json", operation.payloadJson);
            row.put("created_at", operation.createdAt);
            database.insertOrThrow("operation_journal", null, row);
        }
    }

    static HomecomingOperationJournal.Operation automaticMemoryOperation(
            String epochId, String deviceId, String memoryId,
            String payload, long now) {
        return HomecomingOperationJournal.create(
                epochId, deviceId, "memory_auto", memoryId,
                "create", "", payload, now);
    }

    static HomecomingOperationJournal.Operation summaryCheckpointOperation(
            String epochId, String deviceId, String ownerId,
            String previousMessageId, String lastMessageId,
            List<String> sourceMessageIds, List<String> memoryIds, long now) {
        try {
            String checkpointId = UUID.randomUUID().toString();
            JSONObject core = new JSONObject()
                    .put("checkpoint_id", checkpointId)
                    .put("epoch_id", epochId)
                    .put("owner_id", ownerId)
                    .put("previous_message_id",
                            previousMessageId == null ? "" : previousMessageId)
                    .put("last_message_id", lastMessageId)
                    .put("source_message_ids", new JSONArray(sourceMessageIds))
                    .put("memory_ids", new JSONArray(memoryIds));
            String canonical = HomecomingSnapshotStore.canonicalJson(core);
            core.put("payload_sha256", sha256(canonical));
            return HomecomingOperationJournal.create(
                    epochId, deviceId, "summary_checkpoint", checkpointId,
                    "create", "", HomecomingSnapshotStore.canonicalJson(core), now);
        } catch (Exception exception) {
            throw new IllegalStateException("could not encode summary checkpoint", exception);
        }
    }

    private static List<String> messageIds(List<SourceMessage> messages) {
        ArrayList<String> ids = new ArrayList<>();
        for (SourceMessage message : messages) ids.add(message.id);
        return ids;
    }

    private static String sha256(String value) throws Exception {
        byte[] digest = MessageDigest.getInstance("SHA-256")
                .digest(value.getBytes(java.nio.charset.StandardCharsets.UTF_8));
        StringBuilder output = new StringBuilder();
        for (byte item : digest) output.append(String.format(Locale.ROOT, "%02x", item));
        return output.toString();
    }

    private static final class NativeEmbeddingProvider implements EmbeddingProvider {
        private static final MediaType JSON =
                MediaType.get("application/json; charset=utf-8");
        private final HomecomingRouteVault vault;
        private final OkHttpClient client = new OkHttpClient();

        NativeEmbeddingProvider(HomecomingRouteVault vault) {
            this.vault = vault;
        }

        @Override public byte[] embed(String content) throws Exception {
            HomecomingRouteVault.Service service = vault.resolveService("embedding");
            JSONObject body;
            String url;
            Request.Builder request = new Request.Builder();
            if ("gemini".equals(service.provider)) {
                url = trimSlash(service.baseUrl) + "/v1beta/models/"
                        + service.model + ":embedContent";
                body = new JSONObject().put("model", "models/" + service.model)
                        .put("content", new JSONObject()
                                .put("parts", new JSONArray()
                                        .put(new JSONObject().put("text", content))));
                request.header("x-goog-api-key", service.apiKey);
            } else {
                url = trimSlash(service.baseUrl) + "/embeddings";
                body = new JSONObject()
                        .put("model", service.model)
                        .put("input", content);
                request.header("Authorization", "Bearer " + service.apiKey);
            }
            request.url(url)
                    .header("Content-Type", "application/json")
                    .post(RequestBody.create(
                            HomecomingSnapshotStore.canonicalJson(body), JSON));
            try (Response response = client.newCall(request.build()).execute()) {
                if (!response.isSuccessful() || response.body() == null) {
                    throw new IOException("embedding HTTP " + response.code());
                }
                JSONObject value = new JSONObject(response.body().string());
                JSONArray vector;
                if ("gemini".equals(service.provider)) {
                    vector = value.getJSONObject("embedding").getJSONArray("values");
                } else {
                    vector = value.getJSONArray("data")
                            .getJSONObject(0).getJSONArray("embedding");
                }
                java.nio.ByteBuffer bytes = java.nio.ByteBuffer
                        .allocate(vector.length() * 4)
                        .order(java.nio.ByteOrder.LITTLE_ENDIAN);
                for (int index = 0; index < vector.length(); index++) {
                    bytes.putFloat((float) vector.getDouble(index));
                }
                return bytes.array();
            }
        }

        private static String trimSlash(String value) {
            return value.endsWith("/")
                    ? value.substring(0, value.length() - 1) : value;
        }
    }
}
