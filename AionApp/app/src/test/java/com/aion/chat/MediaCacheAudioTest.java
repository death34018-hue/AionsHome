package com.aion.chat;

import org.junit.Test;
import java.io.File;
import java.nio.file.Files;
import java.util.Collections;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import okhttp3.*;
import static org.junit.Assert.*;

public class MediaCacheAudioTest {
    @Test public void readingCacheInfoDoesNotDeleteAnActiveDownload() throws Exception {
        File dir = Files.createTempDirectory("aion-active-download").toFile();
        MediaCacheStore cache = new MediaCacheStore(dir, null, new OkHttpClient());
        File active = new File(dir, "audio.data.part");
        try {
            Files.write(active.toPath(), new byte[] {1, 2, 3});
            cache.getInfo();
            assertTrue("active download must survive cache housekeeping", active.exists());
        } finally { cache.shutdown(); active.delete(); dir.delete(); }
    }

    @Test public void concurrentPlaybackSharesDownloadAndReplayWorksWithoutNetwork() throws Exception {
        AtomicInteger requests = new AtomicInteger();
        CountDownLatch entered = new CountDownLatch(1), release = new CountDownLatch(1);
        OkHttpClient client = new OkHttpClient.Builder().addInterceptor(chain -> {
            requests.incrementAndGet(); entered.countDown();
            try { release.await(3, TimeUnit.SECONDS); } catch (InterruptedException e) { throw new java.io.IOException(e); }
            assertEquals("session=test", chain.request().header("Cookie"));
            return new Response.Builder().request(chain.request()).protocol(Protocol.HTTP_1_1)
                    .code(200).message("OK").header("Content-Type", "audio/mpeg")
                    .body(ResponseBody.create("audio-data", MediaType.get("audio/mpeg"))).build();
        }).build();
        File dir = Files.createTempDirectory("aion-audio-test").toFile();
        MediaCacheStore cache = new MediaCacheStore(dir, null, client);
        String url = "https://home.test/api/tts/audio/m_s0";
        CountDownLatch loaded = new CountDownLatch(2);
        try {
            cache.loadAudio(url, Collections.singletonMap("Cookie", "session=test"), false, file -> { assertNotNull(file); loaded.countDown(); });
            assertTrue(entered.await(2, TimeUnit.SECONDS));
            cache.loadAudio(url, Collections.singletonMap("Cookie", "session=test"), false, file -> { assertNotNull(file); loaded.countDown(); });
            release.countDown();
            assertTrue(loaded.await(4, TimeUnit.SECONDS));
            assertEquals(1, requests.get());
            CountDownLatch replay = new CountDownLatch(1);
            cache.loadAudio(url, Collections.emptyMap(), false, file -> {
                assertEquals(10, file.length()); replay.countDown();
            });
            assertTrue(replay.await(2, TimeUnit.SECONDS));
            assertEquals(1, requests.get());
        } finally { cache.shutdown(); for (File f : dir.listFiles()) f.delete(); dir.delete(); }
    }

    @Test public void loginHtmlIsNeverSavedAsAudio() throws Exception {
        OkHttpClient client = new OkHttpClient.Builder().addInterceptor(chain ->
                new Response.Builder().request(chain.request()).protocol(Protocol.HTTP_1_1)
                    .code(200).message("OK").header("Content-Type", "text/html")
                    .body(ResponseBody.create("login", MediaType.get("text/html"))).build()).build();
        File dir = Files.createTempDirectory("aion-audio-auth").toFile();
        MediaCacheStore cache = new MediaCacheStore(dir, null, client);
        CountDownLatch loaded = new CountDownLatch(1);
        try {
            cache.loadAudio("https://home.test/api/tts/audio/m_s0", Collections.emptyMap(), false,
                    file -> { assertNull(file); loaded.countDown(); });
            assertTrue(loaded.await(3, TimeUnit.SECONDS));
            assertEquals(0, dir.listFiles().length);
        } finally { cache.shutdown(); for (File f : dir.listFiles()) f.delete(); dir.delete(); }
    }

    @Test public void cachedReplayDoesNotWaitForSlowPrefetchWorkers() throws Exception {
        CountDownLatch busy = new CountDownLatch(2), release = new CountDownLatch(1);
        OkHttpClient client = new OkHttpClient.Builder().addInterceptor(chain -> {
            if (chain.request().url().encodedPath().contains("slow")) {
                busy.countDown();
                try { release.await(5, TimeUnit.SECONDS); } catch (InterruptedException e) { throw new java.io.IOException(e); }
            }
            return new Response.Builder().request(chain.request()).protocol(Protocol.HTTP_1_1)
                    .code(200).message("OK").header("Content-Type", "audio/mpeg")
                    .body(ResponseBody.create("audio", MediaType.get("audio/mpeg"))).build();
        }).build();
        File dir = Files.createTempDirectory("aion-cached-replay").toFile();
        MediaCacheStore cache = new MediaCacheStore(dir, null, client);
        CountDownLatch seeded = new CountDownLatch(1), replayed = new CountDownLatch(1), slowDone = new CountDownLatch(2);
        try {
            cache.loadAudio("https://home.test/api/tts/audio/ready", Collections.emptyMap(), false, file -> seeded.countDown());
            assertTrue(seeded.await(2, TimeUnit.SECONDS));
            for (int i = 0; i < 2; i++) cache.loadAudio("https://home.test/api/tts/audio/slow" + i,
                    Collections.emptyMap(), true, file -> slowDone.countDown());
            assertTrue(busy.await(2, TimeUnit.SECONDS));
            cache.loadAudio("https://home.test/api/tts/audio/ready", Collections.emptyMap(), false,
                    file -> { assertNotNull(file); replayed.countDown(); });
            assertTrue("cached audio must play while both network workers are blocked", replayed.await(1, TimeUnit.SECONDS));
        } finally {
            release.countDown(); slowDone.await(3, TimeUnit.SECONDS); cache.shutdown();
            for (File f : dir.listFiles()) f.delete(); dir.delete();
        }
    }
}
