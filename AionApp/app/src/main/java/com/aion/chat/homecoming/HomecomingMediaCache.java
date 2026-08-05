package com.aion.chat.homecoming;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashSet;
import java.util.List;
import java.util.Set;

public final class HomecomingMediaCache {
    public static final long DEFAULT_CAP_BYTES = 128L * 1024L * 1024L;
    private final File root;
    private final long capBytes;
    private final Set<String> pinned = new HashSet<>();

    public HomecomingMediaCache(File root) {
        this(root, DEFAULT_CAP_BYTES);
    }

    HomecomingMediaCache(File root, long capBytes) {
        if (root == null || capBytes < 1) {
            throw new IllegalArgumentException("valid cache root and cap are required");
        }
        this.root = root;
        this.capBytes = capBytes;
    }

    public synchronized void put(String token, byte[] data, long accessTime) throws IOException {
        String id = validateToken(token);
        if (data == null || data.length > capBytes) {
            throw new IOException("media exceeds cache limit");
        }
        if (!root.isDirectory() && !root.mkdirs()) {
            throw new IOException("could not create media cache");
        }
        File temporary = new File(root, id + ".tmp");
        File target = file(id);
        FileOutputStream output = new FileOutputStream(temporary);
        try {
            output.write(data);
            output.getFD().sync();
        } finally {
            output.close();
        }
        if (target.exists() && !target.delete()) {
            temporary.delete();
            throw new IOException("could not replace media cache entry");
        }
        if (!temporary.renameTo(target)) {
            throw new IOException("could not activate media cache entry");
        }
        target.setLastModified(accessTime);
        trim();
    }

    public synchronized byte[] read(String token) throws IOException {
        File target = file(validateToken(token));
        if (!target.isFile()) {
            throw new IOException("media cache entry is missing");
        }
        FileInputStream input = new FileInputStream(target);
        try {
            ByteArrayOutputStream output = new ByteArrayOutputStream();
            byte[] buffer = new byte[8192];
            int count;
            while ((count = input.read(buffer)) != -1) output.write(buffer, 0, count);
            target.setLastModified(System.currentTimeMillis());
            return output.toByteArray();
        } finally {
            input.close();
        }
    }

    public synchronized boolean contains(String token) {
        return file(validateToken(token)).isFile();
    }

    public synchronized void pin(String token) {
        pinned.add(validateToken(token));
    }

    public synchronized void unpin(String token) {
        pinned.remove(validateToken(token));
    }

    public synchronized void remove(String token) {
        String id = validateToken(token);
        if (!pinned.contains(id)) file(id).delete();
    }

    private void trim() {
        List<File> entries = entries();
        long total = 0L;
        for (File entry : entries) total += entry.length();
        entries.sort(Comparator.comparingLong(File::lastModified));
        for (File entry : entries) {
            if (total <= capBytes) break;
            String token = entry.getName().substring(0, entry.getName().length() - 4);
            if (pinned.contains(token)) continue;
            long length = entry.length();
            if (entry.delete()) total -= length;
        }
    }

    private List<File> entries() {
        ArrayList<File> result = new ArrayList<>();
        File[] values = root.listFiles();
        if (values != null) {
            for (File value : values) {
                if (value.isFile() && value.getName().endsWith(".bin")) result.add(value);
            }
        }
        return result;
    }

    private File file(String token) {
        return new File(root, token + ".bin");
    }

    File fileForPlayback(String token) {
        return file(validateToken(token));
    }

    public static String returnText(String kind, String text) {
        String content = text == null ? "" : text.trim();
        if (!content.isEmpty()) return content;
        if ("image".equals(kind)) return "【归巢期间发送了一张图片，图片未保留】";
        if ("audio".equals(kind)) return "【归巢期间发送了一条语音，音频未保留】";
        return "";
    }

    private static String validateToken(String value) {
        if (value == null || !value.matches("[A-Za-z0-9._-]{1,160}")) {
            throw new IllegalArgumentException("invalid media token");
        }
        return value;
    }
}
