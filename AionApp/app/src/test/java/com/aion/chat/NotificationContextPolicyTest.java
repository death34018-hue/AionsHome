package com.aion.chat;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

public class NotificationContextPolicyTest {
    @Test public void ownForegroundAndMediaNotificationsAreNoise() {
        assertTrue(NotificationContextPolicy.isNoise(
                "com.aion.chat", "com.aion.chat", "service", true, false,
                "Aion", "运行中"));
        assertTrue(NotificationContextPolicy.isNoise(
                "com.aion.chat", "com.music", "transport", true, false,
                "歌曲", "播放中"));
    }

    @Test public void ordinaryMessageIsNotLocallyDiscarded() {
        assertFalse(NotificationContextPolicy.isNoise(
                "com.aion.chat", "com.wechat", "message", false, false,
                "宝宝", "晚上见"));
    }

    @Test public void vivoLiveCardIsRetainedEvenWhenCategorizedAsProgress() {
        assertFalse(NotificationContextPolicy.isNoise(
                "com.aion.chat", "me.ele", "progress", false, true,
                "骑士正在送货", "距你1.6km，预计12分钟送达"));
    }

    @Test public void recognizableLiveStatusSurvivesButOrdinaryProgressDoesNot() {
        assertFalse(NotificationContextPolicy.isNoise(
                "com.aion.chat", "com.airline", "progress", false, false,
                "航班延误", "预计18:20起飞"));
        assertTrue(NotificationContextPolicy.isNoise(
                "com.aion.chat", "com.browser", "progress", false, false,
                "正在下载", "42%"));
    }

    @Test public void payloadFingerprintChangesOnlyWhenObservedContentChanges() {
        String first = NotificationContextPolicy.fingerprint(
                " 骑士正在送货 ", "预计 12 分钟送达", "PROGRESS", false);
        String normalized = NotificationContextPolicy.fingerprint(
                "骑士正在送货", "预计 12 分钟送达", "progress", false);
        String updated = NotificationContextPolicy.fingerprint(
                "骑士正在送货", "预计 10 分钟送达", "progress", false);

        assertEquals(first, normalized);
        assertFalse(first.equals(updated));
    }

    @Test public void accessibilityFallbackAcceptsLiveStatusButRejectsPromotion() {
        assertTrue(NotificationContextPolicy.isImportantLiveStatus(
                "司机正在赶来", "距你800米"));
        assertTrue(NotificationContextPolicy.isImportantLiveStatus(
                "快递已到驿站", "请及时取件"));
        assertFalse(NotificationContextPolicy.isImportantLiveStatus(
                "周末大促", "限时五折，立即购买"));
        assertFalse(NotificationContextPolicy.isImportantLiveStatus(
                "外卖红包", "领券后下单更优惠"));
    }

    @Test public void accessibilityLiveIdentityIsStablePerPackageAndScene() {
        assertEquals("me.ele:accessibility-live:takeout",
                NotificationContextPolicy.liveIdentity("me.ele", "TAKEOUT"));
        assertEquals("me.ele:accessibility-live:status",
                NotificationContextPolicy.liveIdentity(" me.ele ", ""));
    }

    @Test public void accessibilityFallbackOnlyOwnsSuperXCards() {
        assertTrue(NotificationContextPolicy.shouldForwardAccessibility(
                true, "订单已送达", "期待再次光临"));
        assertFalse(NotificationContextPolicy.shouldForwardAccessibility(
                false, "快递已到驿站", "请及时取件"));
    }

    @Test public void sameKeyPayloadCarriesStableIdentity() {
        assertEquals("com.food:42", NotificationContextPolicy.identity("com.food", "42"));
        assertEquals("com.food:42", NotificationContextPolicy.identity("com.food", "42"));
    }

    @Test public void onlyNotificationsFromTheFreshWindowAreForwarded() {
        long now = 10_000_000L;
        assertTrue(NotificationContextPolicy.isFresh(now - 30 * 60_000L, now));
        assertFalse(NotificationContextPolicy.isFresh(now - 30 * 60_000L - 1L, now));
    }
}
