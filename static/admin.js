const adminState = {
    jobs: [],
    overviewJobs: [],
    dashboardSummary: {},
    requests: [],
    fileEvents: [],
    searchProviders: [],
    settings: {},
    advancedConfig: {},
    advancedStoredConfig: {},
    advancedConfigMeta: {},
    advancedFormBaseline: {},
    mediaLibraries: [],
    mediaCategories: [],
    mediaSummary: {},
    mediaRunning: [],
    mediaDiagnostics: {},
    sixpanOAuth: {},
    organizerTasks: [],
    organizerRuns: [],
    organizerStatus: {},
    organizerDetailPaging: {},
    updateSubscriptions: [],
    updateScheduler: {},
    trendingCandidates: [],
    trendingStatus: {},
    securityStatus: {},
    historyMaintenance: {},
    notifications: {},
    notificationEvents: {},
    notificationSummary: {},
    notificationDeliveries: [],
    profile: {},
    pagination: {
        requests: { page: 1, per_page: 20, total: 0, pages: 1 },
        jobs: { page: 1, per_page: 20, total: 0, pages: 1 },
        rcloneRuns: { page: 1, per_page: 20, total: 0, pages: 1 },
        taskLogs: { page: 1, per_page: 20, total: 0, pages: 1 },
        organizerTasks: { page: 1, per_page: 50, total: 0, pages: 1 },
        organizerRuns: { page: 1, per_page: 30, total: 0, pages: 1 },
        updates: { page: 1, per_page: 50, total: 0, pages: 1 },
        trending: { page: 1, per_page: 50, total: 0, pages: 1 },
    },
    activeTab: "overview",
    activeSettingsSection: "search",
    loadedTabs: {},
    loadingTabs: {},
    rcloneLiveTimer: null,
    rcloneLiveLoading: false,
    requestFeedback: {
        pending: 0,
        hideTimer: null,
        lastActionButton: null,
        lastActionAt: 0,
    },
};

const $ = (id) => document.getElementById(id);

const ADVANCED_CLOUD_TYPES = [
    ["quark", "夸克"],
    ["tianyi", "天翼云"],
    ["mobile", "移动云盘"],
    ["magnet", "磁链"],
];

const ADVANCED_ROUTE_LABELS = {
    quark: "夸克",
    uc: "UC",
    cloud139: "139 移动云",
    magnet: "磁链",
    torrent: "种子",
};

const ADVANCED_SECRET_FIELDS = {
    advPansouPassword: "pansou.password",
    advPansouDefaultToken: "pansou.default_token",
    advBtbtlaProxyUrl: "btbtla.proxy_url",
    advOpenlistToken: "openlist.token",
    advTmdbToken: "tmdb.token",
    advTmdbProxyUrl: "tmdb.proxy_url",
    advAiApiKey: "ai.api_key",
    advQuarkToken: "quark.token",
    advSixpanClientSecret: "sixpan.client_secret",
    advCmccAccessToken: "cmcc_upload.access_token",
    advFnosPassword: "fnos.password",
    advFnosApiKey: "fnos.api_key",
    advFnosSecret: "fnos.secret",
    advFnosToken: "fnos.token",
};

const ADVANCED_SECRET_LABELS = {
    "pansou.password": "PanSou 密码",
    "pansou.default_token": "PanSou 默认 Token",
    "btbtla.proxy_url": "BT 独立代理",
    "openlist.token": "OpenList Token",
    "tmdb.token": "TMDB Token",
    "tmdb.proxy_url": "TMDB 独立代理",
    "ai.api_key": "AI API Key",
    "quark.token": "Quark Token",
    "sixpan.client_secret": "6盘 ClientSecret",
    "cmcc_upload.access_token": "移动云授权凭据",
    "fnos.password": "飞牛密码",
    "fnos.api_key": "飞牛 API Key",
    "fnos.secret": "飞牛 Secret",
    "fnos.token": "飞牛 Token",
};

const ADVANCED_CATEGORY_ORDER = [
    ["movie", "电影"],
    ["tv", "电视剧"],
    ["anime", "动漫"],
    ["variety", "综艺"],
    ["other", "其他"],
];

function icon(name, className = "icon") {
    const safeName = String(name || "placeholder").replace(/[^a-z0-9_-]/gi, "");
    const safeClass = String(className || "").replace(/[^a-z0-9_ -]/gi, "");
    return `<span class="icon-slot icon-${safeName} ${safeClass}" aria-hidden="true"></span>`;
}

function escapeHtml(value) {
    return window.FnosUI.escapeHtml(value);
}

function formatDate(value) {
    if (!value) return "-";
    if (String(value).startsWith("0001")) return "未知";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return value;
    const pad = (number) => String(number).padStart(2, "0");
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

function formatBytes(value) {
    const size = Number(value || 0);
    if (!Number.isFinite(size) || size <= 0) return "-";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let number = size;
    let index = 0;
    while (number >= 1024 && index < units.length - 1) {
        number /= 1024;
        index += 1;
    }
    return `${number >= 10 || index === 0 ? number.toFixed(0) : number.toFixed(1)} ${units[index]}`;
}

function normalizePagination(pagination, fallback = {}) {
    const page = Math.max(1, Number(pagination?.page || fallback.page || 1));
    const perPage = Math.max(1, Number(pagination?.per_page || fallback.per_page || 50));
    const total = Math.max(0, Number(pagination?.total || 0));
    const pages = Math.max(1, Number(pagination?.pages || Math.ceil(total / perPage) || 1));
    return { page, per_page: perPage, total, pages, has_prev: page > 1, has_next: page < pages };
}


const ADMIN_PAGE_SIZE_OPTIONS = [10, 20, 50, 100, 200];

function renderPager(id, pagination, onPage) {
    const box = $(id);
    if (!box) return;
    const meta = normalizePagination(pagination);
    if (!meta.total) {
        box.innerHTML = `<span class="pager-info">共 0 条</span>`;
        return;
    }
    const pageSizeOptions = Array.from(new Set([...ADMIN_PAGE_SIZE_OPTIONS, meta.per_page])).sort((a, b) => a - b);
    box.innerHTML = `
        <span class="pager-info">第 ${escapeHtml(meta.page)} / ${escapeHtml(meta.pages)} 页，共 ${escapeHtml(meta.total)} 条</span>
        <label class="pager-size-label">每页
            <select class="pager-size" data-per-page>
                ${pageSizeOptions.map((size) => `<option value="${escapeHtml(size)}" ${Number(size) === Number(meta.per_page) ? "selected" : ""}>${escapeHtml(size)}</option>`).join("")}
            </select>
            条
        </label>
        <button class="secondary mini" type="button" data-page="1" ${meta.page <= 1 ? "disabled" : ""}>首页</button>
        <button class="secondary mini" type="button" data-page="${escapeHtml(meta.page - 1)}" ${meta.page <= 1 ? "disabled" : ""}>上一页</button>
        <button class="secondary mini" type="button" data-page="${escapeHtml(meta.page + 1)}" ${meta.page >= meta.pages ? "disabled" : ""}>下一页</button>
        <button class="secondary mini" type="button" data-page="${escapeHtml(meta.pages)}" ${meta.page >= meta.pages ? "disabled" : ""}>末页</button>
    `;
    box.querySelectorAll("[data-page]").forEach((button) => {
        button.addEventListener("click", () => onPage(Number(button.dataset.page || 1)));
    });
    box.querySelector("[data-per-page]")?.addEventListener("change", (event) => {
        handlePagerPageSizeChange(id, Number(event.target.value || meta.per_page));
    });
}

function renderListPagers(ids, pagination, onPage) {
    ids.forEach((id) => renderPager(id, pagination, onPage));
}

function handlePagerPageSizeChange(id, perPage) {
    const value = Math.max(1, Number(perPage || 20));
    const reload = (key, loader) => {
        if (!adminState.pagination[key]) return;
        adminState.pagination[key].page = 1;
        adminState.pagination[key].per_page = value;
        loader().catch((error) => toast(error.message, "error"));
    };
    if (["requestsPager", "requestsPagerTop"].includes(id)) return reload("requests", loadRequests);
    if (["jobsPager", "jobsPagerTop"].includes(id)) return reload("jobs", loadJobs);
    if (id === "rcloneRunsPager") return reload("rcloneRuns", loadRclone);
    if (id === "taskLogsPager") return reload("taskLogs", loadTaskLogs);
    if (id === "organizerTasksPager") return reload("organizerTasks", loadOrganizer);
    if (id === "organizerRunsPager") return reload("organizerRuns", openOrganizerRunsDrawer);
}


function toast(message, type = "") {
    window.FnosUI.showToast("toast", message, type);
}

function ensureRequestFeedbackIndicator() {
    let box = $("adminRequestFeedback");
    if (box) return box;
    if (!document.body) return null;
    box = document.createElement("div");
    box.id = "adminRequestFeedback";
    box.className = "admin-request-feedback hidden";
    box.setAttribute("role", "status");
    box.setAttribute("aria-live", "polite");
    box.innerHTML = `
        <span class="button-spinner admin-request-spinner" aria-hidden="true"></span>
        <span class="admin-request-feedback-text">正在处理，请稍候...</span>
    `;
    document.body.appendChild(box);
    return box;
}

function beginRequestFeedback(message = "正在处理，请稍候...") {
    const state = adminState.requestFeedback;
    const token = { done: false };
    state.pending = Math.max(0, state.pending || 0) + 1;
    if (state.hideTimer) {
        window.clearTimeout(state.hideTimer);
        state.hideTimer = null;
    }
    const box = ensureRequestFeedbackIndicator();
    if (box) {
        const text = box.querySelector(".admin-request-feedback-text");
        if (text) text.textContent = message;
        box.classList.remove("hidden");
        document.body.classList.add("admin-request-active");
    }
    return token;
}

function endRequestFeedback(token) {
    if (!token || token.done) return;
    token.done = true;
    const state = adminState.requestFeedback;
    state.pending = Math.max(0, (state.pending || 0) - 1);
    if (state.pending > 0) return;
    if (state.hideTimer) window.clearTimeout(state.hideTimer);
    state.hideTimer = window.setTimeout(() => {
        if (state.pending > 0) return;
        $("adminRequestFeedback")?.classList.add("hidden");
        document.body.classList.remove("admin-request-active");
        state.hideTimer = null;
    }, 120);
}

function captureActionButton(event) {
    const button = event.target?.closest?.("button, .button, [role='button'], input[type='button'], input[type='submit']");
    if (!button || button.disabled || button.dataset.noAutoLoading === "true") return;
    adminState.requestFeedback.lastActionButton = button;
    adminState.requestFeedback.lastActionAt = Date.now();
}

function recentActionButton() {
    const state = adminState.requestFeedback;
    const button = state.lastActionButton;
    if (!button || !button.isConnected || button.dataset.noAutoLoading === "true") return null;
    if (Date.now() - Number(state.lastActionAt || 0) > 1500) return null;
    return button;
}

function beginButtonBusy(button, label = "处理中...") {
    if (!button) return null;
    const token = { button, done: false };
    const state = button.__busyState || {
        count: 0,
        originalHtml: button.innerHTML,
        originalValue: button.value,
        originalAriaBusy: button.getAttribute("aria-busy"),
        wasDisabled: button.disabled,
        isInput: ["INPUT", "TEXTAREA"].includes(button.tagName),
    };
    state.count += 1;
    button.__busyState = state;
    button.dataset.busy = "1";
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    button.classList.add("is-loading");
    if (state.count === 1) {
        if (state.isInput) {
            button.value = label;
        } else {
            button.innerHTML = `<span class="button-spinner" aria-hidden="true"></span>${escapeHtml(label)}`;
        }
    }
    return token;
}

function endButtonBusy(token) {
    const button = token?.button;
    if (!button || token.done || !button.__busyState) return;
    token.done = true;
    const state = button.__busyState;
    state.count = Math.max(0, Number(state.count || 0) - 1);
    if (state.count > 0) return;
    if (state.isInput) {
        button.value = state.originalValue || "";
    } else {
        button.innerHTML = state.originalHtml || button.textContent || "";
    }
    button.disabled = Boolean(state.wasDisabled);
    if (state.originalAriaBusy === null) button.removeAttribute("aria-busy");
    else button.setAttribute("aria-busy", state.originalAriaBusy);
    button.classList.remove("is-loading");
    delete button.dataset.busy;
    delete button.__busyState;
}

function setButtonBusy(button, busy, label = "处理中...") {
    if (!button) return;
    if (busy) {
        button.__manualBusyToken = beginButtonBusy(button, label);
        return;
    }
    endButtonBusy(button.__manualBusyToken);
    delete button.__manualBusyToken;
}

async function withButtonBusy(button, label, callback) {
    if (button?.dataset.busy === "1") return null;
    const token = beginButtonBusy(button, label);
    try {
        return await callback();
    } finally {
        endButtonBusy(token);
    }
}

let adminCsrfToken = "";

async function ensureAdminCsrfToken() {
    if (adminCsrfToken) return adminCsrfToken;
    const response = await fetch("/api/admin/session", { credentials: "same-origin" });
    if (!response.ok) return "";
    const data = await response.json().catch(() => ({}));
    adminCsrfToken = String(data.csrf_token || "");
    return adminCsrfToken;
}

async function api(path, options = {}) {
    const {
        allowFailure = false,
        headers = {},
        loadingMessage,
        silentLoading = false,
        button = null,
        buttonLabel = "",
        skipButtonLoading = false,
        ...fetchOptions
    } = options;
    const body = fetchOptions.body;
    const isFormData = typeof FormData !== "undefined" && body instanceof FormData;
    const requestHeaders = { ...headers };
    if (!isFormData && !Object.keys(requestHeaders).some((key) => key.toLowerCase() === "content-type")) {
        requestHeaders["Content-Type"] = "application/json";
    }
    const method = String(fetchOptions.method || "GET").toUpperCase();
    if (!["GET", "HEAD", "OPTIONS"].includes(method) && path.startsWith("/api/admin/")) {
        const csrfToken = await ensureAdminCsrfToken();
        if (csrfToken && !Object.keys(requestHeaders).some((key) => key.toLowerCase() === "x-csrf-token")) {
            requestHeaders["X-CSRF-Token"] = csrfToken;
        }
    }
    const feedbackMessage = loadingMessage || (method === "GET" ? "正在加载数据..." : "正在提交操作...");
    const fallbackButtonLabel = method === "GET" ? "加载中..." : "处理中...";
    const feedbackToken = silentLoading ? null : beginRequestFeedback(feedbackMessage);
    const busyButton = skipButtonLoading ? null : (button || recentActionButton());
    const buttonToken = busyButton ? beginButtonBusy(busyButton, buttonLabel || fallbackButtonLabel) : null;
    try {
        return await window.FnosUI.requestJson(path, {
            allowFailure,
            credentials: "same-origin",
            headers: requestHeaders,
            onUnauthorized: () => {
                window.location.href = "/admin/login";
            },
            ...fetchOptions,
        });
    } finally {
        endButtonBusy(buttonToken);
        endRequestFeedback(feedbackToken);
    }
}

async function confirmDialog(options = {}) {
    if (typeof window.showConfirmDialog !== "function") {
        toast("确认组件未加载，已取消本次操作", "error");
        return false;
    }
    return Boolean(await window.showConfirmDialog(options));
}

async function promptDialog(options = {}) {
    if (typeof window.showPromptDialog !== "function") {
        toast("输入组件未加载，已取消本次操作", "error");
        return null;
    }
    return window.showPromptDialog(options);
}

function statusPill(status) {
    const normalized = String(status || "unknown");
    let cls = "pill info";
    if (["done", "success", "ok", "completed", "approved", "ready", "implemented", "auto_approved", "manual_confirmed", "skipped_existing", "enabled", "auto_import"].includes(normalized)) cls = "pill ok";
    if (["failed", "error", "upload_error", "upload_exception", "rejected", "unsupported", "probe_failed"].includes(normalized)) cls = "pill error";
    if (["pending_review", "waiting_transfer", "waiting_openlist", "waiting_organizer", "organizing", "confirming", "review", "submitted", "imported", "created", "provider_submitting", "retry_requested", "placeholder", "unconfigured", "transferring", "stabilizing", "scanning", "matching", "planning", "waiting_review", "executing", "skipped", "empty", "need_edit", "conflict", "delete_ad", "paused", "archived", "running", "pending", "candidate", "discovered", "task_exists", "ignored"].includes(normalized)) cls = "pill warn";
    return `<span class="${cls}">${escapeHtml(statusText(normalized))}</span>`;
}

function statusText(status) {
    const labels = {
        unknown: "未知",
        enabled: "启用中",
        paused: "已暂停",
        archived: "已归档",
        running: "运行中",
        idle: "空闲",
        created: "已创建",
        provider_submitting: "正在提交到网盘（结果待确认）",
        ok: "正常",
        empty: "无候选",
        candidate: "候选",
        auto_import: "自动入库",
        submitted: "已提交",
        imported: "已提交",
        completed: "完整入库",
        pending_review: "等待审核",
        waiting_transfer: "等待搬运",
        transferring: "搬运中",
        waiting_openlist: "等待 OpenList 可见",
        waiting_organizer: "等待整理",
        organizing: "整理中",
        confirming: "确认标准目录",
        review: "等待人工确认",
        done: "入库完成",
        success: "成功",
        failed: "失败",
        error: "错误",
        upload_error: "上传异常",
        upload_exception: "上传异常",
        rejected: "已拒绝",
        cancelled: "已取消",
        ignored_cancelled: "已忽略",
        unsupported: "暂不支持",
        retry_requested: "等待重试",
        placeholder: "占位",
        unconfigured: "未配置",
        probe_failed: "检查失败",
        implemented: "已实现",
        ready: "就绪",
        stabilizing: "等待稳定",
        scanning: "扫描中",
        matching: "匹配中",
        planning: "规划中",
        waiting_review: "需审核",
        auto_approved: "自动通过",
        manual_confirmed: "人工确认",
        executing: "执行中",
        skipped: "已跳过",
        skipped_existing: "已存在",
        need_edit: "需编辑",
        conflict: "冲突",
        delete_ad: "广告删除",
        pending: "待处理",
        start: "开始执行",
        sync_completion: "同步入库完成状态",
        scan_existing: "检查本地已有集",
        discover: "检查追更来源",
        source_health: "更新来源健康状态",
        match: "匹配目标文件",
        filter_candidates: "过滤非目标文件",
        import: "提交单集入库",
        sync_existing: "同步本地新增集",
        finish: "执行完成",
        discovered: "待审核",
        already_exists: "媒体库已有",
        task_exists: "已有任务",
        ignored: "已忽略",
        partial: "部分成功",
    };
    return labels[status] || status || "-";
}

let adminMediaModule = null;

function getAdminMediaModule() {
    if (adminMediaModule) return adminMediaModule;
    adminMediaModule = window.FnosAdminMedia.create({
        state: adminState,
        getElement: $,
        api,
        toast,
        escapeHtml,
        icon,
        openDrawer,
    });
    return adminMediaModule;
}

function formatNumber(...args) {
    return getAdminMediaModule().formatNumber(...args);
}

function mediaRowKey(...args) {
    return getAdminMediaModule().mediaRowKey(...args);
}

function setMediaRefreshState(...args) {
    return getAdminMediaModule().setMediaRefreshState(...args);
}

function mediaRefreshMessage(...args) {
    return getAdminMediaModule().mediaRefreshMessage(...args);
}

function loadMediaLibraries(...args) {
    return getAdminMediaModule().loadMediaLibraries(...args);
}

function renderMediaDiagnosticHint(...args) {
    return getAdminMediaModule().renderMediaDiagnosticHint(...args);
}

function renderMediaStats(...args) {
    return getAdminMediaModule().renderMediaStats(...args);
}

function renderMediaStatCard(...args) {
    return getAdminMediaModule().renderMediaStatCard(...args);
}

function renderMediaLibraries(...args) {
    return getAdminMediaModule().renderMediaLibraries(...args);
}

function renderMediaLibraryRow(...args) {
    return getAdminMediaModule().renderMediaLibraryRow(...args);
}

function mediaStatusForItem(...args) {
    return getAdminMediaModule().mediaStatusForItem(...args);
}

function renderMediaMappings(...args) {
    return getAdminMediaModule().renderMediaMappings(...args);
}

function showMediaLibraryDetail(...args) {
    return getAdminMediaModule().showMediaLibraryDetail(...args);
}

function refreshPayloadFromItem(...args) {
    return getAdminMediaModule().refreshPayloadFromItem(...args);
}

function refreshMediaItem(...args) {
    return getAdminMediaModule().refreshMediaItem(...args);
}

function refreshMediaCategory(...args) {
    return getAdminMediaModule().refreshMediaCategory(...args);
}

function refreshAllMedia(...args) {
    return getAdminMediaModule().refreshAllMedia(...args);
}

let adminBootstrapModule = null;

let adminNotificationsModule = null;

function getAdminNotificationsModule() {
    if (adminNotificationsModule) return adminNotificationsModule;
    adminNotificationsModule = window.FnosAdminNotifications.create({
        state: adminState,
        getElement: $,
        api,
        toast,
        escapeHtml,
        formatDate,
    });
    return adminNotificationsModule;
}

function loadNotificationSettings(...args) {
    return getAdminNotificationsModule().loadNotificationSettings(...args);
}

function bindNotificationSettings(...args) {
    return getAdminNotificationsModule().bindNotificationSettings(...args);
}

function getAdminBootstrapModule() {
    if (adminBootstrapModule) return adminBootstrapModule;
    adminBootstrapModule = window.FnosAdminBootstrap.create({
        state: adminState, getElement: $, api, toast, captureActionButton,
        loadOverview, loadRequests, loadJobs, loadUpdates, openUpdateEditor, runDueUpdates, loadTrending, runTrending, openTrendingScheduler,
        startRclone, stopRclone, checkRclone, loadRclone, loadTaskLogs, loadOrganizer, openOrganizerScanDrawer, openOrganizerRunsDrawer,
        loadAdapters, loadMediaLibraries, refreshAllMedia, saveSettings, saveAllSettingsTransaction, saveProfile, uploadSiteLogo, saveSearchProviders,
        loadAdvancedConfig, saveAdvancedConfig, loadRcloneWebdavConfig, saveRcloneWebdavConfig, testRcloneWebdavConfig,
        exportAdvancedConfig, importAdvancedConfig, applyCategoryTemplate, renderCategoryTemplatePreview,
        loadSecurityStatus, openAdvancedSecretManager, loadHistoryMaintenanceSummary, cleanupHistoryRecords,
        loadNotificationSettings, bindNotificationSettings,
        startSixpanDeviceAuth, checkSixpanDeviceAuth, probeSixpan, testBtbtlaProxy, testOrganizerEndpoint, syncAdvancedDependencies,
        closeRawLogModal, startRcloneLivePolling,
    });
    return adminBootstrapModule;
}

function tabLoader(...args) {
    return getAdminBootstrapModule().tabLoader(...args);
}

function clearTabLoadError(...args) {
    return getAdminBootstrapModule().clearTabLoadError(...args);
}

function renderTabLoadError(...args) {
    return getAdminBootstrapModule().renderTabLoadError(...args);
}

function ensureTabLoaded(...args) {
    return getAdminBootstrapModule().ensureTabLoaded(...args);
}

function activateTab(...args) {
    return getAdminBootstrapModule().activateTab(...args);
}

function activateSettingsSection(...args) {
    return getAdminBootstrapModule().activateSettingsSection(...args);
}

function setConfigExpertMode(...args) {
    return getAdminBootstrapModule().setConfigExpertMode(...args);
}

function initConfigDisplayMode(...args) {
    return getAdminBootstrapModule().initConfigDisplayMode(...args);
}

function scheduleAdvancedConfigMasonry(...args) {
    return getAdminBootstrapModule().scheduleAdvancedConfigMasonry(...args);
}

function scheduleAdvancedConfigMasonrySettled(...args) {
    return getAdminBootstrapModule().scheduleAdvancedConfigMasonrySettled(...args);
}

function layoutAdvancedConfigMasonry(...args) {
    return getAdminBootstrapModule().layoutAdvancedConfigMasonry(...args);
}

function focusAdminModal(...args) {
    return getAdminBootstrapModule().focusAdminModal(...args);
}

function restoreAdminModalFocus(...args) {
    return getAdminBootstrapModule().restoreAdminModalFocus(...args);
}

function trapAdminModalFocus(...args) {
    return getAdminBootstrapModule().trapAdminModalFocus(...args);
}

function openDrawer(...args) {
    return getAdminBootstrapModule().openDrawer(...args);
}

function closeDrawer(...args) {
    return getAdminBootstrapModule().closeDrawer(...args);
}

function logout(...args) {
    return getAdminBootstrapModule().logout(...args);
}

function loadAll(...args) {
    return getAdminBootstrapModule().loadAll(...args);
}

function bindEvents(...args) {
    return getAdminBootstrapModule().bindEvents(...args);
}

let adminSystemModule = null;

function getAdminSystemModule() {
    if (adminSystemModule) return adminSystemModule;
    adminSystemModule = window.FnosAdminSystem.create({
        state: adminState,
        getElement: $,
        api,
        toast,
        escapeHtml,
        statusPill,
        formatDate,
        openDrawer,
        focusAdminModal,
        restoreAdminModalFocus,
        confirmDialog,
        loadOverview,
        loadRequests,
        loadJobs,
        loadRclone,
        loadOrganizer,
        loadUpdates,
    });
    return adminSystemModule;
}

function openRawLogModal(...args) {
    return getAdminSystemModule().openRawLogModal(...args);
}

function closeRawLogModal(...args) {
    return getAdminSystemModule().closeRawLogModal(...args);
}

function loadSecurityStatus(...args) {
    return getAdminSystemModule().loadSecurityStatus(...args);
}

function renderSecurityStatus(...args) {
    return getAdminSystemModule().renderSecurityStatus(...args);
}

function loadHistoryMaintenanceSummary(...args) {
    return getAdminSystemModule().loadHistoryMaintenanceSummary(...args);
}

function renderHistoryMaintenance(...args) {
    return getAdminSystemModule().renderHistoryMaintenance(...args);
}

function cleanupHistoryRecords(...args) {
    return getAdminSystemModule().cleanupHistoryRecords(...args);
}

function renderProfileSettings(...args) {
    return getAdminSystemModule().renderProfileSettings(...args);
}

function renderImagePreview(...args) {
    return getAdminSystemModule().renderImagePreview(...args);
}

function saveProfile(...args) {
    return getAdminSystemModule().saveProfile(...args);
}

function uploadProfileAvatar(...args) {
    return getAdminSystemModule().uploadProfileAvatar(...args);
}

function uploadSiteLogo(...args) {
    return getAdminSystemModule().uploadSiteLogo(...args);
}

function uploadForm(...args) {
    return getAdminSystemModule().uploadForm(...args);
}

let adminJobsModule = null;

function getAdminJobsModule() {
    if (adminJobsModule) return adminJobsModule;
    adminJobsModule = window.FnosAdminJobs.create({
        state: adminState,
        getElement: $,
        api,
        toast,
        escapeHtml,
        icon,
        statusPill,
        statusText,
        formatDate,
        normalizePagination,
        renderPager,
        openDrawer,
        closeDrawer,
        promptDialog,
        confirmDialog,
        renderTimelineRawData,
        loadRclone,
    });
    return adminJobsModule;
}

function loadOverview(...args) {
    return getAdminJobsModule().loadOverview(...args);
}

function renderMetricCard(...args) {
    return getAdminJobsModule().renderMetricCard(...args);
}

function loadRequests(...args) {
    return getAdminJobsModule().loadRequests(...args);
}

function renderRequests(...args) {
    return getAdminJobsModule().renderRequests(...args);
}

function requestActionButtons(...args) {
    return getAdminJobsModule().requestActionButtons(...args);
}

function sourceText(...args) {
    return getAdminJobsModule().sourceText(...args);
}

function riskPill(...args) {
    return getAdminJobsModule().riskPill(...args);
}

function suggestionText(...args) {
    return getAdminJobsModule().suggestionText(...args);
}

function contentGuardInfo(...args) {
    return getAdminJobsModule().contentGuardInfo(...args);
}

function showRequestDetail(...args) {
    return getAdminJobsModule().showRequestDetail(...args);
}

function requestDrawerActions(...args) {
    return getAdminJobsModule().requestDrawerActions(...args);
}

function approveRequest(...args) {
    return getAdminJobsModule().approveRequest(...args);
}

function rejectRequest(...args) {
    return getAdminJobsModule().rejectRequest(...args);
}

function cancelRequest(...args) {
    return getAdminJobsModule().cancelRequest(...args);
}

function loadJobs(...args) {
    return getAdminJobsModule().loadJobs(...args);
}

function renderJobs(...args) {
    return getAdminJobsModule().renderJobs(...args);
}

function jobActionButtons(...args) {
    return getAdminJobsModule().jobActionButtons(...args);
}

function renderOverviewJobs(...args) {
    return getAdminJobsModule().renderOverviewJobs(...args);
}

function retryJob(...args) {
    return getAdminJobsModule().retryJob(...args);
}

function cancelJob(...args) {
    return getAdminJobsModule().cancelJob(...args);
}

function showJobDetail(...args) {
    return getAdminJobsModule().showJobDetail(...args);
}

function loadTaskLogs(...args) {
    return getAdminRcloneModule().loadTaskLogs(...args);
}

function renderCompletionChecks(...args) {
    return getAdminJobsModule().renderCompletionChecks(...args);
}

function renderTimelineItem(...args) {
    return getAdminJobsModule().renderTimelineItem(...args);
}

function renderTimelineRawData() {
    return "";
}

let adminRcloneModule = null;

function getAdminRcloneModule() {
    if (adminRcloneModule) return adminRcloneModule;
    adminRcloneModule = window.FnosAdminRclone.create({
        state: adminState,
        getElement: $,
        api,
        toast,
        escapeHtml,
        statusPill,
        statusText,
        formatDate,
        normalizePagination,
        renderPager,
        renderTimelineRawData,
        openRawLogModal,
        confirmDialog,
        loadOverview,
        showJobDetail,
    });
    return adminRcloneModule;
}

function loadRclone() {
    return getAdminRcloneModule().loadRclone();
}

function loadRcloneLive() {
    return getAdminRcloneModule().loadRcloneLive();
}

function startRcloneLivePolling() {
    return getAdminRcloneModule().startRcloneLivePolling();
}

function retryFileEvent(id) {
    return getAdminRcloneModule().retryFileEvent(id);
}

function startRclone() {
    return getAdminRcloneModule().startRclone();
}

function stopRclone() {
    return getAdminRcloneModule().stopRclone();
}

function checkRclone() {
    return getAdminRcloneModule().checkRclone();
}

function showRcloneRunDetail(runId) {
    return getAdminRcloneModule().showRcloneRunDetail(runId);
}

function renderRcloneEvent(item) {
    return getAdminRcloneModule().renderRcloneEvent(item);
}

let adminOrganizerModule = null;

function getAdminOrganizerModule() {
    if (adminOrganizerModule) return adminOrganizerModule;
    adminOrganizerModule = window.FnosAdminOrganizer.create({
        state: adminState,
        getElement: $,
        api,
        toast,
        escapeHtml,
        statusPill,
        formatDate,
        formatBytes,
        normalizePagination,
        renderPager,
        openDrawer,
        closeDrawer,
        confirmDialog,
        withButtonBusy,
    });
    return adminOrganizerModule;
}

function loadOrganizer(...args) {
    return getAdminOrganizerModule().loadOrganizer(...args);
}

function renderOrganizer(...args) {
    return getAdminOrganizerModule().renderOrganizer(...args);
}

function renderOrganizerStatus(...args) {
    return getAdminOrganizerModule().renderOrganizerStatus(...args);
}

function renderOrganizerTasks(...args) {
    return getAdminOrganizerModule().renderOrganizerTasks(...args);
}

function renderOrganizerTaskRow(...args) {
    return getAdminOrganizerModule().renderOrganizerTaskRow(...args);
}

function organizerTaskAgeMs(...args) {
    return getAdminOrganizerModule().organizerTaskAgeMs(...args);
}

function organizerTaskIsStale(...args) {
    return getAdminOrganizerModule().organizerTaskIsStale(...args);
}

function organizerTaskButtons(...args) {
    return getAdminOrganizerModule().organizerTaskButtons(...args);
}

function organizerDetailActionPanel(...args) {
    return getAdminOrganizerModule().organizerDetailActionPanel(...args);
}

function organizerStatusGuide(...args) {
    return getAdminOrganizerModule().organizerStatusGuide(...args);
}

function organizerDrawerActionButtons(...args) {
    return getAdminOrganizerModule().organizerDrawerActionButtons(...args);
}

function renderOrganizerRuns(...args) {
    return getAdminOrganizerModule().renderOrganizerRuns(...args);
}

function openOrganizerScanDrawer(...args) {
    return getAdminOrganizerModule().openOrganizerScanDrawer(...args);
}

function openOrganizerRunsDrawer(...args) {
    return getAdminOrganizerModule().openOrganizerRunsDrawer(...args);
}

function showOrganizerTask(...args) {
    return getAdminOrganizerModule().showOrganizerTask(...args);
}

function renderOrganizerFileSummary(...args) {
    return getAdminOrganizerModule().renderOrganizerFileSummary(...args);
}

function renderOrganizerFileRow(...args) {
    return getAdminOrganizerModule().renderOrganizerFileRow(...args);
}

function renderOrganizerOperationRow(...args) {
    return getAdminOrganizerModule().renderOrganizerOperationRow(...args);
}

function mountOrganizerPagedList(...args) {
    return getAdminOrganizerModule().mountOrganizerPagedList(...args);
}

function renderOrganizerPagedList(...args) {
    return getAdminOrganizerModule().renderOrganizerPagedList(...args);
}

function renderOrganizerDirCleanup(...args) {
    return getAdminOrganizerModule().renderOrganizerDirCleanup(...args);
}

function renderOrganizerStrmRefresh(...args) {
    return getAdminOrganizerModule().renderOrganizerStrmRefresh(...args);
}

function organizerHasLowConfidence(...args) {
    return getAdminOrganizerModule().organizerHasLowConfidence(...args);
}

function organizerPathDirname(...args) {
    return getAdminOrganizerModule().organizerPathDirname(...args);
}

function organizerPathBasename(...args) {
    return getAdminOrganizerModule().organizerPathBasename(...args);
}

function organizerResourceDirFromTarget(...args) {
    return getAdminOrganizerModule().organizerResourceDirFromTarget(...args);
}

function renderOrganizerReviewFocus(...args) {
    return getAdminOrganizerModule().renderOrganizerReviewFocus(...args);
}

function renderOrganizerProblemSummary(...args) {
    return getAdminOrganizerModule().renderOrganizerProblemSummary(...args);
}

function renderOrganizerMappingRow(...args) {
    return getAdminOrganizerModule().renderOrganizerMappingRow(...args);
}

function saveOrganizerMapping(...args) {
    return getAdminOrganizerModule().saveOrganizerMapping(...args);
}

function organizerTaskAction(...args) {
    return getAdminOrganizerModule().organizerTaskAction(...args);
}

function organizerActionText(...args) {
    return getAdminOrganizerModule().organizerActionText(...args);
}

function scanOrganizerTask(...args) {
    return getAdminOrganizerModule().scanOrganizerTask(...args);
}

function rollbackOrganizerRun(...args) {
    return getAdminOrganizerModule().rollbackOrganizerRun(...args);
}

async function testOrganizerEndpoint(kind) {
    const pathMap = {
        openlist: "/api/admin/openlist/test",
        tmdb: "/api/admin/tmdb/test",
        ai: "/api/admin/ai/test",
    };
    const data = await api(pathMap[kind], {
        method: "POST",
        body: JSON.stringify(kind === "ai" ? { ai: collectAdvancedConfig().ai } : {}),
        allowFailure: true,
    });
    toast(data.message || "测试完成", data.success ? "success" : "error");
}

function renderBtbtlaProxyTestResult(data = {}) {
    const box = $("btbtlaProxyTestResult");
    if (!box) return;
    const proxy = data.proxy && typeof data.proxy === "object" ? data.proxy : {};
    const target = data.target && typeof data.target === "object" ? data.target : {};
    const ip = data.ip && typeof data.ip === "object" ? data.ip : {};
    const warnings = Array.isArray(data.warnings) ? data.warnings.filter(Boolean) : [];
    const modeLabel = {
        explicit: "BTBTLA 独立代理",
        environment: "容器环境代理",
        direct: "直连",
    }[String(data.mode || proxy.source || "direct")] || "未知";
    const dnsLabel = {
        remote: "代理端解析（socks5h）",
        local: "应用本地解析（socks5）",
        proxy: "HTTP 代理处理",
    }[String(proxy.dns_mode || "")] || "-";
    const proxyState = data.proxy_applied
        ? "已用于 BTBTLA 请求"
        : proxy.requested
            ? "未确认生效"
            : "未启用代理";
    const tcpState = proxy.configured
        ? proxy.tcp_reachable
            ? `可连接${proxy.tcp_elapsed_ms !== undefined ? `（${proxy.tcp_elapsed_ms} ms）` : ""}`
            : `不可连接${proxy.tcp_error ? `：${proxy.tcp_error}` : ""}`
        : "未检测";
    const retryState = Number(target.attempts || 0) > 1 ? `，第 ${target.attempts} 次连接成功` : "";
    const targetState = target.transport_ok
        ? `HTTP ${target.status_code ?? "-"}${target.elapsed_ms !== undefined ? `（${target.elapsed_ms} ms）` : ""}${retryState}`
        : target.error || "未连通";
    const ipChanged = ip.changed === true
        ? "是，代理与直连出口不同"
        : ip.changed === false
            ? "否，代理与直连出口相同"
            : "无法比较";
    const rows = [
        ["检测模式", modeLabel],
        ["代理状态", proxyState],
        ["代理地址", proxy.display || "-"],
        ["代理端口", tcpState],
        ["BTBTLA", targetState],
        ["当前链路出口 IP", ip.tested_path || ip.tested_path_error || "未取得"],
    ];
    if (proxy.configured) {
        rows.push(["直连出口 IP", ip.direct || ip.direct_error || "未取得"]);
        rows.push(["出口 IP 是否变化", ipChanged]);
        rows.push(["DNS 模式", dnsLabel]);
        rows.push(["代理认证", proxy.authentication ? "已配置（账号密码已脱敏）" : "未配置"]);
    }
    if (data.elapsed_ms !== undefined) rows.push(["总耗时", `${data.elapsed_ms} ms`]);
    const detailWarnings = [...warnings];
    if (!data.success && data.message && !detailWarnings.includes(data.message)) detailWarnings.unshift(data.message);
    box.className = `notice-box ${data.success && data.proxy_applied && !detailWarnings.length ? "success" : "warning"}`;
    box.innerHTML = `
        <strong>${escapeHtml(data.message || "代理检测完成")}</strong>
        <ul>${rows.map(([label, value]) => `<li><span>${escapeHtml(label)}：</span><code>${escapeHtml(value)}</code></li>`).join("")}</ul>
        ${detailWarnings.length ? `<div><strong>提示</strong><ul>${detailWarnings.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul></div>` : ""}
    `;
    box.hidden = false;
}

async function testBtbtlaProxy() {
    const button = $("btbtlaProxyTestBtn");
    const box = $("btbtlaProxyTestResult");
    if (box) {
        box.className = "notice-box";
        box.textContent = "正在检测代理端口、BTBTLA 访问和出口 IP，请稍候。";
        box.hidden = false;
    }
    try {
        const btbtla = collectAdvancedConfig().btbtla;
        const data = await api("/api/admin/btbtla/proxy-test", {
            method: "POST",
            body: JSON.stringify({ btbtla }),
            allowFailure: true,
            button,
            buttonLabel: "检测中...",
            loadingMessage: "正在检测 BTBTLA 代理链路...",
        });
        renderBtbtlaProxyTestResult(data);
        toast(data.message || "代理检测完成", data.proxy_applied ? "success" : data.success ? "info" : "error");
        return data;
    } catch (error) {
        const message = error?.message || "代理检测请求失败";
        renderBtbtlaProxyTestResult({ success: false, message, warnings: [] });
        toast(message, "error");
        return null;
    }
}

let adminAdaptersModule = null;

function getAdminAdaptersModule() {
    if (adminAdaptersModule) return adminAdaptersModule;
    adminAdaptersModule = window.FnosAdminAdapters.create({
        state: adminState,
        getElement: $,
        api,
        toast,
        escapeHtml,
        statusPill,
        collectSearchProviders: window.FnosAdminSettings.collectSearchProviders,
        loadSettings,
        loadAdvancedConfig,
        loadSecurityStatus,
        saveAdvancedConfig,
    });
    return adminAdaptersModule;
}

function loadAdapters(...args) {
    return getAdminAdaptersModule().loadAdapters(...args);
}

function loadSearchProviders(...args) {
    return getAdminAdaptersModule().loadSearchProviders(...args);
}

function renderSearchProviders(...args) {
    return getAdminAdaptersModule().renderSearchProviders(...args);
}

function saveSearchProviders(...args) {
    return getAdminAdaptersModule().saveSearchProviders(...args);
}

function renderSixpanAuthStatus(...args) {
    return getAdminAdaptersModule().renderSixpanAuthStatus(...args);
}

function startSixpanDeviceAuth(...args) {
    return getAdminAdaptersModule().startSixpanDeviceAuth(...args);
}

function checkSixpanDeviceAuth(...args) {
    return getAdminAdaptersModule().checkSixpanDeviceAuth(...args);
}

function probeSixpan(...args) {
    return getAdminAdaptersModule().probeSixpan(...args);
}

async function loadSettings() {
    const [data, profileData] = await Promise.all([api("/api/admin/settings"), api("/api/admin/profile")]);
    adminState.settings = data.settings || {};
    adminState.profile = profileData.profile || {};
    renderSettings();
    renderProfileSettings();
}

function renderSettings() {
    const publicSettings = adminState.settings.public || {};
    const submission = adminState.settings.submission || {};
    if ($("settingAllowAnonymousSearch")) $("settingAllowAnonymousSearch").checked = publicSettings.allow_anonymous_search !== false;
    if ($("settingRequestQueryEnabled")) $("settingRequestQueryEnabled").checked = publicSettings.request_query_enabled !== false;
    if ($("settingHideFullLinks")) $("settingHideFullLinks").checked = publicSettings.hide_full_links !== false;
    const submitModeSelect = $("settingSubmitMode");
    if (submitModeSelect) {
        submitModeSelect.value = submission.mode || "auto";
        window.syncCustomSelect?.(submitModeSelect);
    }
}

function collectSettingsPayload() {
    return {
        public: {
            allow_anonymous_search: Boolean($("settingAllowAnonymousSearch")?.checked),
            request_query_enabled: Boolean($("settingRequestQueryEnabled")?.checked),
            hide_full_links: Boolean($("settingHideFullLinks")?.checked),
        },
        submission: {
            mode: $("settingSubmitMode")?.value || "auto",
        },
    };
}

async function saveSettings(options = {}) {
    const { silent = false } = options;
    const payload = collectSettingsPayload();
    const data = await api("/api/admin/settings", {
        method: "POST",
        body: JSON.stringify(payload),
    });
    adminState.settings = data.settings || {};
    renderSettings();
    if (!silent) toast(data.message || "系统设置已保存", "success");
    await loadOverview();
    return data;
}

async function loadAdvancedConfig() {
    const box = $("advancedConfigStatus");
    if (box) box.textContent = "正在加载高级配置。";
    const data = await api("/api/admin/advanced-config");
    adminState.advancedConfig = data.config || {};
    adminState.advancedStoredConfig = data.stored || {};
    adminState.advancedConfigMeta = data.meta || {};
    renderAdvancedConfig();
    try {
        await loadRcloneWebdavConfig();
    } catch (error) {
        renderRcloneWebdavError(error);
    }
}

function currentRcloneRemoteName() {
    return String($("advRcloneRemoteName")?.value || "MP").trim() || "MP";
}

async function loadRcloneWebdavConfig() {
    const box = $("rcloneWebdavStatus");
    if (box) {
        box.className = "notice-box compact";
        box.textContent = "正在读取 WebDAV 配置。";
    }
    const remoteName = currentRcloneRemoteName();
    const data = await api(`/api/admin/rclone/webdav-config?remote_name=${encodeURIComponent(remoteName)}`, {
        silentLoading: true,
    });
    renderRcloneWebdavConfig(data);
    return data;
}

function renderRcloneWebdavConfig(data = {}) {
    adminState.rcloneWebdavConfig = data;
    if ($("rcloneWebdavUrl")) $("rcloneWebdavUrl").value = String(data.url || "");
    if ($("rcloneWebdavUsername")) $("rcloneWebdavUsername").value = String(data.user || "");
    if ($("rcloneWebdavPassword")) {
        $("rcloneWebdavPassword").value = "";
        $("rcloneWebdavPassword").placeholder = data.password_set ? "已保存，留空则保留" : "首次配置时填写";
    }
    const box = $("rcloneWebdavStatus");
    if (!box) return;
    box.className = "notice-box compact";
    if (data.connection_status === "success") box.classList.add("success");
    if (data.connection_status === "unconfigured") box.classList.add("warning");
    if (data.connection_status === "unsupported") box.classList.add("status-error");
    box.textContent = String(data.message || (data.configured ? "WebDAV 已配置" : "WebDAV 尚未配置"));
}

function renderRcloneWebdavError(error) {
    const box = $("rcloneWebdavStatus");
    if (!box) return;
    box.className = "notice-box compact status-error";
    box.textContent = error?.message || "WebDAV 配置操作失败";
}

async function saveRcloneWebdavConfig() {
    const advancedResult = await saveAdvancedConfig({ silent: true });
    if (advancedResult?.success === false) return advancedResult;
    const payload = {
        remote_name: currentRcloneRemoteName(),
        url: String($("rcloneWebdavUrl")?.value || "").trim(),
        username: String($("rcloneWebdavUsername")?.value || "").trim(),
        password: String($("rcloneWebdavPassword")?.value || ""),
    };
    try {
        const data = await api("/api/admin/rclone/webdav-config", {
            method: "POST",
            body: JSON.stringify(payload),
            loadingMessage: "正在保存并检测 WebDAV...",
        });
        renderRcloneWebdavConfig(data);
        toast(data.message || "WebDAV 配置已保存", "success");
        return data;
    } catch (error) {
        renderRcloneWebdavError(error);
        throw error;
    }
}

async function testRcloneWebdavConfig() {
    try {
        const data = await api("/api/admin/rclone/webdav-config/test", {
            method: "POST",
            body: JSON.stringify({ remote_name: currentRcloneRemoteName() }),
            loadingMessage: "正在检测 WebDAV 连接...",
        });
        renderRcloneWebdavConfig(data);
        toast(data.message || "WebDAV 连接成功", "success");
        return data;
    } catch (error) {
        renderRcloneWebdavError(error);
        throw error;
    }
}

function renderAdvancedConfig() {
    const config = adminState.advancedConfig || {};
    const meta = adminState.advancedConfigMeta || {};
    const setValue = window.FnosAdminSettings.setValue;
    const setNumber = (id, value) => setValue(id, value ?? "");
    const setChecked = window.FnosAdminSettings.setChecked;
    const setSecret = window.FnosAdminSettings.setSecret;

    const pansou = config.pansou || {};
    setValue("advPansouBaseUrl", pansou.base_url);
    setValue("advPansouUsername", pansou.username);
    setSecret("advPansouPassword", pansou.password);
    setSecret("advPansouDefaultToken", pansou.default_token);
    renderAdvancedCloudTypes(pansou.cloud_types || []);
    setValue("advPansouRes", pansou.res || "merge");
    setValue("advPansouSrc", pansou.src || "all");
    setNumber("advPansouConc", pansou.conc ?? 10);
    setNumber("advPansouTimeout", pansou.timeout ?? 20);
    setChecked("advPansouRefresh", pansou.refresh, false);
    setChecked("advPansouAsyncPollEnabled", pansou.async_poll_enabled, true);
    setNumber("advPansouPollMaxRounds", pansou.async_poll_max_rounds ?? 2);
    setNumber("advPansouPollInterval", pansou.async_poll_interval_seconds ?? 0.8);
    setNumber("advPansouPollStableRounds", pansou.async_poll_stable_rounds ?? 1);
    setValue("advPansouChannels", listToText(pansou.channels || []));
    setValue("advPansouPlugins", listToText(pansou.plugins || []));
    setValue("advPansouFilterInclude", listToText(pansou.filter_include || []));
    setValue("advPansouFilterExclude", listToText(pansou.filter_exclude || []));

    const btbtla = config.btbtla || {};
    setValue("advBtbtlaBaseUrl", btbtla.base_url || "https://www.btbtla.com");
    setNumber("advBtbtlaTimeout", btbtla.timeout ?? 15);
    setNumber("advBtbtlaMaxResults", btbtla.max_results ?? 20);
    setNumber("advBtbtlaMaxDetailResources", btbtla.max_detail_resources ?? 80);
    setChecked("advBtbtlaVerifyTls", btbtla.verify_tls, false);
    setChecked("advBtbtlaProxyEnabled", btbtla.proxy_enabled, false);
    setSecret("advBtbtlaProxyUrl", btbtla.proxy_url);
    if ($("advBtbtlaProxyUrl")) $("advBtbtlaProxyUrl").disabled = !btbtla.proxy_enabled;

    const routes = config.routes || {};
    renderAdvancedRoutes(routes);

    const updateScheduler = config.update_scheduler || {};
    setChecked("advUpdateSchedulerEnabled", updateScheduler.enabled, true);
    setNumber("advUpdateSchedulerTmdbProbeLeadMinutes", updateScheduler.tmdb_probe_lead_minutes ?? 0);

    const openlist = config.openlist || {};
    setValue("advOpenlistBaseUrl", openlist.base_url);
    setSecret("advOpenlistToken", openlist.token);
    setNumber("advOpenlistTimeout", openlist.timeout ?? 30);
    setChecked("advOpenlistListRefreshDefault", openlist.list_refresh_default, false);
    setChecked("advOpenlistVerifyTls", openlist.verify_tls, true);
    setChecked("advOpenlistUseEnvProxy", openlist.use_env_proxy, false);

    const tmdb = config.tmdb || {};
    setSecret("advTmdbToken", tmdb.token);
    setValue("advTmdbLanguage", tmdb.language || "zh-CN");
    setChecked("advTmdbProxyEnabled", tmdb.proxy_enabled, false);
    setSecret("advTmdbProxyUrl", tmdb.proxy_url);
    if ($("advTmdbProxyUrl")) $("advTmdbProxyUrl").disabled = !tmdb.proxy_enabled;

    const ai = config.ai || {};
    setChecked("advAiEnabled", ai.enabled, false);
    setValue("advAiApiStyle", ai.api_style || "auto");
    setValue("advAiBaseUrl", ai.base_url || "https://api.openai.com/v1");
    setSecret("advAiApiKey", ai.api_key);
    setValue("advAiModel", ai.model || "gpt-4.1-mini");
    setNumber("advAiTimeout", ai.timeout ?? 45);

    const organizer = config.organizer || {};
    setChecked("advOrganizerEnabled", organizer.enabled, false);
    setNumber("advOrganizerStableWindowSeconds", organizer.stable_window_seconds ?? 120);
    setNumber("advOrganizerAutoApplyConfidence", organizer.auto_apply_confidence ?? 88);
    setNumber("advOrganizerMaxScanDepth", organizer.max_scan_depth ?? 8);
    setNumber("advOrganizerMaxFilesPerTask", organizer.max_files_per_task ?? 500);
    setChecked("advOrganizerRefreshFnosAfterApply", organizer.refresh_fnos_after_apply, false);
    setNumber("advOrganizerRefreshDelaySeconds", organizer.refresh_delay_seconds ?? 60);
    setValue("advOrganizerStrmRefreshPrefixMovie", organizer.strm_refresh_prefix_movie || organizer.strm_refresh_prefix || "");
    setValue("advOrganizerStrmRefreshPrefixTv", organizer.strm_refresh_prefix_tv || organizer.strm_refresh_prefix || "");
    setValue("advOrganizerStrmRefreshPrefixAnime", organizer.strm_refresh_prefix_anime || organizer.strm_refresh_prefix_tv || organizer.strm_refresh_prefix || "");
    setValue("advOrganizerStrmRefreshPrefixVariety", organizer.strm_refresh_prefix_variety || organizer.strm_refresh_prefix_tv || organizer.strm_refresh_prefix || "");
    setValue("advOrganizerLocalStrmRoot", organizer.local_strm_root || "");

    const quark = config.quark || {};
    setValue("advQuarkAutoSaveUrl", quark.auto_save_url);
    setSecret("advQuarkToken", quark.token);
    setChecked("advQuarkCheckBeforeSave", quark.check_before_save, true);
    setChecked("advQuarkRunImmediately", quark.run_immediately, true);

    const sixpan = config.sixpan || {};
    setValue("advSixpanHost", sixpan.host || sixpan.api_url || "openapi.2dland.cn");
    setValue("advSixpanClientId", sixpan.client_id);
    setSecret("advSixpanClientSecret", sixpan.client_secret);
    setNumber("advSixpanTimeout", sixpan.timeout ?? 20);
    setNumber("advSixpanPollInterval", sixpan.poll_interval_seconds ?? 60);
    setNumber("advSixpanTaskPollLimit", sixpan.task_poll_limit ?? 200);
    setChecked("advSixpanVerifyTls", sixpan.verify_tls, true);
    setChecked("advSixpanParseBeforeAdd", sixpan.parse_before_add, true);
    setChecked("advSixpanParseRequired", sixpan.parse_required, false);
    setChecked("advSixpanPollEnabled", sixpan.poll_enabled, true);
    renderSixpanAuthStatus(sixpan);

    const cmcc = config.cmcc_upload || {};
    setValue("advCmccMode", cmcc.mode || "rapid_first");
    setValue("advCmccRenameMode", cmcc.rename_mode || "auto_rename");
    let cmccHost = String(cmcc.host || "https://personal-kd-njs.yun.139.com/hcy");
    if (cmccHost.includes("miniapp.yun.139.com")) {
        cmccHost = "https://personal-kd-njs.yun.139.com/hcy";
    } else if (cmccHost.replace(/\/+$/, "") === "https://personal-kd-njs.yun.139.com") {
        cmccHost = "https://personal-kd-njs.yun.139.com/hcy";
    }
    setValue("advCmccHost", cmccHost);
    setSecret("advCmccAccessToken", cmcc.access_token);
    setValue("advCmccPhone", cmcc.phone);

    const fnos = config.fnos || {};
    setValue("advFnosServerUrl", fnos.server_url);
    setValue("advFnosUsername", fnos.username);
    setSecret("advFnosPassword", fnos.password);
    setSecret("advFnosApiKey", fnos.api_key);
    setSecret("advFnosSecret", fnos.secret);
    setSecret("advFnosToken", fnos.token);

    const rclone = config.rclone || {};
    setChecked("advRcloneEnabled", rclone.enabled, true);
    setValue("advRcloneRemoteName", rclone.remote_name || "MP");
    renderAdvancedCategories(config.categories || {});
    const sixpanMountName = sixpan.fnos_mount_name
        || sixpan.openlist_mount_name
        || sixpan.mount_name
        || sixpan.mount_path
        || inferCategoryRoot(config.categories || {}, "sixpan_fnos_target_path", "清云");
    syncCategoryTemplateFromCategories(config.categories || {}, sixpanMountName);
    renderCategoryTemplatePreview();
    syncAdvancedDependencies();
    syncAdvancedSecretManager();
    adminState.advancedFormBaseline = collectAdvancedConfig();
    document.querySelectorAll("#tab-adapters select").forEach((select) => window.syncCustomSelect?.(select));
    if ($("advancedConfigStatus")) {
        const sourceText = meta.database_configured ? "配置已加载。" : "当前使用默认配置，保存后生效。";
        $("advancedConfigStatus").classList.remove("status-error");
        $("advancedConfigStatus").textContent = sourceText;
    }
    scheduleAdvancedConfigMasonry();
}

function syncAdvancedDependencies() {
    document.querySelectorAll("#tab-adapters [data-config-dependency]").forEach((container) => {
        const controller = $(container.dataset.configDependency);
        const enabled = Boolean(controller?.checked);
        container.hidden = !enabled;
        container.querySelectorAll("input, select, textarea, button").forEach((control) => {
            control.disabled = !enabled;
        });
    });
    scheduleAdvancedConfigMasonry();
}

function storedAdvancedValue(path) {
    return String(path || "").split(".").reduce((value, key) => {
        return value && typeof value === "object" ? value[key] : undefined;
    }, adminState.advancedStoredConfig || {});
}

function configuredAdvancedSecrets() {
    return Object.values(ADVANCED_SECRET_FIELDS)
        .filter((path) => Boolean(storedAdvancedValue(path)))
        .map((path) => ({ path, label: ADVANCED_SECRET_LABELS[path] || path }));
}

function syncAdvancedSecretManager() {
    const button = $("manageAdvancedSecretsBtn");
    if (!button) return;
    const count = configuredAdvancedSecrets().length;
    button.textContent = count ? `凭据管理 (${count})` : "凭据管理";
}

function openAdvancedSecretManager() {
    const items = configuredAdvancedSecrets();
    openDrawer("凭据管理", items.length ? `
        <div class="credential-manager-list">
            ${items.map((item) => `
                <div class="list-item compact credential-manager-item">
                    <div>
                        <strong>${escapeHtml(item.label)}</strong>
                        <p>已保存在后台配置</p>
                    </div>
                    <button class="danger mini" type="button" data-managed-secret-path="${escapeHtml(item.path)}">清除</button>
                </div>
            `).join("")}
        </div>
    ` : `<div class="empty">暂无已保存凭据</div>`);
    $("adminDrawerBody")?.querySelectorAll("[data-managed-secret-path]").forEach((button) => {
        button.addEventListener("click", () => {
            clearAdvancedSecret(button.dataset.managedSecretPath, button, { refreshManager: true })
                .catch((error) => toast(error.message, "error"));
        });
    });
}

async function clearAdvancedSecret(path, button, options = {}) {
    const { refreshManager = false } = options;
    const confirmed = await confirmDialog({
        title: "清除凭据",
        message: `清除“${ADVANCED_SECRET_LABELS[path] || path}”？环境变量或配置文件中的值不会被修改。`,
        confirmText: "清除",
        tone: "danger",
    });
    if (!confirmed) return;
    if (button) button.disabled = true;
    try {
        const data = await api("/api/admin/advanced-config", {
            method: "POST",
            body: JSON.stringify({ config: {}, clear_secrets: [path] }),
        });
        adminState.advancedConfig = data.config || {};
        adminState.advancedStoredConfig = data.stored || {};
        adminState.advancedConfigMeta = data.meta || {};
        renderAdvancedConfig();
        if (refreshManager) openAdvancedSecretManager();
        toast("已清除页面保存的凭据", "success");
    } finally {
        if (button?.isConnected) button.disabled = false;
    }
}

function renderAdvancedCloudTypes(values) {
    const selected = new Set((values || []).map((item) => String(item).toLowerCase()));
    const box = $("advPansouCloudTypes");
    if (!box) return;
    box.innerHTML = ADVANCED_CLOUD_TYPES.map(([value, label]) => `
        <label class="mini-check"><input type="checkbox" data-adv-cloud-type="${escapeHtml(value)}" ${selected.has(value) ? "checked" : ""}>${escapeHtml(label)}</label>
    `).join("");
}

function renderAdvancedRoutes(routes) {
    const box = $("advancedRouteChecks");
    if (!box) return;
    box.innerHTML = Object.entries(ADVANCED_ROUTE_LABELS).map(([key, label]) => {
        const route = routes[key] || {};
        return `<label class="mini-check"><input type="checkbox" data-adv-route="${escapeHtml(key)}" ${route.enabled ? "checked" : ""}>${escapeHtml(label)}</label>`;
    }).join("");
}

function renderAdvancedCategories(categories) {
    const body = $("advancedCategoryRows");
    if (!body) return;
    const rows = ADVANCED_CATEGORY_ORDER.map(([key, fallbackLabel]) => {
        const item = categories[key] || {};
        const label = item.label || fallbackLabel;
        const dirName = inferCategoryDir(item, label);
        return `
            <tr data-adv-category="${escapeHtml(key)}">
                <td><span class="category-rule-pill">${escapeHtml(fallbackLabel)}</span></td>
                <td><input data-cat-field="label" type="text" value="${escapeHtml(label)}" aria-label="${escapeHtml(fallbackLabel)} 分类显示名"></td>
                <td><input data-cat-field="dir" type="text" value="${escapeHtml(dirName)}" placeholder="${escapeHtml(fallbackLabel)}" aria-label="${escapeHtml(fallbackLabel)} 目录名"></td>
                <td class="category-rule-preview" data-category-row-preview="${escapeHtml(key)}">-</td>
            </tr>
        `;
    }).join("");
    body.innerHTML = `
        <div class="category-rules-table-wrap">
            <table class="category-rules-table">
                <thead>
                    <tr>
                        <th>内置分类</th>
                        <th>前台显示名</th>
                        <th>目录名</th>
                        <th>自动整理位置</th>
                    </tr>
                </thead>
                <tbody>${rows}</tbody>
            </table>
        </div>
    `;
    body.querySelectorAll('[data-cat-field="label"], [data-cat-field="dir"]').forEach((input) => {
        input.addEventListener("input", renderCategoryTemplatePreview);
    });
    renderCategoryTemplatePreview();
}

function inferCategoryDir(item = {}, fallbackLabel = "") {
    return window.FnosAdminSettings.inferCategoryDir(item, fallbackLabel);
}

function categoryLabelFor(key, fallback = "") {
    const item = ADVANCED_CATEGORY_ORDER.find(([categoryKey]) => categoryKey === key);
    return item?.[1] || fallback || key;
}

function normalizeTemplateRoot(value, { leadingSlash = false } = {}) {
    return window.FnosAdminSettings.normalizeTemplateRoot(value, { leadingSlash });
}

function joinTemplatePath(root, label, { leadingSlash = false } = {}) {
    return window.FnosAdminSettings.joinTemplatePath(root, label, { leadingSlash });
}

function inferCategoryRoot(categories, field, fallback = "", { leadingSlash = false } = {}) {
    return window.FnosAdminSettings.inferCategoryRoot(categories, field, fallback, {
        leadingSlash,
        order: ADVANCED_CATEGORY_ORDER,
    });
}

function syncCategoryTemplateFromCategories(categories, sixpanMountFallback = "清云") {
    const setValue = (id, value) => {
        const el = $(id);
        if (el) el.value = value;
    };
    setValue("categoryTemplateQuarkRoot", inferCategoryRoot(categories, "quark_save_path", "/离线下载", { leadingSlash: true }));
    setValue("categoryTemplateCloud139Root", inferCategoryRoot(categories, "cloud139_target_path", "博客"));
    setValue("categoryTemplateCloud139FnosRoot", inferCategoryRoot(categories, "cloud139_fnos_target_path", "移动云2"));
    setValue("categoryTemplateSixpanRoot", inferCategoryRoot(categories, "sixpan_save_path", "/", { leadingSlash: true }));
    setValue("categoryTemplateSixpanFnosRoot", normalizeTemplateRoot(sixpanMountFallback || "清云"));
}

function categoryTemplateValues() {
    return {
        quarkRoot: $("categoryTemplateQuarkRoot")?.value || "/离线下载",
        cloud139Root: $("categoryTemplateCloud139Root")?.value || "",
        cloud139FnosRoot: $("categoryTemplateCloud139FnosRoot")?.value || "",
        sixpanRoot: $("categoryTemplateSixpanRoot")?.value || "/",
        sixpanFnosRoot: $("categoryTemplateSixpanFnosRoot")?.value || "",
    };
}

function buildCategoryTemplateRows() {
    const template = categoryTemplateValues();
    return ADVANCED_CATEGORY_ORDER.map(([key, fallbackLabel]) => {
        const row = document.querySelector(`[data-adv-category="${key}"]`);
        const label = row?.querySelector('[data-cat-field="label"]')?.value?.trim() || fallbackLabel;
        const dir = row?.querySelector('[data-cat-field="dir"]')?.value?.trim() || label;
        const cloud139FnosTargetPath = template.cloud139FnosRoot ? joinTemplatePath(template.cloud139FnosRoot, dir) : "";
        return {
            key,
            label,
            dir,
            quark_save_path: joinTemplatePath(template.quarkRoot, dir, { leadingSlash: true }),
            cloud139_target_path: joinTemplatePath(template.cloud139Root, dir),
            cloud139_fnos_target_path: cloud139FnosTargetPath,
            openlist_root_path: cloud139FnosTargetPath,
            sixpan_save_path: joinTemplatePath(template.sixpanRoot, dir, { leadingSlash: true }),
            sixpan_fnos_target_path: template.sixpanFnosRoot ? joinTemplatePath(template.sixpanFnosRoot, dir) : "",
        };
    });
}

function renderCategoryTemplatePreview() {
    const box = $("categoryTemplatePreview");
    const rows = buildCategoryTemplateRows();
    rows.forEach((row) => {
        const preview = document.querySelector(`[data-category-row-preview="${row.key}"]`);
        if (preview) {
            preview.innerHTML = `
                <span>官方云端：<code>${escapeHtml(row.cloud139_target_path || "-")}</code></span>
                <span>OpenList：<code>${escapeHtml(row.cloud139_fnos_target_path || "-")}</code></span>
                <span>6盘：<code>${escapeHtml(row.sixpan_fnos_target_path || "-")}</code></span>
            `;
        }
    });
    if (!box) return;
    box.innerHTML = `
        <table class="category-template-table">
            <thead><tr><th>分类</th><th>夸克离线</th><th>移动云官方目录（直转/API 上传）</th><th>移动云 OpenList 整理位置</th><th>6盘离线</th><th>6盘整理位置</th></tr></thead>
            <tbody>
                ${rows.map((row) => `
                    <tr>
                        <td>${escapeHtml(row.label)}</td>
                        <td><code>${escapeHtml(row.quark_save_path)}</code></td>
                        <td><code>${escapeHtml(row.cloud139_target_path)}</code></td>
                        <td><code>${escapeHtml(row.cloud139_fnos_target_path || "-")}</code></td>
                        <td><code>${escapeHtml(row.sixpan_save_path)}</code></td>
                        <td><code>${escapeHtml(row.sixpan_fnos_target_path || "-")}</code></td>
                    </tr>
                `).join("")}
            </tbody>
        </table>
        <p>移动云 API 上传和官方转存写入官方目录，Organizer 会自动映射到对应的 OpenList 整理位置。</p>
    `;
}

function applyCategoryTemplate() {
    renderCategoryTemplatePreview();
    toast("路径预览已更新，确认无误后保存配置。", "success");
}

async function saveAdvancedConfig(options = {}) {
    const { silent = false } = options;
    const collected = collectAdvancedConfig();
    const config = window.FnosAdminSettings.changedConfigPatch(collected, adminState.advancedFormBaseline || {});
    const existingBtProxy = String(adminState.advancedConfig?.btbtla?.proxy_url || "").trim();
    if (collected.btbtla?.proxy_enabled && !String(collected.btbtla.proxy_url || "").trim() && !existingBtProxy) {
        toast("请先填写 BT 独立代理地址", "error");
        return { success: false };
    }
    if (!Object.keys(config).length) {
        if (!silent) toast("配置没有变化", "info");
        return { skipped: true };
    }
    const payload = { config };
    const data = await api("/api/admin/advanced-config", {
        method: "POST",
        body: JSON.stringify(payload),
    });
    adminState.advancedConfig = data.config || {};
    adminState.advancedStoredConfig = data.stored || {};
    adminState.advancedConfigMeta = data.meta || {};
    renderAdvancedConfig();
    if (!silent) toast(data.message || "配置已保存", "success");
    await Promise.all([loadMediaLibraries(), loadOrganizer(), loadSecurityStatus()]);
    return data;
}

async function saveAllSettingsTransaction() {
    const collected = collectAdvancedConfig();
    const advancedPatch = window.FnosAdminSettings.changedConfigPatch(
        collected,
        adminState.advancedFormBaseline || {},
    );
    const existingBtProxy = String(adminState.advancedConfig?.btbtla?.proxy_url || "").trim();
    if (collected.btbtla?.proxy_enabled && !String(collected.btbtla.proxy_url || "").trim() && !existingBtProxy) {
        toast("请先填写 BT 独立代理地址", "error");
        return { success: false };
    }

    const data = await api("/api/admin/settings/all", {
        method: "POST",
        body: JSON.stringify({
            settings: collectSettingsPayload(),
            search: {
                providers: window.FnosAdminSettings.collectSearchProviders(adminState.searchProviders),
            },
            advanced: { config: advancedPatch },
        }),
    });
    adminState.settings = data.settings || {};
    adminState.searchProviders = data.search_providers || [];
    adminState.advancedConfig = data.config || {};
    adminState.advancedStoredConfig = data.stored || {};
    adminState.advancedConfigMeta = data.meta || {};
    renderSettings();
    getAdminAdaptersModule().renderSearchProviders();
    renderAdvancedConfig();
    await Promise.allSettled([loadOverview(), loadMediaLibraries(), loadOrganizer(), loadSecurityStatus()]);
    return data;
}

async function exportAdvancedConfig() {
    const confirmed = await confirmDialog({
        message: "导出的文件包含各服务 Token、密码等敏感信息。请只保存到可信位置，确定继续？",
        tone: "danger",
    });
    if (!confirmed) return;
    const button = $("exportAdvancedConfigBtn");
    if (button) button.disabled = true;
    try {
        const data = await api("/api/admin/advanced-config/export", {
            method: "POST",
            body: JSON.stringify({ confirm: true }),
        });
        const documentPayload = data.document || {};
        const blob = new Blob([JSON.stringify(documentPayload, null, 2)], { type: "application/json" });
        const url = URL.createObjectURL(blob);
        const link = document.createElement("a");
        const stamp = new Date().toISOString().slice(0, 10).replace(/-/g, "");
        link.href = url;
        link.download = `fnos-advanced-config-v${documentPayload.version || 1}-${stamp}.json`;
        document.body.appendChild(link);
        link.click();
        link.remove();
        URL.revokeObjectURL(url);
        toast("敏感配置已导出，请妥善保管", "success");
    } finally {
        if (button) button.disabled = false;
    }
}

async function importAdvancedConfig(file) {
    let config;
    try {
        config = JSON.parse(await file.text());
    } catch (error) {
        toast("配置文件解析失败，请确认是 JSON 文件", "error");
        return;
    }
    if (!config || typeof config !== "object" || Array.isArray(config)) {
        toast("配置文件格式不正确", "error");
        return;
    }
    const confirmed = await confirmDialog({
        message: "导入将使用文件中的 stored 配置完整覆盖数据库高级配置；文件未包含的数据库字段和旧密钥会被清除（环境变量/YAML 不受影响）。确定继续？",
        tone: "danger",
    });
    if (!confirmed) return;
    const saveBtn = $("importAdvancedConfigBtn");
    if (saveBtn) saveBtn.disabled = true;
    try {
        const data = await api("/api/admin/advanced-config", {
            method: "POST",
            body: JSON.stringify({ config, mode: "replace", source: "import", scope: "stored" }),
        });
        adminState.advancedConfig = data.config || {};
        adminState.advancedStoredConfig = data.stored || {};
        adminState.advancedConfigMeta = data.meta || {};
        renderAdvancedConfig();
        toast(data.message || "配置已导入", "success");
        if (Array.isArray(data.warnings) && data.warnings.length) {
            toast(data.warnings.join("；"), "warning");
        }
        await Promise.all([loadSearchProviders(), loadMediaLibraries(), loadOrganizer(), loadSecurityStatus()]);
    } finally {
        if (saveBtn) saveBtn.disabled = false;
    }
}

function collectAdvancedConfig() {
    const existingConfig = adminState.advancedConfig || {};
    const reader = window.FnosAdminSettings.createFormReader();
    const value = reader.value.bind(reader);
    const numberValue = reader.number.bind(reader);
    const checked = reader.checked.bind(reader);
    const configBool = window.FnosAdminSettings.toBoolean;
    const cloudTypes = Array.from(document.querySelectorAll("[data-adv-cloud-type]:checked")).map((input) => input.dataset.advCloudType);
    const routes = {};
    document.querySelectorAll("[data-adv-route]").forEach((input) => {
        routes[input.dataset.advRoute] = { enabled: Boolean(input.checked) };
    });
    // 只生成页面可编辑字段的 patch，运行时发现的 folder id / 真实目录由后端保留。
    const categories = window.FnosAdminSettings.buildCategoryPatch(buildCategoryTemplateRows());
    const template = categoryTemplateValues();
    const existingCloud139 = existingConfig.cloud139 || {};
    const existingCloud139Delay = Number(existingCloud139.refresh_delay_seconds);
    const cmccBackend = "cmcc_api";
    return {
        pansou: {
            base_url: value("advPansouBaseUrl"),
            username: value("advPansouUsername"),
            password: value("advPansouPassword"),
            default_token: value("advPansouDefaultToken"),
            cloud_types: cloudTypes,
            res: value("advPansouRes") || "merge",
            src: value("advPansouSrc") || "all",
            conc: numberValue("advPansouConc", 10),
            refresh: checked("advPansouRefresh"),
            channels: value("advPansouChannels"),
            plugins: value("advPansouPlugins"),
            filter_include: value("advPansouFilterInclude"),
            filter_exclude: value("advPansouFilterExclude"),
            async_poll_enabled: checked("advPansouAsyncPollEnabled"),
            async_poll_interval_seconds: numberValue("advPansouPollInterval", 0.8),
            async_poll_max_rounds: numberValue("advPansouPollMaxRounds", 2),
            async_poll_stable_rounds: numberValue("advPansouPollStableRounds", 1),
            timeout: numberValue("advPansouTimeout", 20),
        },
        btbtla: {
            base_url: value("advBtbtlaBaseUrl") || "https://www.btbtla.com",
            timeout: numberValue("advBtbtlaTimeout", 15),
            max_results: numberValue("advBtbtlaMaxResults", 20),
            max_detail_resources: numberValue("advBtbtlaMaxDetailResources", 80),
            verify_tls: checked("advBtbtlaVerifyTls"),
            proxy_enabled: checked("advBtbtlaProxyEnabled"),
            proxy_url: value("advBtbtlaProxyUrl"),
        },
        routes,
        update_scheduler: {
            enabled: checked("advUpdateSchedulerEnabled"),
            tmdb_probe_lead_minutes: numberValue("advUpdateSchedulerTmdbProbeLeadMinutes", 0),
        },
        openlist: {
            base_url: value("advOpenlistBaseUrl"),
            token: value("advOpenlistToken"),
            timeout: numberValue("advOpenlistTimeout", 30),
            list_refresh_default: checked("advOpenlistListRefreshDefault"),
            verify_tls: checked("advOpenlistVerifyTls"),
            use_env_proxy: checked("advOpenlistUseEnvProxy"),
        },
        tmdb: {
            token: value("advTmdbToken"),
            language: value("advTmdbLanguage") || "zh-CN",
            proxy_enabled: checked("advTmdbProxyEnabled"),
            proxy_url: value("advTmdbProxyUrl"),
        },
        ai: {
            enabled: checked("advAiEnabled"),
            api_style: value("advAiApiStyle") || "auto",
            base_url: value("advAiBaseUrl") || "https://api.openai.com/v1",
            api_key: value("advAiApiKey"),
            model: value("advAiModel") || "gpt-4.1-mini",
            timeout: numberValue("advAiTimeout", 45),
        },
        organizer: {
            enabled: checked("advOrganizerEnabled"),
            stable_window_seconds: numberValue("advOrganizerStableWindowSeconds", 120),
            auto_apply_confidence: numberValue("advOrganizerAutoApplyConfidence", 88),
            max_scan_depth: numberValue("advOrganizerMaxScanDepth", 8),
            max_files_per_task: numberValue("advOrganizerMaxFilesPerTask", 500),
            refresh_fnos_after_apply: checked("advOrganizerRefreshFnosAfterApply"),
            refresh_delay_seconds: numberValue("advOrganizerRefreshDelaySeconds", 60),
            strm_refresh_prefix_movie: value("advOrganizerStrmRefreshPrefixMovie"),
            strm_refresh_prefix_tv: value("advOrganizerStrmRefreshPrefixTv"),
            strm_refresh_prefix_anime: value("advOrganizerStrmRefreshPrefixAnime"),
            strm_refresh_prefix_variety: value("advOrganizerStrmRefreshPrefixVariety"),
            local_strm_root: value("advOrganizerLocalStrmRoot"),
        },
        quark: {
            auto_save_url: value("advQuarkAutoSaveUrl"),
            token: value("advQuarkToken"),
            check_before_save: checked("advQuarkCheckBeforeSave"),
            run_immediately: checked("advQuarkRunImmediately"),
        },
        cloud139: {
            refresh_delay_seconds: Number.isFinite(existingCloud139Delay) ? existingCloud139Delay : 90,
            check_before_save: configBool(existingCloud139.check_before_save, true),
            refresh_after_submit: configBool(existingCloud139.refresh_after_submit, true),
            mark_done_after_submit: configBool(existingCloud139.mark_done_after_submit, true),
        },
        sixpan: {
            host: value("advSixpanHost") || "openapi.2dland.cn",
            fnos_mount_name: normalizeTemplateRoot(template.sixpanFnosRoot),
            client_id: value("advSixpanClientId"),
            client_secret: value("advSixpanClientSecret"),
            timeout: numberValue("advSixpanTimeout", 20),
            verify_tls: checked("advSixpanVerifyTls"),
            parse_before_add: checked("advSixpanParseBeforeAdd"),
            parse_required: checked("advSixpanParseBeforeAdd") && checked("advSixpanParseRequired"),
            poll_enabled: checked("advSixpanPollEnabled"),
            poll_interval_seconds: numberValue("advSixpanPollInterval", 60),
            task_poll_limit: numberValue("advSixpanTaskPollLimit", 200),
        },
        cmcc_upload: {
            enabled: true,
            backend: cmccBackend,
            mode: value("advCmccMode") || "rapid_first",
            rename_mode: value("advCmccRenameMode") || "auto_rename",
            host: value("advCmccHost") || "https://personal-kd-njs.yun.139.com/hcy",
            auth_mode: "web_basic",
            access_token: value("advCmccAccessToken"),
            phone: value("advCmccPhone"),
        },
        fnos: {
            server_url: value("advFnosServerUrl"),
            username: value("advFnosUsername"),
            password: value("advFnosPassword"),
            api_key: value("advFnosApiKey"),
            secret: value("advFnosSecret"),
            token: value("advFnosToken"),
        },
        rclone: {
            enabled: checked("advRcloneEnabled"),
            remote_name: value("advRcloneRemoteName"),
            upload_backend: cmccBackend,
        },
        categories,
    };
}

function listToText(value) {
    return window.FnosAdminSettings.listToText(value);
}

let adminUpdatesModule = null;

function getAdminUpdatesModule() {
    if (adminUpdatesModule) return adminUpdatesModule;
    adminUpdatesModule = window.FnosAdminUpdates.create({
        state: adminState,
        getElement: $,
        api,
        toast,
        escapeHtml,
        statusPill,
        statusText,
        sourceText,
        formatDate,
        normalizePagination,
        renderPager,
        openDrawer,
        closeDrawer,
        textToList,
        parseNumberList,
        categoryOrder: ADVANCED_CATEGORY_ORDER,
    });
    return adminUpdatesModule;
}

function loadUpdates(...args) {
    return getAdminUpdatesModule().loadUpdates(...args);
}

let adminTrendingModule = null;

function getAdminTrendingModule() {
    if (adminTrendingModule) return adminTrendingModule;
    adminTrendingModule = window.FnosAdminTrending.create({
        state: adminState,
        getElement: $,
        api,
        toast,
        escapeHtml,
        icon,
        formatDate,
        normalizePagination,
        renderPager,
        statusPill,
        openDrawer,
        confirmDialog,
        loadSearchProviders,
    });
    return adminTrendingModule;
}

function loadTrending(...args) {
    return getAdminTrendingModule().loadTrending(...args);
}

function runTrending(...args) {
    return getAdminTrendingModule().runTrending(...args);
}

function openTrendingScheduler(...args) {
    return getAdminTrendingModule().openTrendingScheduler(...args);
}

function saveTrendingSchedule(...args) {
    return getAdminTrendingModule().saveTrendingSchedule(...args);
}

function renderUpdates(...args) {
    return getAdminUpdatesModule().renderUpdates(...args);
}

function renderUpdateSubscriptionRow(...args) {
    return getAdminUpdatesModule().renderUpdateSubscriptionRow(...args);
}

function updateSubscriptionAction(...args) {
    return getAdminUpdatesModule().updateSubscriptionAction(...args);
}

function openUpdateDetail(...args) {
    return getAdminUpdatesModule().openUpdateDetail(...args);
}

function renderPathHealthChecks(...args) {
    return getAdminUpdatesModule().renderPathHealthChecks(...args);
}

function renderUpdateDetailNotes(...args) {
    return getAdminUpdatesModule().renderUpdateDetailNotes(...args);
}

function renderUpdateSourceSummary(...args) {
    return getAdminUpdatesModule().renderUpdateSourceSummary(...args);
}

function renderUpdateSourceHealth(...args) {
    return getAdminUpdatesModule().renderUpdateSourceHealth(...args);
}

function renderUpdateRunItem(...args) {
    return getAdminUpdatesModule().renderUpdateRunItem(...args);
}

function updateDetailAction(...args) {
    return getAdminUpdatesModule().updateDetailAction(...args);
}

function openUpdateRunLog(...args) {
    return getAdminUpdatesModule().openUpdateRunLog(...args);
}

function renderUpdateSourceItem(...args) {
    return getAdminUpdatesModule().renderUpdateSourceItem(...args);
}

function renderUpdateCandidateItem(...args) {
    return getAdminUpdatesModule().renderUpdateCandidateItem(...args);
}

function updateCandidateAction(...args) {
    return getAdminUpdatesModule().updateCandidateAction(...args);
}

function runDueUpdates(...args) {
    return getAdminUpdatesModule().runDueUpdates(...args);
}

function openUpdateEditor(...args) {
    return getAdminUpdatesModule().openUpdateEditor(...args);
}

function isUpdateEpisodicCategory(...args) {
    return getAdminUpdatesModule().isUpdateEpisodicCategory(...args);
}

function updateExpectedMediaType(...args) {
    return getAdminUpdatesModule().updateExpectedMediaType(...args);
}

function renderUpdateTmdbSelectedText(...args) {
    return getAdminUpdatesModule().renderUpdateTmdbSelectedText(...args);
}

function isUpdateTmdbRequiredMode(...args) {
    return getAdminUpdatesModule().isUpdateTmdbRequiredMode(...args);
}

function updateEditorState(...args) {
    return getAdminUpdatesModule().updateEditorState(...args);
}

function syncUpdateEditorVisibility(...args) {
    return getAdminUpdatesModule().syncUpdateEditorVisibility(...args);
}

function clearUpdateTmdbSelection(...args) {
    return getAdminUpdatesModule().clearUpdateTmdbSelection(...args);
}

function handleUpdateCategoryChange(...args) {
    return getAdminUpdatesModule().handleUpdateCategoryChange(...args);
}

function browseUpdateOpenlistDirs(...args) {
    return getAdminUpdatesModule().browseUpdateOpenlistDirs(...args);
}

function parentOpenlistPath(...args) {
    return getAdminUpdatesModule().parentOpenlistPath(...args);
}

function renderUpdateTimePicker(...args) {
    return getAdminUpdatesModule().renderUpdateTimePicker(...args);
}

function parseTimeParts(...args) {
    return getAdminUpdatesModule().parseTimeParts(...args);
}

function updateMinuteOptions(...args) {
    return getAdminUpdatesModule().updateMinuteOptions(...args);
}

function readUpdateTimeValue(...args) {
    return getAdminUpdatesModule().readUpdateTimeValue(...args);
}

function renderUpdateSourceEditor(...args) {
    return getAdminUpdatesModule().renderUpdateSourceEditor(...args);
}

function normalizeUpdateSources(...args) {
    return getAdminUpdatesModule().normalizeUpdateSources(...args);
}

function renderUpdateSourceCard(...args) {
    return getAdminUpdatesModule().renderUpdateSourceCard(...args);
}

function updateSourceType(...args) {
    return getAdminUpdatesModule().updateSourceType(...args);
}

function updateSourceHelp(...args) {
    return getAdminUpdatesModule().updateSourceHelp(...args);
}

function renderUpdateSourceFields(...args) {
    return getAdminUpdatesModule().renderUpdateSourceFields(...args);
}

function bindUpdateEditorInteractions(...args) {
    return getAdminUpdatesModule().bindUpdateEditorInteractions(...args);
}

function bindUpdateSourceCard(...args) {
    return getAdminUpdatesModule().bindUpdateSourceCard(...args);
}

function setUpdateSourceCardType(...args) {
    return getAdminUpdatesModule().setUpdateSourceCardType(...args);
}

function detectUpdateSourceTypeFromUrl(...args) {
    return getAdminUpdatesModule().detectUpdateSourceTypeFromUrl(...args);
}

function syncUpdateSourceRemoveButtons(...args) {
    return getAdminUpdatesModule().syncUpdateSourceRemoveButtons(...args);
}

function defaultUpdateSourceName(...args) {
    return getAdminUpdatesModule().defaultUpdateSourceName(...args);
}

function sourceToLine(...args) {
    return getAdminUpdatesModule().sourceToLine(...args);
}

function parseUpdateSourceLines(...args) {
    return getAdminUpdatesModule().parseUpdateSourceLines(...args);
}

function collectUpdateSources(...args) {
    return getAdminUpdatesModule().collectUpdateSources(...args);
}

function searchUpdateTmdb(...args) {
    return getAdminUpdatesModule().searchUpdateTmdb(...args);
}

function selectUpdateTmdb(...args) {
    return getAdminUpdatesModule().selectUpdateTmdb(...args);
}

function saveUpdateSubscription(...args) {
    return getAdminUpdatesModule().saveUpdateSubscription(...args);
}

function textToList(value) {
    return window.FnosAdminSettings.textToList(value);
}

function parseNumberList(value) {
    return window.FnosAdminSettings.parseNumberList(value);
}


getAdminBootstrapModule().start();
