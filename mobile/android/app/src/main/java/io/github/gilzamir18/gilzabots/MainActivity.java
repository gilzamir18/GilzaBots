package io.github.gilzamir18.gilzabots;

import android.os.Bundle;

import com.getcapacitor.BridgeActivity;

public class MainActivity extends BridgeActivity {
    @Override
    public void onCreate(Bundle savedInstanceState) {
        // The app's own plugin lives in this module rather than in an npm
        // package, so it is registered by hand before the bridge starts.
        registerPlugin(GilzaNativePlugin.class);
        super.onCreate(savedInstanceState);
    }
}
