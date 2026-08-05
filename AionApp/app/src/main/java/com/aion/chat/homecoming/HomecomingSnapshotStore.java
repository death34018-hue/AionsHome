package com.aion.chat.homecoming;

import android.content.Context;

import org.json.JSONArray;
import org.json.JSONObject;
import org.json.JSONTokener;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Iterator;
import java.util.List;
import java.util.zip.GZIPInputStream;
import java.util.zip.GZIPOutputStream;

public final class HomecomingSnapshotStore {
    private static final String MANIFEST_FILE = "manifest.json";
    private static final String SNAPSHOT_FILE = "snapshot.json.gz";
    private static final String READY_FILE = "READY";
    private static final String EXPECTED_ID_FILE = "EXPECTED_ID";

    private final File root;
    private final FileOperations files;

    public HomecomingSnapshotStore(Context context) {
        this(new File(new File(new File(context.getFilesDir(), "homecoming"), "snapshots"), ""),
                new DefaultFileOperations());
    }

    HomecomingSnapshotStore(File root) {
        this(root, new DefaultFileOperations());
    }

    HomecomingSnapshotStore(File root, FileOperations files) {
        if (root == null || files == null) {
            throw new IllegalArgumentException("root and file operations are required");
        }
        this.root = root;
        this.files = files;
    }

    public File beginStaging(String snapshotId) throws IOException {
        String validatedId = required(snapshotId, "snapshotId");
        File staging = directory("staging");
        if (staging.exists() && !files.deleteTree(staging)) {
            throw new IOException("could not clear staging snapshot");
        }
        if (!files.mkdirs(staging)) {
            throw new IOException("could not create staging snapshot");
        }
        writeBytes(new File(staging, EXPECTED_ID_FILE),
                validatedId.getBytes(StandardCharsets.UTF_8));
        return staging;
    }

    public void writeStagingFile(String name, byte[] data) throws IOException {
        if (!MANIFEST_FILE.equals(name) && !SNAPSHOT_FILE.equals(name)
                && !READY_FILE.equals(name)) {
            throw new IllegalArgumentException("unsupported staging file");
        }
        File staging = directory("staging");
        if (!staging.isDirectory()) {
            throw new IOException("staging has not begun");
        }
        writeBytes(new File(staging, name), data == null ? new byte[0] : data);
    }

    public boolean verifyStaging() {
        return verifyDirectory(directory("staging")) != null;
    }

    public synchronized boolean activateStaging() {
        if (verifyDirectory(directory("staging")) == null) {
            return false;
        }
        File active = directory("active");
        File staging = directory("staging");
        File previous = directory("previous");

        if (previous.exists()) {
            if (verifyDirectory(previous) == null || !files.deleteTree(previous)) {
                return false;
            }
        }
        boolean movedActive = false;
        if (active.exists()) {
            if (verifyDirectory(active) == null || !files.rename(active, previous)) {
                return false;
            }
            movedActive = true;
        }
        if (files.rename(staging, active)) {
            return true;
        }
        if (movedActive) {
            files.rename(previous, active);
        }
        return false;
    }

    public SnapshotManifest activeManifest() {
        return verifyDirectory(directory("active"));
    }

    public SnapshotManifest previousManifest() {
        return verifyDirectory(directory("previous"));
    }

    public void discardStaging() {
        File staging = directory("staging");
        if (staging.exists()) {
            files.deleteTree(staging);
        }
    }

    private SnapshotManifest verifyDirectory(File directory) {
        try {
            File manifestFile = new File(directory, MANIFEST_FILE);
            File snapshotFile = new File(directory, SNAPSHOT_FILE);
            File readyFile = new File(directory, READY_FILE);
            if (!manifestFile.isFile() || !snapshotFile.isFile() || !readyFile.isFile()) {
                return null;
            }
            JSONObject manifest = new JSONObject(
                    new String(readBytes(manifestFile), StandardCharsets.UTF_8));
            int schema = manifest.getInt("schema");
            if (schema != HomecomingContract.SCHEMA_VERSION) {
                return null;
            }
            String snapshotId = required(manifest.getString("snapshot_id"), "snapshot_id");
            File expectedIdFile = new File(directory, EXPECTED_ID_FILE);
            if (expectedIdFile.isFile()) {
                String expected = new String(readBytes(expectedIdFile), StandardCharsets.UTF_8);
                if (!snapshotId.equals(expected)) {
                    return null;
                }
            }
            byte[] compressed = readBytes(snapshotFile);
            if (!constantTimeEquals(
                    manifest.getString("file_sha256"), sha256Hex(compressed))) {
                return null;
            }
            String payloadText = new String(
                    gunzip(compressed), StandardCharsets.UTF_8);
            JSONObject payload = (JSONObject) new JSONTokener(payloadText).nextValue();
            if (payload.getInt("schema") != schema
                    || !snapshotId.equals(payload.getString("snapshot_id"))) {
                return null;
            }
            JSONObject sections = payload.getJSONObject("sections");
            JSONObject sectionHashes = manifest.getJSONObject("section_hashes");
            if (sections.length() != sectionHashes.length()) {
                return null;
            }
            String rawSections = rawObjectMember(payloadText, "sections");
            Iterator<String> names = sections.keys();
            while (names.hasNext()) {
                String name = names.next();
                String rawSection = rawObjectMember(rawSections, name);
                String actual = sha256Hex(
                        rawSection.getBytes(StandardCharsets.UTF_8));
                if (!sectionHashes.has(name)
                        || !constantTimeEquals(sectionHashes.getString(name), actual)) {
                    return null;
                }
            }
            return new SnapshotManifest(snapshotId, schema);
        } catch (Exception ignored) {
            return null;
        }
    }

    private File directory(String name) {
        return new File(root, name);
    }

    private static String required(String value, String label) {
        if (value == null || value.trim().isEmpty()) {
            throw new IllegalArgumentException(label + " is required");
        }
        return value.trim();
    }

    private static byte[] readBytes(File file) throws IOException {
        FileInputStream input = new FileInputStream(file);
        try {
            ByteArrayOutputStream output = new ByteArrayOutputStream();
            byte[] buffer = new byte[8192];
            int count;
            while ((count = input.read(buffer)) != -1) {
                output.write(buffer, 0, count);
            }
            return output.toByteArray();
        } finally {
            input.close();
        }
    }

    private static void writeBytes(File file, byte[] data) throws IOException {
        File parent = file.getParentFile();
        if (parent != null && !parent.isDirectory() && !parent.mkdirs()) {
            throw new IOException("could not create parent directory");
        }
        FileOutputStream output = new FileOutputStream(file);
        try {
            output.write(data);
            output.getFD().sync();
        } finally {
            output.close();
        }
    }

    static byte[] gzip(byte[] data) throws IOException {
        ByteArrayOutputStream bytes = new ByteArrayOutputStream();
        GZIPOutputStream gzip = new GZIPOutputStream(bytes);
        gzip.write(data);
        gzip.close();
        return bytes.toByteArray();
    }

    private static byte[] gunzip(byte[] data) throws IOException {
        GZIPInputStream gzip = new GZIPInputStream(new ByteArrayInputStream(data));
        ByteArrayOutputStream output = new ByteArrayOutputStream();
        byte[] buffer = new byte[8192];
        int count;
        while ((count = gzip.read(buffer)) != -1) {
            output.write(buffer, 0, count);
        }
        gzip.close();
        return output.toByteArray();
    }

    static String sha256Hex(byte[] data) {
        try {
            byte[] digest = MessageDigest.getInstance("SHA-256").digest(data);
            StringBuilder value = new StringBuilder(digest.length * 2);
            for (byte item : digest) {
                value.append(String.format("%02x", item & 0xff));
            }
            return value.toString();
        } catch (Exception exception) {
            throw new IllegalStateException("SHA-256 unavailable", exception);
        }
    }

    private static boolean constantTimeEquals(String expected, String actual) {
        if (expected == null || actual == null) {
            return false;
        }
        return MessageDigest.isEqual(
                expected.getBytes(StandardCharsets.US_ASCII),
                actual.getBytes(StandardCharsets.US_ASCII));
    }

    static String canonicalJson(Object value) {
        if (value == null || value == JSONObject.NULL) {
            return "null";
        }
        if (value instanceof JSONObject) {
            JSONObject object = (JSONObject) value;
            List<String> keys = new ArrayList<>();
            Iterator<String> iterator = object.keys();
            while (iterator.hasNext()) {
                keys.add(iterator.next());
            }
            Collections.sort(keys);
            StringBuilder result = new StringBuilder("{");
            for (int i = 0; i < keys.size(); i++) {
                if (i > 0) {
                    result.append(',');
                }
                String key = keys.get(i);
                result.append(JSONObject.quote(key))
                        .append(':')
                        .append(canonicalJson(object.opt(key)));
            }
            return result.append('}').toString();
        }
        if (value instanceof JSONArray) {
            JSONArray array = (JSONArray) value;
            StringBuilder result = new StringBuilder("[");
            for (int i = 0; i < array.length(); i++) {
                if (i > 0) {
                    result.append(',');
                }
                result.append(canonicalJson(array.opt(i)));
            }
            return result.append(']').toString();
        }
        if (value instanceof Number) {
            try {
                return JSONObject.numberToString((Number) value);
            } catch (Exception exception) {
                throw new IllegalArgumentException("invalid JSON number", exception);
            }
        }
        if (value instanceof Boolean) {
            return value.toString();
        }
        return JSONObject.quote(String.valueOf(value));
    }

    static String rawObjectMember(String objectJson, String requestedName) throws Exception {
        int length = objectJson.length();
        int cursor = skipWhitespace(objectJson, 0);
        if (cursor >= length || objectJson.charAt(cursor) != '{') {
            throw new IllegalArgumentException("JSON value is not an object");
        }
        cursor++;
        while (true) {
            cursor = skipWhitespace(objectJson, cursor);
            if (cursor >= length || objectJson.charAt(cursor) == '}') {
                break;
            }
            if (objectJson.charAt(cursor) != '"') {
                throw new IllegalArgumentException("object key is invalid");
            }
            int keyEnd = stringEnd(objectJson, cursor);
            String key = (String) new JSONTokener(
                    objectJson.substring(cursor, keyEnd)).nextValue();
            cursor = skipWhitespace(objectJson, keyEnd);
            if (cursor >= length || objectJson.charAt(cursor) != ':') {
                throw new IllegalArgumentException("object separator is invalid");
            }
            int valueStart = skipWhitespace(objectJson, cursor + 1);
            int valueEnd = valueEnd(objectJson, valueStart);
            if (requestedName.equals(key)) {
                return objectJson.substring(valueStart, valueEnd);
            }
            cursor = skipWhitespace(objectJson, valueEnd);
            if (cursor < length && objectJson.charAt(cursor) == ',') {
                cursor++;
                continue;
            }
            if (cursor < length && objectJson.charAt(cursor) == '}') {
                break;
            }
            throw new IllegalArgumentException("object terminator is invalid");
        }
        throw new IllegalArgumentException("missing object member: " + requestedName);
    }

    private static int skipWhitespace(String value, int cursor) {
        while (cursor < value.length()
                && Character.isWhitespace(value.charAt(cursor))) {
            cursor++;
        }
        return cursor;
    }

    private static int stringEnd(String value, int start) {
        boolean escaped = false;
        for (int i = start + 1; i < value.length(); i++) {
            char current = value.charAt(i);
            if (escaped) {
                escaped = false;
            } else if (current == '\\') {
                escaped = true;
            } else if (current == '"') {
                return i + 1;
            }
        }
        throw new IllegalArgumentException("unterminated JSON string");
    }

    private static int valueEnd(String value, int start) {
        if (start >= value.length()) {
            throw new IllegalArgumentException("missing JSON value");
        }
        char first = value.charAt(start);
        if (first == '"') {
            return stringEnd(value, start);
        }
        if (first != '{' && first != '[') {
            int cursor = start;
            while (cursor < value.length()) {
                char current = value.charAt(cursor);
                if (current == ',' || current == '}') {
                    break;
                }
                cursor++;
            }
            return cursor;
        }
        int depth = 0;
        boolean insideString = false;
        boolean escaped = false;
        for (int i = start; i < value.length(); i++) {
            char current = value.charAt(i);
            if (insideString) {
                if (escaped) {
                    escaped = false;
                } else if (current == '\\') {
                    escaped = true;
                } else if (current == '"') {
                    insideString = false;
                }
                continue;
            }
            if (current == '"') {
                insideString = true;
            } else if (current == '{' || current == '[') {
                depth++;
            } else if (current == '}' || current == ']') {
                depth--;
                if (depth == 0) {
                    return i + 1;
                }
            }
        }
        throw new IllegalArgumentException("unterminated JSON container");
    }

    interface FileOperations {
        boolean mkdirs(File directory);
        boolean rename(File source, File target);
        boolean deleteTree(File target);
    }

    static class DefaultFileOperations implements FileOperations {
        @Override
        public boolean mkdirs(File directory) {
            return directory.mkdirs() || directory.isDirectory();
        }

        @Override
        public boolean rename(File source, File target) {
            return source.renameTo(target);
        }

        @Override
        public boolean deleteTree(File target) {
            if (!target.exists()) {
                return true;
            }
            File[] children = target.listFiles();
            if (children == null && target.isDirectory()) {
                return false;
            }
            if (children != null) {
                for (File child : children) {
                    if (!deleteTree(child)) {
                        return false;
                    }
                }
            }
            return target.delete();
        }
    }

    public static final class SnapshotManifest {
        public final String snapshotId;
        public final int schema;

        SnapshotManifest(String snapshotId, int schema) {
            this.snapshotId = snapshotId;
            this.schema = schema;
        }
    }
}
