package com.aion.chat.miband;

import org.junit.Test;

import static org.junit.Assert.*;

public class MiBandCryptoTest {
    private static final byte[] PRIVATE = MiBandProtocolTest.hex(
            "0b42b9e61c23340e35c16e2e7de433f4b5859a72ec114027");
    private static final byte[] REMOTE_PUBLIC = MiBandProtocolTest.hex(
            "e6016aba1de7ac0f0c7f0ff7e2243e6662b5e03b01000000" +
            "ad8a4bedc76a1efde7725cc662b54835513e3d5705000000");
    private static final byte[] EXPECTED_PUBLIC = MiBandProtocolTest.hex(
            "a7bf78d434e3bd908a1f16c50fcf87307d814dc005000000" +
            "d8f4a2385f110e16062d9da27f5f1c3038abb56404000000");
    private static final byte[] EXPECTED_SHARED_X = MiBandProtocolTest.hex(
            "119952464b0ff4f0ce59dc012af77a00502efb5e06000000");

    @Test
    public void b163EncodingMatchesKnownMiBandVector() {
        MiBandCrypto.KeyPair pair = MiBandCrypto.keyPairFromPrivate(PRIVATE);

        assertArrayEquals(PRIVATE, pair.privateKey);
        assertArrayEquals(EXPECTED_PUBLIC, pair.publicKey);
        assertArrayEquals(EXPECTED_SHARED_X, MiBandCrypto.deriveSharedX(PRIVATE, REMOTE_PUBLIC));
    }

    @Test
    public void authResponseDerivesSessionKeyAndSequence() throws Exception {
        byte[] authKey = MiBandProtocolTest.hex("00112233445566778899aabbccddeeff");
        byte[] remoteRandom = new byte[16];
        for (int i = 0; i < remoteRandom.length; i++) remoteRandom[i] = (byte) i;

        MiBandCrypto.AuthResponse response = MiBandCrypto.buildAuthResponse(
                authKey,
                MiBandCrypto.keyPairFromPrivate(PRIVATE),
                remoteRandom,
                REMOTE_PUBLIC);

        byte[] expectedSession = new byte[16];
        for (int i = 0; i < expectedSession.length; i++) {
            expectedSession[i] = (byte) (EXPECTED_SHARED_X[i + 8] ^ authKey[i]);
        }
        assertEquals(33, response.command.length);
        assertEquals(0x05, response.command[0] & 0xff);
        assertArrayEquals(expectedSession, response.sessionKey);
        assertEquals(0x46529911L, response.encryptedSequence);
        assertFalse(equalRange(response.command, 1, remoteRandom));
        assertFalse(equalRange(response.command, 17, remoteRandom));
    }

    private static boolean equalRange(byte[] source, int offset, byte[] expected) {
        if (source.length < offset + expected.length) return false;
        for (int i = 0; i < expected.length; i++) {
            if (source[offset + i] != expected[i]) return false;
        }
        return true;
    }
}
