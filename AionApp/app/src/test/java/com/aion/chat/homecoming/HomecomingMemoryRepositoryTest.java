package com.aion.chat.homecoming;

import org.json.JSONArray;
import org.json.JSONObject;
import org.junit.Test;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertThrows;
import static org.junit.Assert.assertTrue;

public class HomecomingMemoryRepositoryTest {
    @Test
    public void recallNeverCrossesOwnerStoresAndRanksKeywordMatches() {
        MemoryBackend backend = new MemoryBackend();
        backend.snapshots.add(memory("main-one", "main", "喜欢雨夜散步", "雨夜,散步"));
        backend.snapshots.add(memory("second-one", "second", "喜欢晴天咖啡", "晴天,咖啡"));
        HomecomingMemoryRepository repository = repository(backend);

        List<HomecomingMemoryRepository.Memory> main =
                repository.recall("main", "雨夜散步", 5);
        List<HomecomingMemoryRepository.Memory> second =
                repository.recall("second", "雨夜散步", 5);

        assertEquals(Arrays.asList("main-one"), ids(main));
        assertEquals(Arrays.asList("second-one"), ids(second));
        assertFalse(main.get(0).content.contains("晴天"));
    }

    @Test
    public void localTombstoneHidesSnapshotMemory() {
        MemoryBackend backend = new MemoryBackend();
        backend.snapshots.add(memory("main-one", "main", "旧记忆", "旧"));
        HomecomingMemoryRepository repository = repository(backend);

        repository.delete("main", "main-one", "base-hash", 20L);

        assertTrue(repository.recall("main", "旧记忆", 5).isEmpty());
        assertEquals("delete", backend.operations.get(0).action);
        assertEquals("base-hash", backend.operations.get(0).baseRevision);
    }

    @Test
    public void lastLocalEditWinsAndEveryCrudHasBaseHash() {
        MemoryBackend backend = new MemoryBackend();
        HomecomingMemoryRepository repository = repository(backend);

        String id = repository.create("main", "第一版", "初始", 10L).id;
        repository.update("main", id, "第二版", "initial-hash", 20L);
        repository.update("main", id, "最终版", "second-hash", 30L);

        assertEquals("最终版", repository.recall("main", "最终版", 5).get(0).content);
        assertEquals(3, backend.operations.size());
        assertEquals("", backend.operations.get(0).baseRevision);
        assertEquals("initial-hash", backend.operations.get(1).baseRevision);
        assertEquals("second-hash", backend.operations.get(2).baseRevision);
    }

    @Test
    public void invalidOwnerIsRejected() {
        assertThrows(IllegalArgumentException.class,
                () -> repository(new MemoryBackend()).recall("group", "x", 5));
    }

    private static HomecomingMemoryRepository repository(MemoryBackend backend) {
        return new HomecomingMemoryRepository(
                backend, "epoch-one", "android:test-device");
    }

    private static HomecomingMemoryRepository.Memory memory(
            String id, String owner, String content, String keywords) {
        return new HomecomingMemoryRepository.Memory(
                id, owner, content, keywords, "", false, 1L);
    }

    private static List<String> ids(List<HomecomingMemoryRepository.Memory> memories) {
        ArrayList<String> ids = new ArrayList<>();
        for (HomecomingMemoryRepository.Memory memory : memories) {
            ids.add(memory.id);
        }
        return ids;
    }

    private static final class MemoryBackend implements HomecomingMemoryRepository.Backend {
        final List<HomecomingMemoryRepository.Memory> snapshots = new ArrayList<>();
        final Map<String, HomecomingMemoryRepository.Memory> locals = new LinkedHashMap<>();
        final List<HomecomingOperationJournal.Operation> operations = new ArrayList<>();
        long sequence;

        @Override public List<HomecomingMemoryRepository.Memory> snapshots(String owner) {
            return filter(snapshots, owner);
        }
        @Override public List<HomecomingMemoryRepository.Memory> locals(String owner) {
            return filter(new ArrayList<>(locals.values()), owner);
        }
        @Override
        public HomecomingMemoryRepository.Memory save(
                HomecomingMemoryRepository.Memory memory,
                HomecomingOperationJournal.Operation operation) {
            locals.put(memory.id, memory);
            operations.add(operation.withDeviceSeq(++sequence));
            return memory;
        }
        private static List<HomecomingMemoryRepository.Memory> filter(
                List<HomecomingMemoryRepository.Memory> source, String owner) {
            ArrayList<HomecomingMemoryRepository.Memory> result = new ArrayList<>();
            for (HomecomingMemoryRepository.Memory memory : source) {
                if (owner.equals(memory.ownerId)) result.add(memory);
            }
            return result;
        }
    }
}
