const state = {
    config: null,
    results: [],
    selected: null,
    searchSequence: 0,
    supplementTimer: null,
    supplementing: false,
};

const $ = (id) => document.getElementById(id);

function escapeHtml(value) {
    return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
}

function formatDate(value) {
    if (!value) return "-";
    if (String(value).startsWith("0001")) return "未知";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return value;
    const pad = (number) => String(number).padStart(2, "0");
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

function toast(message, type = "") {
    const box = $("toast");
    box.textContent = message;
    box.className = `toast ${type}`;
    box.classList.remove("hidden");
    window.clearTimeout(box.__toastTimer);
    box.__toastTimer = window.setTimeout(() => box.classList.add("hidden"), 3000);
}

async function api(path, options = {}) {
    const { allowFailure = false, headers = {}, ...fetchOptions } = options;
    const response = await fetch(path, {
        headers: { "Content-Type": "application/json", ...headers },
        ...fetchOptions,
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || (!allowFailure && data.success === false)) {
        throw new Error(data.message || `请求失败：HTTP ${response.status}`);
    }
    return data;
}

async function confirmAction(options = {}) {
    if (typeof window.showConfirmDialog === "function") {
        return Boolean(await window.showConfirmDialog(options));
    }
    toast("确认组件未加载，已取消本次操作", "error");
    return false;
}

async function loadConfig() {
    state.config = await api("/api/config/public");
    renderRcloneStatus(state.config.rclone || {});
}

function sourceLabel(type) {
    const labels = {
        quark: "夸克",
        uc: "UC",
        cloud139: "移动云",
        cloud189: "天翼云",
        magnet: "磁链",
        torrent: "种子",
        aliyun: "阿里云盘",
        baidu: "百度网盘",
        unknown: "未知",
    };
    return labels[type] || type || "未知";
}

function routeLabel(route) {
    const labels = {
        quark_to_mobile: "Quark → 移动云 → 飞牛",
        cloud139_direct: "移动云直入库",
        cloud189_direct: "天翼云直入库",
        sixpan_offline: "6盘离线",
        unsupported: "暂不支持",
    };
    return labels[route] || route || "暂不支持";
}

function statusPill(item) {
    if (item.supported) return `<span class="pill ok">已支持</span>`;
    return `<span class="pill warn">${escapeHtml(item.reason || "暂不支持")}</span>`;
}

function isInstantImportResource(item) {
    const text = [
        item?.instant_import,
        item?.speed_tag,
        item?.source_type,
        item?.source_hint,
        item?.source,
        item?.url,
        item?.source_url,
    ].filter(Boolean).join(" ").toLowerCase();
    return item?.instant_import === true || text.includes("cloud139") || text.includes("yun.139.com") || text.includes("caiyun.139.com") || text.includes("mobile") || text.includes("移动");
}

function categoryLabel(categoryKey) {
    return state.config?.categories?.[categoryKey]?.label || categoryKey || "-";
}

async function doSearch() {
    const keyword = $("keywordInput").value.trim();
    const token = $("pansouTokenInput").value.trim();
    if (!keyword) {
        toast("请输入搜索关键词", "error");
        return;
    }
    stopSearchSupplement();
    const sequence = state.searchSequence + 1;
    state.searchSequence = sequence;
    $("searchBtn").disabled = true;
    $("searchStatus").textContent = "搜索中，正在返回首批资源...";
    $("results").innerHTML = "";
    try {
        const data = await api("/api/search", {
            method: "POST",
            body: JSON.stringify({ keyword, token, async_poll: false }),
        });
        if (sequence !== state.searchSequence) return;
        state.results = data.items || [];
        renderResults();
        $("searchStatus").textContent = `已返回首批结果，共 ${state.results.length} 条，后台继续补充中...`;
        startSearchSupplement(keyword, token, sequence);
    } catch (error) {
        $("searchStatus").textContent = "";
        toast(error.message, "error");
    } finally {
        $("searchBtn").disabled = false;
    }
}

function stopSearchSupplement() {
    state.supplementing = false;
    if (state.supplementTimer) {
        window.clearTimeout(state.supplementTimer);
        state.supplementTimer = null;
    }
}

function searchResultKey(item) {
    const dedupeKeys = Array.isArray(item?.dedupe_keys) ? item.dedupe_keys.filter(Boolean) : [];
    return String(
        dedupeKeys[0]
        || item?.result_key
        || item?.url
        || item?.source_url
        || `${item?.source_type || ""}:${item?.title || ""}:${item?.size_text || item?.size || ""}`
    ).trim();
}

function mergeSearchResults(items) {
    const incoming = Array.isArray(items) ? items : [];
    const seen = new Set(state.results.map(searchResultKey).filter(Boolean));
    const additions = [];
    incoming.forEach((item) => {
        const key = searchResultKey(item);
        if (!key || seen.has(key)) return;
        seen.add(key);
        additions.push(item);
    });
    if (additions.length) {
        state.results = [...state.results, ...additions];
        renderResults();
    }
    return additions.length;
}

function startSearchSupplement(keyword, token, sequence) {
    const maxRounds = 3;
    const intervalMs = 2200;
    let round = 0;
    state.supplementing = true;

    const poll = async () => {
        if (!state.supplementing || sequence !== state.searchSequence) return;
        round += 1;
        try {
            const data = await api("/api/search", {
                method: "POST",
                body: JSON.stringify({ keyword, token, async_poll: false, background: true }),
                allowFailure: true,
            });
            if (sequence !== state.searchSequence) return;
            const added = data.success === false ? 0 : mergeSearchResults(data.items || []);
            $("searchStatus").textContent = added > 0
                ? `后台补充中，第 ${round}/3 轮新增 ${added} 条，共 ${state.results.length} 条...`
                : `后台补充中，第 ${round}/3 轮暂无新增，共 ${state.results.length} 条...`;
        } catch {
            // 后台补充失败不打断当前首批结果浏览。
        }
        if (!state.supplementing || sequence !== state.searchSequence) return;
        if (round >= maxRounds) {
            state.supplementing = false;
            state.supplementTimer = null;
            $("searchStatus").textContent = `搜索完成，共 ${state.results.length} 条结果`;
            return;
        }
        state.supplementTimer = window.setTimeout(poll, intervalMs);
    };

    state.supplementTimer = window.setTimeout(poll, intervalMs);
}

function renderResults() {
    const root = $("results");
    if (!state.results.length) {
        root.innerHTML = `<div class="muted">没有找到资源。</div>`;
        return;
    }
    root.innerHTML = state.results.map((item, index) => {
        const instantImport = isInstantImportResource(item);
        const instantLabel = item.speed_tag || "官方直转";
        return `
        <article class="result-card ${instantImport ? "is-instant-import" : ""}">
            ${instantImport ? `<div class="instant-import-badge">${escapeHtml(instantLabel)}</div>` : ""}
            <h3 title="${escapeHtml(item.title)}">${escapeHtml(item.title)}</h3>
            <div class="meta">
                <span class="pill">${sourceLabel(item.source_type)}</span>
                ${instantImport ? `<span class="pill instant">移动云直达</span>` : ""}
                <span class="pill">${routeLabel(item.route)}</span>
                ${statusPill(item)}
            </div>
            <div class="muted">发布时间：${escapeHtml(formatDate(item.datetime))}</div>
            <div class="url-box">${escapeHtml(item.url)}</div>
            <div class="card-actions">
                <button class="secondary" onclick="detectResult(${index})">识别</button>
                <button ${item.supported ? "" : "disabled"} onclick="openImportModal(${index})">入库</button>
            </div>
        </article>
    `;
    }).join("");
}

async function detectResult(index) {
    const item = state.results[index];
    try {
        const data = await api("/api/detect", {
            method: "POST",
            body: JSON.stringify({ url: item.url, password: item.password }),
        });
        toast(`识别为：${sourceLabel(data.link.source_type)}，${routeLabel(data.link.route)}`, data.link.supported ? "ok" : "");
    } catch (error) {
        toast(error.message, "error");
    }
}

async function manualDetect() {
    const url = $("manualUrlInput").value.trim();
    const password = $("manualPasswordInput").value.trim();
    if (!url) {
        toast("请先输入资源链接", "error");
        return;
    }
    $("manualDetectBtn").disabled = true;
    $("manualDetectBox").textContent = "正在识别链接...";
    try {
        const data = await api("/api/detect", {
            method: "POST",
            body: JSON.stringify({ url, password }),
        });
        const link = data.link || {};
        $("manualDetectBox").textContent = [
            `资源类型：${sourceLabel(link.source_type)}`,
            `入库线路：${routeLabel(link.route)}`,
            `支持状态：${link.supported ? "已启用" : "未启用"}`,
            `说明：${link.reason || "-"}`,
        ].join("\n");
        if (!$("manualTitleInput").value.trim()) {
            $("manualTitleInput").value = sourceLabel(link.source_type) + " 手动入库资源";
        }
    } catch (error) {
        $("manualDetectBox").textContent = `识别失败：${error.message}`;
    } finally {
        $("manualDetectBtn").disabled = false;
    }
}

async function manualImport() {
    const title = $("manualTitleInput").value.trim() || "手动入库资源";
    const url = $("manualUrlInput").value.trim();
    const password = $("manualPasswordInput").value.trim();
    const category = $("manualCategoryInput").value;
    if (!url) {
        toast("请先输入资源链接", "error");
        return;
    }
    if (!(await confirmAction({
        title: "确认手动入库",
        message: `确认入库到【${categoryLabel(category)}】？`,
        confirmText: "确认入库",
        tone: "warning",
    }))) return;
    $("manualImportBtn").disabled = true;
    try {
        const data = await api("/api/import", {
            method: "POST",
            body: JSON.stringify({ title, url, password, category }),
        });
        toast(data.message || "入库任务已创建", data.success === false ? "" : "ok");
        await loadJobs();
    } catch (error) {
        toast(error.message, "error");
    } finally {
        $("manualImportBtn").disabled = false;
    }
}

function openImportModal(index) {
    const item = state.results[index];
    state.selected = item;
    $("modalTitle").textContent = item.title || "-";
    $("modalSourceType").textContent = sourceLabel(item.source_type);
    $("modalRoute").textContent = routeLabel(item.route);
    $("modalUrl").textContent = item.url || "-";
    $("modalCategory").value = guessCategory(item.title);
    window.syncCustomSelect?.($("modalCategory"));
    $("checkBox").textContent = item.source_type === "quark"
        ? "可先点击“检测资源”查看夸克分享是否有效；直接确认入库时后端也会按配置执行检测。"
        : "该线路当前仅做识别展示。";
    $("importModal").classList.remove("hidden");
}

function guessCategory(title) {
    const text = String(title || "");
    if (/动漫|动画|番剧|anime/i.test(text)) return "anime";
    if (/综艺|真人秀|脱口秀/i.test(text)) return "variety";
    if (/第[一二三四五六七八九十0-9]+季|全集|全\d+集|S\d{2}|E\d{2}|电视剧|剧集/i.test(text)) return "tv";
    return "movie";
}

function closeImportModal() {
    $("importModal").classList.add("hidden");
    state.selected = null;
}

async function checkSelectedResource() {
    if (!state.selected) return;
    if (state.selected.source_type !== "quark") {
        $("checkBox").textContent = "第一版只支持夸克资源检测。";
        return;
    }
    $("checkResourceBtn").disabled = true;
    $("checkBox").textContent = "正在检测夸克分享...";
    try {
        const data = await api("/api/quark/check", {
            method: "POST",
            body: JSON.stringify({ url: state.selected.url, title: state.selected.title }),
        });
        const payload = data.data || {};
        const share = payload.data?.share || {};
        const files = payload.data?.list || [];
        $("checkBox").textContent = [
            "检测通过。",
            `标题：${share.title || "-"}`,
            `文件数：${share.file_num ?? files.length ?? "-"}`,
            `状态：${share.status ?? "-"}`,
            "",
            "前几项文件：",
            ...files.slice(0, 8).map((file) => `- ${file.file_name || file.name || file.fid}`),
        ].join("\n");
    } catch (error) {
        $("checkBox").textContent = `检测失败：${error.message}`;
    } finally {
        $("checkResourceBtn").disabled = false;
    }
}

async function confirmImport() {
    if (!state.selected) return;
    $("confirmImportBtn").disabled = true;
    try {
        const data = await api("/api/import", {
            method: "POST",
            body: JSON.stringify({
                title: state.selected.title,
                url: state.selected.url,
                password: state.selected.password,
                category: $("modalCategory").value,
            }),
        });
        toast(data.message || "入库任务已创建", data.success === false ? "" : "ok");
        closeImportModal();
        await loadJobs();
    } catch (error) {
        toast(error.message, "error");
    } finally {
        $("confirmImportBtn").disabled = false;
    }
}

function statusLabel(status) {
    return state.config?.status_labels?.[status] || status || "-";
}

function statusClass(status) {
    if (status === "done" || status === "waiting_transfer" || status === "submitted") return "ok";
    if (status === "failed") return "bad";
    if (status === "unsupported") return "warn";
    return "";
}

async function loadJobs() {
    const status = $("statusFilter").value;
    const query = status ? `?status=${encodeURIComponent(status)}` : "";
    const data = await api(`/api/jobs${query}`);
    renderJobs(data.items || []);
}

function renderRcloneStatus(status) {
    $("rcloneRunning").textContent = status.running ? "运行中" : "空闲";
    $("rcloneStatusText").textContent = status.status || "-";
    $("rcloneStartedAt").textContent = formatDate(status.last_started_at);
    $("rcloneFinishedAt").textContent = formatDate(status.last_finished_at);
    $("rcloneExitCode").textContent = status.last_exit_code ?? "-";
    $("rcloneInterval").textContent = status.auto_interval_minutes > 0 ? `${status.auto_interval_minutes} 分钟` : "未启用";
    const errorBox = $("rcloneErrorBox");
    if (status.last_error) {
        errorBox.style.display = "";
        errorBox.textContent = status.last_error;
    } else {
        errorBox.style.display = "none";
        errorBox.textContent = "";
    }
}

async function loadRcloneStatus() {
    const data = await api("/api/rclone/status");
    renderRcloneStatus(data.status || {});
    const logs = await api("/api/rclone/logs?limit=300");
    $("rcloneLogs").textContent = (logs.items || []).join("\n") || "暂无日志";
    await loadRcloneRuns();
}

async function checkRcloneEnvironment() {
    $("rcloneCheckBtn").disabled = true;
    $("rcloneCheckBox").textContent = "正在检查 rclone 编排环境...";
    try {
        const data = await api("/api/rclone/check", { allowFailure: true });
        $("rcloneCheckBox").innerHTML = (data.items || []).map((item) => {
            const cls = item.ok ? "ok" : "bad";
            const icon = item.ok ? "通过" : "失败";
            return `<div><span class="pill ${cls}">${icon}</span> <strong>${escapeHtml(item.name)}</strong>：${escapeHtml(item.message || "")}</div>`;
        }).join("") || "无检查结果";
        toast(data.success ? "环境检查通过" : (data.message || "环境检查存在问题"), data.success ? "ok" : "error");
    } catch (error) {
        $("rcloneCheckBox").textContent = `环境检查失败：${error.message}`;
    } finally {
        $("rcloneCheckBtn").disabled = false;
    }
}

async function loadRcloneRuns() {
    const data = await api("/api/rclone/runs?limit=8");
    renderRcloneRuns(data.items || []);
}

function renderRcloneRuns(items) {
    const root = $("rcloneRuns");
    if (!items.length) {
        root.innerHTML = `<div class="muted">暂无搬运记录。</div>`;
        return;
    }
    root.innerHTML = items.map((run) => `
        <article class="run-card">
            <div class="row"><span>#${run.id} ${escapeHtml(run.trigger_reason || "-")}</span><span class="pill ${statusClass(run.status)}">${escapeHtml(run.status || "-")}</span></div>
            <div class="row"><span>开始</span><strong>${escapeHtml(formatDate(run.started_at))}</strong></div>
            <div class="row"><span>结束</span><strong>${escapeHtml(formatDate(run.finished_at))}</strong></div>
            <div class="row"><span>退出码</span><strong>${run.exit_code ?? "-"}</strong></div>
            ${run.error_message ? `<div class="hint warning">${escapeHtml(run.error_message)}</div>` : ""}
        </article>
    `).join("");
}

async function startRclone() {
    if (!(await confirmAction({
        title: "立即执行 rclone 搬运",
        message: "确认立即执行 rclone 搬运？建议第一次先确保 rclone Worker 和 MP remote 可用。",
        confirmText: "启动搬运",
        tone: "warning",
    }))) return;
    try {
        const data = await api("/api/rclone/start", {
            method: "POST",
            body: JSON.stringify({ reason: "web_manual" }),
        });
        toast(data.message || "已启动 rclone", data.success ? "ok" : "");
        await loadRcloneStatus();
    } catch (error) {
        toast(error.message, "error");
    }
}

async function stopRclone() {
    if (!(await confirmAction({
        title: "停止 rclone 搬运",
        message: "确认停止当前 rclone 搬运任务？",
        confirmText: "停止搬运",
        tone: "danger",
    }))) return;
    try {
        const data = await api("/api/rclone/stop", { method: "POST", body: "{}" });
        toast(data.message || "已请求停止", data.success ? "ok" : "");
        await loadRcloneStatus();
    } catch (error) {
        toast(error.message, "error");
    }
}

function renderJobs(items) {
    const root = $("jobs");
    if (!items.length) {
        root.innerHTML = `<div class="muted">暂无任务。</div>`;
        return;
    }
    root.innerHTML = items.map((job) => `
        <article class="job-card">
            <h3>${escapeHtml(job.title)}</h3>
            <div class="row"><span>状态</span><span class="pill ${statusClass(job.status)}">${escapeHtml(statusLabel(job.status))}</span></div>
            <div class="row"><span>分类</span><strong>${escapeHtml(job.category_label)}</strong></div>
            <div class="row"><span>类型</span><strong>${sourceLabel(job.source_type)}</strong></div>
            <div class="row"><span>线路</span><strong>${routeLabel(job.target_route)}</strong></div>
            <div class="row"><span>目标</span><code>${escapeHtml(job.target_path || "-")}</code></div>
            ${job.error_message ? `<div class="hint warning">${escapeHtml(job.error_message)}</div>` : ""}
            <div class="card-actions">
                <button class="secondary" onclick="openJobDetail(${job.id})">详情</button>
                <button class="secondary" onclick="retryJob(${job.id})">重试</button>
            </div>
        </article>
    `).join("");
}

async function openJobDetail(id) {
    try {
        const data = await api(`/api/jobs/${id}`);
        $("jobDetailBox").textContent = JSON.stringify(data.job, null, 2);
        $("jobModal").classList.remove("hidden");
    } catch (error) {
        toast(error.message, "error");
    }
}

async function retryJob(id) {
    if (!(await confirmAction({
        title: `重试任务 #${id}`,
        message: "确认重试该任务？",
        confirmText: "重试",
        tone: "warning",
    }))) return;
    try {
        const data = await api(`/api/jobs/${id}/retry`, { method: "POST", body: "{}" });
        toast(data.message || "已重试", data.success === false ? "" : "ok");
        await loadJobs();
    } catch (error) {
        toast(error.message, "error");
    }
}

async function refreshMedia(category) {
    try {
        const data = await api("/api/media/refresh", {
            method: "POST",
            body: JSON.stringify({ category }),
        });
        toast(data.success ? "已触发飞牛刷新" : (data.message || "刷新失败"), data.success ? "ok" : "error");
    } catch (error) {
        toast(error.message, "error");
    }
}

function bindEvents() {
    $("searchBtn").addEventListener("click", doSearch);
    $("keywordInput").addEventListener("keydown", (event) => {
        if (event.key === "Enter") doSearch();
    });
    $("loadJobsBtn").addEventListener("click", loadJobs);
    $("refreshJobsBtn").addEventListener("click", loadJobs);
    $("manualDetectBtn").addEventListener("click", manualDetect);
    $("manualImportBtn").addEventListener("click", manualImport);
    $("manualUrlInput").addEventListener("keydown", (event) => {
        if (event.key === "Enter") manualDetect();
    });
    $("rcloneCheckBtn").addEventListener("click", checkRcloneEnvironment);
    $("rcloneStatusBtn").addEventListener("click", loadRcloneStatus);
    $("rcloneRunsBtn").addEventListener("click", loadRcloneRuns);
    $("rcloneStartBtn").addEventListener("click", startRclone);
    $("rcloneStopBtn").addEventListener("click", stopRclone);
    $("checkResourceBtn").addEventListener("click", checkSelectedResource);
    $("confirmImportBtn").addEventListener("click", confirmImport);
    document.querySelectorAll("[data-close-modal]").forEach((button) => button.addEventListener("click", closeImportModal));
    document.querySelectorAll("[data-close-job-modal]").forEach((button) => {
        button.addEventListener("click", () => $("jobModal").classList.add("hidden"));
    });
    document.querySelectorAll(".refresh-media").forEach((button) => {
        button.addEventListener("click", () => refreshMedia(button.dataset.category));
    });
}

window.detectResult = detectResult;
window.openImportModal = openImportModal;
window.openJobDetail = openJobDetail;
window.retryJob = retryJob;

(async function boot() {
    bindEvents();
    try {
        await loadConfig();
        await loadJobs();
        await loadRcloneStatus();
        setInterval(() => loadRcloneStatus().catch(() => {}), 5000);
    } catch (error) {
        toast(error.message, "error");
    }
})();
