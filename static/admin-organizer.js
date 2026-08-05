(function () {
    function create(context) {
        const {
            state,
            getElement,
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
        } = context;
        const adminState = state;
        const $ = getElement;

        async function loadOrganizer() {
            const status = $("organizerStatusFilter")?.value || "";
            const page = adminState.pagination.organizerTasks.page || 1;
            const perPage = adminState.pagination.organizerTasks.per_page || 50;
            const query = new URLSearchParams({ page: String(page), per_page: String(perPage) });
            if (status) query.set("status", status);
            const [tasksData, runsData] = await Promise.all([
                api(`/api/admin/organizer/tasks?${query.toString()}`),
                api(`/api/admin/organizer/runs?page=${encodeURIComponent(adminState.pagination.organizerRuns.page || 1)}&per_page=${encodeURIComponent(adminState.pagination.organizerRuns.per_page || 30)}`),
            ]);
            adminState.organizerTasks = tasksData.items || [];
            adminState.organizerRuns = runsData.items || [];
            adminState.pagination.organizerTasks = normalizePagination(tasksData.pagination, { page, per_page: perPage });
            adminState.pagination.organizerRuns = normalizePagination(runsData.pagination, adminState.pagination.organizerRuns);
            adminState.organizerStatus = tasksData.status || {};
            renderOrganizer();
        }

        function renderOrganizer() {
            renderOrganizerStatus();
            renderOrganizerTasks();
            renderOrganizerRuns();
        }

        function renderOrganizerStatus() {
            const box = $("organizerStatusBox");
            if (!box) return;
            const status = adminState.organizerStatus || {};
            const enabled = Boolean(status.enabled);
            const ok = enabled && status.openlist_configured;
            box.className = `notice-box ${ok ? "status-ok" : enabled ? "warning" : ""}`;
            const missing = [];
            if (!status.openlist_configured) missing.push("OpenList");
            if (!status.tmdb_configured) missing.push("TMDB");
            box.innerHTML = `
                <strong>标准化${enabled ? "已启用" : "未启用"}</strong>
                <p>${enabled ? "入库完成后会自动整理文件名和目录。" : "开启后可自动整理入库后的文件。"}${missing.length ? ` 需配置：${escapeHtml(missing.join("、"))}` : ""}</p>
            `;
        }

        function renderOrganizerTasks() {
            const body = $("organizerTasksBody");
            if (!body) return;
            if (!adminState.organizerTasks.length) {
                body.innerHTML = `<tr><td colspan="7" class="empty-cell">暂无标准化任务</td></tr>`;
                renderPager("organizerTasksPager", adminState.pagination.organizerTasks, (page) => {
                    adminState.pagination.organizerTasks.page = page;
                    loadOrganizer().catch((error) => toast(error.message, "error"));
                });
                return;
            }
            body.innerHTML = adminState.organizerTasks.map(renderOrganizerTaskRow).join("");
            body.querySelectorAll("[data-organizer-detail]").forEach((button) => button.addEventListener("click", () => showOrganizerTask(button.dataset.organizerDetail)));
            body.querySelectorAll("[data-organizer-action]").forEach((button) => {
                button.addEventListener("click", () => organizerTaskAction(button.dataset.organizerTask, button.dataset.organizerAction, false, button));
            });
            renderPager("organizerTasksPager", adminState.pagination.organizerTasks, (page) => {
                adminState.pagination.organizerTasks.page = page;
                loadOrganizer().catch((error) => toast(error.message, "error"));
            });
        }

        function renderOrganizerTaskRow(item) {
            const matchText = item.tmdb_id
                ? `${item.tmdb_title || item.title || "-"}${item.tmdb_year ? ` (${item.tmdb_year})` : ""}`
                : "待确认";
            return `
                <tr>
                    <td><code>#${escapeHtml(item.id)}</code></td>
                    <td>
                        <strong>${escapeHtml(item.title || item.source_keyword || "-")}</strong>
                        <small>${escapeHtml(item.category_label || item.category || "-")}</small>
                    </td>
                    <td><code title="${escapeHtml(item.openlist_root_path || "")}">${escapeHtml(item.openlist_root_path || "-")}</code></td>
                    <td>${escapeHtml(matchText)}</td>
                    <td>${statusPill(item.status)}</td>
                    <td>${escapeHtml(formatDate(item.updated_at || item.created_at))}</td>
                    <td class="table-actions">${organizerTaskButtons(item)}</td>
                </tr>
            `;
        }

        function organizerTaskAgeMs(item) {
            const raw = item?.updated_at || item?.created_at || "";
            const timestamp = Date.parse(String(raw).endsWith("Z") ? raw : `${raw}Z`);
            return Number.isFinite(timestamp) ? Date.now() - timestamp : 0;
        }

        function organizerTaskIsStale(item) {
            const status = String(item?.status || "");
            const age = organizerTaskAgeMs(item);
            if (["stabilizing", "scanning"].includes(status)) return age > 3 * 60 * 1000;
            if (status === "executing") return age > 30 * 60 * 1000;
            return false;
        }

        function organizerTaskButtons(item) {
            const status = String(item.status || "");
            const id = escapeHtml(item.id);
            const buttons = [`<button class="secondary mini" type="button" data-organizer-detail="${id}">详情</button>`];
            if (status === "waiting_review") {
                buttons.push(`<button class="mini" type="button" data-organizer-task="${id}" data-organizer-action="apply">确认并整理</button>`);
                buttons.push(`<button class="secondary mini danger" type="button" title="保留原目录和文件名，仅移入正式分类目录。" data-organizer-task="${id}" data-organizer-action="skip">跳过整理</button>`);
            } else if (["auto_approved", "manual_confirmed"].includes(status)) {
                buttons.push(`<button class="mini" type="button" data-organizer-task="${id}" data-organizer-action="apply">开始整理</button>`);
                buttons.push(`<button class="secondary mini danger" type="button" title="保留原目录和文件名，仅移入正式分类目录。" data-organizer-task="${id}" data-organizer-action="skip">跳过整理</button>`);
            } else if (status === "failed") {
                buttons.push(`<button class="secondary mini" type="button" data-organizer-task="${id}" data-organizer-action="retry">重试</button>`);
                buttons.push(`<button class="secondary mini danger" type="button" title="保留原目录和文件名，仅移入正式分类目录。" data-organizer-task="${id}" data-organizer-action="skip">跳过整理</button>`);
            } else if (["done", "cancelled"].includes(status)) {
                buttons.push(`<button class="secondary mini danger" type="button" data-organizer-task="${id}" data-organizer-action="delete">删除</button>`);
            }
            return buttons.join("");
        }

        function organizerDetailActionPanel(task) {
            const status = String(task.status || "");
            const mappings = Array.isArray(task.mappings) ? task.mappings : [];
            const blockedCount = mappings.filter((item) => ["conflict", "need_edit"].includes(String(item.status || ""))).length;
            const guide = organizerStatusGuide(status, { blockedCount, stale: organizerTaskIsStale(task) });
            return `
                <section class="organizer-action-panel" aria-label="OpenList 标准化操作">
                    <div class="organizer-action-copy">
                        <strong>${escapeHtml(guide.title)}</strong>
                        <p>${escapeHtml(guide.body)}</p>
                    </div>
                    <div class="organizer-action-buttons">
                        ${organizerDrawerActionButtons(task)}
                    </div>
                </section>
            `;
        }

        function organizerStatusGuide(status, counts = {}) {
            const blockedCount = Number(counts.blockedCount || 0);
            const stale = Boolean(counts.stale);
            if (stale && ["stabilizing", "scanning"].includes(status)) {
                return {
                    title: "扫描疑似卡住",
                    body: "任务长时间没有更新，可重新识别。",
                };
            }
            if (stale && status === "executing") {
                return {
                    title: "执行疑似卡住",
                    body: "请检查 OpenList 文件状态后再决定是否重试。",
                };
            }
            const map = {
                stabilizing: {
                    title: "等待文件稳定",
                    body: "目录稳定后将自动扫描。",
                },
                scanning: {
                    title: "正在扫描",
                    body: "正在识别文件并生成整理计划。",
                },
                waiting_review: {
                    title: blockedCount ? "需要人工审核" : "需要确认",
                    body: blockedCount ? `有 ${blockedCount} 条映射需要处理。` : "请确认标题、年份和目标目录。",
                },
                auto_approved: {
                    title: "计划已自动通过",
                    body: "整理计划已就绪。",
                },
                manual_confirmed: {
                    title: "计划已确认",
                    body: "映射已确认，可以执行整理。",
                },
                executing: {
                    title: "正在执行整理",
                    body: "正在移动或重命名文件。",
                },
                failed: {
                    title: "任务失败",
                    body: "先重试当前计划；识别结果不对时再重新识别。",
                },
                done: {
                    title: "整理已完成",
                    body: "任务已结束。",
                },
                skipped: {
                    title: "任务已跳过",
                    body: "不会继续执行整理。",
                },
                cancelled: {
                    title: "任务已取消",
                    body: "任务已结束，不会再执行整理或入库。",
                },
            };
            return map[status] || {
                title: "待处理",
                body: "等待系统生成整理计划。",
            };
        }

        function organizerDrawerActionButtons(task) {
            const status = String(task.status || "");
            const stale = organizerTaskIsStale(task);
            const buttons = [];
            const busy = ["stabilizing", "scanning", "executing"].includes(status);
            const finished = ["done", "skipped", "cancelled"].includes(status);
            if (stale && ["stabilizing", "scanning"].includes(status)) {
                buttons.push(
                    { action: "rebuild", label: "重新识别", className: "secondary mini", title: "重新扫描目录并生成新计划。" },
                );
            } else if (status === "failed") {
                buttons.push(
                    { action: "retry", label: "重试", className: "mini", title: "优先续跑当前计划，保留已有修改。" },
                    { action: "skip", label: "跳过整理", className: "secondary mini danger", title: "保留原目录和文件名，仅移入正式分类目录。" },
                    { action: "rebuild", label: "重新识别", className: "secondary mini", title: "放弃当前计划，重新扫描和识别。" },
                );
            } else if (status === "waiting_review") {
                buttons.push(
                    { action: "rename", label: "修改计划", className: "secondary mini", title: "修改片名、季号或映射目标。" },
                    { action: "apply", label: "确认并整理", className: "mini danger", title: "确认当前计划并执行真实移动/重命名。" },
                    { action: "skip", label: "跳过整理", className: "secondary mini danger", title: "保留原目录和文件名，仅移入正式分类目录。" },
                    { action: "rebuild", label: "重新识别", className: "secondary mini", title: "识别不准或文件有变化时重新扫描。" },
                );
            } else if (["auto_approved", "manual_confirmed"].includes(status)) {
                buttons.push(
                    { action: "rename", label: "修改计划", className: "secondary mini", title: "修改片名、季号或映射目标。" },
                    { action: "apply", label: "开始整理", className: "mini danger", title: "按当前计划执行真实移动/重命名。" },
                    { action: "skip", label: "跳过整理", className: "secondary mini danger", title: "保留原目录和文件名，仅移入正式分类目录。" },
                    { action: "rebuild", label: "重新识别", className: "secondary mini", title: "放弃当前计划，重新扫描和识别。" },
                );
            } else if (["done", "cancelled"].includes(status)) {
                buttons.push({ action: "delete", label: "删除记录", className: "secondary mini danger", title: "删除记录" });
            } else if (!busy && !finished) {
                buttons.push(
                    { action: "rebuild", label: "重新识别", className: "secondary mini", title: "重新扫描目录并生成计划。" },
                );
            }
            if (busy && !stale) {
                buttons.push({ label: "处理中", className: "secondary mini", title: "当前阶段暂不建议操作，等待任务状态变化。", disabled: true });
            }
            return buttons.map((button) => {
                const attrs = [
                    `class="${escapeHtml(button.className || "secondary mini")}"`,
                    "type=\"button\"",
                    `title="${escapeHtml(button.title || "")}"`,
                ];
                if (button.disabled) attrs.push("disabled");
                if (button.action) attrs.push(`data-organizer-drawer-action="${escapeHtml(button.action)}"`);
                return `<button ${attrs.join(" ")}>${escapeHtml(button.label)}</button>`;
            }).join("");
        }

        function organizerRunSummaryText(run = {}) {
            if (run.error_message) return String(run.error_message);
            const summary = run.summary && typeof run.summary === "object" ? run.summary : {};
            const labels = { done: "完成", skipped: "跳过", failed: "失败" };
            const parts = Object.entries(labels)
                .filter(([key]) => Number.isFinite(Number(summary[key])))
                .map(([key, label]) => `${label} ${Number(summary[key])}`);
            return parts.join(" · ") || "无补充信息";
        }

        function renderOrganizerRuns() {
            const box = $("organizerRuns");
            if (!box) return;
            if (!adminState.organizerRuns.length) {
                box.innerHTML = `<div class="empty">暂无执行记录</div>`;
                renderPager("organizerRunsPager", adminState.pagination.organizerRuns, (page) => {
                    adminState.pagination.organizerRuns.page = page;
                    openOrganizerRunsDrawer().catch((error) => toast(error.message, "error"));
                });
                return;
            }
            box.innerHTML = adminState.organizerRuns.map((run) => {
                const undoCount = Array.isArray(run.undo_data) ? run.undo_data.length : 0;
                return `
                    <div class="list-item">
                        <div>
                            <strong>run #${escapeHtml(run.id)} / task #${escapeHtml(run.task_id)}</strong>
                            <p>${statusPill(run.status)} · ${escapeHtml(formatDate(run.started_at))} → ${escapeHtml(formatDate(run.finished_at))}</p>
                            <p>${escapeHtml(organizerRunSummaryText(run))}</p>
                        </div>
                        ${undoCount ? `<button class="secondary mini danger" type="button" data-organizer-rollback="${escapeHtml(run.id)}">回滚</button>` : ""}
                    </div>
                `;
            }).join("");
            box.querySelectorAll("[data-organizer-rollback]").forEach((button) => {
                button.addEventListener("click", () => rollbackOrganizerRun(button.dataset.organizerRollback, button));
            });
            renderPager("organizerRunsPager", adminState.pagination.organizerRuns, (page) => {
                adminState.pagination.organizerRuns.page = page;
                openOrganizerRunsDrawer().catch((error) => toast(error.message, "error"));
            });
        }

        function openOrganizerScanDrawer() {
            openDrawer("手动创建 OpenList 标准化任务", `
                <section class="organizer-drawer-card">
                    <label class="advanced-field"><span>分类</span>
                        <select id="organizerScanCategory">
                            <option value="movie">电影</option>
                            <option value="tv">电视剧</option>
                            <option value="anime">动漫</option>
                            <option value="variety">综艺</option>
                            <option value="other">其他</option>
                        </select>
                    </label>
                    <label class="advanced-field"><span>OpenList 目录</span><input id="organizerScanPath" type="text" placeholder="/移动云/电影/资源目录"></label>
                    <label class="advanced-field"><span>搜索名/标题</span><input id="organizerScanTitle" type="text" placeholder="可选，用于匹配 TMDB"></label>
                    <label class="mini-check"><input id="organizerScanAutoApply" type="checkbox">识别通过后自动执行</label>
                    <div class="drawer-action-stack">
                        <button type="button" id="organizerScanBtn">创建扫描任务</button>
                    </div>
                </section>
            `, { mode: "modal", className: "organizer-tool-modal" });
            window.refreshCustomSelects?.();
            $("organizerScanBtn")?.addEventListener("click", (event) => scanOrganizerTask(event.currentTarget).catch((error) => toast(error.message, "error")));
        }

        async function openOrganizerRunsDrawer() {
            openDrawer("执行记录 / 回滚", `
                <section class="organizer-drawer-card">
                    <div class="organizer-drawer-head">
                        <div>
                            <strong>最近执行记录</strong>
                        </div>
                        <button class="secondary mini" type="button" id="refreshOrganizerRunsBtn">刷新记录</button>
                    </div>
                    <div id="organizerRuns" class="list-box organizer-runs-drawer-list"></div>
                    <div id="organizerRunsPager" class="pager-bar compact"></div>
                </section>
            `, { mode: "modal", className: "organizer-runs-modal" });
            const page = adminState.pagination.organizerRuns.page || 1;
            const perPage = adminState.pagination.organizerRuns.per_page || 30;
            const runsData = await api(`/api/admin/organizer/runs?page=${encodeURIComponent(page)}&per_page=${encodeURIComponent(perPage)}`);
            adminState.organizerRuns = runsData.items || [];
            adminState.pagination.organizerRuns = normalizePagination(runsData.pagination, { page, per_page: perPage });
            renderOrganizerRuns();
            $("refreshOrganizerRunsBtn")?.addEventListener("click", async () => {
                const runsData = await api(`/api/admin/organizer/runs?page=${encodeURIComponent(adminState.pagination.organizerRuns.page || 1)}&per_page=${encodeURIComponent(adminState.pagination.organizerRuns.per_page || 30)}`);
                adminState.organizerRuns = runsData.items || [];
                adminState.pagination.organizerRuns = normalizePagination(runsData.pagination, adminState.pagination.organizerRuns);
                renderOrganizerRuns();
            });
        }

        async function showOrganizerTask(id) {
            const data = await api(`/api/admin/organizer/tasks/${encodeURIComponent(id)}`);
            const task = data.task || {};
            const files = task.files || [];
            const mappings = task.mappings || [];
            const blockedMappings = mappings.filter((item) => ["conflict", "need_edit"].includes(String(item.status || "")));
            const readyMappings = mappings.filter((item) => String(item.status || "") === "ready");
            const evidence = task.evidence && typeof task.evidence === "object" ? task.evidence : {};
            const lowConfidenceOnly = organizerHasLowConfidence(evidence.problem_summary) && !blockedMappings.length;
            const mappingsForEdit = blockedMappings.length ? blockedMappings : [];
            const operations = task.operations || [];
            const tmdbMatches = task.tmdb_matches || [];
            const aiSuggestions = task.ai_suggestions || [];
            const matchStatus = task.tmdb_id
                ? `${task.tmdb_title || task.title || "-"}${task.tmdb_year ? ` (${task.tmdb_year})` : ""}`
                : (lowConfidenceOnly ? "需要确认" : "待识别");
            openDrawer(
                `OpenList 标准化 #${task.id}`,
                `
                <div class="organizer-detail-body">
                <div class="detail-grid">
                    <div><span>状态</span>${statusPill(task.status)}</div>
                    <div><span>分类</span>${escapeHtml(task.category_label || task.category || "-")}</div>
                    <div><span>标题</span>${escapeHtml(task.title || "-")}</div>
                    <div><span>OpenList 根目录</span><code>${escapeHtml(task.openlist_root_path || "-")}</code></div>
                    <div><span>匹配结果</span>${escapeHtml(matchStatus)}</div>
                </div>
                ${task.error_message ? `<div class="notice-box status-error">${escapeHtml(task.error_message)}</div>` : ""}
                ${organizerDetailActionPanel(task)}
                <h4>人工审核重点</h4>
                ${renderOrganizerProblemSummary(mappings, evidence.problem_summary)}
                ${renderOrganizerEpisodeCompleteness(evidence.episode_completeness)}
                ${renderOrganizerReviewFocus(task, mappings, evidence)}
                ${mappingsForEdit.length ? `<div class="organizer-mapping-list organizer-focus-list">${mappingsForEdit.map((mapping) => renderOrganizerMappingRow(task.id, mapping)).join("")}</div>` : `<div class="empty organizer-no-row-edit">${lowConfidenceOnly ? "请核对标题、年份和目标目录；确认无误后点“确认并整理”。" : "当前没有需要逐条编辑的映射。"}</div>`}
                ${readyMappings.length ? `<details class="advanced-details"><summary>${lowConfidenceOnly ? "查看可抽查的就绪映射" : "查看已隐藏的就绪映射"}（${escapeHtml(readyMappings.length)} 条）</summary><div class="organizer-mapping-list">${readyMappings.slice(0, 20).map((mapping) => renderOrganizerMappingRow(task.id, mapping)).join("")}${readyMappings.length > 20 ? `<div class="empty">仅预览前 20 条；其余就绪映射不会要求人工处理。</div>` : ""}</div></details>` : ""}
                <details class="advanced-details organizer-secondary-detail">
                    <summary>查看完整文件列表（${escapeHtml(files.length)} 条，分页）</summary>
                    ${renderOrganizerFileSummary(files, mappings)}
                    <div id="organizerFilesPaged" class="organizer-paged-list"></div>
                </details>
                <details class="advanced-details organizer-secondary-detail">
                    <summary>查看执行操作记录（${escapeHtml(operations.length)} 条，分页）</summary>
                    <div id="organizerOperationsPaged" class="organizer-paged-list"></div>
                </details>
                <details class="advanced-details organizer-secondary-detail">
                    <summary>查看识别详情</summary>
                    <h4>TMDB 候选</h4>
                    <div class="list-box">${tmdbMatches.length ? tmdbMatches.slice(0, 10).map((match) => `<div class="list-item"><div><strong>${escapeHtml(match.title || "-")} ${match.year ? `(${escapeHtml(match.year)})` : ""}</strong><p>${escapeHtml(match.media_type || "-")}${match.tmdb_id ? ` · TMDB #${escapeHtml(match.tmdb_id)}` : ""}</p></div></div>`).join("") : `<div class="empty">暂无 TMDB 候选</div>`}</div>
                    <h4>AI 建议</h4>
                    <div class="list-box">${aiSuggestions.length ? aiSuggestions.slice(0, 5).map((item) => `<div class="list-item"><div><strong>${escapeHtml(item.model || item.provider || "AI")}</strong><p>${escapeHtml(item.message || item.reason || "已生成识别建议")}</p></div></div>`).join("") : `<div class="empty">${escapeHtml(evidence.ai_trace?.reason || "暂无 AI 建议")}</div>`}</div>
                </details>
                <details class="advanced-details organizer-secondary-detail">
                    <summary>查看执行详情</summary>
                    <h4>旧目录清理</h4>
                    <div class="list-box">${renderOrganizerDirCleanup(evidence.real_dir_cleanup)}</div>
                    <h4>OpenList 文件夹刷新</h4>
                    <div class="list-box">${renderOrganizerStrmRefresh(evidence.strm_refresh)}</div>
                </details>
                </div>
                `,
                { mode: "modal", className: "organizer-detail-modal" }
            );
            const drawer = $("adminDrawerBody");
            drawer?.querySelectorAll("[data-organizer-mapping-save]").forEach((button) => {
                button.addEventListener("click", () => saveOrganizerMapping(task.id, button.dataset.organizerMappingSave, button));
            });
            drawer?.querySelectorAll("[data-organizer-drawer-action]").forEach((button) => {
                button.addEventListener("click", () => organizerTaskAction(task.id, button.dataset.organizerDrawerAction, true, button));
            });
            drawer?.querySelectorAll("[data-organizer-refresh]").forEach((button) => {
                button.addEventListener("click", () => showOrganizerTask(button.dataset.organizerRefresh || task.id));
            });
            mountOrganizerPagedList("organizerFilesPaged", `task:${task.id}:files`, files, renderOrganizerFileRow, {
                perPage: 30,
                empty: "暂无文件",
            });
            mountOrganizerPagedList("organizerOperationsPaged", `task:${task.id}:operations`, operations, renderOrganizerOperationRow, {
                perPage: 30,
                empty: "暂无操作；保存映射后执行时会自动重建。",
            });
        }

        function renderOrganizerFileSummary(files = [], mappings = []) {
            const fileList = Array.isArray(files) ? files : [];
            const mappingList = Array.isArray(mappings) ? mappings : [];
            const extCounts = fileList.reduce((acc, file) => {
                const ext = String(file.ext || "").replace(/^\./, "").toLowerCase() || "unknown";
                acc[ext] = (acc[ext] || 0) + 1;
                return acc;
            }, {});
            const countByStatus = (status) => mappingList.filter((item) => String(item.status || "") === status).length;
            const needEdit = countByStatus("need_edit");
            const conflict = countByStatus("conflict");
            const ready = countByStatus("ready");
            const deleteAd = countByStatus("delete_ad");
            const skipped = countByStatus("skipped");
            const extText = Object.entries(extCounts)
                .sort((a, b) => b[1] - a[1])
                .slice(0, 5)
                .map(([ext, count]) => `${ext} ${count}`)
                .join(" · ");
            return `
                <div class="organizer-compact-summary">
                    <div><strong>${escapeHtml(fileList.length)}</strong><span>文件总数</span></div>
                    <div><strong>${escapeHtml(ready)}</strong><span>就绪</span></div>
                    <div><strong>${escapeHtml(needEdit + conflict)}</strong><span>需处理/冲突</span></div>
                    <div><strong>${escapeHtml(deleteAd)}</strong><span>广告删除</span></div>
                    <div><strong>${escapeHtml(skipped)}</strong><span>跳过</span></div>
                </div>
                ${extText ? `<div class="notice-box organizer-mapping-help">主要后缀：${escapeHtml(extText)}</div>` : ""}
            `;
        }

        function renderOrganizerFileRow(file = {}) {
            return `
                <div class="list-item compact organizer-file-row">
                    <div>
                        <strong>${escapeHtml(file.name || "-")}</strong>
                        <p><code>${escapeHtml(file.path || "-")}</code></p>
                        <p>Season ${escapeHtml(file.season || "-")} / Episode ${escapeHtml(file.episode || "-")} · ${escapeHtml(file.ext || "-")} · ${escapeHtml(formatBytes(file.size || 0))}</p>
                    </div>
                </div>
            `;
        }

        function renderOrganizerOperationRow(op = {}) {
            const sourceName = organizerPathBasename(op.source_path || "");
            const targetName = organizerPathBasename(op.target_path || "");
            return `
                <div class="list-item compact">
                    <div>
                        <strong>${escapeHtml(op.type || "-")}</strong>
                        <p>${statusPill(op.status)} · ${escapeHtml(op.description || "")}</p>
                        <p>${escapeHtml(sourceName || "-")} → ${escapeHtml(targetName || "-")}</p>
                        <details class="advanced-details"><summary>查看路径</summary><p><code>${escapeHtml(op.source_path || "")}</code> → <code>${escapeHtml(op.target_path || "")}</code></p></details>
                        ${op.error_message ? `<p class="danger-text">${escapeHtml(op.error_message)}</p>` : ""}
                    </div>
                </div>
            `;
        }

        function mountOrganizerPagedList(containerId, key, items, renderer, options = {}) {
            adminState.organizerDetailPaging[key] = {
                page: 1,
                perPage: Math.max(1, Number(options.perPage || 30)),
                items: Array.isArray(items) ? items : [],
                renderer,
                empty: options.empty || "暂无数据",
            };
            renderOrganizerPagedList(containerId, key);
        }

        function renderOrganizerPagedList(containerId, key) {
            const box = $(containerId);
            const state = adminState.organizerDetailPaging[key];
            if (!box || !state) return;
            const total = state.items.length;
            if (!total) {
                box.innerHTML = `<div class="list-box"><div class="empty">${escapeHtml(state.empty)}</div></div>`;
                return;
            }
            const pages = Math.max(1, Math.ceil(total / state.perPage));
            state.page = Math.min(Math.max(1, state.page || 1), pages);
            const start = (state.page - 1) * state.perPage;
            const rows = state.items.slice(start, start + state.perPage);
            box.innerHTML = `
                <div class="organizer-paged-head">
                    <span>第 ${escapeHtml(state.page)} / ${escapeHtml(pages)} 页，共 ${escapeHtml(total)} 条；当前显示 ${escapeHtml(start + 1)}-${escapeHtml(start + rows.length)}</span>
                    <div class="pager-bar compact">
                        <button class="secondary mini" type="button" data-organizer-page="prev" ${state.page <= 1 ? "disabled" : ""}>上一页</button>
                        <button class="secondary mini" type="button" data-organizer-page="next" ${state.page >= pages ? "disabled" : ""}>下一页</button>
                    </div>
                </div>
                <div class="list-box">${rows.map((item) => state.renderer(item)).join("")}</div>
            `;
            box.querySelectorAll("[data-organizer-page]").forEach((button) => {
                button.addEventListener("click", () => {
                    state.page += button.dataset.organizerPage === "next" ? 1 : -1;
                    renderOrganizerPagedList(containerId, key);
                });
            });
        }

        function renderOrganizerDirCleanup(data) {
            if (!data) return `<div class="empty">暂无真实目录清理记录</div>`;
            const items = Array.isArray(data.items) ? data.items : [];
            return `
                <div class="list-item"><div><strong>${escapeHtml(data.message || "真实旧目录清理记录")}</strong><p>删除：${escapeHtml(data.removed ?? 0)}；跳过：${escapeHtml(data.skipped ?? 0)}；失败：${escapeHtml(data.failed ?? 0)}</p></div></div>
                ${items.map((item) => `<div class="list-item"><div><strong>${item.success ? "已删除" : item.failed ? "失败" : "跳过"}</strong><p><code>${escapeHtml(item.path || "")}</code>${item.message ? ` · ${escapeHtml(item.message)}` : ""}</p></div></div>`).join("")}
            `;
        }

        function renderOrganizerStrmRefresh(data) {
            if (!data) return `<div class="empty">暂无 OpenList 文件夹刷新记录</div>`;
            const items = Array.isArray(data.items) ? data.items : [];
            const cleanup = data.cleanup && data.cleanup.enabled ? data.cleanup : null;
            return `
                <div class="list-item"><div><strong>${escapeHtml(data.message || "OpenList 文件夹刷新记录")}</strong><p>接口：${escapeHtml(data.endpoint || "-")}；目录数：${escapeHtml(data.count ?? 0)}；失败：${escapeHtml(data.failed ?? 0)}</p></div></div>
                ${cleanup ? `<div class="list-item"><div><strong>旧 STRM 目录清理</strong><p>${escapeHtml(cleanup.message || "-")}${cleanup.path ? ` · <code>${escapeHtml(cleanup.path)}</code>` : ""}</p></div></div>` : ""}
                ${items.map((item) => `<div class="list-item"><div><strong>${escapeHtml(item.name || "-")}</strong><p>${item.success ? "成功" : "失败"} · <code>${escapeHtml(item.path || "")}</code>${item.message ? ` · ${escapeHtml(item.message)}` : ""}</p></div></div>`).join("")}
            `;
        }


        function organizerHasLowConfidence(summary) {
            const issues = summary && Array.isArray(summary.issues) ? summary.issues : [];
            return issues.some((item) => {
                const type = String(item?.type || "");
                const message = String(item?.message || "");
                return type === "low_confidence" || message.includes("置信度") || message.includes("TMDB/AI");
            });
        }

        function organizerPathDirname(path) {
            const value = String(path || "").replace(/\\/g, "/").replace(/\/+$/, "");
            if (!value || !value.includes("/")) return value;
            return value.slice(0, value.lastIndexOf("/")) || "/";
        }

        function organizerPathBasename(path) {
            const value = String(path || "").replace(/\\/g, "/").replace(/\/+$/, "");
            if (!value || !value.includes("/")) return value;
            return value.slice(value.lastIndexOf("/") + 1);
        }

        function organizerResourceDirFromTarget(path) {
            let dir = organizerPathDirname(path);
            if (/^season\s+\d+$/i.test(organizerPathBasename(dir))) {
                dir = organizerPathDirname(dir);
            }
            return dir;
        }

        function renderOrganizerReviewFocus(task = {}, mappings = [], evidence = {}) {
            const summary = evidence && typeof evidence === "object" ? evidence.problem_summary : null;
            const lowConfidence = organizerHasLowConfidence(summary);
            const blocked = (Array.isArray(mappings) ? mappings : []).filter((item) => ["conflict", "need_edit"].includes(String(item.status || "")));
            if (!lowConfidence || blocked.length) return "";
            const firstMapping = (Array.isArray(mappings) ? mappings : []).find((item) => String(item.status || "") === "ready") || (Array.isArray(mappings) ? mappings[0] : {}) || {};
            const title = firstMapping.title || evidence.title || task.tmdb_title || task.title || "-";
            const year = firstMapping.year || evidence.year || task.tmdb_year || "-";
            const tmdb = task.tmdb_id ? `#${task.tmdb_id} ${task.tmdb_title || ""} ${task.tmdb_year || ""}`.trim() : "-";
            const targetDirs = [];
            (Array.isArray(mappings) ? mappings : []).forEach((item) => {
                if (String(item.status || "") !== "ready") return;
                const dir = organizerResourceDirFromTarget(item.target_path || "");
                if (dir && !targetDirs.includes(dir)) targetDirs.push(dir);
            });
            return `
                <div class="notice-box organizer-review-card warning">
                    <strong>请确认标题和目标目录</strong>
                    <p>系统需要你核对片名、年份和目标目录。最终会按映射里的“目标路径”执行。</p>
                    <div class="organizer-review-grid">
                        <div><span>识别标题</span><strong>${escapeHtml(title)}</strong></div>
                        <div><span>年份</span><strong>${escapeHtml(year)}</strong></div>
                        <div><span>TMDB</span><strong>${escapeHtml(tmdb)}</strong></div>
                    </div>
                    ${targetDirs.length ? `<div class="organizer-review-targets"><span>目标目录</span>${targetDirs.slice(0, 6).map((item) => `<code>${escapeHtml(item)}</code>`).join("")}${targetDirs.length > 6 ? `<em>+${escapeHtml(targetDirs.length - 6)}</em>` : ""}</div>` : ""}
                    <ul>
                        <li>都正确：直接点“确认并整理”。</li>
                        <li>片名/年份不对：展开就绪映射修改标题或年份，保存后会自动重算目标路径。</li>
                    </ul>
                </div>
            `;
        }

        function renderOrganizerProblemSummary(mappings, summary) {
            const issues = [];
            const needEdit = mappings.filter((item) => String(item.status || "") === "need_edit");
            const conflicts = mappings.filter((item) => String(item.status || "") === "conflict");
            if (needEdit.length) {
                issues.push(`${needEdit.length} 个文件没识别出集数：补“季/集”或直接改目标路径。`);
            }
            if (conflicts.length) {
                issues.push(`${conflicts.length} 个文件目标冲突：如果是多版本，给目标文件名加 1080P/4K 等后缀；否则改成正确集数。`);
            }
            if (summary && Array.isArray(summary.issues)) {
                summary.issues.forEach((item) => {
                    const text = item?.type === "low_confidence" ? "标题、年份或目标目录需要确认。" : (item && item.message ? String(item.message) : "");
                    if (text && !issues.includes(text)) issues.push(text);
                });
            }
            if (!issues.length) {
                return `<div class="notice-box organizer-problem-summary success">没有需要人工处理的路径问题；可直接执行或按需微调。</div>`;
            }
            return `
                <div class="notice-box organizer-problem-summary warning">
                    <strong>需要处理 ${escapeHtml(issues.length)} 类问题</strong>
                    <ul>${issues.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>
                </div>
            `;
        }

        function renderOrganizerEpisodeCompleteness(report) {
            if (!report || typeof report !== "object" || report.enabled !== true) return "";
            const seasons = Array.isArray(report.seasons) ? report.seasons : [];
            const unrecognizedFiles = Array.isArray(report.unrecognized_files) ? report.unrecognized_files : [];
            const excluded = report.excluded && typeof report.excluded === "object" ? report.excluded : {};
            const missingCount = Number(report.missing_count || 0);
            const duplicateCount = Number(report.duplicate_count || 0);
            const unrecognizedCount = Number(report.unrecognized_count || 0);
            const hasWarnings = missingCount > 0 || duplicateCount > 0 || unrecognizedCount > 0;
            const seasonRows = seasons.map((season) => {
                const ranges = Array.isArray(season.ranges) ? season.ranges.filter(Boolean) : [];
                const missing = Array.isArray(season.missing_episodes) ? season.missing_episodes : [];
                const duplicates = Array.isArray(season.duplicates) ? season.duplicates : [];
                const duplicateDetails = duplicates.map((item) => {
                    const files = Array.isArray(item.files) ? item.files.filter(Boolean) : [];
                    const fileLabels = files.map((file) => {
                        if (file && typeof file === "object") {
                            return file.path || file.name || file.target_path || "-";
                        }
                        return String(file || "-");
                    });
                    return `<li><strong>${escapeHtml(formatOrganizerEpisodeNumber(item.episode))} × ${escapeHtml(item.count || files.length || 2)}</strong>${fileLabels.length ? `：${fileLabels.map((file) => `<code>${escapeHtml(file)}</code>`).join(" ")}` : ""}</li>`;
                }).join("");
                return `
                    <div class="list-item compact">
                        <div>
                            <strong>${escapeHtml(season.label || `Season ${season.season ?? "-"}`)}</strong>
                            <p>文件 ${escapeHtml(season.file_count ?? 0)} 个 · 唯一集数 ${escapeHtml(season.episode_count ?? 0)} 个 · 范围 ${escapeHtml(ranges.join("、") || "未形成连续区间")}</p>
                            ${missing.length ? `<p class="danger-text">区间内缺失：${missing.map((episode) => escapeHtml(formatOrganizerEpisodeNumber(episode))).join("、")}</p>` : `<p>区间内未发现缺集。</p>`}
                            ${duplicateDetails ? `<details class="advanced-details"><summary>查看重复集（${escapeHtml(duplicates.length)} 集）</summary><ul>${duplicateDetails}</ul></details>` : ""}
                        </div>
                    </div>
                `;
            }).join("");
            const excludedText = [
                Number(excluded.advertisement_count || 0) ? `广告 ${Number(excluded.advertisement_count || 0)} 个` : "",
                Number(excluded.companion_file_count || 0) ? `附件 ${Number(excluded.companion_file_count || 0)} 个` : "",
                Number(excluded.ignored_video_count || 0) ? `忽略视频 ${Number(excluded.ignored_video_count || 0)} 个` : "",
            ].filter(Boolean).join("；");
            return `
                <section class="organizer-episode-report">
                    <h4>剧集完整性报告</h4>
                    <div class="notice-box ${hasWarnings ? "warning" : "success"}">
                        <strong>${escapeHtml(report.message || (hasWarnings ? "发现缺集、重复集或未识别文件，请核对。" : "当前扫描区间内未发现剧集完整性问题。"))}</strong>
                        <p>缺失只统计每季已识别最小集数到最大集数之间的内部空洞，不会把增量资源前面的集数误报为缺失。</p>
                    </div>
                    <div class="organizer-compact-summary">
                        <div><strong>${escapeHtml(report.recognized_episode_count ?? 0)}</strong><span>识别集数</span></div>
                        <div><strong>${escapeHtml(missingCount)}</strong><span>区间内缺失</span></div>
                        <div><strong>${escapeHtml(duplicateCount)}</strong><span>重复集数</span></div>
                        <div><strong>${escapeHtml(report.special_count ?? 0)}</strong><span>特别篇</span></div>
                        <div><strong>${escapeHtml(unrecognizedCount)}</strong><span>未识别视频</span></div>
                    </div>
                    <div class="list-box">${seasonRows || `<div class="empty">暂无可按季统计的剧集。</div>`}</div>
                    ${unrecognizedFiles.length ? `<details class="advanced-details"><summary>查看未识别视频（${escapeHtml(unrecognizedFiles.length)} 个）</summary><div class="list-box">${unrecognizedFiles.map((file) => `<div class="list-item compact"><div><strong>${escapeHtml(file.name || file.path || "-")}</strong>${file.path ? `<p><code>${escapeHtml(file.path)}</code></p>` : ""}</div></div>`).join("")}</div></details>` : ""}
                    ${excludedText ? `<p class="muted">本报告未纳入：${escapeHtml(excludedText)}。</p>` : ""}
                </section>
            `;
        }

        function formatOrganizerEpisodeNumber(value) {
            const number = Number(value);
            if (!Number.isFinite(number) || number < 0) return "E-";
            return `E${String(Math.trunc(number)).padStart(2, "0")}`;
        }

        function renderOrganizerMappingRow(taskId, mapping) {
            const reason = Array.isArray(mapping.reason) ? mapping.reason.join("；") : (mapping.reason || "");
            const mediaType = String(mapping.media_type || "movie");
            const status = String(mapping.status || "ready");
            const sourceName = mapping.source_name || organizerPathBasename(mapping.source_path || "");
            return `
                <section class="organizer-mapping-card" data-organizer-mapping-row="${escapeHtml(mapping.id)}">
                    <div class="organizer-mapping-card-head">
                        <div>
                            <strong>${escapeHtml(sourceName || "-")}</strong>
                            <details class="advanced-details"><summary>查看来源路径</summary><p><code>${escapeHtml(mapping.source_path || "-")}</code></p></details>
                        </div>
                        <div class="organizer-mapping-status">
                            <select data-map-field="status" aria-label="映射状态">
                                <option value="ready" ${status === "ready" ? "selected" : ""}>就绪</option>
                                <option value="need_edit" ${status === "need_edit" ? "selected" : ""}>需编辑</option>
                                <option value="conflict" ${status === "conflict" ? "selected" : ""}>冲突</option>
                                <option value="delete_ad" ${status === "delete_ad" ? "selected" : ""}>广告删除</option>
                                <option value="skipped" ${status === "skipped" ? "selected" : ""}>跳过</option>
                            </select>
                            <button class="mini" type="button" data-organizer-mapping-save="${escapeHtml(mapping.id)}">保存本条</button>
                        </div>
                    </div>
                    <label class="organizer-mapping-target">
                        <span>最终目标路径（确认并整理后按这里移动/重命名）</span>
                        <textarea data-map-field="target_path" rows="3">${escapeHtml(mapping.target_path || "")}</textarea>
                        <small>改标题/年份/季集后保存，会自动重算这里；如果你直接改了目标路径，则以你手写的目标路径为准。</small>
                    </label>
                    <div class="organizer-mapping-fields">
                        <label><span>标题</span><input data-map-field="title" type="text" value="${escapeHtml(mapping.title || "")}" placeholder="标题"></label>
                        <label><span>年份</span><input data-map-field="year" type="text" value="${escapeHtml(mapping.year || "")}" placeholder="年份"></label>
                        <label><span>季</span><input data-map-field="season" type="number" value="${escapeHtml(mapping.season ?? "")}" placeholder="季"></label>
                        <label><span>集</span><input data-map-field="episode" type="number" value="${escapeHtml(mapping.episode ?? "")}" placeholder="集"></label>
                        <label><span>TMDB ID</span><input data-map-field="tmdb_id" type="number" value="${escapeHtml(mapping.tmdb_id ?? "")}" placeholder="可空"></label>
                        <label><span>类型</span><select data-map-field="media_type">
                            <option value="movie" ${mediaType === "movie" ? "selected" : ""}>电影</option>
                            <option value="tv" ${mediaType === "tv" ? "selected" : ""}>剧集</option>
                        </select></label>
                    </div>
                    <p class="organizer-mapping-reason" title="${escapeHtml(reason)}">判断原因：${escapeHtml(reason || "-")}</p>
                </section>
            `;
        }

        async function saveOrganizerMapping(taskId, mappingId, button = null) {
            const row = Array.from(document.querySelectorAll("[data-organizer-mapping-row]")).find((item) => String(item.dataset.organizerMappingRow) === String(mappingId));
            if (!row) return;
            await withButtonBusy(button, "保存中...", async () => {
                const payload = {};
                row.querySelectorAll("[data-map-field]").forEach((field) => {
                    const key = field.dataset.mapField;
                    let value = field.value;
                    if (["season", "episode", "tmdb_id"].includes(key)) {
                        value = value === "" ? null : Number(value);
                    }
                    payload[key] = value;
                });
                toast("正在保存映射...", "info");
                const data = await api(`/api/admin/organizer/tasks/${encodeURIComponent(taskId)}/mappings/${encodeURIComponent(mappingId)}`, {
                    method: "PATCH",
                    body: JSON.stringify(payload),
                    allowFailure: true,
                });
                toast(data.success ? (data.target_path ? `映射已保存，目标已更新：${data.target_path}` : "映射已保存") : data.message || "映射保存失败", data.success ? "success" : "error");
                await Promise.all([loadOrganizer(), showOrganizerTask(taskId)]);
            });
        }

        async function organizerTaskAction(taskId, action, keepDrawer = false, button = null) {
            if (action === "rename") {
                openBatchRenameDialog(taskId);
                return;
            }
            const messages = {
                apply: "确认当前计划并执行真实 OpenList 移动/重命名？目标已存在时不会覆盖。",
                skip: "确认跳过整理？系统会保留原目录和文件名，仅将暂存内容移到正确的媒体分类目录，后续交给飞牛影视刮削；目标已存在时不会覆盖。",
                delete: "确认删除这条记录？此操作不可撤销。",
                rebuild: "",
                retry: "",
            };
            if (messages[action]) {
                const ok = await confirmDialog({
                    title: action === "delete" ? "删除记录" : (action === "apply" ? "确认并整理" : "确认标准化操作"),
                    message: messages[action],
                    confirmText: action === "delete" ? "删除记录" : (action === "apply" ? "确认并整理" : "确认"),
                    tone: ["apply", "delete"].includes(action) ? "danger" : "warning",
                });
                if (!ok) return;
            }
            const labelMap = { apply: "执行中...", rebuild: "识别中...", retry: "重试中...", skip: "提交中...", delete: "删除中..." };
            await withButtonBusy(button, labelMap[action] || "处理中...", async () => {
                const deleting = action === "delete";
                if (action === "apply") {
                    toast("正在提交 OpenList 后台整理请求...", "info");
                } else if (!deleting) {
                    toast(`${organizerActionText(action)}已提交，正在等待后台返回...`, "info");
                }
                const endpoint = deleting
                    ? `/api/admin/organizer/tasks/${encodeURIComponent(taskId)}`
                    : `/api/admin/organizer/tasks/${encodeURIComponent(taskId)}/${encodeURIComponent(action)}`;
                try {
                    const data = await api(endpoint, {
                        method: deleting ? "DELETE" : "POST",
                        body: deleting ? undefined : JSON.stringify({}),
                        allowFailure: true,
                        silentLoading: deleting,
                        skipButtonLoading: true,
                    });
                    const success = data.success !== false;
                    if (!success) {
                        toast(data.message || "操作失败", "error");
                        return;
                    }
                    if (deleting) {
                        closeDrawer();
                        await loadOrganizer();
                        toast("记录已删除", "success");
                        return;
                    }
                    toast(data.message || (action === "apply" ? "OpenList 整理已提交后台执行" : "操作完成"), "success");
                    if (action === "apply") {
                        closeDrawer();
                        await loadOrganizer();
                        return;
                    }
                    await loadOrganizer();
                    if (keepDrawer) {
                        await showOrganizerTask(taskId);
                    }
                } catch (error) {
                    toast(deleting ? `删除失败：${error.message}` : error.message, "error");
                }
            });
        }

        function organizerActionText(action) {
            return { apply: "确认并整理", rebuild: "重新识别", retry: "重试", skip: "跳过整理", delete: "删除记录" }[action] || "操作";
        }

        function openBatchRenameDialog(taskId) {
            const overlay = document.createElement("div");
            overlay.className = "ui-dialog-overlay";
            overlay.innerHTML = `
                <section class="ui-dialog" role="dialog" aria-modal="true" aria-label="统一修改文件">
                    <header class="ui-dialog-head">
                        <div>
                            <span class="ui-dialog-eyebrow">统一修改文件</span>
                            <h2>修改文件夹名 / 季号</h2>
                        </div>
                        <button class="ui-dialog-close secondary mini icon-only" type="button" data-batch-close aria-label="关闭">×</button>
                    </header>
                    <div class="ui-dialog-body">
                        <p class="muted">将应用到本任务全部文件，并自动重算目标路径。留空表示不修改该项。</p>
                        <label class="ui-dialog-prompt">
                            <span>文件夹名（片名）</span>
                            <input id="batchRenameTitle" type="text" autocomplete="off" placeholder="例如：百变猪猪侠">
                        </label>
                        <label class="ui-dialog-prompt">
                            <span>季号</span>
                            <input id="batchRenameSeason" type="number" min="0" max="99" autocomplete="off" placeholder="留空不改">
                        </label>
                    </div>
                    <footer class="ui-dialog-actions">
                        <button class="secondary" type="button" data-batch-close>取消</button>
                        <button type="button" data-batch-confirm>确认修改</button>
                    </footer>
                </section>
            `;
            document.body.appendChild(overlay);
            const close = () => overlay.remove();
            overlay.querySelectorAll("[data-batch-close]").forEach((button) => button.addEventListener("click", close));
            overlay.addEventListener("click", (event) => { if (event.target === overlay) close(); });
            overlay.querySelector("[data-batch-confirm]").addEventListener("click", async () => {
                const title = overlay.querySelector("#batchRenameTitle").value.trim();
                const seasonRaw = overlay.querySelector("#batchRenameSeason").value.trim();
                const payload = {};
                if (title) payload.title = title;
                if (seasonRaw !== "") payload.season = Number(seasonRaw);
                if (!payload.title && !("season" in payload)) {
                    toast("请填写片名或季号", "error");
                    return;
                }
                const confirmButton = overlay.querySelector("[data-batch-confirm]");
                confirmButton.disabled = true;
                try {
                    const data = await api(`/api/admin/organizer/tasks/${encodeURIComponent(taskId)}/mappings/batch`, {
                        method: "POST",
                        body: JSON.stringify(payload),
                        allowFailure: true,
                    });
                    toast(data.message || "已更新", data.success === false ? "error" : "success");
                    if (data.success !== false) {
                        close();
                        await Promise.all([loadOrganizer(), showOrganizerTask(taskId)]);
                    }
                } catch (error) {
                    toast(error.message, "error");
                } finally {
                    confirmButton.disabled = false;
                }
            });
            overlay.querySelector("#batchRenameTitle").focus();
        }
        async function scanOrganizerTask(button = null) {
            const payload = {
                category: $("organizerScanCategory")?.value || "movie",
                path: $("organizerScanPath")?.value?.trim() || "",
                title: $("organizerScanTitle")?.value?.trim() || "",
                source_keyword: $("organizerScanTitle")?.value?.trim() || "",
                auto_apply: Boolean($("organizerScanAutoApply")?.checked),
            };
            if (!payload.path) {
                toast("请填写 OpenList 目录", "error");
                return;
            }
            await withButtonBusy(button, "创建中...", async () => {
                toast("正在创建 OpenList 标准化扫描任务...", "info");
                const data = await api("/api/admin/organizer/tasks/scan", {
                    method: "POST",
                    body: JSON.stringify(payload),
                    allowFailure: true,
                });
                toast(data.message || (data.success === false ? "扫描任务创建失败" : "扫描任务已创建"), data.success === false ? "error" : "success");
                await loadOrganizer();
                if (data.task_id) await showOrganizerTask(data.task_id);
            });
        }

        async function rollbackOrganizerRun(runId, button = null) {
            const ok = await confirmDialog({
                title: `回滚 Organizer run #${runId}`,
                message: "确认执行回滚？回滚同样不会覆盖已有文件，请先确认当前目录没有新增冲突。",
                confirmText: "执行回滚",
                tone: "danger",
            });
            if (!ok) return;
            await withButtonBusy(button, "回滚中...", async () => {
                toast("回滚已提交，正在等待后台返回...", "info");
                const data = await api(`/api/admin/organizer/runs/${encodeURIComponent(runId)}/rollback`, {
                    method: "POST",
                    body: JSON.stringify({}),
                    allowFailure: true,
                });
                toast(data.success ? `回滚完成：${data.done || 0} 项` : data.message || "回滚失败", data.success ? "success" : "error");
                await loadOrganizer();
            });
        }

        return Object.freeze({
            loadOrganizer,
            renderOrganizer,
            renderOrganizerStatus,
            renderOrganizerTasks,
            renderOrganizerTaskRow,
            organizerTaskAgeMs,
            organizerTaskIsStale,
            organizerTaskButtons,
            organizerDetailActionPanel,
            organizerStatusGuide,
            organizerDrawerActionButtons,
            renderOrganizerRuns,
            openOrganizerScanDrawer,
            openOrganizerRunsDrawer,
            showOrganizerTask,
            renderOrganizerFileSummary,
            renderOrganizerFileRow,
            renderOrganizerOperationRow,
            mountOrganizerPagedList,
            renderOrganizerPagedList,
            renderOrganizerDirCleanup,
            renderOrganizerStrmRefresh,
            organizerHasLowConfidence,
            organizerPathDirname,
            organizerPathBasename,
            organizerResourceDirFromTarget,
            renderOrganizerReviewFocus,
            renderOrganizerProblemSummary,
            renderOrganizerMappingRow,
            saveOrganizerMapping,
            organizerTaskAction,
            organizerActionText,
            scanOrganizerTask,
            rollbackOrganizerRun,
        });
    }

    window.FnosAdminOrganizer = Object.freeze({ create });
})();
