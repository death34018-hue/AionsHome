package com.aion.chat;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

import java.util.List;

public class SuperXDumpParserTest {
    @Test public void extractsTakeoutCardFromVivoNotificationDump() {
        String dump = "NotificationRecord(0x1: pkg=me.ele user=UserHandle{0} "
                + "id=943427964 tag=VIVO_SUPERX_TAG importance=4)\n"
                + "  extras={\n"
                + "    android.title=String (骑士已到店)\n"
                + "    android.text=String (预计12分钟后可送出)\n"
                + "    notification.superx.scene=String (TAKEOUT)\n"
                + "    notification.superx.baseInfos=Bundle (Bundle[{"
                + "notification.superx.baseInfos.title=骑士已到店, "
                + "notification.superx.baseInfos.content=预计12分钟后可送出}])\n"
                + "  }\n"
                + "NotificationRecord(0x2: pkg=com.vivo.globaldragdrop user=UserHandle{0} "
                + "id=0 tag=VIVO_SUPERX_TAG importance=4)\n";

        List<SuperXDumpParser.Card> cards = SuperXDumpParser.parse(dump);

        assertEquals(1, cards.size());
        SuperXDumpParser.Card card = cards.get(0);
        assertEquals("me.ele", card.packageName);
        assertEquals("TAKEOUT", card.scene);
        assertEquals("骑士已到店", card.title);
        assertEquals("预计12分钟后可送出", card.text);
        assertFalse(card.terminal);
    }

    @Test public void recognizesTerminalDeliveryState() {
        String dump = "NotificationRecord(0x1: pkg=me.ele user=UserHandle{0} "
                + "id=1 tag=VIVO_SUPERX_TAG importance=4)\n"
                + "android.title=String (订单已送达)\n"
                + "android.text=String (期待再次光临)\n"
                + "notification.superx.scene=String (TAKEOUT)\n";

        SuperXDumpParser.Card card = SuperXDumpParser.parse(dump).get(0);

        assertTrue(card.terminal);
    }
}
