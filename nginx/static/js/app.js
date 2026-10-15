/* WayPointDB shared page behaviour. The theme itself is applied by an inline script in <head> to avoid a flash. */

(function () {
    "use strict";

    // ---------- Theme ----------

    const THEMES = ["auto", "light", "dark"];
    const THEME_LABELS = { auto: "Theme: system", light: "Theme: light", dark: "Theme: dark" };

    function storedTheme() {
        try {
            return localStorage.getItem("theme") || "auto";
        } catch (e) {
            return "auto";
        }
    }

    function applyTheme(theme) {
        const dark = theme === "dark" || (theme === "auto" && window.matchMedia("(prefers-color-scheme: dark)").matches);
        document.documentElement.setAttribute("data-bs-theme", dark ? "dark" : "light");
        document.querySelectorAll("[data-theme-toggle]").forEach(button => {
            button.title = THEME_LABELS[theme];
            button.setAttribute("aria-label", THEME_LABELS[theme]);
            button.dataset.theme = theme;
        });
        document.dispatchEvent(new CustomEvent("themechange"));
    }

    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
        if (storedTheme() === "auto") applyTheme("auto");
    });

    // ---------- Toasts ----------

    function notify(message, type = "info", timeout = 5000) {
        let stack = document.querySelector(".toast-stack");
        if (!stack) {
            stack = document.createElement("div");
            stack.className = "toast-stack";
            stack.setAttribute("role", "status");
            stack.setAttribute("aria-live", "polite");
            document.body.appendChild(stack);
        }
        const toast = document.createElement("div");
        toast.className = "toast-item " + type;
        if (message instanceof Node) toast.appendChild(message);
        else toast.textContent = message;
        stack.appendChild(toast);
        if (timeout) setTimeout(() => toast.remove(), timeout);
        return toast;
    }

    // ---------- Requests ----------

    // POST form fields and return the JSON reply; shows a toast and throws on errors.
    async function postForm(url, fields) {
        const response = await fetch(url, {
            method: "POST",
            headers: { "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json" },
            body: new URLSearchParams(fields),
        });
        let data = null;
        try {
            data = await response.json();
        } catch (e) {
            // Not JSON, e.g. a redirect to the login page.
        }
        if (!response.ok) {
            const message = (data && data.error) || `Request failed (${response.status})`;
            notify(message, "error");
            throw new Error(message);
        }
        return data;
    }

    function setTraceId(traceId) {
        fetch("/set_trace_id", {
            method: "POST",
            headers: { "Content-Type": "application/x-www-form-urlencoded" },
            body: new URLSearchParams({ trace_id: traceId }),
        }).then(() => location.reload());
    }

    // ---------- Formatting ----------

    function formatDuration(seconds) {
        if (seconds == null || !isFinite(seconds)) return "–";
        seconds = Math.max(0, Math.round(seconds));
        const d = Math.floor(seconds / 86400);
        const h = Math.floor((seconds % 86400) / 3600);
        const m = Math.floor((seconds % 3600) / 60);
        const s = seconds % 60;
        if (d) return `${d}d ${h}h`;
        if (h) return `${h}h ${m}m`;
        if (m) return `${m}m ${s}s`;
        return `${s}s`;
    }

    function formatDateTime(iso) {
        if (!iso) return "–";
        return new Date(iso).toLocaleString(undefined, {
            year: "numeric", month: "short", day: "numeric",
            hour: "2-digit", minute: "2-digit", second: "2-digit",
        });
    }

    // ---------- Version check ----------

    function isVersionNewer(remote, local) {
        const parse = v => v.replace(/^v/, "").split(".").map(Number);
        const r = parse(remote), l = parse(local);
        for (let i = 0; i < 3; i++) {
            if ((r[i] || 0) !== (l[i] || 0)) return (r[i] || 0) > (l[i] || 0);
        }
        return false;
    }

    function checkVersion() {
        const versionElem = document.getElementById("versionText");
        if (!versionElem) return;
        const localVersion = versionElem.textContent.trim();
        fetch("https://api.github.com/repos/yniverz/WayPointDB/releases/latest")
            .then(response => response.ok ? response.json() : Promise.reject())
            .then(data => {
                const remoteVersion = (data.tag_name || "").trim();
                if (remoteVersion && isVersionNewer(remoteVersion, localVersion)) {
                    const link = document.createElement("a");
                    link.href = "https://github.com/yniverz/WayPointDB/releases/latest";
                    link.target = "_blank";
                    link.rel = "noopener";
                    link.textContent = `${remoteVersion} is available`;
                    versionElem.append(" · ", link.cloneNode(true));

                    const message = document.createElement("span");
                    message.append("A new version of WayPointDB is out: ", link);
                    notify(message, "info", 8000);
                } else if (remoteVersion && remoteVersion !== localVersion) {
                    versionElem.textContent += " (unreleased)";
                }
            })
            .catch(() => {});
    }

    // ---------- Backdrop ----------

    // A copy of the last logged-in user's backdrop is kept in this browser for the login page.
    const BACKDROP_KEY = "loginBackdrop";
    const BACKDROP_TIME_KEY = "loginBackdropSaved";
    const BACKDROP_OPT_OUT_KEY = "loginBackdropOff";
    const BACKDROP_REFRESH_MS = 6 * 60 * 60 * 1000;
    // Local storage holds about 5 MB per site; larger images are not kept.
    const BACKDROP_MAX_LENGTH = 3 * 1024 * 1024;

    function storage(action) {
        try {
            return action(localStorage);
        } catch (e) {
            return null;
        }
    }

    function backdropOnLogin() {
        return storage(s => s.getItem(BACKDROP_OPT_OUT_KEY)) !== "1";
    }

    function forgetBackdrop() {
        storage(s => {
            s.removeItem(BACKDROP_KEY);
            s.removeItem(BACKDROP_TIME_KEY);
        });
    }

    function setBackdropOnLogin(on) {
        storage(s => on ? s.removeItem(BACKDROP_OPT_OUT_KEY) : s.setItem(BACKDROP_OPT_OUT_KEY, "1"));
        if (on) {
            storage(s => s.removeItem(BACKDROP_TIME_KEY));
            rememberBackdrop(document.querySelector(".backdrop[data-src]")?.dataset.src);
        } else {
            forgetBackdrop();
        }
    }

    function rememberBackdrop(src) {
        if (!src || !backdropOnLogin()) return;
        const saved = Number(storage(s => s.getItem(BACKDROP_TIME_KEY)) || 0);
        if (Date.now() - saved < BACKDROP_REFRESH_MS) return;

        fetch(src)
            .then(response => response.status === 200 ? response.blob() : Promise.reject())
            .then(blob => new Promise(resolve => {
                const reader = new FileReader();
                reader.onload = () => resolve(reader.result);
                reader.readAsDataURL(blob);
            }))
            .then(dataUrl => {
                if (dataUrl.length > BACKDROP_MAX_LENGTH) return;
                storage(s => {
                    s.setItem(BACKDROP_KEY, dataUrl);
                    s.setItem(BACKDROP_TIME_KEY, String(Date.now()));
                });
            })
            .catch(() => {});
    }

    function showBackdrop(backdrop, src) {
        const image = new Image();
        image.onload = () => {
            backdrop.style.backgroundImage = `url("${src}")`;
            backdrop.classList.add("loaded");
        };
        image.src = src;
    }

    function loadBackdrop() {
        const backdrop = document.querySelector(".backdrop");
        if (!backdrop) return;

        if (backdrop.hasAttribute("data-cached")) {
            const cached = backdropOnLogin() ? storage(s => s.getItem(BACKDROP_KEY)) : null;
            if (cached) showBackdrop(backdrop, cached);
            return;
        }

        showBackdrop(backdrop, backdrop.dataset.src);
        rememberBackdrop(backdrop.dataset.src);
    }

    // ---------- Wiring ----------

    document.addEventListener("DOMContentLoaded", () => {
        applyTheme(storedTheme());

        document.querySelectorAll("[data-theme-toggle]").forEach(button => {
            button.addEventListener("click", () => {
                const next = THEMES[(THEMES.indexOf(storedTheme()) + 1) % THEMES.length];
                try { localStorage.setItem("theme", next); } catch (e) { /* not persisted */ }
                applyTheme(next);
            });
        });

        document.querySelectorAll("[data-sidebar-toggle]").forEach(button => {
            button.addEventListener("click", () => document.body.classList.toggle("sidebar-open"));
        });

        const traceSelect = document.getElementById("traceSelect");
        if (traceSelect) traceSelect.addEventListener("change", () => setTraceId(traceSelect.value));

        // Forms with data-confirm ask before submitting (for destructive actions).
        document.querySelectorAll("form[data-confirm]").forEach(form => {
            form.addEventListener("submit", event => {
                if (!confirm(form.dataset.confirm)) event.preventDefault();
            });
        });

        loadBackdrop();
        checkVersion();
    });

    window.WP = { notify, postForm, setTraceId, formatDuration, formatDateTime, backdropOnLogin, setBackdropOnLogin };
})();
