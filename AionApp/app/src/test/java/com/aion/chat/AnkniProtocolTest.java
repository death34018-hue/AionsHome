package com.aion.chat;
import org.junit.Test;
import static org.junit.Assert.*;
import java.util.ArrayList;
import java.util.List;

public class AnkniProtocolTest {
    @Test public void matchesVerifiedDualChannelPackets() {
        assertEquals("AA08030500641E", AnkniProtocol.intensity(5, 0, false));
        assertEquals("AA08030005641E", AnkniProtocol.intensity(5, 0, true));
        assertEquals("AA0803000000B5", AnkniProtocol.stop()[0]);
        assertEquals("AA0F020101BD", AnkniProtocol.classic(1, "0F"));
        assertEquals(2, AnkniProtocol.parse("LOOP:2000,5;3000,6").phases.size());
    }
    @Test public void durationsRunTwoSecondsThenThreeAndRepeat() {
        List<String> writes = new ArrayList<>();
        List<Runnable> timers = new ArrayList<>();
        List<Long> delays = new ArrayList<>();
        AnkniScheduler scheduler = new AnkniScheduler(hex -> { writes.add(hex); return true; },
                (task, delay) -> { timers.add(task); delays.add(delay); return () -> {}; }, action -> {});
        scheduler.execute("LOOP:2000,5;3000,6");
        assertEquals("AA0F020505C5", writes.get(0));
        assertEquals(Long.valueOf(2000), delays.get(0));
        timers.get(0).run();
        assertEquals("AA0F020606C7", writes.get(1));
        assertEquals(Long.valueOf(3000), delays.get(1));
        timers.get(1).run();
        assertEquals("AA0F020505C5", writes.get(2));
        assertEquals(Long.valueOf(2000), delays.get(2));
        scheduler.execute("SEQ:2000,5;3000,5");
        int before = writes.size();
        timers.get(timers.size()-1).run();
        assertEquals(before, writes.size());
        timers.get(timers.size()-1).run();
        assertEquals("AA0F020000BB",writes.get(writes.size()-1));
    }
    @Test public void rejectsOldShortAndOverlongTimelines() {
        assertEquals(3,AnkniProtocol.parse("LOOP:2000,5;1000,6;1000,0").phases.size());
        for(String bad:new String[]{"LOOP:200,5,0","LOOP:2000,5;0,6","LOOP:2000,5;199,6","LOOP:400000,1;400000,2","LOOP:200,11","LOOP:600001,1"}) {
            try {AnkniProtocol.parse(bad);fail("accepted "+bad);}catch(IllegalArgumentException expected) {}
        }
    }
    @Test(expected=IllegalArgumentException.class) public void rejectsOutOfRangeStrength() {
        AnkniProtocol.parse("LOOP:200,101,0");
    }
    @Test public void replacementWaitsForFirstCycleAndKeepsLatestPendingPlan() {
        List<String> writes = new ArrayList<>();
        List<Runnable> timers = new ArrayList<>();
        List<String> replacements = new ArrayList<>();
        AnkniScheduler scheduler = new AnkniScheduler(hex -> { writes.add(hex); return true; },
                (task, delay) -> { timers.add(task); return () -> {}; }, new AnkniScheduler.Listener() {
                    public void onAction(String action) {}
                    public void onReplace() { replacements.add("invalidate"); }
                });
        scheduler.execute("LOOP:2000,5;3000,6");
        scheduler.execute("LOOP:1000,2");
        scheduler.execute("LOOP:1000,3");
        assertEquals(1,writes.size());
        assertEquals("waiting must not invalidate current BLE writes",1,replacements.size());
        timers.get(0).run();
        assertEquals("AA0F020606C7",writes.get(1));
        timers.get(1).run();
        assertEquals("AA0F020303C1",writes.get(2));
        assertEquals("invalidate old writes only when the pending plan starts",2,replacements.size());
        timers.get(2).run();
        scheduler.execute("LOOP:1000,4");
        assertEquals("AA0F020404C3",writes.get(3));
        scheduler.execute("LOOP:1000,8");
        Runnable old=timers.get(timers.size()-1);
        scheduler.stop();
        int count=writes.size();old.run();
        assertEquals(count,writes.size());
        assertEquals("AA0F020000BB",writes.get(count-1));
    }
    @Test public void timedStopResumesWithoutEndingThePlan() {
        List<String> writes = new ArrayList<>();
        List<Runnable> timers = new ArrayList<>();
        List<Long> delays = new ArrayList<>();
        AnkniScheduler scheduler = new AnkniScheduler(hex -> { writes.add(hex); return true; },
                (task, delay) -> { timers.add(task); delays.add(delay); return () -> {}; }, action -> {});
        scheduler.execute("LOOP:2000,5;3000,0;4000,6");
        timers.get(0).run();
        assertEquals("AA0803000000B5",writes.get(1));
        assertEquals("AA0F020000BB",writes.get(2));
        assertEquals(Long.valueOf(3000),delays.get(1));
        timers.get(1).run();
        assertEquals("AA0F020606C7",writes.get(3));
        assertEquals(Long.valueOf(4000),delays.get(2));
        assertEquals(9000L,delays.get(0)+delays.get(1)+delays.get(2));
        timers.get(2).run();
        assertEquals("AA0F020505C5",writes.get(4));
    }
    @Test public void stopCancelsPendingLoopAndNewPlansReplaceOldOnes() {
        List<String> writes = new ArrayList<>();
        List<Runnable> timers = new ArrayList<>();
        AnkniScheduler scheduler = new AnkniScheduler(hex -> { writes.add(hex); return true; },
                (runnable, delay) -> { timers.add(runnable); return () -> {}; }, action -> {});
        scheduler.execute("LOOP:2000,5;3000,6");
        Runnable first = timers.get(0);
        scheduler.execute("SET:3,0");
        int count = writes.size();
        first.run();
        assertEquals(count, writes.size());
        scheduler.execute("LOOP:2000,5;3000,6");
        Runnable old = timers.get(timers.size()-1);
        scheduler.stop();
        count = writes.size();
        old.run();
        assertEquals(count, writes.size());
        assertEquals("AA0F020000BB", writes.get(count-1));
    }
}
