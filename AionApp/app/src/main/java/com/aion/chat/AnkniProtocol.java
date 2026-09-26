package com.aion.chat;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

/** Verified ANKNI devType 1 packets. No SOSEXY framing or SVAKOM semantics. */
public final class AnkniProtocol {
    public static final class Command {
        final String kind;
        final List<int[]> phases = new ArrayList<>();
        int vibration, suction, mode;
        Command(String kind) { this.kind = kind; }
    }
    private static int number(int value, int min, int max) {
        if (value < min || value > max) throw new IllegalArgumentException("ANKNI 参数超出范围");
        return value;
    }
    private static String packet(int command, int... payload) {
        int sum = 0xAA + command + payload.length;
        StringBuilder hex = new StringBuilder(String.format(Locale.US,"AA%02X%02X",command,payload.length));
        for (int value : payload) { sum += value; hex.append(String.format(Locale.US,"%02X",value)); }
        return hex.append(String.format(Locale.US,"%02X",sum & 255)).toString();
    }
    public static String intensity(int v, int s, boolean swap) {
        number(v,0,100); number(s,0,100);
        return swap ? packet(8,s,v,100) : packet(8,v,s,100);
    }
    public static int prefix(String value) {
        if (!"0F".equals(value) && !"0A".equals(value)) throw new IllegalArgumentException("无效模式前缀");
        return Integer.parseInt(value,16);
    }
    public static String classic(int mode, String prefix) { number(mode,1,10); return packet(prefix(prefix),mode,mode); }
    public static String[] stop() { return stop("0F"); }
    public static String[] stop(String prefix) { return new String[]{packet(8,0,0,0),packet(prefix(prefix),0,0)}; }
    public static Command parse(String value) {
        String raw = value == null ? "" : value.replaceAll("\\s+", "").toUpperCase(Locale.US);
        if (raw.length()>8192) throw new IllegalArgumentException("ANKNI 指令过长");
        if (raw.equals("STOP")) return new Command("STOP");
        if (raw.matches("SET:[0-9]{1,3},[0-9]{1,3}")) {
            String[] parts=raw.substring(4).split(","); Command c=new Command("SET");
            c.vibration=number(Integer.parseInt(parts[0]),0,100); c.suction=number(Integer.parseInt(parts[1]),0,100); return c;
        }
        if (raw.matches("MODE:[0-9]{1,2}")) {
            Command c=new Command("MODE"); c.mode=number(Integer.parseInt(raw.substring(5)),1,10); return c;
        }
        if (!raw.startsWith("LOOP:") && !raw.startsWith("SEQ:")) throw new IllegalArgumentException("无效 ANKNI 指令");
        Command c=new Command(raw.startsWith("LOOP:") ? "LOOP" : "SEQ");
        String[] rows=raw.substring(raw.indexOf(':')+1).split(";",-1);
        if(rows.length>128) throw new IllegalArgumentException("最多 128 段");
        int total=0;
        for(String row:rows) {
            if(!row.matches("[0-9]{1,6},[0-9]{1,2}")) throw new IllegalArgumentException("每段需要持续毫秒、模式编号");
            String[] fields=row.split(",");
            int duration=number(Integer.parseInt(fields[0]),200,600000), mode=number(Integer.parseInt(fields[1]),0,10);
            total=number(total+duration,200,600000);
            c.phases.add(new int[]{duration,mode});
        }
        return c;
    }
    private AnkniProtocol() {}
}
