package com.aion.chat.homecoming;

import org.junit.Rule;
import org.junit.Test;
import org.junit.rules.TemporaryFolder;

import java.io.File;
import java.util.Arrays;

import static org.junit.Assert.assertArrayEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

public class HomecomingMediaCacheTest {
    @Rule public TemporaryFolder folder = new TemporaryFolder();

    @Test
    public void capEvictsOldestUnpinnedButNeverPinnedPlayback() throws Exception {
        HomecomingMediaCache cache = new HomecomingMediaCache(folder.getRoot(), 10);
        cache.put("old", bytes(6, (byte) 1), 10L);
        cache.pin("old");
        cache.put("new", bytes(6, (byte) 2), 20L);

        assertTrue(cache.contains("old"));
        assertFalse(cache.contains("new"));

        cache.unpin("old");
        cache.put("new", bytes(6, (byte) 2), 30L);
        assertFalse(cache.contains("old"));
        assertTrue(cache.contains("new"));
        assertArrayEquals(bytes(6, (byte) 2), cache.read("new"));
    }

    @Test
    public void returnProjectionUsesOnlyTransparentTextPlaceholders() {
        assertTrue(HomecomingMediaCache.returnText("image", "").contains("图片未保留"));
        assertTrue(HomecomingMediaCache.returnText("audio", "").contains("音频未保留"));
        assertTrue(HomecomingMediaCache.returnText("image", "图中文字")
                .equals("图中文字"));
    }

    private static byte[] bytes(int size, byte value) {
        byte[] result = new byte[size];
        Arrays.fill(result, value);
        return result;
    }
}
