const publicState = {
    categories: {},
    results: [],
    details: {},
    filePreviews: {},
    selectedPublicId: "",
    pendingSubmit: null,
    security: {},
    selectedCategory: "movie",
    categoryTouched: false,
    activeTab: "search",
    loadingCount: 0,
    detailPromises: {},
    sixpanParses: {},
    btbtlaResolves: {},
    btbtlaDetails: {},
    quarkSelections: {},
    cloud139Selections: {},
    manualLink: null,
    manualPreview: null,
    pendingSlowSubmit: null,
    searchSequence: 0,
    supplementTimer: null,
    supplementing: false,
    searchSource: "pansou",
    searchProviders: [],
    notifications: {},
};

const CATEGORY_ICON = {
    movie: "film",
    tv: "tv",
    anime: "anime",
    variety: "star",
    other: "grid",
};
const SOURCE_LABEL = {
    quark: "夸克网盘",
    uc: "UC 网盘",
    cloud139: "移动云",
    cloud189: "天翼云",
    bt: "BT",
    bt_detail: "BTBTLA",
    btbtla: "BTBTLA",
    magnet: "磁链",
    torrent: "种子",
    pansou: "PanSou",
};

const SEARCH_SOURCE_META = {
    pansou: {
        title: "综合资源",
        label: "综合搜索",
        badge: "推荐",
        desc: "适合夸克、移动云等网盘分享资源",
        icon: "pansou",
    },
    btbtla: {
        title: "磁链资源",
        label: "BT 磁链",
        badge: "",
        desc: "适合磁链离线下载资源",
        icon: "bt",
    },
};

const $ = (id) => document.getElementById(id);

function icon(name, className = "icon") {
    const safeName = String(name || "placeholder").replace(/[^a-z0-9_-]/gi, "");
    const safeClass = String(className || "").replace(/[^a-z0-9_ -]/gi, "");
    return `<span class="icon-slot icon-${safeName} ${safeClass}" aria-hidden="true"></span>`;
}

function escapeHtml(value) {
    return window.FnosUI.escapeHtml(value);
}

const PUBLIC_DETAIL_TEXT_PATTERN = /(OpenList|Organizer|rclone|WebDAV|CMCC|STRM|官方直转|标准目录|保存位置|可见路径|扫描路径|标准化|中转目录|搬运目标|离线\/搬运|最终完成)/i;

function publicStatusForDisplay(status) {
    const text = String(status || "").trim();
    const lower = text.toLowerCase();
    if (!text) return "处理中";
    if (["done", "success"].includes(lower) || text.includes("入库完成") || text.includes("处理完成")) return "入库完成";
    if (["failed", "error"].includes(lower) || text.includes("失败") || text.includes("未完成")) return "处理失败";
    if (lower === "rejected" || text.includes("未通过") || text.includes("已拒绝")) return "未通过";
    if (lower === "cancelled" || text.includes("取消")) return "已取消";
    if (lower === "unsupported" || text.includes("暂不支持")) return "暂不支持";
    if (lower.includes("review") || text.includes("审核") || text.includes("人工") || text.includes("等待处理")) return "等待处理";
    if (
        PUBLIC_DETAIL_TEXT_PATTERN.test(text) ||
        ["created", "provider_submitting", "checking", "submitted", "waiting_transfer", "transferring", "waiting_openlist", "waiting_organizer", "organizing", "confirming", "refreshing"].includes(lower) ||
        text.includes("已提交") ||
        text.includes("处理中") ||
        text.includes("等待")
    ) {
        return "处理中";
    }
    return text;
}

function publicDefaultMessage(status) {
    const normalized = publicStatusForDisplay(status);
    if (normalized === "入库完成") return "系统已完成处理，可在影视库中搜索查看。";
    if (normalized === "处理失败") return "当前提交未完成，请稍后重试或联系管理员。";
    if (normalized === "未通过") return "提交未通过，如有疑问请联系管理员。";
    if (normalized === "已取消") return "提交已取消。";
    if (normalized === "暂不支持") return "当前资源暂不支持自动入库。";
    if (normalized === "等待处理") return "系统已收到你的入库请求，等待管理员处理。";
    return "系统已收到你的入库请求，正在处理。";
}

function publicMessageForDisplay(message, status, fallback = "") {
    const text = String(message || "").trim();
    const safeFallback = String(fallback || "").trim();
    const fallbackText = safeFallback && safeFallback !== text ? safeFallback : publicDefaultMessage(status);
    if (!text) return fallbackText;
    if (PUBLIC_DETAIL_TEXT_PATTERN.test(text)) {
        return text.includes("提交成功") ? "提交成功，系统已收到请求，请保存提交编号。" : fallbackText;
    }
    return text;
}

function formatDate(value) {
    if (!value) return "未知";
    const text = String(value).trim();
    if (!text || text.startsWith("0001") || text.startsWith("1970-01-01")) return "未知";
    const date = new Date(value);
    if (Number.isNaN(date.getTime()) || date.getFullYear() < 2000) return "未知";
    const pad = (number) => String(number).padStart(2, "0");
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

function toast(message, type = "") {
    window.FnosUI.showToast("toast", message, type);
}

function showGlobalLoading(message = "正在处理，请稍候...") {
    publicState.loadingCount = Math.max(0, publicState.loadingCount || 0) + 1;
    const mask = $("globalLoading");
    if (!mask) return;
    const messageBox = $("globalLoadingMessage");
    if (messageBox) messageBox.textContent = message;
    mask.classList.remove("hidden");
    document.body.classList.add("global-loading-active");
}

function hideGlobalLoading() {
    publicState.loadingCount = Math.max(0, (publicState.loadingCount || 0) - 1);
    if (publicState.loadingCount > 0) return;
    const mask = $("globalLoading");
    if (mask) mask.classList.add("hidden");
    document.body.classList.remove("global-loading-active");
}

async function withGlobalLoading(message, callback) {
    showGlobalLoading(message);
    try {
        return await callback();
    } finally {
        hideGlobalLoading();
    }
}

function resetNoticeBox(box) {
    if (!box) return;
    box.classList.remove("status-error", "preflight-status");
}

function setPublicSearchStatus(content, options = {}) {
    const box = $("publicSearchStatus");
    if (!box) return;
    const { html = false, tone = "", visible = true } = options;
    box.className = `notice-box search-primary-status${tone ? ` status-${tone}` : ""}${visible && content ? "" : " hidden"}`;
    box.setAttribute("role", tone === "error" ? "alert" : "status");
    box.setAttribute("aria-live", tone === "error" ? "assertive" : "polite");
    if (html) box.innerHTML = String(content || "");
    else box.textContent = String(content || "");
}

function showPublicModal(modal, preferredFocus = null) {
    if (!modal) return;
    modal.classList.remove("hidden");
    document.body.classList.add("public-modal-open");
    window.FnosUI.rememberAndFocus(modal, preferredFocus);
}

function hidePublicModal(modal, options = {}) {
    if (!modal) return;
    const { restoreFocus = true } = options;
    modal.classList.add("hidden");
    const hasOpenModal = Boolean(document.querySelector(".public-confirm-mask:not(.hidden)"));
    document.body.classList.toggle("public-modal-open", hasOpenModal);
    if (restoreFocus) window.FnosUI.restoreFocus(modal);
    else modal.__previousFocus = null;
}

function trapPublicModalFocus(event, modal) {
    if (!modal || modal.classList.contains("hidden")) return;
    window.FnosUI.trapFocus(event, modal);
}

async function api(path, options = {}) {
    const { allowFailure = false, headers = {}, ...fetchOptions } = options;
    return window.FnosUI.requestJson(path, {
        allowFailure,
        headers: { "Content-Type": "application/json", ...headers },
        ...fetchOptions,
    });
}

let publicBootstrapModule = null;

function getPublicBootstrapModule() {
    if (publicBootstrapModule) return publicBootstrapModule;
    publicBootstrapModule = window.FnosPublicBootstrap.create({
        state: publicState, getElement: $, api, toast, syncSearchSourceOptions, enabledSearchSourceKeys, setPublicSearchStatus,
        loadCaptcha, bindRecentRequests, bindRequestStatusPage, searchPublicResources, selectedSearchSource, updateSearchSourceActiveState,
        loadPublicTrending, bindPublicTrending,
        detectManualPublicLink, submitManualPublicLink, handleSixpanParseClick, handleSixpanParseChange, queryRequestStatus,
        closePublicSubmitConfirm, closeSixpanSlowWarning, continueSixpanSlowSubmit, updatePublicConfirmWarning,
        confirmPublicResourceSubmit, trapPublicModalFocus,
    });
    return publicBootstrapModule;
}

function loadPublicConfig(...args) {
    return getPublicBootstrapModule().loadPublicConfig(...args);
}

function syncCategorySelect(...args) {
    return getPublicBootstrapModule().syncCategorySelect(...args);
}

function normalizeTab(...args) {
    return getPublicBootstrapModule().normalizeTab(...args);
}

function activatePublicTab(...args) {
    return getPublicBootstrapModule().activatePublicTab(...args);
}

function applyPublicFeatureSettings(...args) {
    return getPublicBootstrapModule().applyPublicFeatureSettings(...args);
}

function bindSubmitPage(...args) {
    return getPublicBootstrapModule().bindSubmitPage(...args);
}

function categoryLabel(key) {
    return (publicState.categories[key] && publicState.categories[key].label) || key || "-";
}

function sourceLabel(value) {
    return SOURCE_LABEL[value] || value || "公开资源";
}

function publicRouteLabel(route, sourceType = "") {
    const normalizedRoute = String(route || "").toLowerCase();
    const normalizedSource = String(sourceType || "").toLowerCase();
    if (normalizedSource === "cloud139" || normalizedRoute === "cloud139_direct") return "快速入库";
    if (["magnet", "torrent"].includes(normalizedSource) || normalizedRoute === "sixpan_offline") return "云端处理";
    if (normalizedRoute === "quark_to_mobile") return "自动入库";
    if (normalizedRoute === "unsupported") return "人工处理";
    return normalizedRoute ? "自动处理" : "-";
}

let publicSearchModule = null;

function getPublicSearchModule() {
    if (publicSearchModule) return publicSearchModule;
    publicSearchModule = window.FnosPublicSearch.create({
        state: publicState,
        getElement: $,
        api,
        toast,
        escapeHtml,
        icon,
        formatDate,
        categoryLabel,
        sourceLabel,
        publicRouteLabel,
        publicMessageForDisplay,
        showGlobalLoading,
        hideGlobalLoading,
        resetNoticeBox,
        setPublicSearchStatus,
        loadSixpanParseForPublic,
        resolveBtbtlaResource,
        toggleResourceFolder,
        handleQuarkSelectionAction,
        handleQuarkFolderOpen,
        handleQuarkSelectionChange,
        handleCloud139SelectionChange,
        submitPublicResource,
        renderPublicResourceDetail,
        isPublicSubmitConfirmOpen,
        searchSourceMetaDefaults: SEARCH_SOURCE_META,
    });
    return publicSearchModule;
}

function selectedSearchSource(...args) {
    return getPublicSearchModule().selectedSearchSource(...args);
}

function searchSourcePayload(...args) {
    return getPublicSearchModule().searchSourcePayload(...args);
}

function searchSourceLabel(...args) {
    return getPublicSearchModule().searchSourceLabel(...args);
}

function enabledSearchSourceKeys(...args) {
    return getPublicSearchModule().enabledSearchSourceKeys(...args);
}

function enabledSearchSources(...args) {
    return getPublicSearchModule().enabledSearchSources(...args);
}

function searchSourceMeta(...args) {
    return getPublicSearchModule().searchSourceMeta(...args);
}

function renderSearchSourceOption(...args) {
    return getPublicSearchModule().renderSearchSourceOption(...args);
}

function syncSearchSourceOptions(...args) {
    return getPublicSearchModule().syncSearchSourceOptions(...args);
}

function updateSearchSourceActiveState(...args) {
    return getPublicSearchModule().updateSearchSourceActiveState(...args);
}

function sourceIconKey(...args) {
    return getPublicSearchModule().sourceIconKey(...args);
}

function isInstantImportResource(...args) {
    return getPublicSearchModule().isInstantImportResource(...args);
}

function publicPosterUrl(...args) {
    return getPublicSearchModule().publicPosterUrl(...args);
}

function normalizePublicPosterUrl(...args) {
    return getPublicSearchModule().normalizePublicPosterUrl(...args);
}

function publicSourceOrigin(...args) {
    return getPublicSearchModule().publicSourceOrigin(...args);
}

function isSixpanCandidate(...args) {
    return getPublicSearchModule().isSixpanCandidate(...args);
}

function isBtbtlaCandidate(...args) {
    return getPublicSearchModule().isBtbtlaCandidate(...args);
}

function isQuarkCandidate(...args) {
    return getPublicSearchModule().isQuarkCandidate(...args);
}

function isCloud139Candidate(...args) {
    return getPublicSearchModule().isCloud139Candidate(...args);
}

function sortPublicResources(...args) {
    return getPublicSearchModule().sortPublicResources(...args);
}

function inferQuality(...args) {
    return getPublicSearchModule().inferQuality(...args);
}

function categoryConfidenceText(...args) {
    return getPublicSearchModule().categoryConfidenceText(...args);
}

function knownSizeText(...args) {
    return getPublicSearchModule().knownSizeText(...args);
}

function categoryForPublicResource(...args) {
    return getPublicSearchModule().categoryForPublicResource(...args);
}

function recommendedCategoryForItem(...args) {
    return getPublicSearchModule().recommendedCategoryForItem(...args);
}

function publicResourceById(...args) {
    return getPublicSearchModule().publicResourceById(...args);
}

function isPublicDetailCached(...args) {
    return getPublicSearchModule().isPublicDetailCached(...args);
}

function publicDetailButtonLabel(...args) {
    return getPublicSearchModule().publicDetailButtonLabel(...args);
}

function ensurePublicResourceDetail(...args) {
    return getPublicSearchModule().ensurePublicResourceDetail(...args);
}

function updatePublicResourceState(...args) {
    return getPublicSearchModule().updatePublicResourceState(...args);
}

function isResourceInvalid(...args) {
    return getPublicSearchModule().isResourceInvalid(...args);
}

function isInspectionInvalid(...args) {
    return getPublicSearchModule().isInspectionInvalid(...args);
}

function resourceStateTag(...args) {
    return getPublicSearchModule().resourceStateTag(...args);
}

function sixpanHasSelectableFiles(...args) {
    return getPublicSearchModule().sixpanHasSelectableFiles(...args);
}

function sixpanSubmitSpeed(...args) {
    return getPublicSearchModule().sixpanSubmitSpeed(...args);
}

function publicSubmitButtonMeta(...args) {
    return getPublicSearchModule().publicSubmitButtonMeta(...args);
}

function searchPublicResources(...args) {
    return getPublicSearchModule().searchPublicResources(...args);
}

function bindSearchSourceSwitchActions(...args) {
    return getPublicSearchModule().bindSearchSourceSwitchActions(...args);
}

function setSearchSupplementStatus(...args) {
    return getPublicSearchModule().setSearchSupplementStatus(...args);
}

function stopPublicSearchSupplement(...args) {
    return getPublicSearchModule().stopPublicSearchSupplement(...args);
}

function publicResultKey(...args) {
    return getPublicSearchModule().publicResultKey(...args);
}

function mergePublicSearchResults(...args) {
    return getPublicSearchModule().mergePublicSearchResults(...args);
}

function startPublicSearchSupplement(...args) {
    return getPublicSearchModule().startPublicSearchSupplement(...args);
}

function renderPublicResults(...args) {
    return getPublicSearchModule().renderPublicResults(...args);
}

function bindPreviewInteractions(...args) {
    return getPublicSearchModule().bindPreviewInteractions(...args);
}

function refreshPublicResourceViews(...args) {
    return getPublicSearchModule().refreshPublicResourceViews(...args);
}

function renderPublicResultItem(...args) {
    return getPublicSearchModule().renderPublicResultItem(...args);
}

function selectPublicResource(...args) {
    return getPublicSearchModule().selectPublicResource(...args);
}

let publicTrendingModule = null;

function getPublicTrendingModule() {
    if (publicTrendingModule) return publicTrendingModule;
    publicTrendingModule = window.FnosPublicTrending.create({
        state: publicState,
        getElement: $,
        api,
        toast,
        escapeHtml,
        activatePublicTab,
        searchPublicResources,
    });
    return publicTrendingModule;
}

function loadPublicTrending(...args) {
    return getPublicTrendingModule().load(...args);
}

function bindPublicTrending(...args) {
    return getPublicTrendingModule().bind(...args);
}

let publicSubmitModule = null;

function getPublicSubmitModule() {
    if (publicSubmitModule) return publicSubmitModule;
    publicSubmitModule = window.FnosPublicSubmit.create({
        state: publicState,
        getElement: $,
        api,
        toast,
        escapeHtml,
        icon,
        formatDate,
        categoryLabel,
        sourceLabel,
        publicRouteLabel,
        publicStatusForDisplay,
        publicDefaultMessage,
        publicMessageForDisplay,
        showGlobalLoading,
        hideGlobalLoading,
        showPublicModal,
        hidePublicModal,
        resetNoticeBox,
        syncCategorySelect,
        activatePublicTab,
        categoryIcons: CATEGORY_ICON,
        publicResourceById,
        isBtbtlaCandidate,
        isSixpanCandidate,
        isQuarkCandidate,
        isCloud139Candidate,
        recommendedCategoryForItem,
        isResourceInvalid,
        ensurePublicResourceDetail,
        updatePublicResourceState,
        refreshPublicResourceViews,
        renderPublicResults,
        categoryConfidenceText,
        knownSizeText,
        bindPreviewInteractions,
        sixpanSubmitSpeed,
        isInspectionInvalid,
        categoryForPublicResource,
        selectPublicResource,
        sixpanHasSelectableFiles,
    });
    return publicSubmitModule;
}

function isPublicSubmitConfirmOpen(...args) {
    return getPublicSubmitModule().isPublicSubmitConfirmOpen(...args);
}

function renderPublicResourceDetail(...args) {
    return getPublicSubmitModule().renderPublicResourceDetail(...args);
}

function renderBtbtlaResourcePreview(...args) {
    return getPublicSubmitModule().renderBtbtlaResourcePreview(...args);
}

function btbtlaResolveKey(...args) {
    return getPublicSubmitModule().btbtlaResolveKey(...args);
}

function btbtlaResolveState(...args) {
    return getPublicSubmitModule().btbtlaResolveState(...args);
}

function setBtbtlaResolveState(...args) {
    return getPublicSubmitModule().setBtbtlaResolveState(...args);
}

function renderBtbtlaResourceRow(...args) {
    return getPublicSubmitModule().renderBtbtlaResourceRow(...args);
}

function resolveBtbtlaResource(...args) {
    return getPublicSubmitModule().resolveBtbtlaResource(...args);
}

function renderEmptyFilePreview(...args) {
    return getPublicSubmitModule().renderEmptyFilePreview(...args);
}

function previewKey(...args) {
    return getPublicSubmitModule().previewKey(...args);
}

function closePreviewBranch(...args) {
    return getPublicSubmitModule().closePreviewBranch(...args);
}

function renderFilePreview(...args) {
    return getPublicSubmitModule().renderFilePreview(...args);
}

function renderFilePreviewItem(...args) {
    return getPublicSubmitModule().renderFilePreviewItem(...args);
}

function renderFolderChildren(...args) {
    return getPublicSubmitModule().renderFolderChildren(...args);
}

function toggleResourceFolder(...args) {
    return getPublicSubmitModule().toggleResourceFolder(...args);
}

function quarkSelectionState(...args) {
    return getPublicSubmitModule().quarkSelectionState(...args);
}

function setQuarkSelectionState(...args) {
    return getPublicSubmitModule().setQuarkSelectionState(...args);
}

function quarkItemPayloadFromDataset(...args) {
    return getPublicSubmitModule().quarkItemPayloadFromDataset(...args);
}

function quarkDirPayload(...args) {
    return getPublicSubmitModule().quarkDirPayload(...args);
}

function quarkLoadedChildItems(...args) {
    return getPublicSubmitModule().quarkLoadedChildItems(...args);
}

function quarkSubdirSelectionCoversParent(...args) {
    return getPublicSubmitModule().quarkSubdirSelectionCoversParent(...args);
}

function collapseQuarkSubdirSelectionIfComplete(...args) {
    return getPublicSubmitModule().collapseQuarkSubdirSelectionIfComplete(...args);
}

function quarkSelectionSummaryText(...args) {
    return getPublicSubmitModule().quarkSelectionSummaryText(...args);
}

function renderQuarkFilePreview(...args) {
    return getPublicSubmitModule().renderQuarkFilePreview(...args);
}

function renderQuarkFilePreviewItem(...args) {
    return getPublicSubmitModule().renderQuarkFilePreviewItem(...args);
}

function renderQuarkFolderChildren(...args) {
    return getPublicSubmitModule().renderQuarkFolderChildren(...args);
}

function ensureResourceFolderLoaded(...args) {
    return getPublicSubmitModule().ensureResourceFolderLoaded(...args);
}

function handleQuarkSelectionAction(...args) {
    return getPublicSubmitModule().handleQuarkSelectionAction(...args);
}

function handleQuarkFolderOpen(...args) {
    return getPublicSubmitModule().handleQuarkFolderOpen(...args);
}

function handleQuarkSelectionChange(...args) {
    return getPublicSubmitModule().handleQuarkSelectionChange(...args);
}

function quarkRootPreviewItems(...args) {
    return getPublicSubmitModule().quarkRootPreviewItems(...args);
}

function quarkSelectionPayload(...args) {
    return getPublicSubmitModule().quarkSelectionPayload(...args);
}

function cloud139SelectionState(...args) {
    return getPublicSubmitModule().cloud139SelectionState(...args);
}

function cloud139SelectionKey(...args) {
    return getPublicSubmitModule().cloud139SelectionKey(...args);
}

function cloud139SelectionSummaryText(...args) {
    return getPublicSubmitModule().cloud139SelectionSummaryText(...args);
}

function renderCloud139FilePreview(...args) {
    return getPublicSubmitModule().renderCloud139FilePreview(...args);
}

function renderCloud139FilePreviewItem(...args) {
    return getPublicSubmitModule().renderCloud139FilePreviewItem(...args);
}

function renderCloud139FolderChildren(...args) {
    return getPublicSubmitModule().renderCloud139FolderChildren(...args);
}

function cloud139ItemPayloadFromDataset(...args) {
    return getPublicSubmitModule().cloud139ItemPayloadFromDataset(...args);
}

function handleCloud139SelectionChange(...args) {
    return getPublicSubmitModule().handleCloud139SelectionChange(...args);
}

function cloud139SelectionPayload(...args) {
    return getPublicSubmitModule().cloud139SelectionPayload(...args);
}

function sixpanPublicKey(...args) {
    return getPublicSubmitModule().sixpanPublicKey(...args);
}

function sixpanParseState(...args) {
    return getPublicSubmitModule().sixpanParseState(...args);
}

function setSixpanParseState(...args) {
    return getPublicSubmitModule().setSixpanParseState(...args);
}

function initSixpanParseSelection(...args) {
    return getPublicSubmitModule().initSixpanParseSelection(...args);
}

function loadSixpanParseForPublic(...args) {
    return getPublicSubmitModule().loadSixpanParseForPublic(...args);
}

function loadSixpanParseForManual(...args) {
    return getPublicSubmitModule().loadSixpanParseForManual(...args);
}

function renderPublicConfirmPreview(...args) {
    return getPublicSubmitModule().renderPublicConfirmPreview(...args);
}

function renderEmptyManualPreview(...args) {
    return getPublicSubmitModule().renderEmptyManualPreview(...args);
}

function renderManualPublicStatus(...args) {
    return getPublicSubmitModule().renderManualPublicStatus(...args);
}

function renderSixpanParseHtml(...args) {
    return getPublicSubmitModule().renderSixpanParseHtml(...args);
}

function renderSixpanFileRow(...args) {
    return getPublicSubmitModule().renderSixpanFileRow(...args);
}

function rerenderSixpanParse(...args) {
    return getPublicSubmitModule().rerenderSixpanParse(...args);
}

function sixpanSelectionPayload(...args) {
    return getPublicSubmitModule().sixpanSelectionPayload(...args);
}

function applySixpanAction(...args) {
    return getPublicSubmitModule().applySixpanAction(...args);
}

function isSixpanPreferredMedia(...args) {
    return getPublicSubmitModule().isSixpanPreferredMedia(...args);
}

function handleSixpanParseClick(...args) {
    return getPublicSubmitModule().handleSixpanParseClick(...args);
}

function handleSixpanParseChange(...args) {
    return getPublicSubmitModule().handleSixpanParseChange(...args);
}

function shouldWarnSlowSixpanSubmit(...args) {
    return getPublicSubmitModule().shouldWarnSlowSixpanSubmit(...args);
}

function openSixpanSlowWarning(...args) {
    return getPublicSubmitModule().openSixpanSlowWarning(...args);
}

function closeSixpanSlowWarning(...args) {
    return getPublicSubmitModule().closeSixpanSlowWarning(...args);
}

function continueSixpanSlowSubmit(...args) {
    return getPublicSubmitModule().continueSixpanSlowSubmit(...args);
}

function openPublicSubmitConfirm(...args) {
    return getPublicSubmitModule().openPublicSubmitConfirm(...args);
}

function closePublicSubmitConfirm(...args) {
    return getPublicSubmitModule().closePublicSubmitConfirm(...args);
}

function setPublicConfirmError(...args) {
    return getPublicSubmitModule().setPublicConfirmError(...args);
}

function updatePublicConfirmWarning(...args) {
    return getPublicSubmitModule().updatePublicConfirmWarning(...args);
}

function submitPublicResource(...args) {
    return getPublicSubmitModule().submitPublicResource(...args);
}

function confirmPublicResourceSubmit(...args) {
    return getPublicSubmitModule().confirmPublicResourceSubmit(...args);
}

function detectManualPublicLink(...args) {
    return getPublicSubmitModule().detectManualPublicLink(...args);
}

function submitManualPublicLink(...args) {
    return getPublicSubmitModule().submitManualPublicLink(...args);
}

function openManualPreviewConfirm(...args) {
    return getPublicSubmitModule().openManualPreviewConfirm(...args);
}

function validateManualPublicForm(...args) {
    return getPublicSubmitModule().validateManualPublicForm(...args);
}

function handleSubmitResponse(...args) {
    return getPublicSubmitModule().handleSubmitResponse(...args);
}

function renderPreflightFailure(...args) {
    return getPublicSubmitModule().renderPreflightFailure(...args);
}

function renderPreflightFailureHtml(...args) {
    return getPublicSubmitModule().renderPreflightFailureHtml(...args);
}

function renderRequestResult(...args) {
    return getPublicSubmitModule().renderRequestResult(...args);
}

function queryRequestStatus(...args) {
    return getPublicSubmitModule().queryRequestStatus(...args);
}

function renderRequestStatus(...args) {
    return getPublicSubmitModule().renderRequestStatus(...args);
}

function renderStatusCard(...args) {
    return getPublicSubmitModule().renderStatusCard(...args);
}

function isDoneStatus(...args) {
    return getPublicSubmitModule().isDoneStatus(...args);
}

function isFailedStatus(...args) {
    return getPublicSubmitModule().isFailedStatus(...args);
}

function bindStatusCardActions(...args) {
    return getPublicSubmitModule().bindStatusCardActions(...args);
}

function copyText(...args) {
    return getPublicSubmitModule().copyText(...args);
}

function loadCaptcha(...args) {
    return getPublicSubmitModule().loadCaptcha(...args);
}

function captchaPayload(...args) {
    return getPublicSubmitModule().captchaPayload(...args);
}

function loadRecentRequests(...args) {
    return getPublicSubmitModule().loadRecentRequests(...args);
}

function renderRecentRequests(...args) {
    return getPublicSubmitModule().renderRecentRequests(...args);
}

function saveRecentRequest(...args) {
    return getPublicSubmitModule().saveRecentRequest(...args);
}

function bindRecentRequests(...args) {
    return getPublicSubmitModule().bindRecentRequests(...args);
}

function bindRequestStatusPage(...args) {
    return getPublicSubmitModule().bindRequestStatusPage(...args);
}

getPublicBootstrapModule().start();
