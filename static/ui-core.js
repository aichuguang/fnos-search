(function () {
    const DEFAULT_FOCUSABLE = "button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), a[href], [tabindex]:not([tabindex='-1'])";
    let adminCsrfToken = "";

    function escapeHtml(value) {
        return String(value ?? "")
            .replaceAll("&", "&amp;")
            .replaceAll("<", "&lt;")
            .replaceAll(">", "&gt;")
            .replaceAll('"', "&quot;")
            .replaceAll("'", "&#039;");
    }

    function showToast(target, message, type = "", timeout = 3000) {
        const box = typeof target === "string" ? document.getElementById(target) : target;
        if (!box) return;
        box.textContent = message;
        box.setAttribute("role", type === "error" ? "alert" : "status");
        box.setAttribute("aria-live", type === "error" ? "assertive" : "polite");
        box.className = `toast ${type}`;
        box.classList.remove("hidden");
        window.clearTimeout(box.__toastTimer);
        box.__toastTimer = window.setTimeout(() => box.classList.add("hidden"), timeout);
    }

    async function requestJson(path, options = {}) {
        const {
            allowFailure = false,
            headers = {},
            onUnauthorized = null,
            credentials = "same-origin",
            ...fetchOptions
        } = options;
        const body = fetchOptions.body;
        const isFormData = typeof FormData !== "undefined" && body instanceof FormData;
        const requestHeaders = { ...headers };
        const hasContentType = Object.keys(requestHeaders).some(
            (key) => key.toLowerCase() === "content-type",
        );
        // Keep JSON request headers in one place. FormData must remain untouched
        // so the browser can generate its multipart boundary automatically.
        if (!isFormData && body != null && !hasContentType) {
            requestHeaders["Content-Type"] = "application/json";
        }
        const response = await fetch(path, {
            credentials,
            headers: requestHeaders,
            ...fetchOptions,
        });
        const data = await response.json().catch(() => ({}));
        if (response.status === 401 && typeof onUnauthorized === "function") onUnauthorized(response, data);
        if (!response.ok || (!allowFailure && data.success === false)) {
            const error = new Error(data.message || `请求失败：HTTP ${response.status}`);
            error.status = response.status;
            error.data = data;
            throw error;
        }
        return data;
    }

    async function requestAdminJson(path, options = {}) {
        const method = String(options.method || "GET").toUpperCase();
        const headers = { ...(options.headers || {}) };
        if (!adminCsrfToken && !["GET", "HEAD", "OPTIONS"].includes(method)) {
            const sessionResponse = await fetch("/api/admin/session", { credentials: "same-origin" });
            const sessionData = await sessionResponse.json().catch(() => ({}));
            adminCsrfToken = String(sessionData.csrf_token || "");
        }
        if (adminCsrfToken && !Object.keys(headers).some((key) => key.toLowerCase() === "x-csrf-token")) {
            headers["X-CSRF-Token"] = adminCsrfToken;
        }
        return requestJson(path, {
            ...options,
            credentials: "same-origin",
            headers,
            onUnauthorized: (response, data) => {
                adminCsrfToken = "";
                options.onUnauthorized?.(response, data);
            },
        });
    }

    function rememberAndFocus(container, preferred = null) {
        if (!container) return;
        container.__previousFocus = document.activeElement;
        window.setTimeout(() => (preferred || container.querySelector(DEFAULT_FOCUSABLE) || container)?.focus?.({ preventScroll: true }), 0);
    }

    function restoreFocus(container) {
        const previous = container?.__previousFocus;
        if (previous?.isConnected && typeof previous.focus === "function") {
            window.setTimeout(() => previous.focus({ preventScroll: true }), 0);
        }
        if (container) container.__previousFocus = null;
    }

    function trapFocus(event, container) {
        if (event.key !== "Tab" || !container) return;
        const focusables = Array.from(container.querySelectorAll(DEFAULT_FOCUSABLE)).filter((item) => !item.hidden && item.offsetParent !== null);
        if (!focusables.length) {
            event.preventDefault();
            container.focus();
            return;
        }
        const first = focusables[0];
        const last = focusables[focusables.length - 1];
        if (event.shiftKey && document.activeElement === first) {
            event.preventDefault();
            last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
            event.preventDefault();
            first.focus();
        }
    }

    function bindRovingTabs(options = {}) {
        const {
            selector,
            dataKey,
            orientation = "horizontal",
            onActivate,
        } = options;
        const buttons = Array.from(document.querySelectorAll(selector || "[role='tab']"));
        const previousKey = orientation === "vertical" ? "ArrowUp" : "ArrowLeft";
        const nextKey = orientation === "vertical" ? "ArrowDown" : "ArrowRight";
        buttons.forEach((button) => {
            const activate = () => onActivate?.(button.dataset[dataKey], button);
            button.addEventListener("click", activate);
            button.addEventListener("keydown", (event) => {
                if (![previousKey, nextKey, "Home", "End"].includes(event.key)) return;
                event.preventDefault();
                const enabled = buttons.filter((item) => !item.disabled);
                const current = enabled.indexOf(button);
                const nextIndex = event.key === "Home"
                    ? 0
                    : event.key === "End"
                        ? enabled.length - 1
                        : (current + (event.key === nextKey ? 1 : -1) + enabled.length) % enabled.length;
                const next = enabled[nextIndex];
                if (next) {
                    onActivate?.(next.dataset[dataKey], next);
                    next.focus();
                }
            });
        });
        return buttons;
    }

    window.FnosUI = Object.freeze({
        escapeHtml,
        showToast,
        requestJson,
        requestAdminJson,
        rememberAndFocus,
        restoreFocus,
        trapFocus,
        bindRovingTabs,
    });
})();
