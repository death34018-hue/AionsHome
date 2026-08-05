package com.aion.chat.homecoming;

import android.content.Context;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;

import org.bouncycastle.util.encoders.Base64;
import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.ByteArrayOutputStream;
import java.nio.charset.StandardCharsets;
import java.security.GeneralSecurityException;
import java.security.KeyPairGenerator;
import java.security.KeyStore;
import java.security.PrivateKey;
import java.security.PublicKey;
import java.security.SecureRandom;
import java.security.Signature;
import java.security.spec.MGF1ParameterSpec;
import java.security.spec.PSSParameterSpec;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

import javax.crypto.Cipher;
import javax.crypto.KeyGenerator;
import javax.crypto.SecretKey;
import javax.crypto.spec.GCMParameterSpec;
import javax.crypto.spec.OAEPParameterSpec;
import javax.crypto.spec.PSource;
import javax.crypto.spec.SecretKeySpec;

public final class HomecomingKeyStore implements HomecomingBackupClient.KeyPort {
    public static final String DEVICE_KEY_ALIAS = "aionshome.homecoming.device.v1";
    public static final String ROUTE_KEY_ALIAS = "aionshome.homecoming.routes.v1";
    public static final String RETURN_SIGNING_KEY_ALIAS =
            "aionshome.homecoming.return-signing.v1";

    private static final String ANDROID_KEY_STORE = "AndroidKeyStore";
    private final Context context;
    private final String deviceId;

    public HomecomingKeyStore(Context context, String deviceId) {
        this.context = context.getApplicationContext();
        this.deviceId = required(deviceId, "deviceId");
    }

    @Override
    public String deviceId() {
        return deviceId;
    }

    @Override
    public String publicKeySpkiBase64() throws GeneralSecurityException {
        return getOrCreateDevicePublicKeySpkiBase64();
    }

    public String getOrCreateDevicePublicKeySpkiBase64() throws GeneralSecurityException {
        KeyStore store = loadStore();
        if (!store.containsAlias(DEVICE_KEY_ALIAS)) {
            KeyPairGenerator generator = KeyPairGenerator.getInstance(
                    KeyProperties.KEY_ALGORITHM_RSA, ANDROID_KEY_STORE);
            generator.initialize(new KeyGenParameterSpec.Builder(
                    DEVICE_KEY_ALIAS,
                    KeyProperties.PURPOSE_DECRYPT)
                    .setKeySize(3072)
                    .setDigests(KeyProperties.DIGEST_SHA256)
                    .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_RSA_OAEP)
                    .build());
            generator.generateKeyPair();
        }
        PublicKey publicKey = store.getCertificate(DEVICE_KEY_ALIAS).getPublicKey();
        return Base64.toBase64String(publicKey.getEncoded());
    }

    @Override
    public int decryptAndStoreRoutes(
            JSONObject envelope, String boundDeviceId, String snapshotId) throws Exception {
        KeyStore store = loadStore();
        PrivateKey privateKey = (PrivateKey) store.getKey(DEVICE_KEY_ALIAS, null);
        if (privateKey == null) {
            throw new GeneralSecurityException("device key is unavailable");
        }
        List<RouteDescriptor> descriptors = new RouteCryptoCodec(
                privateKey, new AndroidEncryptedSecretStore(context, store))
                .decryptAndStore(envelope, boundDeviceId, snapshotId);
        context.getSharedPreferences(
                HomecomingModeStore.PREFERENCES_NAME, Context.MODE_PRIVATE)
                .edit()
                .putInt("portable_route_count", descriptors.size())
                .apply();
        return descriptors.size();
    }

    public HomecomingRouteVault openRouteVault() throws Exception {
        KeyStore store = loadStore();
        byte[] plaintext = new AndroidEncryptedSecretStore(context, store).load();
        return HomecomingRouteVault.fromPlaintext(plaintext);
    }

    public String signingPublicKeySpkiBase64() throws GeneralSecurityException {
        KeyStore store = loadStore();
        ensureReturnSigningKey(store);
        return Base64.toBase64String(
                store.getCertificate(RETURN_SIGNING_KEY_ALIAS)
                        .getPublicKey().getEncoded());
    }

    public byte[] signReturnPayload(byte[] canonical) throws GeneralSecurityException {
        KeyStore store = loadStore();
        ensureReturnSigningKey(store);
        PrivateKey privateKey = (PrivateKey) store.getKey(
                RETURN_SIGNING_KEY_ALIAS, null);
        if (privateKey == null) {
            throw new GeneralSecurityException("return signing key is unavailable");
        }
        return new ReturnSigningCodec(privateKey).sign(canonical);
    }

    private static void ensureReturnSigningKey(KeyStore store)
            throws GeneralSecurityException {
        if (store.containsAlias(RETURN_SIGNING_KEY_ALIAS)) return;
        KeyPairGenerator generator = KeyPairGenerator.getInstance(
                KeyProperties.KEY_ALGORITHM_RSA, ANDROID_KEY_STORE);
        generator.initialize(new KeyGenParameterSpec.Builder(
                RETURN_SIGNING_KEY_ALIAS,
                KeyProperties.PURPOSE_SIGN | KeyProperties.PURPOSE_VERIFY)
                .setKeySize(3072)
                .setDigests(KeyProperties.DIGEST_SHA256)
                .setSignaturePaddings(KeyProperties.SIGNATURE_PADDING_RSA_PSS)
                .build());
        generator.generateKeyPair();
    }

    private static KeyStore loadStore() throws GeneralSecurityException {
        try {
            KeyStore store = KeyStore.getInstance(ANDROID_KEY_STORE);
            store.load(null);
            return store;
        } catch (GeneralSecurityException exception) {
            throw exception;
        } catch (Exception exception) {
            throw new GeneralSecurityException("could not load Android Keystore", exception);
        }
    }

    interface SecretStore {
        void store(byte[] plaintext) throws Exception;
    }

    static final class AndroidEncryptedSecretStore implements SecretStore {
        private final Context context;
        private final KeyStore keyStore;

        AndroidEncryptedSecretStore(Context context, KeyStore keyStore) {
            this.context = context;
            this.keyStore = keyStore;
        }

        @Override
        public void store(byte[] plaintext) throws Exception {
            if (!keyStore.containsAlias(ROUTE_KEY_ALIAS)) {
                KeyGenerator generator = KeyGenerator.getInstance(
                        KeyProperties.KEY_ALGORITHM_AES, ANDROID_KEY_STORE);
                generator.init(new KeyGenParameterSpec.Builder(
                        ROUTE_KEY_ALIAS,
                        KeyProperties.PURPOSE_ENCRYPT | KeyProperties.PURPOSE_DECRYPT)
                        .setKeySize(256)
                        .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                        .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                        .build());
                generator.generateKey();
            }
            SecretKey key = (SecretKey) keyStore.getKey(ROUTE_KEY_ALIAS, null);
            Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
            cipher.init(Cipher.ENCRYPT_MODE, key);
            byte[] ciphertext = cipher.doFinal(plaintext);
            JSONObject stored = new JSONObject()
                    .put("nonce_b64", Base64.toBase64String(cipher.getIV()))
                    .put("ciphertext_b64", Base64.toBase64String(ciphertext));
            File directory = new File(context.getFilesDir(), "homecoming");
            if (!directory.isDirectory() && !directory.mkdirs()) {
                throw new IllegalStateException("could not create Homecoming secret directory");
            }
            File target = new File(directory, "routes.enc");
            File temporary = new File(directory, "routes.enc.tmp");
            FileOutputStream output = new FileOutputStream(temporary);
            try {
                output.write(stored.toString().getBytes(StandardCharsets.UTF_8));
                output.getFD().sync();
            } finally {
                output.close();
            }
            if (target.exists() && !target.delete()) {
                throw new IllegalStateException("could not replace encrypted routes");
            }
            if (!temporary.renameTo(target)) {
                throw new IllegalStateException("could not activate encrypted routes");
            }
        }

        byte[] load() throws Exception {
            SecretKey key = (SecretKey) keyStore.getKey(ROUTE_KEY_ALIAS, null);
            if (key == null) {
                throw new GeneralSecurityException("route key is unavailable");
            }
            File target = new File(new File(context.getFilesDir(), "homecoming"), "routes.enc");
            FileInputStream input = new FileInputStream(target);
            ByteArrayOutputStream bytes = new ByteArrayOutputStream();
            try {
                byte[] buffer = new byte[4096];
                int count;
                while ((count = input.read(buffer)) != -1) {
                    bytes.write(buffer, 0, count);
                    if (bytes.size() > 4 * 1024 * 1024) {
                        throw new IllegalStateException("encrypted route store is too large");
                    }
                }
            } finally {
                input.close();
            }
            JSONObject stored = new JSONObject(
                    new String(bytes.toByteArray(), StandardCharsets.UTF_8));
            Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
            cipher.init(Cipher.DECRYPT_MODE, key, new GCMParameterSpec(
                    128, Base64.decode(stored.getString("nonce_b64"))));
            return cipher.doFinal(Base64.decode(stored.getString("ciphertext_b64")));
        }
    }

    static final class RouteCryptoCodec {
        private static final String ENVELOPE_ALGORITHM =
                "RSA-OAEP-256-MGF1-SHA1+A256GCM";
        private static final OAEPParameterSpec OAEP_SHA256 = new OAEPParameterSpec(
                "SHA-256",
                "MGF1",
                MGF1ParameterSpec.SHA1,
                PSource.PSpecified.DEFAULT);

        private final PrivateKey privateKey;
        private final SecretStore secretStore;

        RouteCryptoCodec(PrivateKey privateKey, SecretStore secretStore) {
            this.privateKey = privateKey;
            this.secretStore = secretStore;
        }

        List<RouteDescriptor> decryptAndStore(
                JSONObject envelope, String deviceId, String snapshotId) throws Exception {
            String algorithm = envelope.optString("algorithm", "");
            if (!ENVELOPE_ALGORITHM.equals(algorithm)) {
                throw new GeneralSecurityException("unsupported route envelope");
            }
            byte[] expectedAad = aad(deviceId, snapshotId);
            byte[] suppliedAad = Base64.decode(envelope.getString("aad_b64"));
            if (!java.security.MessageDigest.isEqual(expectedAad, suppliedAad)) {
                throw new GeneralSecurityException("route envelope binding mismatch");
            }

            Cipher rsa = Cipher.getInstance("RSA/ECB/OAEPPadding");
            rsa.init(Cipher.DECRYPT_MODE, privateKey, OAEP_SHA256);
            byte[] aesKey = rsa.doFinal(Base64.decode(
                    envelope.getString("wrapped_key_b64")));

            Cipher aes = Cipher.getInstance("AES/GCM/NoPadding");
            aes.init(Cipher.DECRYPT_MODE, new SecretKeySpec(aesKey, "AES"),
                    new GCMParameterSpec(128, Base64.decode(envelope.getString("nonce_b64"))));
            aes.updateAAD(expectedAad);
            byte[] plaintext = aes.doFinal(Base64.decode(
                    envelope.getString("ciphertext_b64")));
            JSONObject routeBundle = new JSONObject(
                    new String(plaintext, StandardCharsets.UTF_8));
            List<RouteDescriptor> projection = project(routeBundle);
            secretStore.store(plaintext);
            return projection;
        }

        static JSONObject encryptForTest(
                JSONObject routes, PublicKey publicKey, String deviceId, String snapshotId)
                throws Exception {
            byte[] aad = aad(deviceId, snapshotId);
            byte[] key = new byte[32];
            byte[] nonce = new byte[12];
            SecureRandom random = new SecureRandom();
            random.nextBytes(key);
            random.nextBytes(nonce);
            Cipher aes = Cipher.getInstance("AES/GCM/NoPadding");
            aes.init(Cipher.ENCRYPT_MODE, new SecretKeySpec(key, "AES"),
                    new GCMParameterSpec(128, nonce));
            aes.updateAAD(aad);
            byte[] ciphertext = aes.doFinal(
                    HomecomingSnapshotStore.canonicalJson(routes)
                            .getBytes(StandardCharsets.UTF_8));
            Cipher rsa = Cipher.getInstance("RSA/ECB/OAEPPadding");
            rsa.init(Cipher.ENCRYPT_MODE, publicKey, OAEP_SHA256);
            return new JSONObject()
                    .put("algorithm", ENVELOPE_ALGORITHM)
                    .put("wrapped_key_b64", Base64.toBase64String(rsa.doFinal(key)))
                    .put("nonce_b64", Base64.toBase64String(nonce))
                    .put("ciphertext_b64", Base64.toBase64String(ciphertext))
                    .put("aad_b64", Base64.toBase64String(aad));
        }

        private static byte[] aad(String deviceId, String snapshotId) throws Exception {
            JSONObject value = new JSONObject()
                    .put("schema", HomecomingContract.SCHEMA_VERSION)
                    .put("purpose", "aionshome-homecoming-routes")
                    .put("device_id", required(deviceId, "deviceId"))
                    .put("snapshot_id", required(snapshotId, "snapshotId"));
            return HomecomingSnapshotStore.canonicalJson(value)
                    .getBytes(StandardCharsets.UTF_8);
        }

        private static List<RouteDescriptor> project(JSONObject routes) throws Exception {
            JSONArray chat = routes.optJSONArray("chat");
            if (chat == null) {
                return Collections.emptyList();
            }
            List<RouteDescriptor> descriptors = new ArrayList<>();
            for (int i = 0; i < chat.length(); i++) {
                JSONObject route = chat.getJSONObject(i);
                JSONArray models = route.optJSONArray("models");
                List<String> modelKeys = new ArrayList<>();
                boolean vision = false;
                boolean audio = false;
                if (models != null) {
                    for (int j = 0; j < models.length(); j++) {
                        JSONObject model = models.getJSONObject(j);
                        modelKeys.add(model.optString("key", ""));
                        vision |= model.optBoolean("vision", false);
                        audio |= model.optBoolean("audio", false);
                    }
                }
                descriptors.add(new RouteDescriptor(
                        route.getString("route_id"),
                        route.optString("label", route.getString("route_id")),
                        route.optString("provider", ""),
                        modelKeys,
                        vision,
                        audio,
                        true));
            }
            return Collections.unmodifiableList(descriptors);
        }
    }

    static final class ReturnSigningCodec {
        static final String ALGORITHM_LABEL = "SHA256withRSA/PSS";
        private static final PSSParameterSpec PARAMETERS = new PSSParameterSpec(
                "SHA-256", "MGF1", MGF1ParameterSpec.SHA256, 32, 1);
        private final PrivateKey privateKey;

        ReturnSigningCodec(PrivateKey privateKey) {
            this.privateKey = privateKey;
        }

        byte[] sign(byte[] payload) throws GeneralSecurityException {
            if (payload == null) {
                throw new IllegalArgumentException("payload is required");
            }
            Signature signature;
            try {
                signature = Signature.getInstance(ALGORITHM_LABEL);
            } catch (java.security.NoSuchAlgorithmException unavailableOnJvm) {
                signature = Signature.getInstance("RSASSA-PSS");
                signature.setParameter(PARAMETERS);
            }
            signature.initSign(privateKey);
            signature.update(payload);
            return signature.sign();
        }

        static List<String> algorithmCandidates() {
            return java.util.Arrays.asList(
                    ALGORITHM_LABEL, "RSASSA-PSS");
        }
    }

    public static final class RouteDescriptor {
        public final String routeId;
        public final String label;
        public final String provider;
        public final List<String> modelKeys;
        public final boolean vision;
        public final boolean audio;
        public final boolean available;

        RouteDescriptor(String routeId, String label, String provider,
                List<String> modelKeys, boolean vision, boolean audio, boolean available) {
            this.routeId = routeId;
            this.label = label;
            this.provider = provider;
            this.modelKeys = Collections.unmodifiableList(new ArrayList<>(modelKeys));
            this.vision = vision;
            this.audio = audio;
            this.available = available;
        }

        public JSONObject toJson() {
            try {
                return new JSONObject()
                        .put("routeId", routeId)
                        .put("label", label)
                        .put("provider", provider)
                        .put("modelKeys", new JSONArray(modelKeys))
                        .put("vision", vision)
                        .put("audio", audio)
                        .put("available", available);
            } catch (org.json.JSONException exception) {
                throw new IllegalStateException("could not encode route descriptor", exception);
            }
        }
    }

    private static String required(String value, String label) {
        if (value == null || value.trim().isEmpty()) {
            throw new IllegalArgumentException(label + " is required");
        }
        return value.trim();
    }
}
