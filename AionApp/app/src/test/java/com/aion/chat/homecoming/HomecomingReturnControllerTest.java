package com.aion.chat.homecoming;

import org.junit.Test;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.List;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

public class HomecomingReturnControllerTest {
    @Test
    public void successFreezesBeforeNetworkAndVerifiesBeforeConfirming() {
        Fixture fixture = new Fixture(true);
        fixture.controller.synchronize(fixture.route, fixture.observer);

        assertEquals(Arrays.asList(
                "phase:freezing", "freezing", "phase:building", "freeze", "build",
                "phase:frozen", "frozen", "stop_homecoming",
                "phase:uploading", "returning", "upload",
                "phase:planning", "dry_run", "phase:applying", "apply", "status",
                "phase:verifying", "phase:confirming", "confirm", "inactive",
                "phase:complete", "open_normal"),
                fixture.events);
        assertFalse(fixture.mode.active);
        assertTrue(fixture.packages.confirmed);
    }

    @Test
    public void uploadFailureAllowsReturnWithoutDeletingPackage() {
        Fixture fixture = new Fixture(true);
        fixture.client.failUpload = true;
        fixture.controller.synchronize(fixture.route, fixture.observer);

        assertTrue(fixture.mode.frozen);
        assertEquals(1, fixture.packages.pendingInSequence().size());
        assertFalse(fixture.events.contains("open_normal"));

        fixture.controller.archiveAndReturn(fixture.route);

        assertFalse(fixture.mode.active);
        assertFalse(fixture.mode.frozen);
        assertEquals(1, fixture.packages.pendingInSequence().size());
        assertTrue(fixture.events.contains("open_normal"));
    }

    @Test
    public void buildFailureResumesHomecomingAndCannotForceReturn() {
        Fixture fixture = new Fixture(true);
        fixture.packages.failBuild = true;
        fixture.controller.synchronize(fixture.route, fixture.observer);
        fixture.controller.archiveAndReturn(fixture.route);

        assertTrue(fixture.mode.active);
        assertTrue(fixture.events.contains("resume_homecoming"));
        assertTrue(fixture.events.contains(
                "diagnostic:package_build:Exception"));
        assertFalse(fixture.events.contains("open_normal"));
    }

    @Test
    public void partialApplyPollsOnlyInsideExplicitSynchronization() {
        Fixture fixture = new Fixture(true);
        fixture.client.receipts.add(receipt(false, false, 1L, ""));
        fixture.client.receipts.add(receipt(true, false, 2L, "summary"));
        fixture.controller.synchronize(fixture.route, fixture.observer);

        assertEquals(2, Collections.frequency(fixture.events, "apply"));
        assertTrue(fixture.packages.confirmed);
    }

    @Test
    public void retryableServerResponseDoesNotRetryAutomatically() {
        Fixture fixture = new Fixture(true);
        fixture.client.receipts.add(receipt(false, true, 0L, ""));
        fixture.controller.synchronize(fixture.route, fixture.observer);

        assertEquals(1, Collections.frequency(fixture.events, "apply"));
        assertFalse(fixture.packages.confirmed);
        assertTrue(fixture.mode.frozen);
    }

    @Test
    public void receiptMismatchNeverConfirmsOrReturns() {
        Fixture fixture = new Fixture(true);
        fixture.client.receipts.add(receipt(true, false, 99L, "summary"));
        fixture.controller.synchronize(fixture.route, fixture.observer);

        assertFalse(fixture.packages.confirmed);
        assertTrue(fixture.mode.frozen);
        assertFalse(fixture.events.contains("open_normal"));
    }

    @Test
    public void recreationImportsOldPackagesBeforeCurrentWithoutRebuilding() {
        Fixture fixture = new Fixture(false);
        fixture.mode.frozen = true;
        fixture.packages.values.add(packageValue("package-old", 1L));
        fixture.packages.values.add(packageValue("package-new", 2L));
        fixture.controller.synchronize(fixture.route, fixture.observer);

        assertFalse(fixture.events.contains("build"));
        assertEquals(Arrays.asList("package-old", "package-new"),
                fixture.client.uploaded);
        assertTrue(fixture.packages.confirmed);
    }

    @Test
    public void alreadyConfirmedUploadVerifiesReceiptWithoutApplyingAgain() {
        Fixture fixture = new Fixture(false);
        fixture.mode.frozen = true;
        fixture.packages.values.add(packageValue("package-one", 2L));
        fixture.client.uploadState = "confirmed";
        fixture.client.latest = new HomecomingReturnClient.Receipt(
                "plan", "package-one", 2L,
                Collections.<String, Integer>emptyMap(),
                "summary", true, false);

        fixture.controller.synchronize(fixture.route, fixture.observer);

        assertFalse(fixture.events.contains("dry_run"));
        assertFalse(fixture.events.contains("apply"));
        assertTrue(fixture.packages.confirmed);
        assertTrue(fixture.events.contains("open_normal"));
    }

    private static HomecomingReturnClient.Receipt receipt(
            boolean complete, boolean retryable, long sequence, String hash) {
        return new HomecomingReturnClient.Receipt(
                "plan", "package-one", sequence,
                Collections.<String, Integer>emptyMap(),
                hash, complete, retryable);
    }

    private static HomecomingReturnPackageRepository.ReturnPackage packageValue(
            String id, long sequence) {
        return new HomecomingReturnPackageRepository.ReturnPackage(
                id, "epoch-one", "device-one", "snapshot-one",
                sequence, sequence, 1, "hash", "signature",
                new byte[]{1}, "", "ready", 1L, 1L);
    }

    private static final class Fixture {
        final List<String> events = new ArrayList<>();
        final FakeMode mode;
        final FakePackages packages = new FakePackages();
        final FakeClient client = new FakeClient();
        final FakeRoute route = new FakeRoute();
        final HomecomingReturnController.Observer observer =
                new HomecomingReturnController.Observer() {
                    @Override public void onPhase(String phase) {
                        events.add("phase:" + phase);
                    }
                    @Override public void onFailure(
                            String code, boolean canReturnWithoutSync) {
                        events.add("failure:" + code);
                    }
                    @Override public void onDiagnostic(
                            String stage, Throwable failure) {
                        events.add("diagnostic:" + stage + ":"
                                + failure.getClass().getSimpleName());
                    }
                };
        final HomecomingReturnController controller;

        Fixture(boolean active) {
            mode = new FakeMode(active, events);
            packages.events = events;
            client.events = events;
            route.events = events;
            controller = new HomecomingReturnController(
                    mode, packages, client, () -> 100L);
        }
    }

    private static final class FakeMode
            implements HomecomingReturnController.ModePort {
        boolean active;
        boolean frozen;
        final List<String> events;

        FakeMode(boolean active, List<String> events) {
            this.active = active;
            this.events = events;
        }

        @Override public boolean isActive() { return active; }
        @Override public boolean isFrozen() { return frozen; }
        @Override public String currentEpoch() { return "epoch-one"; }
        @Override public void beginFreezing() {
            events.add("freezing");
            active = false;
        }
        @Override public void resumeActive() {
            events.add("resume_active");
            active = true;
            frozen = false;
        }
        @Override public void markFrozen(String packageId) {
            events.add("frozen");
            frozen = true;
        }
        @Override public void markReturning(String packageId) {
            events.add("returning");
        }
        @Override public void deactivate() {
            events.add("inactive");
            active = false;
            frozen = false;
        }
    }

    private static final class FakePackages
            implements HomecomingReturnController.PackagePort {
        final List<HomecomingReturnPackageRepository.ReturnPackage> values =
                new ArrayList<>();
        List<String> events;
        boolean failBuild;
        boolean confirmed;

        @Override
        public HomecomingReturnPackageRepository.ReturnPackage freezeAndBuild(
                String epochId, long now) throws Exception {
            events.add("freeze");
            if (failBuild) throw new Exception("build failed");
            events.add("build");
            HomecomingReturnPackageRepository.ReturnPackage value =
                    packageValue("package-one", 2L);
            values.add(value);
            return value;
        }

        @Override
        public List<HomecomingReturnPackageRepository.ReturnPackage>
                pendingInSequence() {
            return new ArrayList<>(values);
        }

        @Override
        public void confirm(
                HomecomingReturnPackageRepository.ReturnPackage value,
                HomecomingReturnClient.Receipt receipt,
                long now) {
            events.add("confirm");
            values.remove(value);
            confirmed = true;
        }
    }

    private static final class FakeClient
            implements HomecomingReturnController.ClientPort {
        List<String> events;
        boolean failUpload;
        String uploadState = "received";
        final List<HomecomingReturnClient.Receipt> receipts = new ArrayList<>();
        final List<String> uploaded = new ArrayList<>();
        int nextReceipt;
        long uploadedHighest;
        HomecomingReturnClient.Receipt latest;

        @Override
        public HomecomingReturnClient.ServerState upload(
                String route,
                HomecomingReturnPackageRepository.ReturnPackage value)
                throws Exception {
            events.add("upload");
            uploaded.add(value.packageId);
            uploadedHighest = value.highestDeviceSeq;
            if (failUpload) throw new Exception("offline");
            return new HomecomingReturnClient.ServerState(
                    value.packageId, uploadState);
        }

        @Override public HomecomingReturnClient.Plan dryRun(
                String route, String packageId) {
            events.add("dry_run");
            return new HomecomingReturnClient.Plan(
                    "plan", Collections.<String, Integer>emptyMap());
        }

        @Override public HomecomingReturnClient.Receipt apply(
                String route, String packageId) {
            events.add("apply");
            latest = nextReceipt < receipts.size()
                    ? receipts.get(nextReceipt++)
                    : new HomecomingReturnClient.Receipt(
                            "plan", packageId, uploadedHighest,
                            Collections.<String, Integer>emptyMap(),
                            "summary", true, false);
            return withPackage(latest, packageId);
        }

        @Override public HomecomingReturnClient.Receipt status(
                String route, String packageId) {
            events.add("status");
            return withPackage(latest, packageId);
        }

        @Override public void cancel() {
        }

        private static HomecomingReturnClient.Receipt withPackage(
                HomecomingReturnClient.Receipt source, String packageId) {
            return new HomecomingReturnClient.Receipt(
                    source.importSessionId, packageId,
                    source.acceptedHighestDeviceSeq, source.counts,
                    source.resultSummarySha256, source.complete, source.retryable);
        }
    }

    private static final class FakeRoute
            implements HomecomingReturnController.ReturnRoute {
        List<String> events;
        @Override public String serverPageUrl() {
            return "https://house.example/chat";
        }
        @Override public void stopHomecoming() {
            events.add("stop_homecoming");
        }
        @Override public void resumeHomecoming() {
            events.add("resume_homecoming");
        }
        @Override public void openNormal() {
            events.add("open_normal");
        }
    }
}
