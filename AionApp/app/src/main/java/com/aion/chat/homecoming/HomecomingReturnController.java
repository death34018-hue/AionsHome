package com.aion.chat.homecoming;

import java.util.List;

public final class HomecomingReturnController {
    private static final int MAX_FOREGROUND_APPLY_CALLS = 1000;
    private final ModePort mode;
    private final PackagePort packages;
    private final ClientPort client;
    private final Clock clock;

    public HomecomingReturnController(
            ModePort mode, PackagePort packages, ClientPort client, Clock clock) {
        if (mode == null || packages == null || client == null || clock == null) {
            throw new IllegalArgumentException("return controller dependencies are required");
        }
        this.mode = mode;
        this.packages = packages;
        this.client = client;
        this.clock = clock;
    }

    public synchronized void synchronize(ReturnRoute route, Observer observer) {
        if (route == null || observer == null) {
            throw new IllegalArgumentException("route and observer are required");
        }
        try {
            if (mode.isActive()) {
                observer.onPhase("freezing");
                mode.beginFreezing();
                HomecomingReturnPackageRepository.ReturnPackage built;
                try {
                    observer.onPhase("building");
                    built = packages.freezeAndBuild(mode.currentEpoch(), clock.now());
                } catch (Exception exception) {
                    observer.onDiagnostic("package_build", exception);
                    mode.resumeActive();
                    route.resumeHomecoming();
                    observer.onFailure("package_build_failed", false);
                    return;
                }
                if (built == null) {
                    route.stopHomecoming();
                    mode.deactivate();
                    observer.onPhase("complete");
                    route.openNormal();
                    return;
                }
                observer.onPhase("frozen");
                mode.markFrozen(built.packageId);
                route.stopHomecoming();
            }

            List<HomecomingReturnPackageRepository.ReturnPackage> pending =
                    packages.pendingInSequence();
            if (pending.isEmpty()) {
                observer.onFailure("no_pending_package", false);
                return;
            }
            for (HomecomingReturnPackageRepository.ReturnPackage value : pending) {
                observer.onPhase("uploading");
                mode.markReturning(value.packageId);
                HomecomingReturnClient.ServerState uploaded =
                        client.upload(route.serverPageUrl(), value);
                if (!value.packageId.equals(uploaded.packageId)) {
                    observer.onFailure("upload_package_mismatch", true);
                    return;
                }
                if ("confirmed".equals(uploaded.state)) {
                    observer.onPhase("verifying");
                    HomecomingReturnClient.Receipt verified =
                            client.status(route.serverPageUrl(), value.packageId);
                    if (!validReceipt(value, verified)) {
                        observer.onFailure("receipt_mismatch", true);
                        return;
                    }
                    observer.onPhase("confirming");
                    packages.confirm(value, verified, clock.now());
                    continue;
                }
                observer.onPhase("planning");
                HomecomingReturnClient.Plan plan =
                        client.dryRun(route.serverPageUrl(), value.packageId);
                observer.onCounts(plan.counts);
                HomecomingReturnClient.Receipt receipt = null;
                for (int attempt = 0; attempt < MAX_FOREGROUND_APPLY_CALLS; attempt++) {
                    observer.onPhase("applying");
                    receipt = client.apply(route.serverPageUrl(), value.packageId);
                    observer.onCounts(receipt.counts);
                    if (receipt.retryable) {
                        observer.onFailure("server_retryable", true);
                        return;
                    }
                    if (receipt.complete) break;
                }
                if (receipt == null || !receipt.complete) {
                    observer.onFailure("apply_incomplete", true);
                    return;
                }
                HomecomingReturnClient.Receipt verified =
                        client.status(route.serverPageUrl(), value.packageId);
                observer.onPhase("verifying");
                if (!validReceipt(value, verified)) {
                    observer.onFailure("receipt_mismatch", true);
                    return;
                }
                observer.onPhase("confirming");
                packages.confirm(value, verified, clock.now());
            }
            mode.deactivate();
            observer.onPhase("complete");
            route.openNormal();
        } catch (Exception exception) {
            observer.onDiagnostic("return_sync", exception);
            observer.onFailure("return_sync_failed", !mode.isActive());
        }
    }

    public synchronized void archiveAndReturn(ReturnRoute route) {
        if (route == null || mode.isActive() || !mode.isFrozen()) return;
        try {
            if (packages.pendingInSequence().isEmpty()) return;
            mode.deactivate();
            route.openNormal();
        } catch (Exception ignored) {
            // A force return is permitted only while the immutable package is readable.
        }
    }

    public void cancel() {
        client.cancel();
    }

    private static boolean validReceipt(
            HomecomingReturnPackageRepository.ReturnPackage value,
            HomecomingReturnClient.Receipt receipt) {
        return receipt != null
                && receipt.complete
                && !receipt.retryable
                && value.packageId.equals(receipt.packageId)
                && value.highestDeviceSeq == receipt.acceptedHighestDeviceSeq
                && receipt.resultSummarySha256 != null
                && !receipt.resultSummarySha256.trim().isEmpty();
    }

    public interface ModePort {
        boolean isActive();
        boolean isFrozen();
        String currentEpoch();
        void beginFreezing();
        void resumeActive();
        void markFrozen(String packageId);
        void markReturning(String packageId);
        void deactivate();
    }

    public interface PackagePort {
        HomecomingReturnPackageRepository.ReturnPackage freezeAndBuild(
                String epochId, long now) throws Exception;
        List<HomecomingReturnPackageRepository.ReturnPackage>
                pendingInSequence() throws Exception;
        void confirm(
                HomecomingReturnPackageRepository.ReturnPackage value,
                HomecomingReturnClient.Receipt receipt,
                long now) throws Exception;
    }

    public interface ClientPort {
        HomecomingReturnClient.ServerState upload(
                String route,
                HomecomingReturnPackageRepository.ReturnPackage value) throws Exception;
        HomecomingReturnClient.Plan dryRun(
                String route, String packageId) throws Exception;
        HomecomingReturnClient.Receipt apply(
                String route, String packageId) throws Exception;
        HomecomingReturnClient.Receipt status(
                String route, String packageId) throws Exception;
        void cancel();
    }

    public interface ReturnRoute {
        String serverPageUrl();
        void stopHomecoming();
        void resumeHomecoming();
        void openNormal();
    }

    public interface Observer {
        void onPhase(String phase);
        void onFailure(String code, boolean canReturnWithoutSync);
        default void onDiagnostic(String stage, Throwable failure) {
        }
        default void onCounts(java.util.Map<String, Integer> counts) {
        }
    }

    public interface Clock {
        long now();
    }
}
