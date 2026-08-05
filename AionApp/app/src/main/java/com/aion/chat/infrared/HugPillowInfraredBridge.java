package com.aion.chat.infrared;

import android.content.Context;
import android.hardware.ConsumerIrManager;
import android.webkit.JavascriptInterface;

public final class HugPillowInfraredBridge {
    public static final int CARRIER_HZ = 38000;

    private final IrTransmitter transmitter;

    public HugPillowInfraredBridge(Context context) {
        this(new AndroidIrTransmitter(context));
    }

    HugPillowInfraredBridge(IrTransmitter transmitter) {
        this.transmitter = transmitter;
    }

    @JavascriptInterface
    public String getStatus() {
        if (!transmitter.hasEmitter()) {
            return failure("NO_EMITTER", "手机没有可用的红外发射器", false);
        }
        if (!transmitter.supportsCarrier(CARRIER_HZ)) {
            return failure("UNSUPPORTED_CARRIER", "手机不支持 38 kHz 红外载波", false);
        }
        return success();
    }

    @JavascriptInterface
    public String transmit(String commandKey) {
        HugPillowCommand command = HugPillowCommand.fromKey(commandKey);
        if (command == null) {
            return failure("UNKNOWN_COMMAND", "未知的抱枕指令", false);
        }
        if (!transmitter.hasEmitter()) {
            return failure("NO_EMITTER", "手机没有可用的红外发射器", false);
        }
        if (!transmitter.supportsCarrier(CARRIER_HZ)) {
            return failure("UNSUPPORTED_CARRIER", "手机不支持 38 kHz 红外载波", false);
        }

        try {
            int[] pattern = NecIrEncoder.encode(
                    command.address(),
                    command.inverseAddress(),
                    command.command());
            transmitter.transmit(CARRIER_HZ, pattern);
            return success();
        } catch (RuntimeException error) {
            return failure("TRANSMIT_FAILED", "红外发射失败", true);
        }
    }

    interface IrTransmitter {
        boolean hasEmitter();

        boolean supportsCarrier(int carrierHz);

        void transmit(int carrierHz, int[] pattern);
    }

    private static final class AndroidIrTransmitter implements IrTransmitter {
        private final ConsumerIrManager manager;

        AndroidIrTransmitter(Context context) {
            manager = (ConsumerIrManager) context.getSystemService(
                    Context.CONSUMER_IR_SERVICE);
        }

        @Override
        public boolean hasEmitter() {
            return manager != null && manager.hasIrEmitter();
        }

        @Override
        public boolean supportsCarrier(int carrierHz) {
            if (manager == null) {
                return false;
            }
            ConsumerIrManager.CarrierFrequencyRange[] ranges =
                    manager.getCarrierFrequencies();
            if (ranges == null) {
                return true;
            }
            for (ConsumerIrManager.CarrierFrequencyRange range : ranges) {
                if (range.getMinFrequency() <= carrierHz
                        && carrierHz <= range.getMaxFrequency()) {
                    return true;
                }
            }
            return false;
        }

        @Override
        public void transmit(int carrierHz, int[] pattern) {
            manager.transmit(carrierHz, pattern);
        }
    }

    private static String success() {
        return "{\"ok\":true,\"available\":true,\"carrierHz\":38000}";
    }

    private static String failure(
            String error,
            String message,
            boolean available) {
        return "{\"ok\":false,\"available\":" + available
                + ",\"error\":\"" + error
                + "\",\"message\":\"" + message + "\"}";
    }
}
