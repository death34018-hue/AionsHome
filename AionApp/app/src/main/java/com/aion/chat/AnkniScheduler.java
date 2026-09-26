package com.aion.chat;

/** Phone-owned timer. Returning to chat never transfers timing to a hidden web page. */
public final class AnkniScheduler {
    public interface Writer { boolean write(String hex); }
    public interface Cancellation { void cancel(); }
    public interface Clock { Cancellation schedule(Runnable runnable,long delay); }
    public interface Listener {
        void onAction(String action);
        default void onReplace() {}
    }
    private final Writer writer;
    private final Clock clock;
    private final Listener listener;
    private Cancellation next;
    private int generation;
    private int activeMode = -1;
    private boolean timelineRunning, firstCycleComplete;
    private AnkniProtocol.Command pending;
    private boolean swap;
    private String prefix="0F";
    public AnkniScheduler(Writer writer,Clock clock,Listener listener) {
        this.writer=writer;this.clock=clock;this.listener=listener;
    }
    public synchronized void cancel() {
        generation++; activeMode=-1; timelineRunning=false; firstCycleComplete=false; pending=null;
        if(next!=null) next.cancel(); next=null;
    }
    public synchronized void configure(boolean swap,String prefix) {
        AnkniProtocol.prefix(prefix); stop(); this.swap=swap;this.prefix=prefix;
    }
    public synchronized void stop() {
        cancel();
        listener.onReplace();
        activeMode=0;
        for(String hex:AnkniProtocol.stop(prefix)) writer.write(hex);
        listener.onAction("STOP");
    }
    public synchronized void execute(String raw) {
        AnkniProtocol.Command c=AnkniProtocol.parse(raw);
        if(c.kind.equals("STOP")) {stop();return;}
        if((c.kind.equals("LOOP") || c.kind.equals("SEQ")) && timelineRunning && !firstCycleComplete) {
            pending=c;
            return;
        }
        start(c);
    }
    private void start(AnkniProtocol.Command c) {
        cancel();
        listener.onReplace();
        if(c.kind.equals("SET")) {write(c.vibration,c.suction);return;}
        if(c.kind.equals("MODE")) {mode(c.mode);return;}
        timelineRunning=true;
        step(c,0,generation);
    }
    private void write(int v,int s) {
        activeMode=-1;
        if(!writer.write(AnkniProtocol.intensity(v,s,swap))) {cancel();return;}
        listener.onAction("SET:"+v+","+s);
    }
    private void mode(int value) {
        if(activeMode==value) return;
        String[] packets=value==0 ? AnkniProtocol.stop(prefix) : new String[]{AnkniProtocol.classic(value,prefix)};
        boolean accepted=true;
        for(String hex:packets) if(!writer.write(hex)) accepted=false;
        if(!accepted) {cancel();return;}
        activeMode=value;
        listener.onAction("MODE:"+value);
    }
    private synchronized void step(AnkniProtocol.Command c,int index,int token) {
        if(token!=generation) return;
        if(index==c.phases.size()) {
            firstCycleComplete=true;
            if(pending!=null) {AnkniProtocol.Command replacement=pending;start(replacement);return;}
            if(c.kind.equals("SEQ")){stop();return;}
            index=0;
        }
        int[] phase=c.phases.get(index); mode(phase[1]);
        final int following=index+1;
        if(token==generation) next=clock.schedule(()->step(c,following,token),phase[0]);
    }
}
