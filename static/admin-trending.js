(function () {
    function create(context) {
        const { state, getElement, api, toast, escapeHtml, icon, formatDate, normalizePagination, renderPager, statusPill, openDrawer, confirmDialog, loadSearchProviders } = context;
        const $ = getElement;
        const workbench = window.FnosResourceSearchWorkbench;
        let activeCandidateId = 0;
        let workbenchCandidate = {};
        let workbenchItems = [];
        let selectedPublicId = "";
        let workbenchDetails = {};
        let workbenchSelections = {};
        let workbenchSearchSource = "pansou";
        let workbenchKeyword = "";
        let supplementController = null;

        const sourceLabels = { tencent: "腾讯视频", iqiyi: "爱奇艺", youku: "优酷" };
        const typeLabels = { tv: "电视剧", movie: "电影", anime: "动漫", variety: "综艺", other: "其他" };

        function ensureSchedulerTimeOptions() {
            const populate = (id, count) => {
                const select = $(id);
                if (!select || select.options.length === count) return;
                select.innerHTML = Array.from({ length: count }, (_, value) => {
                    const text = String(value).padStart(2, "0");
                    return `<option value="${text}">${text}</option>`;
                }).join("");
            };
            populate("trendingSchedulerHour", 24);
            populate("trendingSchedulerMinute", 60);
        }

        function syncTrendingSelects() {
            [
                "trendingSourceFilter",
                "trendingTypeFilter",
                "trendingStatusFilter",
                "trendingSchedulerHour",
                "trendingSchedulerMinute",
            ].forEach((id) => window.syncCustomSelect?.($(id)));
        }

        function schedulerSourceLine(scheduler) {
            const sources = scheduler.sources || {};
            return Object.entries(sources).map(([key, value]) => {
                if (value?.success !== false) return `${sourceLabels[key] || key}: 正常`;
                return `${sourceLabels[key] || key}: 失败（${value?.error || "未提供错误详情"}）`;
            }).join("、") || "尚无来源运行记录";
        }

        function schedulerStatusMarkup(scheduler) {
            const enabled = Boolean(scheduler.enabled);
            const latestRun = scheduler.latest_run && typeof scheduler.latest_run === "object" ? scheduler.latest_run : {};
            const nextRunText = enabled
                ? (scheduler.next_run_at ? formatDate(scheduler.next_run_at) : "等待调度器计算")
                : "未启用";
            const lastRunAt = scheduler.last_run_at || latestRun.finished_at || latestRun.started_at || "";
            const lastRunText = lastRunAt ? formatDate(lastRunAt) : "尚未运行";
            return `
                <div class="trending-scheduler-status-head">
                    <div><span>运行状态</span><strong>热榜发现${enabled ? "已启用" : "未启用"}</strong></div>
                    <span class="pill ${enabled ? "ok" : "warn"}">${enabled ? "自动调度中" : "仅手动运行"}</span>
                </div>
                <div class="trending-scheduler-status-grid">
                    <article><span>每日运行</span><strong>${escapeHtml(scheduler.run_at || "08:30")}</strong></article>
                    <article><span>下次运行</span><strong>${escapeHtml(nextRunText)}</strong></article>
                    <article><span>最近运行</span><strong>${escapeHtml(lastRunText)}</strong></article>
                </div>
                <p class="trending-scheduler-source-line">${escapeHtml(schedulerSourceLine(scheduler))}</p>
                ${scheduler.last_error ? `<p class="notice-box status-error">${escapeHtml(scheduler.last_error)}</p>` : ""}
            `;
        }

        function schedulerDialogMarkup() {
            return `
                <div class="trending-scheduler-dialog">
                    <section class="trending-scheduler-settings-card">
                        <div class="trending-scheduler-dialog-copy">
                            <strong>自动更新设置</strong>
                            <p>启用后由后台每天自动抓取腾讯、爱奇艺和优酷热榜。</p>
                        </div>
                        <label class="mini-check trending-scheduler-toggle">
                            <input id="trendingSchedulerEnabled" type="checkbox">
                            启用每日自动更新
                        </label>
                        <label class="advanced-field trending-scheduler-time">
                            <span>每日执行时间</span>
                            <div class="update-time-picker trending-time-picker">
                                <select id="trendingSchedulerHour" aria-label="每日热榜执行小时"></select>
                                <em aria-hidden="true">:</em>
                                <select id="trendingSchedulerMinute" aria-label="每日热榜执行分钟"></select>
                            </div>
                            <small id="trendingSchedulerTimezone">北京时间</small>
                        </label>
                        <div class="trending-scheduler-dialog-actions">
                            <button type="button" id="saveTrendingSchedulerBtn">保存调度配置</button>
                        </div>
                    </section>
                    <section class="trending-scheduler-status-card" id="trendingSchedulerStatus"></section>
                </div>
            `;
        }

        function applySchedulerState(scheduler) {
            ensureSchedulerTimeOptions();
            if ($("trendingSchedulerEnabled")) $("trendingSchedulerEnabled").checked = Boolean(scheduler.enabled);
            const [runHour = "08", runMinute = "30"] = String(scheduler.run_at || "08:30").split(":", 2);
            [
                ["trendingSchedulerHour", runHour],
                ["trendingSchedulerMinute", runMinute],
            ].forEach(([id, value]) => {
                if (!$(id)) return;
                $(id).value = value;
                $(id).disabled = !scheduler.enabled;
            });
            if ($("trendingSchedulerTimezone")) {
                $("trendingSchedulerTimezone").textContent = scheduler.timezone === "Asia/Shanghai"
                    ? "北京时间"
                    : (scheduler.timezone || "Asia/Shanghai");
            }
            if ($("trendingSchedulerStatus")) $("trendingSchedulerStatus").innerHTML = schedulerStatusMarkup(scheduler);
            syncTrendingSelects();
        }

        function bindSchedulerDialog() {
            $("trendingSchedulerEnabled")?.addEventListener("change", () => {
                const disabled = !$("trendingSchedulerEnabled").checked;
                ["trendingSchedulerHour", "trendingSchedulerMinute"].forEach((id) => {
                    if ($(id)) $(id).disabled = disabled;
                    window.syncCustomSelect?.($(id));
                });
            });
            $("saveTrendingSchedulerBtn")?.addEventListener("click", () => {
                saveTrendingSchedule().catch((error) => toast(error.message, "error"));
            });
        }

        function openTrendingScheduler() {
            openDrawer("每日热榜 · 自动调度", schedulerDialogMarkup(), { mode: "modal", className: "trending-scheduler-modal" });
            applySchedulerState(state.trendingStatus || {});
            bindSchedulerDialog();
        }

        async function loadTrending() {
            const page = state.pagination.trending.page || 1;
            const perPage = 200;
            const query = new URLSearchParams({ group_by: "category" });
            const source = $("trendingSourceFilter")?.value || "";
            const mediaType = $("trendingTypeFilter")?.value || "";
            const status = $("trendingStatusFilter")?.value || "";
            if (source) query.set("source", source);
            if (status) query.set("status", status);
            const [candidateData, statusData] = await Promise.all([
                api(`/api/admin/trending/candidates?${query.toString()}`),
                api("/api/admin/trending/status"),
            ]);
            const groups = candidateData.groups || {};
            state.trendingCandidates = candidateData.grouped
                ? categoryOrder.flatMap((type) => groups[type]?.items || [])
                : candidateData.items || [];
            state.trendingStatus = statusData.status || {};
            state.pagination.trending = candidateData.grouped
                ? { page: 1, per_page: perPage, total: Number(candidateData.total || 0), pages: 1 }
                : normalizePagination(candidateData.pagination, { page, per_page: perPage });
            renderTrending();
        }

        function renderTrending() {
            const items = state.trendingCandidates || [];
            const scheduler = state.trendingStatus || {};
            const counts = scheduler.counts || {};
            applySchedulerState(scheduler);
            if ($("trendingMetrics")) {
                $("trendingMetrics").innerHTML = `
                    ${metric("待审核", counts.discovered || 0, "首次入库候选")}
                    ${metric("今日发现", counts.today || 0, "多来源合并后")}
                    ${metric("媒体库已有", counts.already_exists || 0, "不会重复入库")}
                    ${metric("已有任务", counts.task_exists || 0, "不会重复创建任务")}
                    ${metric("首入库中", counts.importing || 0, "已提交选中资源")}
                    ${metric("首入库完成", counts.imported || 0, "任务已完整完成")}
                    ${metric("首入库失败", counts.import_failed || 0, "可重新选择资源")}
                    ${metric("已忽略", counts.ignored || 0, "可随时恢复")}
                `;
            }
            if ($("trendingCandidateBadge")) $("trendingCandidateBadge").textContent = String(counts.discovered || 0);
            const boards = $("trendingCategoryBoards");
            if (!boards) return;
            boards.innerHTML = renderCategoryBoards(items);
            boards.querySelectorAll("[data-trending-action]").forEach((button) => {
                button.addEventListener("click", () => {
                    candidateAction(button.dataset.trendingId, button.dataset.trendingAction)
                        .catch((error) => toast(error.message, "error"));
                });
            });
            if ($("trendingPager")) $("trendingPager").innerHTML = `<span class="pager-info">每个分类综合展示 Top 20</span>`;
        }

        function metric(label, value, note) {
            return `<article class="metric-card"><span class="metric-label">${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong><small>${escapeHtml(note)}</small></article>`;
        }

        const categoryOrder = ["tv", "movie", "variety", "anime"];

        function platformRows(item) {
            if (Array.isArray(item.sources) && item.sources.length) return item.sources;
            if (Array.isArray(item.raw_data?.sources) && item.raw_data.sources.length) return item.raw_data.sources;
            if (Array.isArray(item.raw_data?.source_items) && item.raw_data.source_items.length) return item.raw_data.source_items;
            if (item.platform_ranks && typeof item.platform_ranks === "object") {
                return Object.entries(item.platform_ranks).map(([source, rank]) => ({ source, rank }));
            }
            if (item.raw_data?.platform_ranks && typeof item.raw_data.platform_ranks === "object") {
                return Object.entries(item.raw_data.platform_ranks).map(([source, rank]) => ({ source, rank }));
            }
            return [{ source: item.source || "", rank: item.rank }];
        }

        function comprehensiveRank(item) {
            const direct = Number(item.category_rank || item.rank || item.raw_data?.category_rank || item.best_rank || 0);
            if (direct > 0) return direct;
            const ranks = platformRows(item).map((entry) => Number(entry.rank || 0)).filter((rank) => rank > 0);
            return ranks.length ? Math.min(...ranks) : 9999;
        }

        function platformSummary(item) {
            const names = platformRows(item).map((entry) => sourceLabels[entry.source] || entry.source).filter(Boolean);
            return Array.from(new Set(names)).join(" / ") || "-";
        }

        function platformRankSummary(item) {
            return platformRows(item).map((entry) => `${sourceLabels[entry.source] || entry.source || "-"} #${entry.rank || "-"}`).join(" · ") || "-";
        }

        function renderCategoryBoards(items) {
            const grouped = new Map(categoryOrder.map((key) => [key, []]));
            items.forEach((item) => {
                const key = categoryOrder.includes(item.media_type) ? item.media_type : "other";
                if (!grouped.has(key)) grouped.set(key, []);
                grouped.get(key).push(item);
            });
            const selectedType = $("trendingTypeFilter")?.value || "";
            const visibleTypes = selectedType ? [selectedType] : categoryOrder;
            return visibleTypes.map((type) => {
                const rows = (grouped.get(type) || [])
                    .slice()
                    .sort((left, right) => comprehensiveRank(left) - comprehensiveRank(right) || Number(right.heat || 0) - Number(left.heat || 0))
                    .slice(0, 20);
                return `<section class="trending-category-board" data-trending-category="${escapeHtml(type)}">
                    <header class="trending-category-head">
                        <div><h3>${escapeHtml(typeLabels[type] || type)}</h3><p>综合 Top ${escapeHtml(rows.length || 0)}，合并爱奇艺、腾讯视频和优酷来源。</p></div>
                        <span class="pill">${escapeHtml(rows.length)} 条</span>
                    </header>
                    <div class="table-wrap"><table class="data-table trending-category-table">
                        <thead><tr><th>综合排名</th><th>标题</th><th>来源 / 多平台</th><th>各平台排名</th><th>状态</th><th>操作</th></tr></thead>
                        <tbody>${rows.length ? rows.map(renderRow).join("") : '<tr><td colspan="6" class="empty-cell">当前分类暂无符合筛选条件的条目。</td></tr>'}</tbody>
                    </table></div>
                </section>`;
            }).join("");
        }

        function renderRow(item) {
            const importable = ["discovered", "import_failed"].includes(String(item.status || "discovered"));
            const score = item.heat || item.score ? `${item.heat ? `热度 ${item.heat}` : ""}${item.heat && item.score ? " / " : ""}${item.score ? `评分 ${item.score}` : ""}` : "-";
            const statusAction = item.status === "ignored"
                ? `<button class="secondary mini" data-trending-action="restore" data-trending-id="${escapeHtml(item.id)}">恢复</button>`
                : `<button class="secondary mini" data-trending-action="ignore" data-trending-id="${escapeHtml(item.id)}" ${item.status === "importing" ? "disabled" : ""}>忽略</button>`;
            const action = `
                <button class="secondary mini" data-trending-action="detail" data-trending-id="${escapeHtml(item.id)}">详情</button>
                <button class="secondary mini" data-trending-action="search" data-trending-id="${escapeHtml(item.id)}" ${importable ? "" : "disabled"}>搜索资源</button>
                ${statusAction}
            `;
            return `<tr>
                <td data-label="综合排名"><strong>#${escapeHtml(comprehensiveRank(item) === 9999 ? "-" : comprehensiveRank(item))}</strong></td>
                <td data-label="标题"><strong>${escapeHtml(item.title || "-")}</strong>${item.year ? `<div class="muted">${escapeHtml(item.year)}</div>` : ""}${score !== "-" ? `<div class="muted">${escapeHtml(score)}</div>` : ""}</td>
                <td data-label="来源">${escapeHtml(platformSummary(item))}</td>
                <td data-label="各平台排名">${escapeHtml(platformRankSummary(item))}</td>
                <td data-label="状态">${candidateStatusPill(item.status || "discovered")}</td>
                <td data-label="操作" class="trending-row-actions">${action}</td>
            </tr>`;
        }

        function candidateStatusPill(status) {
            const value = String(status || "discovered");
            if (value === "importing") return '<span class="pill warn">首入库中</span>';
            if (value === "imported") return '<span class="pill ok">首入库完成</span>';
            if (value === "import_failed") return '<span class="pill error">首入库失败</span>';
            return statusPill(value);
        }

        async function runTrending() {
            const button = $("runTrendingBtn");
            if (button) button.disabled = true;
            try {
                const data = await api("/api/admin/trending/run", { method: "POST", body: JSON.stringify({}), allowFailure: true });
                const errorDetail = Array.isArray(data.errors) && data.errors.length ? `：${data.errors.join("；")}` : "";
                toast(data.message || (data.success ? "热榜发现完成" : `热榜发现失败${errorDetail}`), data.success ? "success" : "error");
                await loadTrending();
            } finally {
                if (button) button.disabled = false;
            }
        }

        async function saveTrendingSchedule() {
            const enabled = Boolean($("trendingSchedulerEnabled")?.checked);
            const runHour = String($("trendingSchedulerHour")?.value || "08").padStart(2, "0");
            const runMinute = String($("trendingSchedulerMinute")?.value || "30").padStart(2, "0");
            const runAt = `${runHour}:${runMinute}`;
            if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(runAt)) {
                toast("请选择有效的每日执行时间", "error");
                return;
            }
            const button = $("saveTrendingSchedulerBtn");
            if (button) button.disabled = true;
            try {
                const data = await api("/api/admin/advanced-config", {
                    method: "POST",
                    body: JSON.stringify({
                        config: {
                            hot_discovery: {
                                enabled,
                                run_at: runAt,
                            },
                        },
                    }),
                });
                toast(data.message || "每日热榜调度配置已保存", "success");
                await loadTrending();
            } finally {
                if (button) button.disabled = false;
            }
        }

        async function candidateAction(id, action) {
            if (action === "detail") return openCandidateDetail(id);
            if (action === "search") return searchCandidateResources(id);
            const data = await api(`/api/admin/trending/candidates/${encodeURIComponent(id)}/${action}`, { method: "POST", body: JSON.stringify({}), allowFailure: true });
            toast(data.message || "候选状态已更新", data.success === false ? "error" : "success");
            await loadTrending();
        }

        function renderCandidateDetail(item) {
            const importable = ["discovered", "import_failed"].includes(String(item.status || "discovered"));
            const subscribedId = Number(item.subscription_id || 0);
            return `
                <div class="detail-grid">
                    <article><span class="muted">标题</span><strong>${escapeHtml(item.title || "-")}</strong></article>
                    <article><span class="muted">类型</span><strong>${escapeHtml(typeLabels[item.media_type] || item.media_type || "其他")}</strong></article>
                    <article><span class="muted">来源</span><strong>${escapeHtml(sourceLabels[item.source] || item.source || "-")}</strong></article>
                    <article><span class="muted">状态</span><strong>${candidateStatusPill(item.status || "discovered")}</strong></article>
                    <article><span class="muted">排名 / 热度</span><strong>${escapeHtml(`${item.rank || "-"} / ${item.heat || "-"}`)}</strong></article>
                    <article><span class="muted">首次发现</span><strong>${escapeHtml(formatDate(item.first_seen_at))}</strong></article>
                    <article><span class="muted">首入库任务</span><strong>${item.initial_import_job_id ? `#${escapeHtml(item.initial_import_job_id)}` : "-"}</strong></article>
                    <article><span class="muted">追更订阅</span><strong>${subscribedId ? `#${escapeHtml(String(subscribedId))}` : "未创建"}</strong></article>
                </div>
                <div class="filter-row" style="margin-top:16px;">
                    <button type="button" data-trending-detail-search="${escapeHtml(item.id)}" ${importable ? "" : "disabled"}>搜索资源</button>
                    <button type="button" data-trending-detail-subscribe="${escapeHtml(item.id)}" ${subscribedId ? "disabled" : ""}>同时创建追更订阅</button>
                </div>
            `;
        }

        async function openCandidateDetail(id) {
            const data = await api(`/api/admin/trending/candidates/${encodeURIComponent(id)}`);
            openDrawer("热榜候选详情", renderCandidateDetail(data.item || {}), { mode: "modal", className: "trending-detail-modal" });
            document.querySelector("[data-trending-detail-search]")?.addEventListener("click", () => {
                searchCandidateResources(id).catch((error) => toast(error.message, "error"));
            });
            document.querySelector("[data-trending-detail-subscribe]")?.addEventListener("click", (event) => {
                subscribeCandidate(id, event.currentTarget).catch((error) => toast(error.message, "error"));
            });
        }

        async function subscribeCandidate(id, button = null) {
            if (button?.dataset.busy === "1") return;
            if (button) {
                button.dataset.busy = "1";
                button.disabled = true;
                button.textContent = "正在匹配并创建...";
            }
            try {
                const data = await api(`/api/admin/trending/candidates/${encodeURIComponent(id)}/subscribe`, {
                    method: "POST",
                    body: JSON.stringify({}),
                    allowFailure: true,
                    skipButtonLoading: true,
                });
                toast(data.message || "追更订阅已创建", data.success === false ? "error" : "success");
                await loadTrending();
                if (data.success !== false) {
                    await openCandidateDetail(id);
                }
            } finally {
                if (button?.isConnected) {
                    delete button.dataset.busy;
                    button.disabled = false;
                    button.textContent = "同时创建追更订阅";
                }
            }
        }

        function resourceSourceLabel(item) {
            return sourceLabels[item.source] || item.source_type || item.source || "-";
        }

        function resourceSourceIcon(item) {
            return workbench.sourceIconKey(item);
        }

        function resourcePoster(item) {
            return String(item.poster || item.cover || item.image_url || "").trim();
        }

        function availableSearchSources() {
            const providers = Array.isArray(state.searchProviders) ? state.searchProviders : [];
            return providers
                .filter((item) => item && item.enabled !== false && item.configured !== false && item.key)
                .map((item) => ({ key: String(item.key).toLowerCase(), name: String(item.name || item.key) }));
        }

        function normalizedSearchSource(value = "") {
            const sources = availableSearchSources();
            if (!sources.length) return "";
            const requested = String(value || "").toLowerCase();
            if (sources.some((item) => item.key === requested)) return requested;
            return sources.some((item) => item.key === "pansou") ? "pansou" : sources[0].key;
        }

        function searchSourceOptions() {
            return availableSearchSources().map((item) => `<option value="${escapeHtml(item.key)}" ${item.key === workbenchSearchSource ? "selected" : ""}>${escapeHtml(item.name)}</option>`).join("");
        }

        function detailHtml(publicId) {
            const state = workbenchDetails[publicId];
            if (!state || state.loading) return '<section class="resource-detail-panel glass-panel soft"><div class="detail-loading">正在读取资源详情...</div></section>';
            if (state.error) return `<section class="resource-detail-panel glass-panel soft"><div class="notice-box status-error">${escapeHtml(state.error)}</div></section>`;
            const detail = workbench.detailValue(state.response);
            const inspection = detail.inspection || {};
            const summary = inspection.summary || {};
            const selection = workbenchSelections[publicId] || new Set();
            return `<section class="resource-detail-panel glass-panel soft">
                <div class="section-title compact"><div><h3>资源详情</h3><p>${escapeHtml(detail.title || workbenchCandidate.title || "-")}</p></div><span class="pill ${inspection.success === false ? "error" : "ok"}">${escapeHtml(inspection.message || (inspection.success === false ? "检测未通过" : "已读取"))}</span></div>
                <div class="resource-detail-grid">
                    <div><span>来源</span><strong>${escapeHtml(detail.source_type || detail.link?.source_type || "-")}</strong></div>
                    <div><span>文件数</span><strong>${escapeHtml(summary.file_count || workbench.detailItems(state.response).length || "-")}</strong></div>
                    <div><span>总大小</span><strong>${escapeHtml(summary.total_size_text || detail.size_text || "-")}</strong></div>
                    <div><span>建议分类</span><strong>${escapeHtml(detail.category_suggestion?.label || typeLabels[workbenchCandidate.media_type] || workbenchCandidate.media_type || "-")}</strong></div>
                </div>
                <div class="notice-box" style="margin-top:12px;">可选择文件或文件夹；不选时沿用原有默认全部入库规则。</div>
                ${workbench.renderFileSelection(state.response, { publicId, selected: selection, escapeHtml, sourceType: detail.source_type || detail.link?.source_type || "" })}
            </section>`;
        }

        function renderResourceResults() {
            const importable = ["discovered", "import_failed"].includes(String(workbenchCandidate.status || "discovered"));
            const rows = workbenchItems.map((item) => {
                const publicId = String(item.public_id || "");
                const selected = selectedPublicId === publicId;
                const size = item.size_text || item.size || "";
                const source = resourceSourceLabel(item);
                const detailState = workbenchDetails[publicId];
                return workbench.renderCard({
                    id: publicId,
                    escapeHtml,
                    selected,
                    title: item.title || workbenchCandidate.title || "未命名资源",
                    poster: resourcePoster(item),
                    iconClass: `tile-source-${escapeHtml(resourceSourceIcon(item))}`,
                    iconHtml: icon(resourceSourceIcon(item)),
                    classes: [selected ? "selected" : ""],
                    tagsHtml: `<span class="tag tag-blue">${escapeHtml(source)}</span>${item.quality ? `<span class="tag tag-gold">${escapeHtml(item.quality)}</span>` : ""}`,
                    metaHtml: `${size ? `<span>文件大小：${escapeHtml(size)}</span>` : ""}<span>${escapeHtml(formatDate(item.datetime || item.created_at))}</span>`,
                    actionsHtml: `<button class="secondary detail-trigger" type="button" data-workbench-detail-id="${escapeHtml(publicId)}">${detailState?.loading ? "读取中..." : selected ? "收起详情" : "查看详情"}</button><button type="button" data-workbench-submit-id="${escapeHtml(publicId)}" ${publicId && importable ? "" : "disabled"}>确认首入库</button>`,
                    detailHtml: detailHtml(publicId),
                });
            }).join("");
            return `<div class="trending-workbench-toolbar"><label><span>搜索源</span><select id="trendingWorkbenchSource">${searchSourceOptions()}</select></label><label><span>搜索关键词</span><input id="trendingWorkbenchKeyword" type="search" maxlength="300" value="${escapeHtml(workbenchKeyword || workbenchCandidate.title || "")}" placeholder="搜索关键词"></label><button class="secondary" id="trendingWorkbenchResearch" type="button">重新搜索</button></div><div id="trendingWorkbenchStatus" class="notice-box">已找到 ${escapeHtml(workbenchItems.length)} 条资源。${importable ? "" : " 当前状态不能创建任务。"}</div><div id="trendingWorkbenchResults" class="trending-workbench-results" style="margin-top:16px;">${rows || '<div class="empty glass-panel soft">未找到可用资源。</div>'}</div>`;
        }

        function renderWorkbenchDrawer() {
            openDrawer(`${workbenchCandidate.title || "热榜候选"} · 搜索结果`, renderResourceResults(), { mode: "modal", className: "trending-detail-modal" });
            const root = document.getElementById("adminDrawerBody");
            window.syncCustomSelect?.(root?.querySelector("#trendingWorkbenchSource"));
            workbench.bindCards(root, {
                onSelect: selectWorkbenchResource,
                onDetail: selectWorkbenchResource,
                onSubmit: (publicId) => confirmCandidateImport(activeCandidateId, workbenchCandidate, publicId).catch((error) => toast(error.message, "error")),
            });
            root?.querySelectorAll("[data-workbench-file-key]").forEach((input) => input.addEventListener("change", () => {
                const publicId = input.dataset.workbenchFilePublicId || "";
                const selected = workbenchSelections[publicId] || new Set();
                if (input.checked) selected.add(input.dataset.workbenchFileKey || "");
                else selected.delete(input.dataset.workbenchFileKey || "");
                workbenchSelections[publicId] = selected;
            }));
            root?.querySelectorAll("[data-workbench-folder-public-id]").forEach((button) => button.addEventListener("click", () => {
                expandWorkbenchFolder(button.dataset.workbenchFolderPublicId, button.dataset.workbenchFolderFid).catch((error) => toast(error.message, "error"));
            }));
            root?.querySelectorAll("[data-workbench-root-public-id]").forEach((button) => button.addEventListener("click", () => {
                restoreWorkbenchRoot(button.dataset.workbenchRootPublicId);
            }));
            root?.querySelector("#trendingWorkbenchResearch")?.addEventListener("click", () => {
                const source = root.querySelector("#trendingWorkbenchSource")?.value || workbenchSearchSource;
                const keyword = root.querySelector("#trendingWorkbenchKeyword")?.value || workbenchKeyword || workbenchCandidate.title || "";
                searchCandidateResources(activeCandidateId, source, keyword).catch((error) => toast(error.message, "error"));
            });
        }

        async function selectWorkbenchResource(publicId) {
            if (!publicId) return;
            if (selectedPublicId === publicId) {
                selectedPublicId = "";
                renderWorkbenchDrawer();
                return;
            }
            selectedPublicId = publicId;
            if (!workbenchDetails[publicId]) {
                workbenchDetails[publicId] = { loading: true };
                renderWorkbenchDrawer();
                try {
                    const response = await api(`/api/admin/trending/candidates/${encodeURIComponent(activeCandidateId)}/resources/${encodeURIComponent(publicId)}/detail`, { allowFailure: true });
                    workbenchDetails[publicId] = response.success === false ? { error: response.message || "资源详情读取失败" } : { response };
                } catch (error) {
                    workbenchDetails[publicId] = { error: error.message || "资源详情读取失败" };
                }
            }
            renderWorkbenchDrawer();
        }

        async function expandWorkbenchFolder(publicId, fid) {
            if (!publicId || !fid) return;
            const detailState = workbenchDetails[publicId];
            if (!detailState?.response) return;
            const detail = workbench.detailValue(detailState.response);
            const currentItems = workbench.detailItems(detailState.response);
            const parent = currentItems.find((item) => String(item.fid || item.id || "") === String(fid));
            const response = await api(`/api/admin/trending/candidates/${encodeURIComponent(activeCandidateId)}/resources/${encodeURIComponent(publicId)}/files?fid=${encodeURIComponent(fid)}`, { allowFailure: true });
            if (response.success === false) return toast(response.message || "文件夹读取失败", "error");
            const inspection = detail.inspection || (detail.inspection = {});
            const source = workbenchItems.find((item) => String(item.public_id || "") === String(publicId)) || {};
            const kind = workbench.sourceKind(source.source_type || source.source || detail.source_type || detail.link?.source_type || "");
            if (kind === "quark" && parent) {
                detail._workbench_selection_context = {
                    mode: "subdir_items",
                    base_dir: parent,
                    root_items: currentItems,
                };
                inspection.items = Array.isArray(response.items) ? response.items : [];
                workbenchSelections[publicId] = new Set();
            } else {
                const merged = workbench.mergeResults(currentItems, response.items || []);
                inspection.items = merged.items;
            }
            renderWorkbenchDrawer();
        }

        function restoreWorkbenchRoot(publicId) {
            const detailState = workbenchDetails[publicId];
            if (!detailState?.response) return;
            const detail = workbench.detailValue(detailState.response);
            const context = detail._workbench_selection_context || {};
            if (!Array.isArray(context.root_items)) return;
            const inspection = detail.inspection || (detail.inspection = {});
            inspection.items = context.root_items;
            delete detail._workbench_selection_context;
            workbenchSelections[publicId] = new Set();
            renderWorkbenchDrawer();
        }

        function startAdminSupplement(id) {
            supplementController?.stop();
            supplementController = workbench.createSupplementController({
                maxRounds: 3,
                intervalMs: 2200,
                search: () => api(`/api/admin/trending/candidates/${encodeURIComponent(id)}/search`, { method: "POST", body: JSON.stringify({ refresh: false, keyword: workbenchKeyword, sources: [workbenchSearchSource] }), allowFailure: true }),
                onResult: (data) => {
                    if (data.success === false) return 0;
                    const merged = workbench.mergeResults(workbenchItems, data.items || []);
                    workbenchItems = merged.items;
                    if (merged.additions.length) renderWorkbenchDrawer();
                    return merged.additions.length;
                },
                onStatus: (message, visible) => {
                    const box = document.getElementById("trendingWorkbenchStatus");
                    if (box && visible && message) box.textContent = message;
                },
            });
            supplementController.start({});
        }

        async function searchCandidateResources(id, source = "", keyword = "") {
            supplementController?.stop();
            if (!Array.isArray(state.searchProviders) || state.searchProviders.length === 0) {
                await loadSearchProviders();
            }
            workbenchSearchSource = normalizedSearchSource(source || workbenchSearchSource);
            workbenchKeyword = String(keyword || workbenchKeyword || "").trim().slice(0, 300);
            if (!workbenchSearchSource) {
                openDrawer(
                    "无可用搜索源",
                    '<div class="notice-box status-error">请先在“系统配置 → 搜索与入库”中启用并配置至少一个搜索源。</div>',
                    { mode: "modal", className: "trending-detail-modal" },
                );
                return;
            }
            openDrawer("正在搜索资源", '<div class="notice-box">正在调用现有搜索源，请稍候...</div>', { mode: "modal", className: "trending-detail-modal" });
            const data = await api(`/api/admin/trending/candidates/${encodeURIComponent(id)}/search`, { method: "POST", body: JSON.stringify({ refresh: true, keyword: workbenchKeyword, sources: [workbenchSearchSource] }), allowFailure: true });
            if (data.success === false) {
                openDrawer("资源搜索失败", `<div class="notice-box status-error">${escapeHtml(data.message || "资源搜索失败")}</div>`, { mode: "modal", className: "trending-detail-modal" });
                return;
            }
            activeCandidateId = Number(id);
            workbenchCandidate = data.candidate || {};
            workbenchKeyword = String(data.keyword || workbenchKeyword || workbenchCandidate.title || "").trim().slice(0, 300);
            workbenchItems = workbench.mergeResults([], data.items || []).items;
            selectedPublicId = "";
            workbenchDetails = {};
            workbenchSelections = {};
            renderWorkbenchDrawer();
            startAdminSupplement(id);
        }

        async function confirmCandidateImport(id, candidate, publicId) {
            if (!publicId) return toast("请先选择可入库资源", "error");
            const confirmed = await confirmDialog({
                title: "确认创建首入库任务",
                message: `为“${candidate.title || "该候选"}”创建首入库任务？`,
                confirmText: "创建任务",
                tone: "warning",
            });
            if (!confirmed) return;
            const detailState = workbenchDetails[publicId];
            const resource = workbenchItems.find((item) => String(item.public_id || "") === String(publicId)) || {};
            const selection = workbench.selectionPayload(detailState?.response || {}, workbenchSelections[publicId] || new Set(), resource.source_type || resource.source || "");
            const body = { public_id: publicId, category: candidate.media_type || "movie", ...selection };
            const data = await api(`/api/admin/trending/candidates/${encodeURIComponent(id)}/import`, { method: "POST", body: JSON.stringify(body), allowFailure: true });
            toast(data.message || (data.success ? "首入库任务已创建" : "首入库任务创建失败"), data.success ? "success" : "error");
            if (data.success) {
                supplementController?.stop();
                await loadTrending();
                await openCandidateDetail(id);
            }
        }

        return Object.freeze({ loadTrending, renderTrending, runTrending, openTrendingScheduler, saveTrendingSchedule, candidateAction, openCandidateDetail, searchCandidateResources, confirmCandidateImport });
    }

    window.FnosAdminTrending = Object.freeze({ create });
})();
