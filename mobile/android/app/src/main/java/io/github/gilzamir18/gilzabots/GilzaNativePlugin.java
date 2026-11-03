package io.github.gilzamir18.gilzabots;

import android.accounts.Account;
import android.app.Activity;
import android.app.PendingIntent;
import android.content.Context;
import android.content.SharedPreferences;
import android.content.pm.PackageInfo;
import android.content.pm.PackageManager;
import android.content.pm.Signature;
import android.os.Build;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import android.util.Base64;
import android.util.Log;

import androidx.activity.result.ActivityResultLauncher;
import androidx.activity.result.IntentSenderRequest;
import androidx.activity.result.contract.ActivityResultContracts;

import com.getcapacitor.JSArray;
import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;
import com.google.android.gms.auth.api.identity.AuthorizationClient;
import com.google.android.gms.auth.api.identity.AuthorizationRequest;
import com.google.android.gms.auth.api.identity.AuthorizationResult;
import com.google.android.gms.auth.api.identity.ClearTokenRequest;
import com.google.android.gms.auth.api.identity.Identity;
import com.google.android.gms.auth.api.identity.RevokeAccessRequest;
import com.google.android.gms.auth.api.signin.GoogleSignInAccount;
import com.google.android.gms.common.api.ApiException;
import com.google.android.gms.common.api.CommonStatusCodes;
import com.google.android.gms.common.api.Scope;

import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.io.Reader;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.security.KeyStore;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.Iterator;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

import javax.crypto.Cipher;
import javax.crypto.KeyGenerator;
import javax.crypto.SecretKey;
import javax.crypto.spec.GCMParameterSpec;

/**
 * Native services for the GilzaBots web layer (see src/platform/native.js).
 *
 * authorize      Google Identity Services AuthorizationClient for the drive.file
 *                scope. The Android OAuth client is matched by package name and
 *                signing certificate, so no client id is compiled in.
 * secure*        Values encrypted with an AES-GCM key that never leaves the
 *                Android Keystore; ciphertext in private SharedPreferences.
 * streamRequest  HTTP whose response body is delivered as it arrives, for the
 *                model providers: a web view's fetch is bound by CORS.
 */
@CapacitorPlugin(name = "GilzaNative")
public class GilzaNativePlugin extends Plugin {

    private static final String TAG = "GilzaNative";
    private static final String KEY_ALIAS = "gilzabots_secrets";
    private static final String PREFS = "gilzabots_secure";
    private static final int GCM_IV_BYTES = 12;
    private static final int GCM_TAG_BITS = 128;

    private ActivityResultLauncher<IntentSenderRequest> authLauncher;
    private PluginCall pendingAuthCall;
    private Account lastAccount;
    private final ExecutorService network = Executors.newCachedThreadPool();
    private final Map<String, HttpURLConnection> connections = new ConcurrentHashMap<>();

    @Override
    public void load() {
        // Registered while the activity is being created, as the Activity Result
        // API requires; the consent screen's answer arrives here.
        authLauncher = getActivity().registerForActivityResult(
            new ActivityResultContracts.StartIntentSenderForResult(),
            result -> {
                PluginCall call = pendingAuthCall;
                pendingAuthCall = null;
                if (call == null) {
                    return;
                }
                try {
                    AuthorizationResult authorization =
                        authorizationClient().getAuthorizationResultFromIntent(result.getData());
                    resolveAuthorization(call, authorization);
                } catch (ApiException e) {
                    rejectAuthorization(call, e, result.getResultCode());
                }
            }
        );
    }

    @Override
    protected void handleOnDestroy() {
        network.shutdownNow();
    }

    private AuthorizationClient authorizationClient() {
        return Identity.getAuthorizationClient(getActivity());
    }

    // ── Google authorization ────────────────────────────────────────────────

    @PluginMethod
    public void authorize(PluginCall call) {
        JSArray requested = call.getArray("scopes", new JSArray());
        List<Scope> scopes = new ArrayList<>();
        try {
            for (Object value : requested.toList()) {
                scopes.add(new Scope(String.valueOf(value)));
            }
        } catch (Exception e) {
            call.reject("scopes must be a list of strings.");
            return;
        }
        if (scopes.isEmpty()) {
            call.reject("At least one scope is required.");
            return;
        }
        AuthorizationRequest request = AuthorizationRequest.builder().setRequestedScopes(scopes).build();
        authorizationClient()
            .authorize(request)
            .addOnSuccessListener(result -> {
                if (!result.hasResolution()) {
                    resolveAuthorization(call, result);
                    return;
                }
                PendingIntent intent = result.getPendingIntent();
                if (intent == null) {
                    call.reject("Google asked for consent but supplied no screen to show.");
                    return;
                }
                if (pendingAuthCall != null) {
                    call.reject("An authorization is already in progress.");
                    return;
                }
                pendingAuthCall = call;
                authLauncher.launch(new IntentSenderRequest.Builder(intent.getIntentSender()).build());
            })
            .addOnFailureListener(e -> {
                if (e instanceof ApiException) {
                    rejectAuthorization(call, (ApiException) e, Activity.RESULT_OK);
                } else {
                    call.reject(describe(e), e);
                }
            });
    }

    /**
     * Reject with Google's status name and number, and the status number as the
     * call's code, so the web layer can explain the likely cause. The status
     * alone ("10") sends the user searching; DEVELOPER_ERROR plus the package
     * and certificate this build is signed with is what they need to fix it.
     */
    private void rejectAuthorization(PluginCall call, ApiException e, int resultCode) {
        int status = e.getStatusCode();
        String message = "Google authorization was not granted: "
            + CommonStatusCodes.getStatusCodeString(status) + " (" + status + ")"
            + (resultCode == Activity.RESULT_OK ? "" : ", consent screen result " + resultCode)
            + (e.getStatus().getStatusMessage() == null ? "" : ": " + e.getStatus().getStatusMessage());
        Log.w(TAG, message, e);
        call.reject(message, String.valueOf(status), e);
    }

    private void resolveAuthorization(PluginCall call, AuthorizationResult result) {
        String token = result.getAccessToken();
        if (token == null || token.isEmpty()) {
            call.reject("Google returned no access token.");
            return;
        }
        JSObject answer = new JSObject();
        answer.put("accessToken", token);
        GoogleSignInAccount account = result.toGoogleSignInAccount();
        if (account != null) {
            lastAccount = account.getAccount();
            answer.put("email", account.getEmail() == null ? "" : account.getEmail());
        }
        call.resolve(answer);
    }

    @PluginMethod
    public void clearToken(PluginCall call) {
        String token = call.getString("token", "");
        if (token.isEmpty()) {
            call.resolve();
            return;
        }
        authorizationClient()
            .clearToken(ClearTokenRequest.builder().setToken(token).build())
            .addOnSuccessListener(unused -> call.resolve())
            .addOnFailureListener(e -> call.reject(describe(e), e));
    }

    @PluginMethod
    public void revoke(PluginCall call) {
        if (lastAccount == null) {
            // Nothing to name in the request; the app forgets the session and
            // the grant can still be removed from the Google account settings.
            JSObject answer = new JSObject();
            answer.put("revoked", false);
            call.resolve(answer);
            return;
        }
        RevokeAccessRequest request = RevokeAccessRequest.builder()
            .setAccount(lastAccount)
            .setScopes(new ArrayList<>(List.of(new Scope("https://www.googleapis.com/auth/drive.file"))))
            .build();
        authorizationClient()
            .revokeAccess(request)
            .addOnSuccessListener(unused -> {
                lastAccount = null;
                JSObject answer = new JSObject();
                answer.put("revoked", true);
                call.resolve(answer);
            })
            .addOnFailureListener(e -> call.reject(describe(e), e));
    }

    /**
     * The package name and signing-certificate SHA-1 of this installed build:
     * the two values an Android OAuth client must be registered with.
     */
    @PluginMethod
    public void appIdentity(PluginCall call) {
        try {
            Context context = getContext();
            PackageManager manager = context.getPackageManager();
            Signature[] signatures;
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
                PackageInfo info = manager.getPackageInfo(context.getPackageName(), PackageManager.GET_SIGNING_CERTIFICATES);
                signatures = info.signingInfo.getApkContentsSigners();
            } else {
                PackageInfo info = manager.getPackageInfo(context.getPackageName(), PackageManager.GET_SIGNATURES);
                signatures = info.signatures;
            }
            JSArray sha1 = new JSArray();
            for (Signature signature : signatures) {
                byte[] digest = MessageDigest.getInstance("SHA-1").digest(signature.toByteArray());
                StringBuilder text = new StringBuilder();
                for (int i = 0; i < digest.length; i++) {
                    if (i > 0) {
                        text.append(':');
                    }
                    text.append(String.format("%02X", digest[i]));
                }
                sha1.put(text.toString());
            }
            JSObject answer = new JSObject();
            answer.put("packageName", context.getPackageName());
            answer.put("sha1", sha1);
            call.resolve(answer);
        } catch (Exception e) {
            call.reject(describe(e), e);
        }
    }

    // ── Secret storage ──────────────────────────────────────────────────────

    private SecretKey secretKey() throws Exception {
        KeyStore store = KeyStore.getInstance("AndroidKeyStore");
        store.load(null);
        if (store.containsAlias(KEY_ALIAS)) {
            return ((KeyStore.SecretKeyEntry) store.getEntry(KEY_ALIAS, null)).getSecretKey();
        }
        KeyGenerator generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore");
        generator.init(new KeyGenParameterSpec.Builder(
            KEY_ALIAS, KeyProperties.PURPOSE_ENCRYPT | KeyProperties.PURPOSE_DECRYPT)
            .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
            .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
            .setKeySize(256)
            .build());
        return generator.generateKey();
    }

    private SharedPreferences prefs() {
        return getContext().getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    @PluginMethod
    public void secureSet(PluginCall call) {
        String key = call.getString("key", "");
        String value = call.getString("value");
        if (key.isEmpty() || value == null) {
            call.reject("key and value are required.");
            return;
        }
        try {
            Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
            cipher.init(Cipher.ENCRYPT_MODE, secretKey());
            byte[] iv = cipher.getIV();
            byte[] sealed = cipher.doFinal(value.getBytes(StandardCharsets.UTF_8));
            byte[] payload = ByteBuffer.allocate(iv.length + sealed.length).put(iv).put(sealed).array();
            prefs().edit().putString(key, Base64.encodeToString(payload, Base64.NO_WRAP)).apply();
            call.resolve();
        } catch (Exception e) {
            call.reject("The value could not be encrypted: " + describe(e), e);
        }
    }

    @PluginMethod
    public void secureGet(PluginCall call) {
        String key = call.getString("key", "");
        String stored = prefs().getString(key, null);
        JSObject answer = new JSObject();
        if (stored == null) {
            answer.put("value", null);
            call.resolve(answer);
            return;
        }
        try {
            byte[] payload = Base64.decode(stored, Base64.NO_WRAP);
            Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
            cipher.init(Cipher.DECRYPT_MODE, secretKey(), new GCMParameterSpec(GCM_TAG_BITS, payload, 0, GCM_IV_BYTES));
            byte[] plain = cipher.doFinal(payload, GCM_IV_BYTES, payload.length - GCM_IV_BYTES);
            answer.put("value", new String(plain, StandardCharsets.UTF_8));
            call.resolve(answer);
        } catch (Exception e) {
            call.reject("The stored value could not be decrypted: " + describe(e), e);
        }
    }

    @PluginMethod
    public void secureRemove(PluginCall call) {
        prefs().edit().remove(call.getString("key", "")).apply();
        call.resolve();
    }

    // ── Streaming HTTP ──────────────────────────────────────────────────────

    @PluginMethod(returnType = PluginMethod.RETURN_CALLBACK)
    public void streamRequest(PluginCall call) {
        String id = call.getString("id", "");
        String url = call.getString("url", "");
        String method = call.getString("method", "GET");
        JSObject headers = call.getObject("headers", new JSObject());
        String body = call.getString("body");
        if (id.isEmpty() || url.isEmpty()) {
            call.reject("id and url are required.");
            return;
        }
        call.setKeepAlive(true);
        network.execute(() -> {
            HttpURLConnection connection = null;
            try {
                connection = (HttpURLConnection) new URL(url).openConnection();
                connections.put(id, connection);
                connection.setRequestMethod(method);
                connection.setConnectTimeout(15000);
                // A reasoning model can think for minutes before its first token.
                connection.setReadTimeout(10 * 60 * 1000);
                boolean hasUserAgent = false;
                Iterator<String> names = headers.keys();
                while (names.hasNext()) {
                    String name = names.next();
                    hasUserAgent |= "user-agent".equalsIgnoreCase(name);
                    connection.setRequestProperty(name, headers.getString(name));
                }
                if (!hasUserAgent) {
                    // HttpURLConnection otherwise sends the platform default,
                    // "Dalvik/2.1.0 (…)", which some providers' edge filters
                    // refuse outright: ollama.com answers it with a 403 page
                    // before the API key is even looked at.
                    connection.setRequestProperty("User-Agent", userAgent());
                }
                if (body != null) {
                    byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
                    connection.setDoOutput(true);
                    connection.setFixedLengthStreamingMode(bytes.length);
                    try (OutputStream out = connection.getOutputStream()) {
                        out.write(bytes);
                    }
                }
                int status = connection.getResponseCode();
                JSObject head = new JSObject();
                head.put("type", "head");
                head.put("status", status);
                JSObject responseHeaders = new JSObject();
                for (Map.Entry<String, List<String>> field : connection.getHeaderFields().entrySet()) {
                    if (field.getKey() != null && !field.getValue().isEmpty()) {
                        responseHeaders.put(field.getKey().toLowerCase(), field.getValue().get(0));
                    }
                }
                head.put("headers", responseHeaders);
                call.resolve(head);

                InputStream stream = status >= 400 ? connection.getErrorStream() : connection.getInputStream();
                if (stream != null) {
                    // The reader decodes UTF-8 across buffer boundaries, so a
                    // character split between two reads is never broken.
                    try (Reader reader = new InputStreamReader(stream, StandardCharsets.UTF_8)) {
                        char[] buffer = new char[8192];
                        int read;
                        while ((read = reader.read(buffer)) != -1) {
                            JSObject data = new JSObject();
                            data.put("type", "data");
                            data.put("chunk", new String(buffer, 0, read));
                            call.resolve(data);
                        }
                    }
                }
                JSObject end = new JSObject();
                end.put("type", "end");
                call.resolve(end);
            } catch (Exception e) {
                call.reject(describe(e));
            } finally {
                connections.remove(id);
                if (connection != null) {
                    connection.disconnect();
                }
                call.release(getBridge());
            }
        });
    }

    private String userAgent() {
        String version = "0";
        try {
            Context context = getContext();
            version = context.getPackageManager().getPackageInfo(context.getPackageName(), 0).versionName;
        } catch (Exception ignored) {
            // The version is informative; the agent string works without it.
        }
        return "GilzaBots-Mobile/" + version + " (Android " + Build.VERSION.RELEASE + ")";
    }

    @PluginMethod
    public void cancelRequest(PluginCall call) {
        HttpURLConnection connection = connections.remove(call.getString("id", ""));
        if (connection != null) {
            network.execute(connection::disconnect);
        }
        call.resolve();
    }

    private static String describe(Exception e) {
        String message = e.getMessage();
        return message == null || message.isEmpty() ? e.getClass().getSimpleName() : message;
    }
}
