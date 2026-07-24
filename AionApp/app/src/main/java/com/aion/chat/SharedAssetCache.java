package com.aion.chat;

import android.content.Context;
import android.net.Uri;
import android.webkit.CookieManager;
import android.webkit.WebResourceResponse;

import org.json.JSONObject;

import java.io.BufferedInputStream;
import java.io.BufferedOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.URI;
import java.security.DigestInputStream;
import java.security.MessageDigest;
import java.util.Collections;
import java.util.HashMap;
import java.util.HashSet;
import java.util.Iterator;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;

import okhttp3.Call;
import okhttp3.Callback;
import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.Response;
import okhttp3.ResponseBody;

/**
 * Content-addressed asset store shared by LAN, Tailscale, and Cloudflare URLs.
 *
 * Files live under files/client-assets rather than cache/, so Android's normal
 * "clear cache" action does not remove the verified frontend bundle. Cache
 * entries are activated only after the selected route returns a fresh manifest.
 */
final class SharedAssetCache {
    enum RefreshResult {
        REFRESHED,
        NOT_REFRESHED,
        AUTH_REQUIRED
    }

    interface Completion {
        void onComplete(RefreshResult result);
    }

    private static final String TAG = "SharedAssetCache";
    private static final String MANIFEST_NAME = "manifest.json";
    private static final String PREVIOUS_MANIFEST_NAME = "manifest.previous.json";
    private static final String STAGING_MANIFEST_NAME = "manifest.staging.json";
    private static final String ACTIVE_VERSION_NAME = "active-version";
    private static final int BUFFER_SIZE = 32 * 1024;

    private final Context context;
    private final File rootDir;
    private final File objectDir;
    private final OkHttpClient client;
    private final OkHttpClient manifestClient;
    private final Set<String> verifiedObjects =
            Collections.newSetFromMap(new ConcurrentHashMap<>());
    private volatile Map<String, Entry> activeEntries = Collections.emptyMap();
    private volatile String activeVersion = "";
    private volatile boolean frozenForPageLoad = false;

    SharedAssetCache(Context context) {
        this.context = context.getApplicationContext();
        this.rootDir = new File(this.context.getFilesDir(), "client-assets");
        this.objectDir = new File(rootDir, "objects");
        if (!objectDir.exists() && !objectDir.mkdirs()) {
            android.util.Log.w(TAG, "Unable to create persistent asset directory");
        }
        this.client = new OkHttpClient.Builder()
                .connectTimeout(4, java.util.concurrent.TimeUnit.SECONDS)
                .readTimeout(12, java.util.concurrent.TimeUnit.SECONDS)
                .build();
        this.manifestClient = client.newBuilder()
                .followRedirects(false)
                .followSslRedirects(false)
                .build();
        loadPersistedManifest();
    }

    void freezeForPageLoad() {
        frozenForPageLoad = true;
    }

    void refreshManifest(String pageUrl, Completion completion) {
        String manifestUrl = buildManifestUrl(pageUrl);
        if (manifestUrl == null) {
            completion.onComplete(RefreshResult.NOT_REFRESHED);
            return;
        }

        Request.Builder builder = new Request.Builder()
                .url(manifestUrl)
                .header("Accept", "application/json")
                .header("Cache-Control", "no-cache");
        String cookie = CookieManager.getInstance().getCookie(manifestUrl);
        if (cookie != null && !cookie.trim().isEmpty()) {
            builder.header("Cookie", cookie);
        }

        manifestClient.newCall(builder.build()).enqueue(new Callback() {
            @Override
            public void onFailure(Call call, java.io.IOException e) {
                android.util.Log.i(TAG, "Manifest unavailable: " + e.getClass().getSimpleName());
                completion.onComplete(RefreshResult.NOT_REFRESHED);
            }

            @Override
            public void onResponse(Call call, Response response) {
                try (Response closeable = response) {
                    RefreshResult responseResult = classifyManifestResponse(
                            pageUrl, response.code(), response.header("Location"));
                    if (responseResult == RefreshResult.AUTH_REQUIRED) {
                        android.util.Log.i(TAG, "Cloudflare Access authentication required");
                        completion.onComplete(responseResult);
                        return;
                    }
                    ResponseBody body = response.body();
                    if (!response.isSuccessful() || body == null) {
                        completion.onComplete(RefreshResult.NOT_REFRESHED);
                        return;
                    }
                    JSONObject json = new JSONObject(body.string());
                    ParsedManifest parsed = parseManifest(json);
                    if (parsed.version.equals(activeVersion)) {
                        completion.onComplete(RefreshResult.NOT_REFRESHED);
                        return;
                    }
                    if (!stageEssentialAssets(pageUrl, parsed)) {
                        android.util.Log.w(TAG, "Staging bundle incomplete; keeping active version");
                        completion.onComplete(RefreshResult.NOT_REFRESHED);
                        return;
                    }
                    persistAndActivate(json, parsed);
                    completion.onComplete(RefreshResult.REFRESHED);
                } catch (Exception e) {
                    android.util.Log.w(TAG, "Manifest rejected: " + e.getClass().getSimpleName());
                    completion.onComplete(RefreshResult.NOT_REFRESHED);
                }
            }
        });
    }

    WebResourceResponse intercept(Uri uri, Map<String, String> requestHeaders) {
        if (uri == null || activeEntries.isEmpty()) return null;
        String path = canonicalPath(uri);
        if (path == null) return null;
        Entry entry = activeEntries.get(path);
        if (entry == null) return null;

        File objectFile = new File(objectDir, entry.sha256);
        try {
            if (!isVerified(objectFile, entry)) {
                if (!copyBundledAsset(path, objectFile, entry)
                        && !downloadVerified(uri.toString(), requestHeaders, objectFile, entry)) {
                    return null;
                }
            }
            objectFile.setLastModified(System.currentTimeMillis());
            Map<String, String> headers = new HashMap<>();
            headers.put("Cache-Control", "no-store");
            headers.put("X-Shared-Asset-Cache", "hit");
            headers.put("X-Asset-Version", activeVersion);
            return new WebResourceResponse(
                    entry.contentType,
                    textEncoding(entry.contentType),
                    200,
                    "OK",
                    headers,
                    new BufferedInputStream(new FileInputStream(objectFile)));
        } catch (Exception e) {
            android.util.Log.w(TAG, "Asset fallback for " + path + ": "
                    + e.getClass().getSimpleName());
            return null;
        }
    }

    String getActiveVersion() {
        return activeVersion;
    }

    private boolean downloadVerified(String url, Map<String, String> requestHeaders,
                                     File destination, Entry entry) {
        Request.Builder builder = new Request.Builder().url(url);
        copyHeader(requestHeaders, builder, "User-Agent");
        copyHeader(requestHeaders, builder, "Accept");
        String cookie = CookieManager.getInstance().getCookie(url);
        if (cookie != null && !cookie.trim().isEmpty()) builder.header("Cookie", cookie);

        try (Response response = client.newCall(builder.build()).execute()) {
            ResponseBody body = response.body();
            if (!response.isSuccessful() || body == null) return false;
            return writeVerified(body.byteStream(), destination, entry);
        } catch (Exception e) {
            return false;
        }
    }

    private boolean stageEssentialAssets(String pageUrl, ParsedManifest parsed) {
        for (Map.Entry<String, Entry> item : parsed.entries.entrySet()) {
            Entry entry = item.getValue();
            if (!entry.isEssential()) continue;
            File objectFile = new File(objectDir, entry.sha256);
            try {
                if (isVerified(objectFile, entry)) continue;
                String path = item.getKey();
                if (copyBundledAsset(path, objectFile, entry)) continue;
                String url = resolveUrl(pageUrl, path);
                if (url == null || !downloadVerified(url, Collections.emptyMap(), objectFile, entry)) {
                    return false;
                }
            } catch (Exception e) {
                return false;
            }
        }
        return true;
    }

    private boolean copyBundledAsset(String path, File destination, Entry entry) {
        String assetPath;
        if (path.startsWith("/static/")) {
            assetPath = "static/" + path.substring("/static/".length());
        } else if (path.startsWith("/public/")) {
            assetPath = "public/" + path.substring("/public/".length());
        } else {
            return false;
        }
        try (InputStream input = context.getAssets().open(assetPath)) {
            return writeVerified(input, destination, entry);
        } catch (Exception ignored) {
            return false;
        }
    }

    private boolean writeVerified(InputStream source, File destination, Entry entry)
            throws Exception {
        File temp = new File(objectDir, entry.sha256 + ".part");
        MessageDigest digest = MessageDigest.getInstance("SHA-256");
        long written = 0;
        try (InputStream input = new DigestInputStream(new BufferedInputStream(source), digest);
             OutputStream output = new BufferedOutputStream(new FileOutputStream(temp))) {
            byte[] buffer = new byte[BUFFER_SIZE];
            int count;
            while ((count = input.read(buffer)) != -1) {
                output.write(buffer, 0, count);
                written += count;
            }
        } catch (Exception e) {
            temp.delete();
            throw e;
        }

        String actualHash = toHex(digest.digest());
        if (written != entry.size || !entry.sha256.equals(actualHash)) {
            temp.delete();
            return false;
        }
        if (destination.exists() && !destination.delete()) temp.delete();
        if (!temp.renameTo(destination)) {
            temp.delete();
            return false;
        }
        verifiedObjects.add(entry.sha256);
        return true;
    }

    private boolean isVerified(File file, Entry entry) throws Exception {
        if (!file.isFile() || file.length() != entry.size) return false;
        if (verifiedObjects.contains(entry.sha256)) return true;
        try (InputStream input = new FileInputStream(file)) {
            MessageDigest digest = MessageDigest.getInstance("SHA-256");
            byte[] buffer = new byte[BUFFER_SIZE];
            int count;
            while ((count = input.read(buffer)) != -1) digest.update(buffer, 0, count);
            if (!entry.sha256.equals(toHex(digest.digest()))) {
                file.delete();
                return false;
            }
        }
        verifiedObjects.add(entry.sha256);
        return true;
    }

    private synchronized void persistAndActivate(JSONObject json, ParsedManifest parsed)
            throws Exception {
        if (!rootDir.exists() && !rootDir.mkdirs()) throw new java.io.IOException("mkdir failed");
        File current = new File(rootDir, MANIFEST_NAME);
        File previous = new File(rootDir, PREVIOUS_MANIFEST_NAME);
        File temp = new File(rootDir, STAGING_MANIFEST_NAME);
        try (OutputStream output = new BufferedOutputStream(new FileOutputStream(temp))) {
            output.write(json.toString().getBytes(java.nio.charset.StandardCharsets.UTF_8));
        }
        if (previous.exists()) previous.delete();
        if (current.exists() && !current.renameTo(previous)) {
            android.util.Log.w(TAG, "Could not retain previous manifest");
        }
        if (!temp.renameTo(current)) throw new java.io.IOException("manifest rename failed");
        File activeVersionFile = new File(rootDir, ACTIVE_VERSION_NAME);
        File activeVersionTemp = new File(rootDir, ACTIVE_VERSION_NAME + ".part");
        try (OutputStream output = new BufferedOutputStream(new FileOutputStream(activeVersionTemp))) {
            output.write(parsed.version.getBytes(java.nio.charset.StandardCharsets.UTF_8));
        }
        if (activeVersionFile.exists() && !activeVersionFile.delete()) {
            throw new java.io.IOException("active version delete failed");
        }
        if (!activeVersionTemp.renameTo(activeVersionFile)) {
            throw new java.io.IOException("active version rename failed");
        }
        // Every essential object was staged and verified before this point, so
        // switching the in-memory map is safe. The already parsed page keeps
        // running until WebViewActivity requests one idle reload.
        activeEntries = parsed.entries;
        this.activeVersion = parsed.version;
        pruneObjects(parsed.entries, readManifestEntries(previous));
    }

    private void loadPersistedManifest() {
        File current = new File(rootDir, MANIFEST_NAME);
        if (!current.isFile()) return;
        try {
            JSONObject json = readJsonFile(current);
            ParsedManifest parsed = parseManifest(json);
            File pointer = new File(rootDir, ACTIVE_VERSION_NAME);
            String pointedVersion = pointer.isFile() ? readTextFile(pointer).trim() : "";
            if (!pointedVersion.isEmpty() && !pointedVersion.equals(parsed.version)) {
                File previous = new File(rootDir, PREVIOUS_MANIFEST_NAME);
                if (previous.isFile()) {
                    ParsedManifest fallback = parseManifest(readJsonFile(previous));
                    if (pointedVersion.equals(fallback.version)) parsed = fallback;
                }
            }
            activeEntries = parsed.entries;
            activeVersion = parsed.version;
        } catch (Exception e) {
            android.util.Log.w(TAG, "Persisted manifest rejected");
        }
    }

    private void pruneObjects(Map<String, Entry> current, Map<String, Entry> previous) {
        Set<String> keep = new HashSet<>();
        for (Entry entry : current.values()) keep.add(entry.sha256);
        for (Entry entry : previous.values()) keep.add(entry.sha256);
        File[] files = objectDir.listFiles();
        if (files == null) return;
        for (File file : files) {
            if (file.getName().endsWith(".part") || !keep.contains(file.getName())) {
                file.delete();
                verifiedObjects.remove(file.getName());
            }
        }
    }

    private Map<String, Entry> readManifestEntries(File file) {
        if (!file.isFile()) return Collections.emptyMap();
        try {
            return parseManifest(readJsonFile(file)).entries;
        } catch (Exception ignored) {
            return Collections.emptyMap();
        }
    }

    private static JSONObject readJsonFile(File file) throws Exception {
        try (InputStream input = new FileInputStream(file)) {
            byte[] bytes = new byte[(int) file.length()];
            int offset = 0;
            while (offset < bytes.length) {
                int count = input.read(bytes, offset, bytes.length - offset);
                if (count < 0) break;
                offset += count;
            }
            return new JSONObject(new String(bytes, 0, offset,
                    java.nio.charset.StandardCharsets.UTF_8));
        }
    }

    private static String readTextFile(File file) throws Exception {
        try (InputStream input = new FileInputStream(file)) {
            byte[] bytes = new byte[(int) file.length()];
            int offset = 0;
            while (offset < bytes.length) {
                int count = input.read(bytes, offset, bytes.length - offset);
                if (count < 0) break;
                offset += count;
            }
            return new String(bytes, 0, offset, java.nio.charset.StandardCharsets.UTF_8);
        }
    }

    private static ParsedManifest parseManifest(JSONObject json) throws Exception {
        if (json.getInt("schema") != 2) throw new IllegalArgumentException("schema");
        String version = json.getString("version");
        JSONObject files = json.getJSONObject("files");
        Map<String, Entry> entries = new HashMap<>();
        Iterator<String> keys = files.keys();
        while (keys.hasNext()) {
            String path = keys.next();
            if (!path.startsWith("/")) continue;
            JSONObject value = files.getJSONObject(path);
            String sha = value.getString("sha256").toLowerCase(Locale.ROOT);
            long size = value.getLong("size");
            if (!sha.matches("[0-9a-f]{64}") || size < 0) continue;
            entries.put(path, new Entry(
                    sha,
                    size,
                    value.getString("content_type"),
                    value.optString("category", "visual")));
        }
        if (entries.isEmpty()) throw new IllegalArgumentException("empty manifest");
        return new ParsedManifest(version, Collections.unmodifiableMap(entries));
    }

    static String buildManifestUrl(String pageUrl) {
        try {
            URI uri = new URI(pageUrl);
            return new URI(uri.getScheme(), null, uri.getHost(), uri.getPort(),
                    "/api/client-assets", null, null).toString();
        } catch (Exception e) {
            return null;
        }
    }

    static RefreshResult classifyManifestResponse(
            String pageUrl, int statusCode, String location) {
        if (statusCode >= 300 && statusCode < 400
                && ConnectionEndpoint.isCloudflareUrl(pageUrl)
                && ConnectionEndpoint.isCloudflareAccessUrl(location)) {
            return RefreshResult.AUTH_REQUIRED;
        }
        return RefreshResult.NOT_REFRESHED;
    }

    static String canonicalPath(Uri uri) {
        if (uri == null) return null;
        return canonicalPath(uri.getPath());
    }

    static String canonicalPath(String path) {
        if (path == null || path.isEmpty()) return "/";
        if (path.length() > 1 && path.endsWith("/")) return path.substring(0, path.length() - 1);
        return path;
    }

    static String resolveUrl(String pageUrl, String path) {
        try {
            URI page = new URI(pageUrl);
            return new URI(page.getScheme(), null, page.getHost(), page.getPort(), path, null, null).toString();
        } catch (Exception e) {
            return null;
        }
    }

    private static void copyHeader(Map<String, String> headers, Request.Builder builder,
                                   String name) {
        if (headers == null) return;
        for (Map.Entry<String, String> header : headers.entrySet()) {
            if (name.equalsIgnoreCase(header.getKey()) && header.getValue() != null) {
                builder.header(name, header.getValue());
                return;
            }
        }
    }

    private static String textEncoding(String contentType) {
        if (contentType.startsWith("text/")
                || contentType.contains("javascript")
                || contentType.contains("json")
                || contentType.contains("svg")) return "UTF-8";
        return null;
    }

    private static String toHex(byte[] bytes) {
        StringBuilder result = new StringBuilder(bytes.length * 2);
        for (byte value : bytes) result.append(String.format(Locale.ROOT, "%02x", value));
        return result.toString();
    }

    private static final class Entry {
        final String sha256;
        final long size;
        final String contentType;
        final String category;

        Entry(String sha256, long size, String contentType, String category) {
            this.sha256 = sha256;
            this.size = size;
            this.contentType = contentType;
            this.category = category;
        }

        boolean isEssential() {
            return "frontend".equals(category) || "document".equals(category);
        }
    }

    private static final class ParsedManifest {
        final String version;
        final Map<String, Entry> entries;

        ParsedManifest(String version, Map<String, Entry> entries) {
            this.version = version;
            this.entries = entries;
        }
    }
}
