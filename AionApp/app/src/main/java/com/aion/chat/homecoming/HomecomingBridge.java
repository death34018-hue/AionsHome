package com.aion.chat.homecoming;

import android.webkit.JavascriptInterface;

public final class HomecomingBridge {
    private final HomecomingActivity activity;

    HomecomingBridge(HomecomingActivity activity) {
        this.activity = activity;
    }

    @JavascriptInterface
    public String getReadinessJson() {
        return activity.readinessJson();
    }

    @JavascriptInterface
    public void requestRefresh() {
        activity.runOnUiThread(activity::requestRefresh);
    }

    @JavascriptInterface
    public void confirmEnter() {
        activity.runOnUiThread(activity::confirmEnter);
    }

    @JavascriptInterface
    public void cancelEnter() {
        activity.runOnUiThread(activity::cancelEnter);
    }

    @JavascriptInterface
    public void requestFoundationReturn() {
        activity.runOnUiThread(activity::requestFoundationReturn);
    }

    @JavascriptInterface
    public void startReturnSync() {
        activity.runOnUiThread(activity::startReturnSync);
    }

    @JavascriptInterface
    public void retryReturnSync() {
        activity.runOnUiThread(activity::retryReturnSync);
    }

    @JavascriptInterface
    public void returnWithoutSync() {
        activity.runOnUiThread(activity::returnWithoutSync);
    }

    @JavascriptInterface
    public String getReturnStateJson() {
        return activity.returnStateJson();
    }

    @JavascriptInterface
    public boolean isActive() {
        return activity.isHomecomingActive();
    }

    @JavascriptInterface
    public String getBootstrapJson() {
        return activity.bootstrapJson();
    }

    @JavascriptInterface
    public String getSupervisionStatusJson() {
        return activity.supervisionStatusJson();
    }

    @JavascriptInterface
    public String getMessagesJson(String timelineId, long beforeCreatedAt, int limit) {
        return activity.messagesJson(timelineId, beforeCreatedAt, limit);
    }

    @JavascriptInterface
    public void sendMessage(String requestId, String timelineId, String responderOwner,
            String text, String routeId, String modelId) {
        activity.sendMessage(
                requestId, timelineId, responderOwner, text, routeId, modelId);
    }

    @JavascriptInterface
    public void stopMessage(String requestId) {
        activity.stopMessage(requestId);
    }

    @JavascriptInterface
    public void setTtsEnabled(boolean enabled) {
        activity.setTtsEnabled(enabled);
    }

    @JavascriptInterface
    public void setRoutePreference(
            String ownerId, String routeId, String modelId) {
        activity.setRoutePreference(ownerId, routeId, modelId);
    }

    @JavascriptInterface
    public void replayTts(String messageId) {
        activity.replayTts(messageId);
    }

    @JavascriptInterface
    public String getMemoriesJson(String ownerId, String query) {
        return activity.memoriesJson(ownerId, query);
    }

    @JavascriptInterface
    public void summarizeMemories(String ownerId, String routeId, String modelId) {
        activity.summarizeMemories(ownerId, routeId, modelId);
    }

    @JavascriptInterface
    public void summarizeAllMemories() {
        activity.summarizeAllMemories();
    }

    @JavascriptInterface
    public String createMemory(String ownerId, String content, String keywords) {
        return activity.createMemory(ownerId, content, keywords);
    }

    @JavascriptInterface
    public String updateMemory(
            String ownerId, String memoryId, String content, String baseHash) {
        return activity.updateMemory(ownerId, memoryId, content, baseHash);
    }

    @JavascriptInterface
    public boolean deleteMemory(String ownerId, String memoryId, String baseHash) {
        return activity.deleteMemory(ownerId, memoryId, baseHash);
    }

    @JavascriptInterface
    public String listSchedules() {
        return activity.schedulesJson();
    }

    @JavascriptInterface
    public String createSchedule(
            String type,
            long triggerAt,
            String content,
            String ownerId,
            String timelineId) {
        return activity.createSchedule(
                type, triggerAt, content, ownerId, timelineId);
    }

    @JavascriptInterface
    public boolean deleteSchedule(String id) {
        return activity.deleteSchedule(id);
    }

    @JavascriptInterface
    public void pickImage() {
        activity.runOnUiThread(activity::pickImage);
    }

    @JavascriptInterface
    public void captureImage() {
        activity.runOnUiThread(activity::captureImage);
    }
}
