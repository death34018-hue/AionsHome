package com.aion.chat.homecoming;

import org.junit.Test;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;

import static org.junit.Assert.assertEquals;

public class HomecomingBackupSchedulerTest {
    @Test
    public void launcherRefreshIsDebouncedForFiveMinutes() {
        FakeClock clock = new FakeClock();
        FakeRequests requests = new FakeRequests();
        HomecomingBackupScheduler scheduler = scheduler(clock, requests);

        scheduler.onLauncherForeground("https://server.example");
        clock.now += 299_000L;
        scheduler.onLauncherForeground("https://server.example");
        assertEquals(1, requests.bases.size());
        clock.now += 1_000L;
        scheduler.onLauncherForeground("https://server.example");
        assertEquals(2, requests.bases.size());
    }

    @Test
    public void routeChangeMayRefreshImmediately() {
        FakeClock clock = new FakeClock();
        FakeRequests requests = new FakeRequests();
        HomecomingBackupScheduler scheduler = scheduler(clock, requests);

        scheduler.onLauncherForeground("https://one.example");
        scheduler.onNormalRouteSelected("https://two.example");

        assertEquals(Arrays.asList(
                "https://one.example", "https://two.example"), requests.bases);
    }

    @Test
    public void concurrentTriggersCollapseIntoOneRequest() {
        FakeClock clock = new FakeClock();
        FakeRequests requests = new FakeRequests();
        requests.hold = true;
        HomecomingBackupScheduler scheduler = scheduler(clock, requests);

        scheduler.onLauncherForeground("https://server.example");
        scheduler.markDirty();
        scheduler.onNormalRouteSelected("https://server.example");

        assertEquals(1, requests.bases.size());
    }

    @Test
    public void activeHomecomingDisablesNormalBackupRefresh() {
        FakeClock clock = new FakeClock();
        FakeRequests requests = new FakeRequests();
        HomecomingBackupScheduler scheduler = new HomecomingBackupScheduler(
                clock, requests, () -> true, () -> true);

        scheduler.onLauncherForeground("https://server.example");
        scheduler.onNormalRouteSelected("https://server.example");
        scheduler.runPeriodicFallback();

        assertEquals(0, requests.bases.size());
    }

    @Test
    public void periodicFallbackRequiresNetworkAndDoesNotActivateMode() {
        FakeClock clock = new FakeClock();
        FakeRequests requests = new FakeRequests();
        MutableNetwork network = new MutableNetwork();
        HomecomingBackupScheduler scheduler = new HomecomingBackupScheduler(
                clock, requests, () -> false, network);
        scheduler.onNormalRouteSelected("https://server.example");
        requests.bases.clear();

        network.available = false;
        scheduler.runPeriodicFallback();
        assertEquals(0, requests.bases.size());
        network.available = true;
        scheduler.runPeriodicFallback();
        assertEquals(1, requests.bases.size());
    }

    private static HomecomingBackupScheduler scheduler(
            FakeClock clock, FakeRequests requests) {
        return new HomecomingBackupScheduler(
                clock, requests, () -> false, () -> true);
    }

    private static final class FakeClock implements HomecomingBackupScheduler.Clock {
        long now = 1_000L;
        @Override public long nowMs() { return now; }
    }

    private static final class MutableNetwork
            implements HomecomingBackupScheduler.NetworkState {
        boolean available;
        @Override public boolean isAvailable() { return available; }
    }

    private static final class FakeRequests
            implements HomecomingBackupScheduler.RequestSink {
        final List<String> bases = new ArrayList<>();
        boolean hold;

        @Override
        public void request(String baseUrl, HomecomingBackupClient.RefreshReason reason,
                Runnable completion) {
            bases.add(baseUrl);
            if (!hold) {
                completion.run();
            }
        }
    }
}
