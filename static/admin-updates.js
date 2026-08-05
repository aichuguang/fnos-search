(function () {
    function create(context) {
        const {
            state,
            getElement,
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
            categoryOrder,
        } = context;
        const adminState = state;
        const $ = getElement;
        const ADVANCED_CATEGORY_ORDER = categoryOrder;

        async function loadUpdates() {
            const status = $("updateStatusFilter")?.value || "";
            const page = adminState.pagination.updates.page || 1;
            const perPage = adminState.pagination.updates.per_page || 50;
            const query = new URLSearchParams({ page: String(page), per_page: String(perPage) });
            if (status) query.set("status", status);
            const data = await api(`/api/admin/update-subscriptions?${query.toString()}`);
            adminState.updateSubscriptions = data.items || [];
            adminState.updateScheduler = data.scheduler || {};
            adminState.pagination.updates = normalizePagination(data.pagination, { page, per_page: perPage });
            renderUpdates();
        }

        function renderUpdates() {
            const items = adminState.updateSubscriptions || [];
            const scheduler = adminState.updateScheduler || {};
            if ($("updateSchedulerStatus")) {
                const next = scheduler.next_subscription || {};
                const current = scheduler.current_run || {};
                const schedulerText = scheduler.enabled === false ? "已停用" : scheduler.running ? "运行中" : "未运行";
                const scheduleLine = scheduler.task_running && current.id
                    ? `正在检查：${escapeHtml(current.subscription_title || `#${current.id}`)}`
                    : next.id
                      ? `下次检查：${escapeHtml(next.title || `#${next.id}`)}（${escapeHtml(formatDate(next.next_run_at) || "待计算")}）`
                      : "暂无待检查订阅";
                const errorLine = scheduler.last_error ? `<p class="muted">异常：${escapeHtml(scheduler.last_error)}</p>` : "";
                $("updateSchedulerStatus").innerHTML = `
                    <strong>定时追更${escapeHtml(schedulerText)}</strong>
                    <p>${scheduleLine}</p>
                    ${errorLine}
                `;
            }
            const enabled = items.filter((item) => item.status === "enabled").length;
            const review = items.reduce((sum, item) => sum + Number(item.review_candidate_count || 0), 0);
            if ($("updateMetrics")) {
                $("updateMetrics").innerHTML = `
                    ${renderMetricCard("追更订阅", items.length, "当前页", "refresh", "blue")}
                    ${renderMetricCard("启用中", enabled, "会自动到点执行", "success", "cyan")}
                    ${renderMetricCard("候选文件", review, "仅异常时处理", "warning", "orange")}
                    ${renderMetricCard("调度器", scheduler.running ? "运行中" : "未运行", "自动检查", "settings", "green")}
                `;
            }
            const body = $("updateSubscriptionsBody");
            if (!body) return;
            if (!items.length) {
                body.innerHTML = `<tr><td colspan="8" class="empty-cell">暂无追更订阅。</td></tr>`;
                renderPager("updatesPager", adminState.pagination.updates, (page) => {
                    adminState.pagination.updates.page = page;
                    loadUpdates().catch((error) => toast(error.message, "error"));
                });
                return;
            }
            body.innerHTML = items.map(renderUpdateSubscriptionRow).join("");
            body.querySelectorAll("[data-update-action]").forEach((button) => {
                button.addEventListener("click", () => updateSubscriptionAction(button.dataset.updateId, button.dataset.updateAction));
            });
            renderPager("updatesPager", adminState.pagination.updates, (page) => {
                adminState.pagination.updates.page = page;
                loadUpdates().catch((error) => toast(error.message, "error"));
            });
        }

        function renderUpdateSubscriptionRow(item) {
            const status = item.status || "";
            const rawData = item.raw_data && typeof item.raw_data === "object" ? item.raw_data : {};
            const pendingImport = rawData.pending_import && typeof rawData.pending_import === "object" ? rawData.pending_import : {};
            const pendingImportStatus = rawData.pending_import_status && typeof rawData.pending_import_status === "object" ? rawData.pending_import_status : {};
            const pathHealth = rawData.path_health && typeof rawData.path_health === "object" ? rawData.path_health : {};
            const tmdbSchedule = rawData.tmdb_schedule && typeof rawData.tmdb_schedule === "object" ? rawData.tmdb_schedule : {};
            const season = item.season ? `第 ${item.season} 季` : "-";
            const nextEpisode = item.next_episode ? `目标 E${String(item.next_episode).padStart(2, "0")}` : "自动判断";
            const missing = Array.isArray(item.missing_episodes) && item.missing_episodes.length ? `缺 ${item.missing_episodes.join(", ")}` : nextEpisode;
            const tmdbNext = tmdbSchedule.next_air_episode || tmdbSchedule.episode || "";
            const localLatest = item.last_success_episode || "";
            const pendingEpisode = pendingImportStatus.episode || pendingImport.episode;
            const scheduleNote = pendingEpisode
                ? `等待 E${pendingEpisode} 完整入库确认`
                : item.schedule_kind === "tmdb"
                  ? `TMDB${tmdbNext ? ` 下一播 E${tmdbNext}` : " 播出日驱动"}`
                  : "手动规则兜底";
            const pathNote = pathHealth.success === false ? "保存目录异常" : "";
            const buttons = [
                `<button class="secondary mini" data-update-id="${escapeHtml(item.id)}" data-update-action="detail">详情</button>`,
                `<button class="mini" data-update-id="${escapeHtml(item.id)}" data-update-action="run">运行</button>`,
                `<button class="secondary mini" data-update-id="${escapeHtml(item.id)}" data-update-action="edit">编辑</button>`,
            ];
            if (status === "enabled") {
                buttons.push(`<button class="secondary mini danger" data-update-id="${escapeHtml(item.id)}" data-update-action="pause">暂停</button>`);
            } else {
                buttons.push(`<button class="secondary mini" data-update-id="${escapeHtml(item.id)}" data-update-action="enable">启用</button>`);
            }
            buttons.push(`<button class="secondary mini danger" data-update-id="${escapeHtml(item.id)}" data-update-action="delete">删除</button>`);
            return `
                <tr>
                    <td>#${escapeHtml(item.id)}</td>
                    <td><strong>${escapeHtml(item.title)}</strong><p class="muted">${escapeHtml((item.aliases || []).join("、"))}</p></td>
                    <td>${escapeHtml(item.category_label || item.category)}<p class="muted">${escapeHtml(season)}</p></td>
                    <td>${escapeHtml(formatDate(item.next_run_at) || "-")}<p class="muted">${escapeHtml(scheduleNote)}</p></td>
                    <td>${escapeHtml(missing)}<p class="muted">本地最新 E${escapeHtml(localLatest || "-")}${pathNote ? ` · ${escapeHtml(pathNote)}` : ""}</p></td>
                    <td>${Number(item.review_candidate_count || 0)}</td>
                    <td>${statusPill(status)}</td>
                    <td class="table-actions">${buttons.join("")}</td>
                </tr>
            `;
        }

        async function updateSubscriptionAction(id, action) {
            if (action === "detail") return openUpdateDetail(id);
            if (action === "edit") return openUpdateEditor(id);
            if (action === "run") {
                const data = await api(`/api/admin/update-subscriptions/${encodeURIComponent(id)}/run`, { method: "POST", body: JSON.stringify({}), allowFailure: true });
                toast(data.message || (data.success ? "追更运行完成" : "追更运行失败"), data.success ? "success" : "error");
                await loadUpdates();
                return;
            }
            if (action === "pause" || action === "enable") {
                const data = await api(`/api/admin/update-subscriptions/${encodeURIComponent(id)}/${action}`, { method: "POST", body: JSON.stringify({}) });
                toast(data.message || "状态已更新", "success");
                await loadUpdates();
                return;
            }
            if (action === "delete") {
                const confirmed = await confirmDialog({
                    message: "确定删除这个追更订阅？相关运行记录、候选、事件也会一起删除。",
                    title: "删除追更订阅",
                    confirmText: "删除",
                    cancelText: "取消",
                    tone: "danger",
                });
                if (!confirmed) return;
                const data = await api(`/api/admin/update-subscriptions/${encodeURIComponent(id)}`, { method: "DELETE", allowFailure: true });
                toast(data.message || (data.success ? "追更订阅已删除" : "删除失败"), data.success ? "success" : "error");
                await loadUpdates();
            }
        }

        async function openUpdateDetail(id) {
            const data = await api(`/api/admin/update-subscriptions/${encodeURIComponent(id)}`);
            const item = data.item || {};
            const allCandidates = data.candidates || [];
            const candidates = allCandidates.slice(0, 20);
            const runs = (data.runs || []).slice(0, 3);
            const snapshot = data.snapshot || {};
            const rawData = item.raw_data && typeof item.raw_data === "object" ? item.raw_data : {};
            const tmdbSchedule = rawData.tmdb_schedule && typeof rawData.tmdb_schedule === "object" ? rawData.tmdb_schedule : {};
            const tmdbRetry = rawData.tmdb_retry && typeof rawData.tmdb_retry === "object" ? rawData.tmdb_retry : {};
            const tmdbWait = rawData.tmdb_wait && typeof rawData.tmdb_wait === "object" ? rawData.tmdb_wait : {};
            const pendingImport = rawData.pending_import && typeof rawData.pending_import === "object" ? rawData.pending_import : {};
            const pendingImportStatus = rawData.pending_import_status && typeof rawData.pending_import_status === "object" ? rawData.pending_import_status : {};
            const pathHealth = rawData.path_health && typeof rawData.path_health === "object" ? rawData.path_health : {};
            const rootResolution = rawData.canonical_root_resolution && typeof rawData.canonical_root_resolution === "object" ? rawData.canonical_root_resolution : {};
            const fixedSourceGate = rawData.fixed_source_gate && typeof rawData.fixed_source_gate === "object" ? rawData.fixed_source_gate : {};
            const lastRunOutcome = rawData.last_run_outcome && typeof rawData.last_run_outcome === "object" ? rawData.last_run_outcome : {};
            const scheduleText = item.schedule_kind === "tmdb" ? "TMDB 播出日驱动" : item.schedule_kind || "-";
            const targetEpisode = Array.isArray(item.missing_episodes) && item.missing_episodes.length ? item.missing_episodes.join("、E") : item.next_episode || tmdbSchedule.episode || "-";
            const latestEpisode = snapshot.latest_episode ? `S${escapeHtml(snapshot.latest_season || item.season || "-")}E${escapeHtml(snapshot.latest_episode)}` : "暂无";
            const saveRoot = rootResolution.canonical_openlist_root || rawData.canonical_openlist_root || pathHealth.openlist_path || "自动识别中";
            const tmdbLatestAiredEpisode = tmdbSchedule.latest_aired_episode || tmdbSchedule.last_air_episode || "";
            const tmdbLatestAiredSeason = tmdbSchedule.latest_aired_season || tmdbSchedule.last_air_season || item.season || "-";
            const tmdbLatestText = tmdbLatestAiredEpisode ? `S${escapeHtml(tmdbLatestAiredSeason)}E${escapeHtml(tmdbLatestAiredEpisode)}${tmdbSchedule.latest_aired_date || tmdbSchedule.last_air_date ? ` · ${escapeHtml(tmdbSchedule.latest_aired_date || tmdbSchedule.last_air_date)}` : ""}` : "未知";
            const tmdbNextAirEpisode = tmdbSchedule.next_air_episode || tmdbSchedule.episode || "";
            const tmdbNextAirSeason = tmdbSchedule.next_air_season || tmdbSchedule.season || item.season || "-";
            const tmdbNextText = tmdbNextAirEpisode ? `S${escapeHtml(tmdbNextAirSeason)}E${escapeHtml(tmdbNextAirEpisode)}${tmdbSchedule.next_air_date || tmdbSchedule.air_date ? ` · ${escapeHtml(tmdbSchedule.next_air_date || tmdbSchedule.air_date)}` : ""}` : "暂无";
            const localLatestNumber = Number(snapshot.latest_episode || item.last_success_episode || 0);
            const tmdbReferenceNumber = Number(tmdbNextAirEpisode || tmdbLatestAiredEpisode || 0);
            const tmdbLagNotice = tmdbReferenceNumber && localLatestNumber > tmdbReferenceNumber
                ? `<div class="notice-box warning"><strong>集数信息不一致</strong><p>媒体库已到 E${escapeHtml(localLatestNumber)}，TMDB 当前到 E${escapeHtml(tmdbReferenceNumber)}。系统会以媒体库进度为准，避免重复提交旧集。</p></div>`
                : "";
            openDrawer(
                `追更详情：${escapeHtml(item.title || id)}`,
                `
                <div class="notice-box info">
                    <strong>${escapeHtml(item.title || "-")} · ${statusPill(item.status)}</strong>
                    <p>系统目标 E${escapeHtml(targetEpisode)}；OpenList 本地最新 E${escapeHtml(snapshot.latest_episode || item.last_success_episode || "-")}；下次运行 ${escapeHtml(formatDate(item.next_run_at) || "-")}。</p>
                </div>
                <div class="detail-grid">
                    <div><span>分类</span>${escapeHtml(item.category_label || item.category || "-")}</div>
                    <div><span>本轮目标</span>${targetEpisode && targetEpisode !== "-" ? `E${escapeHtml(targetEpisode)}` : "-"}</div>
                    <div><span>TMDB 最新已播</span>${tmdbLatestText}</div>
                    <div><span>TMDB 下一播出</span>${tmdbNextText}</div>
                    <div><span>媒体库最新集</span>${latestEpisode}</div>
                    <div><span>追更保存目录</span>${escapeHtml(saveRoot)}</div>
                </div>
                ${tmdbLagNotice}
                ${renderUpdateDetailNotes({ tmdbRetry, tmdbWait, pendingImport, pendingImportStatus, fixedSourceGate, lastRunOutcome })}
                ${pathHealth.success === false ? `<div class="notice-box warning"><strong>保存目录异常</strong><p>${escapeHtml(pathHealth.message || "")}</p>${renderPathHealthChecks(pathHealth.checks || [])}</div>` : ""}
                <details class="advanced-details">
                    <summary>查看调度与保存信息</summary>
                    <div class="detail-grid">
                        <div><span>季号</span>${escapeHtml(item.season || tmdbSchedule.season || "-")}</div>
                        <div><span>调度方式</span>${escapeHtml(scheduleText)}</div>
                        <div><span>保存目录状态</span>${pathHealth.success === false ? "异常" : pathHealth.success === true ? "正常" : "未记录"}</div>
                    </div>
                </details>
                <div class="table-actions" style="margin:12px 0;">
                    <button class="secondary mini" data-update-detail-action="preview" data-update-id="${escapeHtml(id)}">检查来源</button>
                    <button class="secondary mini" data-update-detail-action="snapshot" data-update-id="${escapeHtml(id)}">同步目录状态</button>
                </div>
                <h4>追更源</h4>
                ${renderUpdateSourceSummary(item.sources || [])}
                <h4>最近候选</h4>
                <div class="list-box">${candidates.map(renderUpdateCandidateItem).join("") || `<div class="empty">暂无候选</div>`}</div>
                <h4>最近三次运行</h4>
                <div class="list-box">${runs.map(renderUpdateRunItem).join("") || `<div class="empty">暂无运行记录</div>`}</div>
                `,
                { mode: "modal" }
            );
            $("adminDrawerBody")?.querySelectorAll("[data-update-candidate-action]").forEach((button) => {
                button.addEventListener("click", () => updateCandidateAction(button.dataset.updateCandidateId, button.dataset.updateCandidateAction));
            });
            $("adminDrawerBody")?.querySelectorAll("[data-update-detail-action]").forEach((button) => {
                button.addEventListener("click", () => updateDetailAction(button.dataset.updateId, button.dataset.updateDetailAction));
            });
            $("adminDrawerBody")?.querySelectorAll("[data-update-run-log]").forEach((button) => {
                button.addEventListener("click", () => openUpdateRunLog(button.dataset.updateRunLog));
            });
        }

        function renderPathHealthChecks(checks = []) {
            if (!Array.isArray(checks) || !checks.length) return "";
            return `<ul>${checks.map((check) => `<li>${check.success ? "✅" : "⚠️"} ${escapeHtml(check.message || check.name || "")}${check.path ? `：${escapeHtml(check.path)}` : ""}</li>`).join("")}</ul>`;
        }

        function renderUpdateDetailNotes({ tmdbRetry = {}, tmdbWait = {}, pendingImport = {}, pendingImportStatus = {}, fixedSourceGate = {}, lastRunOutcome = {} } = {}) {
            const notes = [];
            if (lastRunOutcome.reason) {
                notes.push(`${escapeHtml(lastRunOutcome.reason)}${lastRunOutcome.checked_at ? `（${escapeHtml(formatDate(lastRunOutcome.checked_at))}）` : ""}`);
            }
            if (pendingImportStatus.episode || pendingImportStatus.job_id) {
                const episode = pendingImportStatus.episode ? `E${escapeHtml(pendingImportStatus.episode)}` : "该集";
                const jobText = pendingImportStatus.job_id ? `关联任务 #${escapeHtml(pendingImportStatus.job_id)}` : "关联任务";
                const taskText = pendingImportStatus.organizer_task_id ? `，整理任务 #${escapeHtml(pendingImportStatus.organizer_task_id)}` : "";
                const reason = pendingImportStatus.reason || pendingImportStatus.message || "等待整理完成";
                notes.push(`${episode} 已提交但尚未完整入库：${escapeHtml(reason)}（${jobText}${taskText}）。`);
            }
            if (pendingImport.episode && !pendingImportStatus.episode) {
                notes.push(`E${escapeHtml(pendingImport.episode)} 已提交，正在等待整理完成。`);
            }
            if (tmdbWait.reason) {
                notes.push(`等待下一次自动检查；本地最新 E${escapeHtml(tmdbWait.local_latest_episode || "-")}，系统目标 E${escapeHtml(tmdbWait.next_episode || "-")}。`);
            }
            if (tmdbRetry.episode) {
                notes.push(`E${escapeHtml(tmdbRetry.episode)} 暂未找到准确文件，下次会继续自动检查。`);
            }
            if (fixedSourceGate.target_key) {
                notes.push(fixedSourceGate.search_used ? "固定来源未命中，本轮已自动使用综合搜索补漏。" : "固定来源暂未命中目标单集，系统会继续自动检查。");
            }
            if (!notes.length) return "";
            return `<div class="notice-box info"><strong>当前追更状态</strong><ul>${notes.map((note) => `<li>${note}</li>`).join("")}</ul></div>`;
        }

        function renderUpdateSourceSummary(sources = []) {
            const enabled = (sources || []).filter((source) => source.enabled !== false);
            if (!enabled.length) return `<div class="empty">暂无启用来源。</div>`;
            const names = enabled.map((source) => source.name || defaultUpdateSourceName(source.type) || source.type || "-").join("、");
            return `
                <div class="notice-box">
                    <strong>${escapeHtml(names)}</strong>
                    <p class="muted">优先检查已配置来源；未命中时系统会自动补漏。</p>
                </div>
            `;
        }

        function renderUpdateSourceHealth(sourceHealth = {}) {
            const items = Object.values(sourceHealth || {}).filter((item) => item && typeof item === "object" && item.status !== "ok");
            if (!items.length) return `<div class="empty">暂无来源异常。</div>`;
            return `
                <div class="list-box">
                    ${items
                        .map((item) => {
                            const status = item.status || "unknown";
                            const tone = status === "ok" ? "success" : status === "error" ? "warning" : "info";
                            const warn = item.repair_suggested ? " · 建议检查来源链接" : "";
                            return `
                                <div class="list-item">
                                    <div>
                                        <strong>${statusPill(status)} ${escapeHtml(item.name || item.type || "-")}</strong>
                                        <p>${escapeHtml(item.message || "")}${warn}</p>
                                        <p class="muted">${escapeHtml(formatDate(item.last_checked_at) || "")}</p>
                                    </div>
                                </div>
                            `;
                        })
                        .join("")}
                </div>
            `;
        }

        function renderUpdateRunItem(run) {
            const summary = run.summary && typeof run.summary === "object" ? run.summary : {};
            const message = run.error_message || summary.finish_reason || "";
            return `
                <div class="list-item">
                    <div>
                        <strong>#${escapeHtml(run.id)} ${statusPill(run.status)}</strong>
                        <p>${escapeHtml(formatDate(run.started_at))}${message ? ` · ${escapeHtml(message)}` : ""}</p>
                    </div>
                    <button class="secondary mini" data-update-run-log="${escapeHtml(run.id)}">查看</button>
                </div>
            `;
        }

        async function updateDetailAction(id, action) {
            const path = action === "snapshot" ? "refresh-snapshot" : "preview";
            const data = await api(`/api/admin/update-subscriptions/${encodeURIComponent(id)}/${path}`, { method: "POST", body: JSON.stringify({}), allowFailure: true });
            if (action === "snapshot") {
                toast(data.message || `目录状态已同步，识别 ${data.count || 0} 集`, data.success ? "success" : "error");
                await openUpdateDetail(id);
                return;
            }
            const items = data.items || [];
            const errors = data.errors || [];
            openDrawer(
                `来源预览：${escapeHtml(id)}`,
                `
                <div class="notice-box ${errors.length ? "warning" : "info"}">
                    <span>${errors.length ? "存在异常来源" : "检查完成"}</span>
                    <small>只展示本轮目标集；无关结果不会出现在这里。</small>
                </div>
                ${errors.length ? `<div class="notice-box warning"><strong>来源异常</strong><ul>${errors.slice(0, 5).map((item) => `<li>${escapeHtml(item.title || item.source_type || "来源")}：${escapeHtml(item.error || "")}</li>`).join("")}</ul></div>` : ""}
                <div class="list-box">${items.map(renderUpdateCandidateItem).join("") || `<div class="empty">暂无候选</div>`}</div>
                `,
                { mode: "modal" }
            );
        }

        async function openUpdateRunLog(runId) {
            const data = await api(`/api/admin/update-runs/${encodeURIComponent(runId)}`, { allowFailure: true });
            const item = data.item || {};
            const visibleStages = new Set(["start", "scan_existing", "import", "sync_existing", "finish", "failed", "error"]);
            const logs = (Array.isArray(item.run_log) ? item.run_log : []).filter((log) => visibleStages.has(String(log.stage || "")) || log.level === "error");
            openDrawer(
                `追更运行记录 #${escapeHtml(runId)}`,
                `<div class="timeline">${logs.map((log) => `<div class="timeline-item"><strong>${escapeHtml(statusText(log.stage || "-"))}</strong><p>${escapeHtml(log.message || "")}</p><small>${escapeHtml(formatDate(log.created_at) || "")}</small></div>`).join("") || `<div class="empty">本次没有需要处理的目标文件。</div>`}</div>`,
                { mode: "modal" }
            );
        }

        function renderUpdateSourceItem(source) {
            return `<div class="list-item"><div><strong>${escapeHtml(source.name || source.type)}</strong><p>${source.enabled ? "启用" : "停用"} · ${escapeHtml(sourceText(source.type || source.provider || ""))}</p></div></div>`;
        }

        function renderUpdateCandidateItem(item) {
            const canImport = item.decision === "review" || item.decision === "failed";
            const canReject = ["review", "failed", "submitted", "imported"].includes(item.decision || "");
            const raw = item.raw_data || {};
            const candidate = raw.candidate || item;
            const error = item.error || candidate.error || "";
            const episodeText = item.episode || candidate.episode ? `S${escapeHtml(item.season || candidate.season || "-")}E${escapeHtml(item.episode || candidate.episode || "-")}` : "集数待识别";
            const reason = error || item.reason || candidate.decision_hint || "";
            return `
                <div class="list-item">
                    <div>
                        <strong>${escapeHtml(item.title)}</strong>
                        <p>${statusPill(item.decision || (error ? "error" : "candidate"))} · ${episodeText} · ${escapeHtml(sourceText(item.source_type || candidate.source_type || ""))}</p>
                        ${reason ? `<p>${escapeHtml(reason)}</p>` : ""}
                        ${item.job_id ? `<p>关联任务 #${escapeHtml(item.job_id)} ${escapeHtml(statusText(item.job_status || ""))}</p>` : ""}
                    </div>
                    <div class="table-actions">
                        ${canImport ? `<button class="mini" data-update-candidate-id="${escapeHtml(item.id)}" data-update-candidate-action="import">入库</button>` : ""}
                        ${canReject ? `<button class="secondary mini danger" data-update-candidate-id="${escapeHtml(item.id)}" data-update-candidate-action="reject">拒绝</button>` : ""}
                    </div>
                </div>
            `;
        }

        async function updateCandidateAction(id, action) {
            const path = action === "import" ? "import" : "reject";
            const data = await api(`/api/admin/update-candidates/${encodeURIComponent(id)}/${path}`, {
                method: "POST",
                body: JSON.stringify(action === "reject" ? { reason: "管理员拒绝候选" } : {}),
                allowFailure: true,
            });
            toast(data.message || "候选已处理", data.success ? "success" : "error");
            closeDrawer();
            await loadUpdates();
        }

        async function runDueUpdates() {
            const data = await api("/api/admin/update-scheduler/run-due", {
                method: "POST",
                body: JSON.stringify({ limit: 10 }),
                allowFailure: true,
            });
            toast(data.message || `已运行 ${data.count || 0} 个到期追更`, data.success ? "success" : "error");
            await loadUpdates();
        }

        async function openUpdateEditor(id = "") {
            let item = {};
            if (id) {
                const data = await api(`/api/admin/update-subscriptions/${encodeURIComponent(id)}`);
                item = data.item || {};
            }
            const isEditing = Boolean(id);
            const sources = item.sources || [{ type: "search", name: "综合搜索", enabled: true, priority: 100, options: {} }];
            const minScore = Number(item.min_score || 75);
            const initialCategory = item.category || "anime";
            const initialMediaType = item.tmdb_id ? (item.media_type || updateExpectedMediaType(initialCategory)) : updateExpectedMediaType(initialCategory);
            const rawData = item.raw_data && typeof item.raw_data === "object" ? item.raw_data : {};
            const rootResolution = rawData.canonical_root_resolution && typeof rawData.canonical_root_resolution === "object" ? rawData.canonical_root_resolution : {};
            const initialOpenlistPath = rawData.openlist_path || rawData.canonical_openlist_root || rootResolution.canonical_openlist_root || "";
            openDrawer(
                isEditing ? "编辑定时追更" : "新建定时追更",
                `
                <div class="update-editor" data-update-require-tmdb="1">
                    <section class="update-editor-section update-tmdb-card">
                        <div class="update-editor-section-title">
                            <strong>选择影视</strong>
                        </div>
                        <div class="update-tmdb-toolbar update-tmdb-toolbar-main">
                            <label class="advanced-field update-category-field"><span>分类</span><select id="updateEditCategory">
                                ${ADVANCED_CATEGORY_ORDER.map(([key, label]) => `<option value="${escapeHtml(key)}" ${initialCategory === key ? "selected" : ""}>${escapeHtml(label)}</option>`).join("")}
                            </select></label>
                            <input id="updateTmdbQuery" type="text" value="${escapeHtml(item.title || "")}" placeholder="搜索片名 / 剧名">
                            <button class="secondary mini" type="button" id="updateTmdbSearchBtn">搜索 TMDB</button>
                            <button class="secondary mini update-tmdb-clear" type="button" id="clearUpdateTmdbBtn">清除</button>
                        </div>
                        <input id="updateEditTitle" type="hidden" value="${escapeHtml(item.title || "")}">
                        <input id="updateEditStatus" type="hidden" value="${escapeHtml(item.status || "enabled")}">
                        <input id="updateEditTmdbId" type="hidden" value="${escapeHtml(item.tmdb_id || "")}">
                        <input id="updateEditMediaType" type="hidden" value="${escapeHtml(initialMediaType)}">
                        <input id="updateEditYear" type="hidden" value="${escapeHtml(item.year || "")}">
                        <div id="updateTmdbSelected" class="update-tmdb-selected muted">${renderUpdateTmdbSelectedText(item, initialCategory, initialMediaType, { requireTmdb: true })}</div>
                        <div id="updateTmdbResults" class="list-box update-tmdb-results" style="display:none;"></div>
                    </section>

                    <section class="update-editor-section update-episode-section" data-update-section="episode">
                        <div class="update-editor-section-title">
                            <strong>剧集进度</strong>
                        </div>
                        <div class="update-editor-grid">
                            <label class="advanced-field" data-update-field="season"><span>季号</span><input id="updateEditSeason" type="number" min="1" value="${escapeHtml(item.season || "")}" placeholder="例如 1"></label>
                            <label class="advanced-field" data-update-field="next-episode"><span>目标/当前集</span><input id="updateEditNextEpisode" type="number" min="1" value="${escapeHtml(item.next_episode || "")}" placeholder="例如 6，首轮补齐 1-6"></label>
                        </div>
                    </section>

                    <section class="update-editor-section update-source-primary">
                        <div class="update-editor-section-title">
                            <strong>追更来源</strong>
                        </div>
                        ${renderUpdateSourceEditor(sources)}
                    </section>

                    <details class="advanced-details update-editor-details update-schedule-details" data-update-section="schedule">
                        <summary><span id="updateScheduleSummary">检查时间</span></summary>
                        <p id="updateScheduleHelp" class="update-section-help"></p>
                        <div class="update-editor-grid">
                            <label class="advanced-field" data-update-field="days"><span>每周星期</span><input id="updateEditDays" type="text" value="${escapeHtml((item.days_of_week || [5]).join(","))}" placeholder="1=周一，5=周五"></label>
                            ${renderUpdateTimePicker(item.time_of_day || "10:00")}
                        </div>
                    </details>

                    <details class="advanced-details update-editor-details update-save-path-details" data-update-section="save-path">
                        <summary>保存目录（可选）</summary>
                        <div class="update-openlist-picker">
                            <label class="advanced-field"><span>既有资源目录</span><input id="updateEditOpenlistPath" type="text" value="${escapeHtml(initialOpenlistPath)}" placeholder="自动识别，或点击浏览选择"></label>
                            <button class="secondary mini" type="button" id="browseUpdateOpenlistBtn">浏览目录</button>
                            <div id="updateOpenlistDirList" class="list-box update-openlist-dir-list" style="display:none;"></div>
                        </div>
                    </details>

                    <input id="updateEditMinScore" type="hidden" value="${escapeHtml(minScore)}">
                </div>
                <div class="form-actions" style="margin-top:16px;"><button type="button" id="saveUpdateSubscriptionBtn">${isEditing ? "保存" : "创建追更"}</button></div>
                `,
                { mode: "modal", className: "update-editor-modal" }
            );
            $("saveUpdateSubscriptionBtn")?.addEventListener("click", () => saveUpdateSubscription(id).catch((error) => toast(error.message, "error")));
            $("updateTmdbSearchBtn")?.addEventListener("click", () => searchUpdateTmdb().catch((error) => toast(error.message, "error")));
            $("updateTmdbQuery")?.addEventListener("keydown", (event) => {
                if (event.key === "Enter") {
                    event.preventDefault();
                    searchUpdateTmdb().catch((error) => toast(error.message, "error"));
                }
            });
            $("clearUpdateTmdbBtn")?.addEventListener("click", () => clearUpdateTmdbSelection());
            $("updateEditCategory")?.addEventListener("change", handleUpdateCategoryChange);
            $("browseUpdateOpenlistBtn")?.addEventListener("click", () => browseUpdateOpenlistDirs().catch((error) => toast(error.message, "error")));
            bindUpdateEditorInteractions();
            syncUpdateEditorVisibility();
            document.querySelectorAll("#adminDrawer select").forEach((select) => window.syncCustomSelect?.(select));
        }

        const UPDATE_SOURCE_TYPES = [
            { value: "search", label: "综合搜索", help: "不填链接时自动搜索。" },
            { value: "quark", label: "夸克链接", help: "从夸克分享链接检查更新。" },
            { value: "cloud139", label: "139 链接", help: "从 139 移动云分享链接检查更新。" },
        ];

        function isUpdateEpisodicCategory(category, mediaType = "") {
            const categoryValue = String(category || "").toLowerCase();
            const typeValue = String(mediaType || "").toLowerCase();
            return categoryValue !== "movie" && typeValue !== "movie" && ["tv", "anime", "variety"].includes(categoryValue);
        }

        function updateExpectedMediaType(category) {
            return String(category || "") === "movie" ? "movie" : "tv";
        }

        function renderUpdateTmdbSelectedText(item = {}, category = "", mediaType = "", options = {}) {
            const tmdbId = item.tmdb_id || "";
            const typeValue = mediaType || item.media_type || updateExpectedMediaType(category || item.category);
            if (!tmdbId) {
                return options.requireTmdb ? "请选择 TMDB 影视" : "未绑定 TMDB";
            }
            const titleText = item.title ? `${escapeHtml(item.title)}${item.year ? ` (${escapeHtml(item.year)})` : ""} · ` : "";
            if (typeValue === "movie") {
                return `${titleText}TMDB #${escapeHtml(tmdbId)}`;
            }
            return `${titleText}TMDB #${escapeHtml(tmdbId)}`;
        }

        function isUpdateTmdbRequiredMode() {
            return document.querySelector(".update-editor")?.dataset?.updateRequireTmdb === "1";
        }

        function updateEditorState() {
            const category = $("updateEditCategory")?.value || "movie";
            const tmdbId = Number($("updateEditTmdbId")?.value || 0) || null;
            const storedMediaType = $("updateEditMediaType")?.value || "";
            const mediaType = tmdbId ? (storedMediaType || updateExpectedMediaType(category)) : updateExpectedMediaType(category);
            return {
                category,
                tmdbId,
                mediaType,
                isEpisodic: isUpdateEpisodicCategory(category, mediaType),
            };
        }

        function syncUpdateEditorVisibility() {
            const state = updateEditorState();
            const requireTmdb = isUpdateTmdbRequiredMode();
            const episodeSection = document.querySelector("[data-update-section='episode']");
            const scheduleDetails = document.querySelector("[data-update-section='schedule']");
            const daysField = document.querySelector("[data-update-field='days']");
            const selected = $("updateTmdbSelected");
            const scheduleSummary = $("updateScheduleSummary");
            const scheduleHelp = $("updateScheduleHelp");
            const timeLabel = document.querySelector(".update-time-field > span");
            const clearButton = $("clearUpdateTmdbBtn");
            const titleInput = $("updateEditTitle");
            const queryInput = $("updateTmdbQuery");

            const hideDaysField = state.isEpisodic && state.tmdbId && state.mediaType === "tv";
            episodeSection?.classList.toggle("hidden", !state.isEpisodic);
            daysField?.classList.toggle("hidden", hideDaysField);
            scheduleDetails?.classList.toggle("time-only", hideDaysField);
            clearButton?.classList.toggle("hidden", !state.tmdbId);
            if (titleInput && queryInput && !queryInput.value.trim()) queryInput.value = titleInput.value || "";

            if (selected && !state.tmdbId) {
                selected.textContent = requireTmdb ? "请选择 TMDB 影视" : "未绑定 TMDB";
            }
            if (scheduleSummary) {
                scheduleSummary.textContent = state.isEpisodic && state.tmdbId && state.mediaType === "tv"
                    ? "播出日检查时间"
                    : "兜底检查时间（可选）";
            }
            if (timeLabel) {
                timeLabel.textContent = state.isEpisodic && state.tmdbId && state.mediaType === "tv" ? "播出日检查时间" : "检查时间";
            }
            if (scheduleHelp) {
                if (!state.isEpisodic) {
                    scheduleHelp.textContent = "按固定时间检查。";
                } else if (state.tmdbId && state.mediaType === "tv") {
                    scheduleHelp.textContent = "按播出日检查。";
                } else {
                    scheduleHelp.textContent = requireTmdb ? "请选择 TMDB 影视。" : "按每周时间检查。";
                }
            }
            if (scheduleDetails) {
                scheduleDetails.open = state.isEpisodic;
            }
        }

        function clearUpdateTmdbSelection(message = "") {
            const category = $("updateEditCategory")?.value || "movie";
            const requireTmdb = isUpdateTmdbRequiredMode();
            if ($("updateEditTmdbId")) $("updateEditTmdbId").value = "";
            if ($("updateEditMediaType")) $("updateEditMediaType").value = updateExpectedMediaType(category);
            if ($("updateEditYear")) $("updateEditYear").value = "";
            if (requireTmdb && $("updateEditTitle")) $("updateEditTitle").value = "";
            const selected = $("updateTmdbSelected");
            if (selected) {
                selected.textContent = message || (requireTmdb
                    ? "请选择 TMDB 影视。"
                    : "未绑定 TMDB");
            }
            const box = $("updateTmdbResults");
            if (box) {
                box.style.display = "none";
                box.innerHTML = "";
            }
            syncUpdateEditorVisibility();
        }

        function handleUpdateCategoryChange() {
            const category = $("updateEditCategory")?.value || "movie";
            const expectedMediaType = updateExpectedMediaType(category);
            const currentMediaType = $("updateEditMediaType")?.value || "";
            const tmdbId = Number($("updateEditTmdbId")?.value || 0) || null;
            if (tmdbId && currentMediaType && currentMediaType !== expectedMediaType) {
                clearUpdateTmdbSelection("分类已切换，原 TMDB 类型不匹配，请重新搜索并绑定。");
                return;
            }
            if (!tmdbId && $("updateEditMediaType")) $("updateEditMediaType").value = expectedMediaType;
            syncUpdateEditorVisibility();
        }

        async function browseUpdateOpenlistDirs(path = "") {
            const input = $("updateEditOpenlistPath");
            const box = $("updateOpenlistDirList");
            if (!box) return;
            const targetPath = path || input?.value?.trim() || "/";
            box.style.display = "";
            box.innerHTML = `<div class="empty">正在读取 ${escapeHtml(targetPath)} ...</div>`;
            const data = await api(`/api/admin/openlist/dirs?path=${encodeURIComponent(targetPath)}`, { allowFailure: true });
            if (!data.success) {
                box.innerHTML = `<div class="empty">${escapeHtml(data.message || "目录读取失败")}</div>`;
                return;
            }
            const parent = parentOpenlistPath(data.path || "/");
            const items = Array.isArray(data.items) ? data.items : [];
            box.innerHTML = `
                <div class="list-item">
                    <div><strong>${escapeHtml(data.path || "/")}</strong><p>点击目录名称进入；点击“选此目录”作为追更保存目录。</p></div>
                    <button class="secondary mini" type="button" data-update-openlist-select="${escapeHtml(data.path || "/")}">选此目录</button>
                </div>
                ${parent ? `<div class="list-item"><div><strong>..</strong><p>返回上级目录</p></div><button class="secondary mini" type="button" data-update-openlist-enter="${escapeHtml(parent)}">上级</button></div>` : ""}
                ${items.map((item) => `
                    <div class="list-item">
                        <div><strong>${escapeHtml(item.name || item.path || "-")}</strong><p>${escapeHtml(item.path || "")}</p></div>
                        <div class="table-actions">
                            <button class="secondary mini" type="button" data-update-openlist-enter="${escapeHtml(item.path || "")}">进入</button>
                            <button class="mini" type="button" data-update-openlist-select="${escapeHtml(item.path || "")}">选择</button>
                        </div>
                    </div>
                `).join("") || `<div class="empty">这个目录下没有子目录，可直接选择当前目录。</div>`}
            `;
            box.querySelectorAll("[data-update-openlist-enter]").forEach((button) => {
                button.addEventListener("click", () => browseUpdateOpenlistDirs(button.dataset.updateOpenlistEnter).catch((error) => toast(error.message, "error")));
            });
            box.querySelectorAll("[data-update-openlist-select]").forEach((button) => {
                button.addEventListener("click", () => {
                    if (input) input.value = button.dataset.updateOpenlistSelect || "";
                    box.style.display = "none";
                });
            });
        }

        function parentOpenlistPath(path) {
            const parts = String(path || "/").replace(/\\/g, "/").replace(/^\/+|\/+$/g, "").split("/").filter(Boolean);
            if (!parts.length) return "";
            parts.pop();
            return parts.length ? `/${parts.join("/")}` : "/";
        }

        function renderUpdateTimePicker(value) {
            const parts = parseTimeParts(value);
            return `
                <div class="advanced-field update-time-field" data-update-field="time">
                    <span>播出日检查时间</span>
                    <div class="update-time-picker" id="updateEditTimePicker">
                        <select id="updateEditHour" aria-label="更新时间小时">
                            ${Array.from({ length: 24 }, (_, hour) => {
                                const text = String(hour).padStart(2, "0");
                                return `<option value="${text}" ${parts.hour === text ? "selected" : ""}>${text} 点</option>`;
                            }).join("")}
                        </select>
                        <em>:</em>
                        <select id="updateEditMinute" aria-label="更新时间分钟">
                            ${updateMinuteOptions(parts.minute).map((minute) => `<option value="${minute}" ${parts.minute === minute ? "selected" : ""}>${minute} 分</option>`).join("")}
                        </select>
                    </div>
                </div>
            `;
        }

        function parseTimeParts(value) {
            const match = String(value || "").match(/^(\d{1,2}):(\d{1,2})/);
            const hour = Math.min(23, Math.max(0, Number(match?.[1] ?? 10)));
            const minute = Math.min(59, Math.max(0, Number(match?.[2] ?? 0)));
            return { hour: String(hour).padStart(2, "0"), minute: String(minute).padStart(2, "0") };
        }

        function updateMinuteOptions(selected = "00") {
            const options = new Set(Array.from({ length: 12 }, (_, index) => String(index * 5).padStart(2, "0")));
            options.add(String(selected || "00").padStart(2, "0"));
            return Array.from(options).sort((a, b) => Number(a) - Number(b));
        }

        function readUpdateTimeValue() {
            const hour = $("updateEditHour")?.value || "10";
            const minute = $("updateEditMinute")?.value || "00";
            return `${hour.padStart(2, "0")}:${minute.padStart(2, "0")}`;
        }

        function renderUpdateSourceEditor(sources) {
            const normalized = normalizeUpdateSources(sources);
            return `
                <section class="update-source-editor" aria-label="追更来源配置">
                    <div class="update-source-list" id="updateSourceList">
                        ${normalized.map((source) => renderUpdateSourceCard(source)).join("")}
                    </div>
                    <button class="secondary mini update-source-add" type="button" id="addUpdateSourceBtn">添加来源</button>
                </section>
            `;
        }

        function normalizeUpdateSources(sources) {
            const list = Array.isArray(sources) ? sources.filter((source) => source && typeof source === "object") : [];
            return list.length ? list : [{ type: "search", name: "综合搜索", enabled: true, priority: 100, options: {} }];
        }

        function renderUpdateSourceCard(source = {}) {
            const type = updateSourceType(source.type);
            return `
                <article class="update-source-card" data-update-source-card>
                    <div class="update-source-head">
                        <label class="advanced-field update-source-type-field">
                            <span>来源类型</span>
                            <select data-update-source-type>
                                ${UPDATE_SOURCE_TYPES.map((item) => `<option value="${escapeHtml(item.value)}" ${type === item.value ? "selected" : ""}>${escapeHtml(item.label)}</option>`).join("")}
                            </select>
                        </label>
                        <button class="secondary mini update-source-remove" type="button" data-update-source-remove>移除</button>
                    </div>
                    <div class="update-source-fields" data-update-source-fields>${renderUpdateSourceFields({ ...source, type })}</div>
                </article>
            `;
        }

        function updateSourceType(type) {
            const value = String(type || "search").trim().toLowerCase();
            return UPDATE_SOURCE_TYPES.some((item) => item.value === value) ? value : "search";
        }

        function updateSourceHelp(type) {
            return UPDATE_SOURCE_TYPES.find((item) => item.value === updateSourceType(type))?.help || "";
        }

        function renderUpdateSourceFields(source = {}) {
            const type = updateSourceType(source.type);
            const options = source.options || {};
            const idValue = type === "cloud139" ? options.folder_id : options.fid;
            if (type === "search") {
                return `
                    <label class="advanced-field"><span>分享链接</span><input data-update-source-url type="url" value="${escapeHtml(source.url || "")}" placeholder="可不填；粘贴夸克/139 链接会自动识别"></label>
                    <input data-update-source-password type="hidden" value="">
                    <input data-update-source-resource-id type="hidden" value="">
                `;
            }
            return `
                <label class="advanced-field"><span>分享链接</span><input data-update-source-url type="url" value="${escapeHtml(source.url || "")}" placeholder="粘贴分享链接"></label>
                <label class="advanced-field"><span>提取码</span><input data-update-source-password type="text" value="${escapeHtml(source.password || "")}" placeholder="没有可不填"></label>
                <input data-update-source-resource-id type="hidden" value="${escapeHtml(idValue || "")}">
            `;
        }

        function bindUpdateEditorInteractions() {
            $("addUpdateSourceBtn")?.addEventListener("click", () => {
                const list = $("updateSourceList");
                if (!list) return;
                list.insertAdjacentHTML("beforeend", renderUpdateSourceCard({ type: "search", name: "综合搜索", enabled: true, priority: 100, options: {} }));
                const card = list.querySelector("[data-update-source-card]:last-child");
                bindUpdateSourceCard(card);
                syncUpdateSourceRemoveButtons();
                card?.querySelectorAll("select").forEach((select) => window.syncCustomSelect?.(select));
            });
            document.querySelectorAll("[data-update-source-card]").forEach(bindUpdateSourceCard);
            syncUpdateSourceRemoveButtons();
        }

        function bindUpdateSourceCard(card) {
            if (!card || card.__updateSourceBound) return;
            card.__updateSourceBound = true;
            card.querySelector("[data-update-source-type]")?.addEventListener("change", (event) => {
                setUpdateSourceCardType(card, updateSourceType(event.currentTarget.value));
            });
            card.querySelector("[data-update-source-fields]")?.addEventListener("input", (event) => {
                if (!event.target?.matches?.("[data-update-source-url]")) return;
                const select = card.querySelector("[data-update-source-type]");
                if (updateSourceType(select?.value || "search") !== "search") return;
                const detected = detectUpdateSourceTypeFromUrl(event.target.value || "");
                if (detected) setUpdateSourceCardType(card, detected, { url: event.target.value || "" });
            });
            card.querySelector("[data-update-source-remove]")?.addEventListener("click", () => {
                const list = $("updateSourceList");
                if (!list) return;
                const cards = list.querySelectorAll("[data-update-source-card]");
                if (cards.length <= 1) {
                    setUpdateSourceCardType(card, "search");
                    return;
                }
                card.remove();
                syncUpdateSourceRemoveButtons();
            });
        }

        function setUpdateSourceCardType(card, type, seed = {}) {
            const normalizedType = updateSourceType(type);
            const select = card?.querySelector("[data-update-source-type]");
            const fields = card?.querySelector("[data-update-source-fields]");
            if (select) select.value = normalizedType;
            if (fields) {
                fields.innerHTML = renderUpdateSourceFields({
                    type: normalizedType,
                    name: defaultUpdateSourceName(normalizedType),
                    url: seed.url || "",
                    password: seed.password || "",
                    options: seed.options || {},
                });
            }
            card?.querySelectorAll("select").forEach((selectNode) => window.syncCustomSelect?.(selectNode));
        }

        function detectUpdateSourceTypeFromUrl(url) {
            const value = String(url || "").trim().toLowerCase();
            if (!value) return "";
            if (value.includes("pan.quark.cn") || value.includes("drive.uc.cn")) return "quark";
            if (value.includes("yun.139.com") || value.includes("caiyun.139.com") || value.includes("cloud.139.com")) return "cloud139";
            return "";
        }

        function syncUpdateSourceRemoveButtons() {
            const cards = document.querySelectorAll("[data-update-source-card]");
            cards.forEach((card) => {
                const button = card.querySelector("[data-update-source-remove]");
                if (button) button.disabled = cards.length <= 1;
            });
        }

        function defaultUpdateSourceName(type) {
            if (type === "quark") return "夸克分享";
            if (type === "cloud139") return "139 分享";
            return "综合搜索";
        }

        function sourceToLine(source) {
            const options = source.options || {};
            return [source.type || "search", source.name || "", source.url || "", source.password || "", options.fid || options.folder_id || ""].join("|").replace(/\|+$/, "");
        }

        function parseUpdateSourceLines(value) {
            return String(value || "").split(/\n+/).map((line) => {
                const parts = line.split("|").map((item) => item.trim());
                if (!parts[0]) return null;
                const options = {};
                if (parts[4]) {
                    if (parts[0] === "cloud139") options.folder_id = parts[4];
                    else options.fid = parts[4];
                }
                return { type: parts[0], name: parts[1] || parts[0], url: parts[2] || "", password: parts[3] || "", enabled: true, priority: parts[0] === "search" ? 100 : 10, options };
            }).filter(Boolean);
        }

        function collectUpdateSources() {
            const cards = Array.from(document.querySelectorAll("[data-update-source-card]"));
            return cards.map((card) => {
                const selectedType = updateSourceType(card.querySelector("[data-update-source-type]")?.value || "search");
                const url = card.querySelector("[data-update-source-url]")?.value?.trim() || "";
                const password = card.querySelector("[data-update-source-password]")?.value?.trim() || "";
                const resourceId = card.querySelector("[data-update-source-resource-id]")?.value?.trim() || "";
                const detectedType = selectedType === "search" ? detectUpdateSourceTypeFromUrl(url) : "";
                const type = detectedType || selectedType;
                const name = defaultUpdateSourceName(type);
                const options = {};
                if (resourceId) {
                    if (type === "cloud139") options.folder_id = resourceId;
                    else options.fid = resourceId;
                }
                return {
                    type,
                    name,
                    url: type === "search" ? "" : url,
                    password: type === "search" ? "" : password,
                    enabled: true,
                    priority: type === "search" ? 100 : 10,
                    options,
                };
            }).filter(Boolean);
        }

        async function searchUpdateTmdb() {
            const query = $("updateTmdbQuery")?.value?.trim() || $("updateEditTitle")?.value?.trim() || "";
            if (!query) {
                toast("请输入片名或剧名搜索 TMDB", "error");
                $("updateTmdbQuery")?.focus();
                return;
            }
            const category = $("updateEditCategory")?.value || "movie";
            const mediaType = category === "movie" ? "movie" : "tv";
            const box = $("updateTmdbResults");
            if (box) {
                box.style.display = "";
                box.innerHTML = `<div class="empty">正在搜索 TMDB...</div>`;
            }
            const data = await api(`/api/admin/tmdb/search?query=${encodeURIComponent(query)}&media_type=${encodeURIComponent(mediaType)}`, { allowFailure: true });
            const items = Array.isArray(data.items) ? data.items : [];
            if (!box) return;
            if (!data.success || !items.length) {
                box.innerHTML = `<div class="empty">${escapeHtml(data.message || "未找到 TMDB 候选，请检查 Token 或换个标题")}</div>`;
                return;
            }
            box.innerHTML = items.map((item) => `
                <div class="list-item">
                    <div>
                        <strong>${escapeHtml(item.title || "-")} ${item.year ? `(${escapeHtml(item.year)})` : ""}</strong>
                        <p>${escapeHtml(item.media_type || "-")} · TMDB #${escapeHtml(item.id || "-")} · ${escapeHtml(item.overview || "")}</p>
                    </div>
                    <button class="secondary mini" type="button" data-update-tmdb-pick="${escapeHtml(item.id)}" data-update-tmdb-title="${escapeHtml(item.title || "")}" data-update-tmdb-year="${escapeHtml(item.year || "")}" data-update-tmdb-media-type="${escapeHtml(item.media_type || mediaType)}">选择此影视</button>
                </div>
            `).join("");
            box.querySelectorAll("[data-update-tmdb-pick]").forEach((button) => {
                button.addEventListener("click", () => selectUpdateTmdb(button.dataset).catch((error) => toast(error.message, "error")));
            });
        }

        async function selectUpdateTmdb(dataset) {
            const id = dataset.updateTmdbPick || "";
            const title = dataset.updateTmdbTitle || "";
            const year = dataset.updateTmdbYear || "";
            const mediaType = dataset.updateTmdbMediaType || "tv";
            const category = $("updateEditCategory")?.value || "movie";
            const requireTmdb = isUpdateTmdbRequiredMode();
            if ($("updateEditTmdbId")) $("updateEditTmdbId").value = id;
            if ($("updateEditMediaType")) $("updateEditMediaType").value = mediaType;
            if ($("updateEditYear")) $("updateEditYear").value = year;
            if (title && $("updateEditTitle")) $("updateEditTitle").value = title;
            if (title && $("updateTmdbQuery")) $("updateTmdbQuery").value = title;
            const selected = $("updateTmdbSelected");
            if (selected) selected.textContent = `已选择 ${title || "TMDB"} ${year ? `(${year})` : ""} · #${id}，正在读取详情...`;
            let resolvedTitle = title;
            let resolvedYear = year;
            if (mediaType === "tv" && id) {
                const detail = await api(`/api/admin/tmdb/${encodeURIComponent(mediaType)}/${encodeURIComponent(id)}`, { allowFailure: true });
                resolvedTitle = detail.item?.title || resolvedTitle;
                resolvedYear = detail.item?.year || resolvedYear;
                const nextAir = detail.item?.next_episode_to_air || {};
                const lastAir = detail.item?.last_episode_to_air || {};
                const seasonNumber = Number(nextAir.season_number || lastAir.season_number || 0);
                const episodeNumber = Number(nextAir.episode_number || 0);
                if (resolvedTitle && $("updateEditTitle")) $("updateEditTitle").value = resolvedTitle;
                if (resolvedTitle && $("updateTmdbQuery")) $("updateTmdbQuery").value = resolvedTitle;
                if (resolvedYear && $("updateEditYear")) $("updateEditYear").value = resolvedYear;
                if (seasonNumber && $("updateEditSeason") && (requireTmdb || !$("updateEditSeason").value)) $("updateEditSeason").value = String(seasonNumber);
                if (episodeNumber && $("updateEditNextEpisode") && (requireTmdb || !$("updateEditNextEpisode").value)) $("updateEditNextEpisode").value = String(episodeNumber);
                if (selected) {
                    const nextText = episodeNumber ? `下一集 S${seasonNumber || "-"}E${episodeNumber}，播出日 ${nextAir.air_date || "待定"}` : "TMDB 暂无下一集日期";
                    selected.textContent = `已选择 ${resolvedTitle || "TMDB"} ${resolvedYear ? `(${resolvedYear})` : ""} · #${id}，${nextText}。`;
                }
            } else if (selected) {
                if (id) {
                    const detail = await api(`/api/admin/tmdb/${encodeURIComponent(mediaType)}/${encodeURIComponent(id)}`, { allowFailure: true });
                    resolvedTitle = detail.item?.title || resolvedTitle;
                    resolvedYear = detail.item?.year || resolvedYear;
                    if (resolvedTitle && $("updateEditTitle")) $("updateEditTitle").value = resolvedTitle;
                    if (resolvedTitle && $("updateTmdbQuery")) $("updateTmdbQuery").value = resolvedTitle;
                    if (resolvedYear && $("updateEditYear")) $("updateEditYear").value = resolvedYear;
                }
                selected.textContent = `已选择 ${resolvedTitle || "TMDB"} ${resolvedYear ? `(${resolvedYear})` : ""} · #${id}，${category === "movie" ? "用于标题和年份识别。" : "将按兜底时间检查资源。"}`;
            }
            const box = $("updateTmdbResults");
            if (box) box.style.display = "none";
            syncUpdateEditorVisibility();
        }

        async function saveUpdateSubscription(id = "") {
            const tmdbId = Number($("updateEditTmdbId")?.value || 0) || null;
            const category = $("updateEditCategory")?.value || "movie";
            const selectedMediaType = $("updateEditMediaType")?.value || "";
            const mediaType = tmdbId ? (selectedMediaType || (category === "movie" ? "movie" : "tv")) : (category === "movie" ? "movie" : "tv");
            const isEpisodic = isUpdateEpisodicCategory(category, mediaType);
            const openlistPath = $("updateEditOpenlistPath")?.value?.trim() || "";
            if (!tmdbId) {
                toast("请先从 TMDB 搜索并选择要追更的影视", "error");
                $("updateTmdbQuery")?.focus();
                return;
            }
            const payload = {
                title: $("updateEditTitle")?.value?.trim() || "",
                category,
                media_type: mediaType,
                tmdb_id: tmdbId,
                year: $("updateEditYear")?.value || "",
                season: isEpisodic ? (Number($("updateEditSeason")?.value || 0) || null) : null,
                next_episode: isEpisodic ? (Number($("updateEditNextEpisode")?.value || 0) || null) : null,
                schedule_kind: tmdbId && mediaType === "tv" ? "tmdb" : "weekly",
                days_of_week: parseNumberList($("updateEditDays")?.value || "5"),
                time_of_day: readUpdateTimeValue(),
                aliases: textToList($("updateEditAliases")?.value || ""),
                exclude_keywords: textToList($("updateEditExclude")?.value || ""),
                min_score: Number($("updateEditMinScore")?.value || 75),
                status: $("updateEditStatus")?.value || "enabled",
                sources: collectUpdateSources(),
            };
            if (openlistPath) {
                payload.raw_data = { openlist_path: openlistPath, canonical_openlist_root: openlistPath };
            }
            const data = await api(id ? `/api/admin/update-subscriptions/${encodeURIComponent(id)}` : "/api/admin/update-subscriptions", {
                method: id ? "PUT" : "POST",
                body: JSON.stringify(payload),
                allowFailure: true,
            });
            if (!data.success) {
                toast(data.message || "保存追更订阅失败", "error");
                return;
            }
            toast(data.message || "追更订阅已保存", "success");
            closeDrawer();
            await loadUpdates();
        }

        return Object.freeze({
            loadUpdates,
            renderUpdates,
            renderUpdateSubscriptionRow,
            updateSubscriptionAction,
            openUpdateDetail,
            renderPathHealthChecks,
            renderUpdateDetailNotes,
            renderUpdateSourceSummary,
            renderUpdateSourceHealth,
            renderUpdateRunItem,
            updateDetailAction,
            openUpdateRunLog,
            renderUpdateSourceItem,
            renderUpdateCandidateItem,
            updateCandidateAction,
            runDueUpdates,
            openUpdateEditor,
            isUpdateEpisodicCategory,
            updateExpectedMediaType,
            renderUpdateTmdbSelectedText,
            isUpdateTmdbRequiredMode,
            updateEditorState,
            syncUpdateEditorVisibility,
            clearUpdateTmdbSelection,
            handleUpdateCategoryChange,
            browseUpdateOpenlistDirs,
            parentOpenlistPath,
            renderUpdateTimePicker,
            parseTimeParts,
            updateMinuteOptions,
            readUpdateTimeValue,
            renderUpdateSourceEditor,
            normalizeUpdateSources,
            renderUpdateSourceCard,
            updateSourceType,
            updateSourceHelp,
            renderUpdateSourceFields,
            bindUpdateEditorInteractions,
            bindUpdateSourceCard,
            setUpdateSourceCardType,
            detectUpdateSourceTypeFromUrl,
            syncUpdateSourceRemoveButtons,
            defaultUpdateSourceName,
            sourceToLine,
            parseUpdateSourceLines,
            collectUpdateSources,
            searchUpdateTmdb,
            selectUpdateTmdb,
            saveUpdateSubscription,
        });
    }

    window.FnosAdminUpdates = Object.freeze({ create });
})();
