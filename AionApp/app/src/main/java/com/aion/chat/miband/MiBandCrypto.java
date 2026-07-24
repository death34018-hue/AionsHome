package com.aion.chat.miband;

import org.bouncycastle.asn1.x9.X9ECParameters;
import org.bouncycastle.asn1.x9.X9ECPoint;
import org.bouncycastle.crypto.ec.CustomNamedCurves;
import org.bouncycastle.math.ec.ECPoint;

import java.math.BigInteger;
import java.security.GeneralSecurityException;
import java.security.SecureRandom;
import java.util.Arrays;

import javax.crypto.Cipher;
import javax.crypto.spec.SecretKeySpec;

public final class MiBandCrypto {
    private static final int PRIVATE_BYTES = 24;
    private static final X9ECParameters CURVE = requireCurve();
    private static final SecureRandom RANDOM = new SecureRandom();

    private MiBandCrypto() {}

    public static KeyPair generateKeyPair() {
        while (true) {
            byte[] little = new byte[PRIVATE_BYTES];
            RANDOM.nextBytes(little);
            little[20] &= 0x03;
            little[21] = 0;
            little[22] = 0;
            little[23] = 0;
            if (toBigIntegerLittle(little).bitLength() >= 82) return keyPairFromPrivate(little);
        }
    }

    public static KeyPair keyPairFromPrivate(byte[] privateKey) {
        BigInteger scalar = privateScalar(privateKey);
        ECPoint point = CURVE.getG().multiply(scalar).normalize();
        byte[] x = toLittleFixed(point.getAffineXCoord().toBigInteger(), PRIVATE_BYTES);
        byte[] y = toLittleFixed(point.getAffineYCoord().toBigInteger(), PRIVATE_BYTES);
        byte[] publicKey = new byte[48];
        System.arraycopy(x, 0, publicKey, 0, 24);
        System.arraycopy(y, 0, publicKey, 24, 24);
        return new KeyPair(Arrays.copyOf(privateKey, privateKey.length), publicKey);
    }

    public static byte[] deriveSharedX(byte[] privateKey, byte[] remotePublic) {
        if (remotePublic == null || remotePublic.length != 48) {
            throw new IllegalArgumentException("B163 public key must contain 48 bytes");
        }
        BigInteger x = toBigIntegerLittle(Arrays.copyOfRange(remotePublic, 0, 24));
        BigInteger y = toBigIntegerLittle(Arrays.copyOfRange(remotePublic, 24, 48));
        ECPoint remote = CURVE.getCurve().createPoint(x, y).normalize();
        if (!remote.isValid()) throw new IllegalArgumentException("invalid B163 public key");
        ECPoint shared = remote.multiply(privateScalar(privateKey)).normalize();
        return toLittleFixed(shared.getAffineXCoord().toBigInteger(), PRIVATE_BYTES);
    }

    public static AuthResponse buildAuthResponse(byte[] authKey, KeyPair local,
                                                  byte[] remoteRandom, byte[] remotePublic)
            throws GeneralSecurityException {
        if (authKey == null || authKey.length != 16) throw new IllegalArgumentException("auth key must be 16 bytes");
        if (remoteRandom == null || remoteRandom.length != 16) throw new IllegalArgumentException("remote random must be 16 bytes");
        byte[] sharedX = deriveSharedX(local.privateKey, remotePublic);
        byte[] sessionKey = new byte[16];
        for (int i = 0; i < sessionKey.length; i++) sessionKey[i] = (byte) (sharedX[i + 8] ^ authKey[i]);
        byte[] encryptedAuth = aes(authKey, remoteRandom);
        byte[] encryptedSession = aes(sessionKey, remoteRandom);
        byte[] command = new byte[33];
        command[0] = 0x05;
        System.arraycopy(encryptedAuth, 0, command, 1, 16);
        System.arraycopy(encryptedSession, 0, command, 17, 16);
        long sequence = (long) (sharedX[0] & 0xff)
                | ((long) (sharedX[1] & 0xff) << 8)
                | ((long) (sharedX[2] & 0xff) << 16)
                | ((long) (sharedX[3] & 0xff) << 24);
        return new AuthResponse(command, sessionKey, sequence);
    }

    private static byte[] aes(byte[] key, byte[] value) throws GeneralSecurityException {
        Cipher cipher = Cipher.getInstance("AES/ECB/NoPadding");
        cipher.init(Cipher.ENCRYPT_MODE, new SecretKeySpec(key, "AES"));
        return cipher.doFinal(value);
    }

    private static BigInteger privateScalar(byte[] privateKey) {
        if (privateKey == null || privateKey.length != PRIVATE_BYTES) {
            throw new IllegalArgumentException("B163 private key must contain 24 bytes");
        }
        BigInteger scalar = toBigIntegerLittle(privateKey).and(BigInteger.ONE.shiftLeft(162).subtract(BigInteger.ONE));
        if (scalar.bitLength() < 82) throw new IllegalArgumentException("B163 private key is too small");
        return scalar;
    }

    private static BigInteger toBigIntegerLittle(byte[] little) {
        byte[] big = new byte[little.length];
        for (int i = 0; i < little.length; i++) big[i] = little[little.length - 1 - i];
        return new BigInteger(1, big);
    }

    private static byte[] toLittleFixed(BigInteger value, int length) {
        byte[] big = value.toByteArray();
        byte[] little = new byte[length];
        for (int i = 0; i < length && i < big.length; i++) little[i] = big[big.length - 1 - i];
        return little;
    }

    private static X9ECParameters requireCurve() {
        X9ECParameters params = CustomNamedCurves.getByName("sect163r2");
        if (params == null) throw new IllegalStateException("sect163r2 is unavailable");
        return params;
    }

    public static final class KeyPair {
        public final byte[] privateKey;
        public final byte[] publicKey;

        KeyPair(byte[] privateKey, byte[] publicKey) {
            this.privateKey = privateKey;
            this.publicKey = publicKey;
        }
    }

    public static final class AuthResponse {
        public final byte[] command;
        public final byte[] sessionKey;
        public final long encryptedSequence;

        AuthResponse(byte[] command, byte[] sessionKey, long encryptedSequence) {
            this.command = command;
            this.sessionKey = sessionKey;
            this.encryptedSequence = encryptedSequence;
        }
    }
}
