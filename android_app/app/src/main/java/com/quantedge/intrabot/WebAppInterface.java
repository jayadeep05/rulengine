package com.quantedge.intrabot;

import android.content.Context;
import android.os.Build;
import android.os.VibrationEffect;
import android.os.Vibrator;
import android.webkit.JavascriptInterface;
import android.widget.Toast;

public class WebAppInterface {
    private final MainActivity activity;
    private final Vibrator vibrator;

    public WebAppInterface(MainActivity activity) {
        this.activity = activity;
        this.vibrator = (Vibrator) activity.getSystemService(Context.VIBRATOR_SERVICE);
    }

    @JavascriptInterface
    public void showToast(String message) {
        activity.runOnUiThread(() -> 
            Toast.makeText(activity, message, Toast.LENGTH_SHORT).show()
        );
    }

    @JavascriptInterface
    public void vibrate(int durationMs) {
        if (vibrator != null && vibrator.hasVibrator()) {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                vibrator.vibrate(VibrationEffect.createOneShot(durationMs, VibrationEffect.DEFAULT_AMPLITUDE));
            } else {
                vibrator.vibrate(durationMs);
            }
        }
    }

    @JavascriptInterface
    public void playHaptic(String type) {
        if (vibrator == null || !vibrator.hasVibrator()) return;
        int ms = 40;
        int amp = 100;
        switch (type != null ? type.toLowerCase() : "") {
            case "light":
                ms = 25;
                amp = 60;
                break;
            case "medium":
                ms = 50;
                amp = 140;
                break;
            case "heavy":
            case "halt":
            case "emergency":
                ms = 120;
                amp = 255;
                break;
            case "success":
                ms = 60;
                amp = 180;
                break;
            case "error":
                ms = 100;
                amp = 220;
                break;
            default:
                ms = 40;
                amp = 120;
                break;
        }

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            vibrator.vibrate(VibrationEffect.createOneShot(ms, Math.min(255, Math.max(1, amp))));
        } else {
            vibrator.vibrate(ms);
        }
    }

    @JavascriptInterface
    public String getServerUrl() {
        return activity.getServerUrl();
    }

    @JavascriptInterface
    public void setServerUrl(String newUrl) {
        activity.saveServerUrl(newUrl);
    }

    @JavascriptInterface
    public void openServerSettingsDialog() {
        activity.runOnUiThread(activity::showServerSettingsDialog);
    }

    @JavascriptInterface
    public void promptExit() {
        activity.runOnUiThread(activity::finish);
    }

    @JavascriptInterface
    public boolean isNativeAndroid() {
        return true;
    }
}
