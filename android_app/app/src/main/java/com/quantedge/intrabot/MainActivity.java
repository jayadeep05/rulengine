package com.quantedge.intrabot;

import android.annotation.SuppressLint;
import android.content.Context;
import android.content.SharedPreferences;
import android.graphics.Bitmap;
import android.net.ConnectivityManager;
import android.net.NetworkCapabilities;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.View;
import android.webkit.ConsoleMessage;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.TextView;
import android.widget.Toast;

import androidx.activity.OnBackPressedCallback;
import androidx.appcompat.app.AlertDialog;
import androidx.appcompat.app.AppCompatActivity;
import androidx.swiperefreshlayout.widget.SwipeRefreshLayout;

public class MainActivity extends AppCompatActivity {

    private static final String PREFS_NAME = "IntraBotPrefs";
    private static final String KEY_SERVER_URL = "server_url";
    private static final String DEFAULT_SERVER_URL = "https://bot.deepcodev.site";

    private WebView webView;
    private SwipeRefreshLayout swipeRefresh;
    private ProgressBar progressBar;
    private LinearLayout errorLayout;
    private TextView currentServerUrlText;
    private Button btnRetry;
    private Button btnChangeServer;

    private SharedPreferences prefs;
    private boolean doubleBackToExitPressedOnce = false;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        prefs = getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE);

        initViews();
        setupWebView();
        setupSwipeRefresh();
        setupErrorLayout();
        setupBackHandler();

        loadTradingTerminal();
    }

    private void initViews() {
        webView = findViewById(R.id.webView);
        swipeRefresh = findViewById(R.id.swipeRefreshLayout);
        progressBar = findViewById(R.id.progressBar);
        errorLayout = findViewById(R.id.errorLayout);
        currentServerUrlText = findViewById(R.id.currentServerUrlText);
        btnRetry = findViewById(R.id.btnRetry);
        btnChangeServer = findViewById(R.id.btnChangeServer);
    }

    @SuppressLint("SetJavaScriptEnabled")
    private void setupWebView() {
        WebSettings ws = webView.getSettings();
        ws.setJavaScriptEnabled(true);
        ws.setDomStorageEnabled(true);
        ws.setDatabaseEnabled(true);
        ws.setAllowFileAccess(true);
        ws.setAllowContentAccess(true);
        ws.setLoadsImagesAutomatically(true);
        ws.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);
        ws.setUseWideViewPort(true);
        ws.setLoadWithOverviewMode(true);
        ws.setSupportZoom(false);
        ws.setBuiltInZoomControls(false);

        // Native JS Bridge
        webView.addJavascriptInterface(new WebAppInterface(this), "AndroidBridge");

        webView.setWebChromeClient(new WebChromeClient() {
            @Override
            public void onProgressChanged(WebView view, int newProgress) {
                if (newProgress < 100) {
                    progressBar.setVisibility(View.VISIBLE);
                    progressBar.setProgress(newProgress);
                } else {
                    progressBar.setVisibility(View.GONE);
                    swipeRefresh.setRefreshing(false);
                }
            }

            @Override
            public boolean onConsoleMessage(ConsoleMessage consoleMessage) {
                return super.onConsoleMessage(consoleMessage);
            }
        });

        webView.setWebViewClient(new WebViewClient() {
            @Override
            public void onPageStarted(WebView view, String url, Bitmap favicon) {
                super.onPageStarted(view, url, favicon);
                errorLayout.setVisibility(View.GONE);
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                super.onPageFinished(view, url);
                swipeRefresh.setRefreshing(false);
                progressBar.setVisibility(View.GONE);

                // Ensure Android APK identifies itself so bottom nav dock renders strictly in the APK
                view.evaluateJavascript(
                    "(function() {" +
                    "  document.documentElement.classList.add('is-android-app');" +
                    "  if (document.body) document.body.classList.add('is-android-app');" +
                    "})()", null
                );
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
                super.onReceivedError(view, request, error);
                if (request.isForMainFrame()) {
                    showErrorView();
                }
            }
        });
    }

    private void setupSwipeRefresh() {
        swipeRefresh.setColorSchemeResources(R.color.brand_orange, R.color.trading_green);
        swipeRefresh.setProgressBackgroundColorSchemeResource(R.color.bg_panel);
        swipeRefresh.setOnRefreshListener(() -> {
            // Tell the web application to trigger a manual sync if supported, otherwise reload
            webView.evaluateJavascript(
                "if (typeof triggerSync === 'function') { triggerSync(); 'sync'; } else { location.reload(); 'reload'; }",
                result -> {
                    new Handler(Looper.getMainLooper()).postDelayed(() -> swipeRefresh.setRefreshing(false), 1200);
                }
            );
        });
    }

    private void setupErrorLayout() {
        btnRetry.setOnClickListener(v -> {
            errorLayout.setVisibility(View.GONE);
            loadTradingTerminal();
        });

        btnChangeServer.setOnClickListener(v -> showServerSettingsDialog());
    }

    private void setupBackHandler() {
        getOnBackPressedDispatcher().addCallback(this, new OnBackPressedCallback(true) {
            @Override
            public void handleOnBackPressed() {
                // First ask web page if any modal/dialog is open
                webView.evaluateJavascript(
                    "(function() { " +
                    "  if (typeof handleAndroidBack === 'function') { return handleAndroidBack(); } " +
                    "  const hist = document.getElementById('history-screen'); " +
                    "  if (hist && hist.style.display !== 'none' && hist.classList.contains('active')) { " +
                    "    if (typeof toggleHistoryScreen === 'function') { toggleHistoryScreen(); return true; } " +
                    "  } " +
                    "  const configModal = document.getElementById('config-modal'); " +
                    "  if (configModal && configModal.style.display !== 'none') { " +
                    "    if (typeof closeConfig === 'function') { closeConfig(); return true; } " +
                    "  } " +
                    "  return false; " +
                    "})()",
                    value -> {
                        if ("true".equals(value)) {
                            // Handled by web page modal dismiss
                            return;
                        }

                        if (webView.canGoBack()) {
                            webView.goBack();
                            return;
                        }

                        if (doubleBackToExitPressedOnce) {
                            finish();
                            return;
                        }

                        doubleBackToExitPressedOnce = true;
                        Toast.makeText(MainActivity.this, getString(R.string.exit_prompt), Toast.LENGTH_SHORT).show();

                        new Handler(Looper.getMainLooper()).postDelayed(() -> {
                            doubleBackToExitPressedOnce = false;
                        }, 2000);
                    }
                );
            }
        });
    }

    public void loadTradingTerminal() {
        String url = getServerUrl();
        if (url == null || url.trim().isEmpty()) {
            url = DEFAULT_SERVER_URL;
        }

        currentServerUrlText.setText("Server: " + url);
        webView.loadUrl(url);
    }

    private void showErrorView() {
        String url = getServerUrl();
        currentServerUrlText.setText("Server: " + url);
        errorLayout.setVisibility(View.VISIBLE);
        swipeRefresh.setRefreshing(false);
        progressBar.setVisibility(View.GONE);
    }

    public String getServerUrl() {
        return prefs.getString(KEY_SERVER_URL, DEFAULT_SERVER_URL);
    }

    public void saveServerUrl(String newUrl) {
        if (newUrl == null || newUrl.trim().isEmpty()) return;
        newUrl = newUrl.trim();
        if (!newUrl.startsWith("http://") && !newUrl.startsWith("https://") && !newUrl.startsWith("file://")) {
            newUrl = "http://" + newUrl;
        }
        if (newUrl.endsWith("/")) {
            newUrl = newUrl.substring(0, newUrl.length() - 1);
        }
        prefs.edit().putString(KEY_SERVER_URL, newUrl).apply();
    }

    public void showServerSettingsDialog() {
        AlertDialog.Builder builder = new AlertDialog.Builder(this);
        builder.setTitle(R.string.server_settings_title);

        LinearLayout layout = new LinearLayout(this);
        layout.setOrientation(LinearLayout.VERTICAL);
        layout.setPadding(48, 24, 48, 16);

        final EditText input = new EditText(this);
        input.setHint(R.string.server_settings_hint);
        input.setText(getServerUrl());
        layout.addView(input);

        // Presets helper text
        TextView presets = new TextView(this);
        presets.setText("\nPresets:\n• Android Emulator: http://10.0.2.2:8000\n• Local Wi-Fi (replace IP): http://192.168.1.100:8000\n• Bundled Offline: file:///android_asset/www/index.html");
        presets.setTextSize(12);
        presets.setTextColor(0xFF94A3B8);
        layout.addView(presets);

        builder.setView(layout);

        builder.setPositiveButton("Connect", (dialog, which) -> {
            String entered = input.getText().toString().trim();
            if (!entered.isEmpty()) {
                saveServerUrl(entered);
                Toast.makeText(this, "Connecting to " + entered, Toast.LENGTH_SHORT).show();
                loadTradingTerminal();
            }
        });

        builder.setNegativeButton("Cancel", (dialog, which) -> dialog.cancel());

        builder.show();
    }
}
