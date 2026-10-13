/**
 * android-adapter.js
 * ───────────────────
 * Native bridge and mobile UX enhancements for QuantEdge IntraBot Android App.
 */

(function () {
    "use strict";

    console.log("[AndroidAdapter] Initializing QuantEdge Android Mobile Adapter...");

    // Check if running inside native Android WebView
    const isAndroid = !!(window.AndroidBridge && typeof window.AndroidBridge.isNativeAndroid === 'function');

    // ── Haptic Feedback Helpers ──────────────────────────────────────────────
    function haptic(type = "light") {
        if (isAndroid && window.AndroidBridge.playHaptic) {
            window.AndroidBridge.playHaptic(type);
        } else if (navigator.vibrate) {
            navigator.vibrate(type === "heavy" ? 80 : 30);
        }
    }

    function showToast(msg) {
        if (isAndroid && window.AndroidBridge.showToast) {
            window.AndroidBridge.showToast(msg);
        } else {
            console.log("[Toast]", msg);
        }
    }

    // Attach haptics to critical buttons
    document.addEventListener("click", function (e) {
        const target = e.target.closest("button, .btn-mode, .btn-toggle, .btn-danger, .hf-preset-chip, .hf-type-chip");
        if (!target) return;

        if (target.classList.contains("btn-danger") || target.innerText.includes("HALT")) {
            haptic("heavy");
        } else if (target.classList.contains("btn-toggle") || target.innerText.includes("START")) {
            haptic("success");
        } else {
            haptic("light");
        }
    }, true);

    // ── Android Hardware / Gesture Back Handling ─────────────────────────────
    window.handleAndroidBack = function () {
        // 1. History modal open?
        const histScreen = document.getElementById("history-screen");
        if (histScreen && (histScreen.style.display === "block" || histScreen.classList.contains("active") || getComputedStyle(histScreen).display !== "none")) {
            if (typeof toggleHistoryScreen === "function") {
                toggleHistoryScreen();
                haptic("light");
                return true;
            }
        }

        // 2. Config modal open?
        const configModal = document.getElementById("config-modal");
        if (configModal && configModal.style.display !== "none") {
            if (typeof closeConfig === "function") {
                closeConfig();
                haptic("light");
                return true;
            }
        }

        // 3. Server modal open?
        const serverModal = document.getElementById("mobile-server-modal");
        if (serverModal && serverModal.style.display !== "none") {
            serverModal.style.display = "none";
            haptic("light");
            return true;
        }

        // 4. Sub-tab active? Return to main dashboard
        const activeNav = document.querySelector(".mob-nav-item.active");
        if (activeNav && activeNav.dataset.tab !== "dashboard") {
            switchMobileTab("dashboard");
            haptic("light");
            return true;
        }

        return false; // Let MainActivity handle back stack / exit toast
    };

    // ── Mobile Bottom Navigation Bar ──────────────────────────────────────────
    function setupMobileBottomNav() {
        if (document.getElementById("android-bottom-nav")) return;

        const isDualPage = window.location.pathname.endsWith("dual.html") || window.location.pathname.endsWith("/dual");
        const activeTab = isDualPage ? "dual" : "dashboard";

        const nav = document.createElement("nav");
        nav.id = "android-bottom-nav";
        nav.innerHTML = `
            <div class="mob-nav-item ${activeTab === 'dashboard' ? 'active' : ''}" data-tab="dashboard" onclick="window.switchMobileTab('dashboard')">
                <span class="mob-nav-icon">📈</span>
                <span class="mob-nav-label">Terminal</span>
            </div>
            <div class="mob-nav-item" data-tab="pipeline" onclick="window.switchMobileTab('pipeline')">
                <span class="mob-nav-icon">⚡</span>
                <span class="mob-nav-label">Pipeline</span>
            </div>
            <div class="mob-nav-item ${activeTab === 'dual' ? 'active' : ''}" data-tab="dual" onclick="window.switchMobileTab('dual')">
                <span class="mob-nav-icon">🔄</span>
                <span class="mob-nav-label">Dual Mode</span>
            </div>
            <div class="mob-nav-item" data-tab="history" onclick="window.switchMobileTab('history')">
                <span class="mob-nav-icon">📋</span>
                <span class="mob-nav-label">History</span>
            </div>
            <div class="mob-nav-item" data-tab="settings" onclick="window.switchMobileTab('settings')">
                <span class="mob-nav-icon">⚙️</span>
                <span class="mob-nav-label">Settings</span>
            </div>
        `;
        document.body.appendChild(nav);

        // Check if navigated with ?tab=history
        const urlParams = new URLSearchParams(window.location.search);
        if (urlParams.get("tab") === "history") {
            setTimeout(() => { if (typeof toggleHistoryScreen === "function") toggleHistoryScreen(); }, 300);
        }
    }

    window.switchMobileTab = function (tab) {
        haptic("light");
        const isDualPage = window.location.pathname.endsWith("dual.html") || window.location.pathname.endsWith("/dual");
        
        if (isDualPage && (tab === "dashboard" || tab === "pipeline" || tab === "history")) {
            const target = (window.location.protocol === "file:") ? `index.html?tab=${tab}` : `/?tab=${tab}`;
            window.location.href = target;
            return;
        }

        const items = document.querySelectorAll(".mob-nav-item");
        items.forEach(it => it.classList.toggle("active", it.dataset.tab === tab));

        if (tab === "dashboard") {
            const hist = document.getElementById("history-screen");
            if (hist) hist.style.display = "none";
            window.scrollTo({ top: 0, behavior: "smooth" });
        } else if (tab === "pipeline") {
            const hist = document.getElementById("history-screen");
            if (hist) hist.style.display = "none";
            const radar = document.querySelector(".panel") || document.querySelector(".intelligence-panel");
            if (radar) radar.scrollIntoView({ behavior: "smooth" });
        } else if (tab === "dual") {
            const target = (window.location.protocol === "file:") ? "dual.html" : "/dual";
            window.location.href = target;
        } else if (tab === "history") {
            if (typeof toggleHistoryScreen === "function") {
                toggleHistoryScreen();
            }
        } else if (tab === "settings") {
            if (typeof openConfig === "function") {
                openConfig();
            } else {
                openServerModal();
            }
        }
    };

    // ── Server Connection Modal ───────────────────────────────────────────────
    function openServerModal() {
        let modal = document.getElementById("mobile-server-modal");
        if (!modal) {
            modal = document.createElement("div");
            modal.id = "mobile-server-modal";
            modal.innerHTML = `
                <div class="msm-content">
                    <div class="msm-header">
                        <h3>⚙️ Server Configuration</h3>
                        <button onclick="document.getElementById('mobile-server-modal').style.display='none'">✕</button>
                    </div>
                    <p style="font-size:12px;color:var(--muted);margin-bottom:12px;">Configure backend host URL for real-time order execution & data ticks.</p>
                    <input type="text" id="msm-url-input" class="msm-input" placeholder="http://10.0.2.2:8000" />
                    <div class="msm-presets">
                        <button onclick="setServerUrlPreset('http://10.0.2.2:8000')">Emulator (10.0.2.2)</button>
                        <button onclick="setServerUrlPreset('http://127.0.0.1:8000')">Localhost (8000)</button>
                        <button onclick="setServerUrlPreset('https://bot.deepcodev.site')">Cloud Server</button>
                    </div>
                    <div class="msm-actions">
                        <button class="msm-btn-save" onclick="saveMobileServerUrl()">Save &amp; Connect</button>
                    </div>
                </div>
            `;
            document.body.appendChild(modal);
        }

        const input = document.getElementById("msm-url-input");
        if (input) {
            const current = (isAndroid && window.AndroidBridge.getServerUrl) 
                ? window.AndroidBridge.getServerUrl() 
                : (localStorage.getItem("_ib_server_url") || window.location.origin);
            input.value = current;
        }
        modal.style.display = "flex";
    }

    window.openServerModal = openServerModal;

    window.setServerUrlPreset = function(url) {
        const input = document.getElementById("msm-url-input");
        if (input) input.value = url;
    };

    window.saveMobileServerUrl = function() {
        const input = document.getElementById("msm-url-input");
        if (!input) return;
        const url = input.value.trim();
        if (!url) return;

        localStorage.setItem("_ib_server_url", url);
        if (isAndroid && window.AndroidBridge.setServerUrl) {
            window.AndroidBridge.setServerUrl(url);
            showToast("Server updated. Reloading...");
        }
        document.getElementById("mobile-server-modal").style.display = "none";
        setTimeout(() => location.reload(), 500);
    };

    // ── Safe-Area Inset Styles & Bottom Navigation Styles ─────────────────────
    function injectMobileStyles() {
        const style = document.createElement("style");
        style.textContent = `
            /* Android Safe Area & Navigation */
            :root {
                --safe-top: env(safe-area-inset-top, 0px);
                --safe-bottom: env(safe-area-inset-bottom, 0px);
            }

            body {
                padding-top: var(--safe-top);
                padding-bottom: calc(64px + var(--safe-bottom)) !important;
                -webkit-tap-highlight-color: transparent;
                user-select: none;
            }

            /* Android Bottom Nav */
            #android-bottom-nav {
                position: fixed;
                bottom: 0;
                left: 0;
                right: 0;
                height: 58px;
                background: rgba(13, 20, 36, 0.96);
                backdrop-filter: blur(16px);
                -webkit-backdrop-filter: blur(16px);
                border-top: 1px solid rgba(255, 255, 255, 0.08);
                display: flex;
                align-items: center;
                justify-content: space-around;
                z-index: 9999;
                padding-bottom: var(--safe-bottom);
            }

            .mob-nav-item {
                display: flex;
                flex-direction: column;
                align-items: center;
                justify-content: center;
                flex: 1;
                height: 100%;
                cursor: pointer;
                transition: all 0.2s ease;
                color: #94a3b8;
            }

            .mob-nav-item.active {
                color: #ff6b00;
            }

            .mob-nav-icon {
                font-size: 18px;
                line-height: 1;
                margin-bottom: 2px;
            }

            .mob-nav-label {
                font-size: 10px;
                font-weight: 600;
                letter-spacing: 0.3px;
                font-family: 'Inter', sans-serif;
            }

            /* Server Modal */
            #mobile-server-modal {
                position: fixed;
                inset: 0;
                background: rgba(0, 0, 0, 0.75);
                backdrop-filter: blur(8px);
                z-index: 10000;
                display: none;
                align-items: center;
                justify-content: center;
                padding: 20px;
            }

            .msm-content {
                background: #0d1424;
                border: 1px solid rgba(255, 107, 0, 0.3);
                border-radius: 16px;
                padding: 20px;
                width: 100%;
                max-width: 380px;
                color: #f8fafc;
                box-shadow: 0 12px 32px rgba(0, 0, 0, 0.7);
            }

            .msm-header {
                display: flex;
                justify-content: space-between;
                align-items: center;
                margin-bottom: 8px;
            }

            .msm-header h3 {
                font-size: 16px;
                font-weight: 700;
                color: #ff6b00;
            }

            .msm-header button {
                background: transparent;
                border: none;
                color: #94a3b8;
                font-size: 16px;
                cursor: pointer;
            }

            .msm-input {
                width: 100%;
                background: rgba(6, 9, 19, 0.9);
                border: 1px solid rgba(255, 255, 255, 0.15);
                border-radius: 8px;
                padding: 10px 12px;
                color: #f8fafc;
                font-family: 'JetBrains Mono', monospace;
                font-size: 13px;
                margin-bottom: 12px;
                box-sizing: border-box;
            }

            .msm-presets {
                display: flex;
                flex-wrap: wrap;
                gap: 6px;
                margin-bottom: 16px;
            }

            .msm-presets button {
                background: rgba(255, 255, 255, 0.06);
                border: 1px solid rgba(255, 255, 255, 0.12);
                border-radius: 6px;
                padding: 6px 10px;
                color: #cbd5e1;
                font-size: 11px;
                cursor: pointer;
            }

            .msm-btn-save {
                width: 100%;
                background: #ff6b00;
                color: #ffffff;
                font-weight: 700;
                padding: 10px;
                border: none;
                border-radius: 8px;
                cursor: pointer;
                font-size: 13px;
            }
        `;
        document.head.appendChild(style);
    }

    // ── Init on DOM ready (STRICTLY FOR ANDROID APK) ─────────────────────────
    if (isAndroid) {
        const initAndroid = () => {
            document.documentElement.classList.add("is-android-app");
            if (document.body) document.body.classList.add("is-android-app");
            injectMobileStyles();
            setupMobileBottomNav();
        };

        if (document.readyState === "loading") {
            document.addEventListener("DOMContentLoaded", initAndroid);
        } else {
            initAndroid();
        }
    }

    // Export
    window.AndroidAdapter = { isAndroid, haptic, showToast, openServerModal };
})();
