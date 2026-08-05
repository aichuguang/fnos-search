(function () {
    function create(context) {
        const { state, getElement, api, toast, escapeHtml, openDrawer } = context;
        const adminState = state;
        const $ = getElement;

        function formatNumber(value) {
            const number = Number(value);
            if (!Number.isFinite(number)) return escapeHtml(value ?? "-");
            return number.toLocaleString();
        }

        function mediaRowKey(item = {}) {
            const raw = String(item.row_key || item.matched_category_key || item.key || item.guid || item.title || "library");
            const safe = raw.replace(/[^a-zA-Z0-9_-]+/g, "_").replace(/^_+|_+$/g, "");
            return safe || "library";
        }

        function setMediaRefreshState(rowKey, status, message, details = {}) {
            const pill = $(`mediaStatus-${rowKey}`);
            const messageBox = $(`mediaMessage-${rowKey}`);
            if (pill) {
                const cls = status === "ok" ? "pill ok" : status === "error" ? "pill error" : "pill warn";
                const label = status === "ok" ? "已触发" : status === "error" ? "失败" : "刷新中";
                pill.className = cls;
                pill.textContent = label;
            }
            if (messageBox) {
                messageBox.textContent = message || "-";
                messageBox.title = details.url || details.base_url || message || "";
            }
        }

        function mediaRefreshMessage(data) {
            if (data?.message) return data.message;
            return data?.success ? "已触发飞牛刷新" : "刷新失败";
        }

        function mediaTaskSummary(task = {}) {
            const parts = [];
            const status = task.status || task.state || task.phase;
            const progress = task.progress ?? task.percent;
            const current = task.current || task.current_item || task.message;
            if (status) parts.push(`状态：${status}`);
            if (progress !== undefined && progress !== null && progress !== "") parts.push(`进度：${progress}${String(progress).includes("%") ? "" : "%"}`);
            if (current) parts.push(String(current));
            return parts.join(" · ") || "任务正在运行";
        }

        async function loadMediaLibraries() {
            const statusBox = $("mediaStatusBox");
            if (statusBox) {
                statusBox.className = "notice-box";
                statusBox.textContent = "正在加载飞牛媒体库列表、统计和刷新任务状态...";
            }
            try {
                const data = await api("/api/admin/media/libraries", { allowFailure: true });
                adminState.mediaLibraries = data.items || [];
                adminState.mediaCategories = data.categories || [];
                adminState.mediaSummary = data.summary || {};
                adminState.mediaRunning = data.running || [];
                adminState.mediaDiagnostics = data.diagnostics || {};
                renderMediaStats();
                renderMediaLibraries();
                renderMediaMappings();
                const message = data.success
                    ? `已加载 ${adminState.mediaLibraries.length} 个飞牛媒体库，运行中任务 ${adminState.mediaRunning.length} 个。`
                    : data.message || "飞牛媒体库加载失败，请检查 FNOS_SERVER_URL / API Key / Secret / 登录配置。";
                if (statusBox) {
                    statusBox.className = `notice-box ${data.success ? "status-ok" : "status-error"}`;
                    statusBox.innerHTML = `
                        <strong>${escapeHtml(data.success ? "飞牛媒体库已连接" : "飞牛媒体库未连接")}</strong>
                        <p>${escapeHtml(message)}</p>
                        ${renderMediaDiagnosticHint(data)}
                    `;
                }
            } catch (error) {
                if (statusBox) {
                    statusBox.className = "notice-box status-error";
                    statusBox.textContent = error.message;
                }
                adminState.mediaLibraries = [];
                renderMediaLibraries();
                throw error;
            }
        }

        function renderMediaDiagnosticHint(data) {
            const diagnostics = data?.diagnostics || {};
            const parts = [];
            if (diagnostics.running && diagnostics.running.success === false && diagnostics.running.message) {
                parts.push(`刷新状态：${diagnostics.running.message}`);
            }
            if (diagnostics.refresh_libraries && diagnostics.refresh_libraries.success === false && diagnostics.refresh_libraries.message) {
                parts.push(`媒体刷新列表：${diagnostics.refresh_libraries.message}`);
            }
            const summary = diagnostics.summary || {};
            if (summary.success === false && summary.message) {
                parts.push(`统计：${summary.message}`);
            }
            if (!parts.length) return "";
            return `<p class="media-diagnostic-hint">${parts.map(escapeHtml).join("；")}</p>`;
        }

        function renderMediaStats() {
            const box = $("mediaStatGrid");
            if (!box) return;
            const categories = adminState.mediaCategories || [];
            const summary = adminState.mediaSummary || {};
            const total = summary.total ?? categories.reduce((sum, item) => sum + Number(item.count || 0), 0);
            const cards = [
                renderMediaStatCard({ label: "总资源", count: total, sub: `电影 ${summary.movie ?? "-"} / 剧集 ${summary.tv ?? "-"} / 视频 ${summary.video ?? "-"}`, tone: "total" }),
                ...categories.map((item) =>
                    renderMediaStatCard({
                        label: item.label || item.key,
                        count: item.found ? item.count : "-",
                        sub: item.found ? `飞牛库：${item.library_title || item.fnos_lib}` : `未匹配：${item.fnos_lib || item.label}`,
                        tone: item.key || "default",
                    })
                ),
            ];
            box.innerHTML = cards.join("");
        }

        function renderMediaStatCard(item) {
            return `
                <article class="media-stat-card tone-${escapeHtml(item.tone || "default")}">
                    <span class="media-card-title">${escapeHtml(item.label || "-")}</span>
                    <strong>${formatNumber(item.count)}</strong>
                    <small>${escapeHtml(item.sub || "")}</small>
                </article>
            `;
        }

        function renderMediaLibraries() {
            const body = $("mediaLibraryBody");
            if (!body) return;
            if (!adminState.mediaLibraries.length) {
                body.innerHTML = `<tr><td colspan="5" class="empty-cell">暂无飞牛媒体库数据，或接口暂不可用。</td></tr>`;
                return;
            }
            body.innerHTML = adminState.mediaLibraries.map(renderMediaLibraryRow).join("");
            body.querySelectorAll("[data-media-refresh]").forEach((button) => {
                button.addEventListener("click", () => refreshMediaItem(adminState.mediaLibraries[Number(button.dataset.mediaRefresh)], button));
            });
            body.querySelectorAll("[data-media-detail]").forEach((button) => {
                button.addEventListener("click", () => showMediaLibraryDetail(Number(button.dataset.mediaDetail)));
            });
        }

        function renderMediaLibraryRow(item, index) {
            const rowKey = mediaRowKey(item);
            const posters = (item.posters || []).slice(0, 4);
            const matched = item.matched_category_key;
            const status = mediaStatusForItem(item);
            return `
                <tr>
                    <td>
                        <div class="media-library-cell">
                            <strong title="${escapeHtml(item.title || "-")}">${escapeHtml(item.title || "-")}</strong>
                            <small title="${escapeHtml(item.guid || "")}">${escapeHtml(item.guid || "无 GUID")}</small>
                            ${posters.length ? `<div class="media-poster-strip">${posters.map((src) => `<img src="${escapeHtml(src)}" loading="lazy" alt="" onerror="this.hidden=true">`).join("")}</div>` : ""}
                        </div>
                    </td>
                    <td>${escapeHtml(item.category_label || item.category || "未知")}</td>
                    <td><strong class="media-count">${formatNumber(item.count)}</strong></td>
                    <td>
                        <div class="media-target-meta">
                            ${matched ? `<span class="pill info">${escapeHtml(item.matched_category_label || matched)}</span>` : `<span class="pill warn">未匹配分类</span>`}
                            <small title="${escapeHtml(item.target_path || "")}">${escapeHtml(item.target_path || "未配置目标目录")}</small>
                            ${item.fnos_dir_list?.length ? `<small title="${escapeHtml(item.fnos_dir_list.join(", "))}">真实刷新目录：${escapeHtml(item.fnos_dir_list.join("，"))}</small>` : ""}
                        </div>
                    </td>
                    <td class="media-status-action-cell">
                        <div class="media-status-action">
                            <div class="media-status-stack">
                                <span class="${status.className}" id="mediaStatus-${rowKey}">${escapeHtml(status.label)}</span>
                                <small class="media-refresh-message" id="mediaMessage-${rowKey}">${escapeHtml(status.message)}</small>
                            </div>
                            <div class="table-actions">
                                <button class="secondary mini media-refresh-btn" type="button" data-media-refresh="${index}" ${item.refreshable && item.dir_refresh ? "" : "disabled"} ${matched ? `data-category="${escapeHtml(matched)}"` : ""}>刷新</button>
                                <button class="secondary mini" type="button" data-media-detail="${index}">详情</button>
                            </div>
                        </div>
                    </td>
                </tr>
            `;
        }

        function mediaStatusForItem(item) {
            if (item.running) {
                return { className: "pill warn", label: "刷新中", message: "飞牛当前存在该媒体库扫描任务" };
            }
            if (!item.refreshable) {
                return { className: "pill error", label: "不可刷新", message: "接口未返回 GUID，无法触发扫描" };
            }
            if (item.fnos_dir_list?.length) {
                return { className: "pill ok", label: "可局部刷新", message: `将扫描 ${item.fnos_dir_list.length} 个目录` };
            }
            return { className: "pill error", label: "需配置目录", message: "未匹配到真实刷新目录，已禁止整库刷新" };
        }

        function renderMediaMappings() {
            const box = $("mediaMappingList");
            if (!box) return;
            const categories = adminState.mediaCategories || [];
            if (!categories.length) {
                box.innerHTML = `<div><span>暂无分类映射</span><code>-</code></div>`;
                return;
            }
            box.innerHTML = categories.map((item) => `
                <div class="media-mapping-row">
                    <span>
                        <strong>${escapeHtml(item.label || item.key)}</strong>
                        <small>${item.found ? `飞牛库：${escapeHtml(item.library_title || item.fnos_lib || "-")}` : `未匹配飞牛库：${escapeHtml(item.fnos_lib || "-")}`}</small>
                    </span>
                    <code title="${escapeHtml((item.fnos_dir_list || []).join(", "))}">${escapeHtml((item.fnos_dir_list || []).join("，") || item.target_path || "-")}</code>
                    <button class="secondary mini media-category-refresh-btn" type="button" data-category="${escapeHtml(item.key)}" ${item.refreshable && item.fnos_dir_list?.length ? "" : "disabled"}>刷新</button>
                </div>
            `).join("");
            box.querySelectorAll(".media-category-refresh-btn").forEach((button) => {
                button.addEventListener("click", () => refreshMediaCategory(button.dataset.category, button));
            });
        }

        function showMediaLibraryDetail(index) {
            const item = adminState.mediaLibraries[index] || {};
            const rowKey = mediaRowKey(item);
            const posters = (item.posters || []).slice(0, 6);
            const running = item.running_tasks || [];
            openDrawer(
                `媒体库：${item.title || "-"}`,
                `
                <div class="detail-grid">
                    <div><span>媒体库名称</span>${escapeHtml(item.title || "-")}</div>
                    <div><span>GUID</span><code>${escapeHtml(item.guid || "-")}</code></div>
                    <div><span>飞牛类型</span>${escapeHtml(item.category_label || item.category || "未知")}</div>
                    <div><span>资源数量</span>${formatNumber(item.count)}</div>
                    <div><span>命中分类</span>${escapeHtml(item.matched_category_label || item.matched_category_key || "未匹配")}</div>
                    <div><span>目标目录</span>${escapeHtml(item.target_path || "-")}</div>
                </div>
                <h4>局部刷新目录</h4>
                <div class="notice-box">${(item.fnos_dir_list || []).length ? item.fnos_dir_list.map((dir) => `<code>${escapeHtml(dir)}</code>`).join(" ") : "未命中分类真实目录，已禁止无目录限制的整库刷新。"}</div>
                ${posters.length ? `<h4>海报预览</h4><div class="media-poster-strip detail">${posters.map((src) => `<img src="${escapeHtml(src)}" loading="lazy" alt="" onerror="this.hidden=true">`).join("")}</div>` : ""}
                <h4>运行中任务</h4>
                <div class="list-box">${running.length ? running.map((task) => `<div class="list-item"><div><strong>${escapeHtml(task.name || task.title || task.type || "飞牛任务")}</strong><p>${escapeHtml(mediaTaskSummary(task))}</p></div></div>`).join("") : `<div class="empty">当前没有匹配的运行中扫描任务</div>`}</div>
                <div class="drawer-action-stack">
                    <button data-media-drawer-refresh="${index}" ${item.refreshable ? "" : "disabled"}>刷新该媒体库</button>
                </div>
                `
            );
            $("adminDrawerBody").querySelector("[data-media-drawer-refresh]")?.addEventListener("click", (event) => refreshMediaItem(item, event.currentTarget, rowKey));
        }

        function refreshPayloadFromItem(item = {}) {
            if (item.guid && item.fnos_dir_list?.length) {
                return { guid: item.guid, library: item.title || "", dir_list: item.fnos_dir_list };
            }
            if (item.matched_category_key) {
                return { category: item.matched_category_key };
            }
            if (item.guid) {
                return { guid: item.guid, library: item.title || "" };
            }
            return { library: item.title || "" };
        }

        async function refreshMediaItem(item, button = null, forcedRowKey = "") {
            if (!item) return;
            const rowKey = forcedRowKey || mediaRowKey(item);
            if (button) button.disabled = true;
            setMediaRefreshState(rowKey, "warn", "正在请求飞牛媒体库刷新接口...");
            try {
                const data = await api("/api/admin/media/refresh", {
                    method: "POST",
                    body: JSON.stringify(refreshPayloadFromItem(item)),
                    allowFailure: true,
                });
                const ok = data.success !== false;
                const message = mediaRefreshMessage(data);
                setMediaRefreshState(rowKey, ok ? "ok" : "error", message, data);
                toast(message, ok ? "ok" : "error");
            } catch (error) {
                setMediaRefreshState(rowKey, "error", error.message);
                toast(error.message, "error");
            } finally {
                if (button) button.disabled = false;
            }
        }

        async function refreshMediaCategory(category, button = null) {
            if (!category) return;
            const item = (adminState.mediaLibraries || []).find((library) => library.matched_category_key === category);
            const rowKey = item ? mediaRowKey(item) : category;
            if (button) button.disabled = true;
            setMediaRefreshState(rowKey, "warn", "正在请求飞牛刷新接口...");
            try {
                const data = await api("/api/admin/media/refresh", {
                    method: "POST",
                    body: JSON.stringify({ category }),
                    allowFailure: true,
                });
                const ok = data.success !== false;
                const message = mediaRefreshMessage(data);
                setMediaRefreshState(rowKey, ok ? "ok" : "error", message, data);
                toast(message, ok ? "ok" : "error");
            } catch (error) {
                setMediaRefreshState(rowKey, "error", error.message);
                toast(error.message, "error");
            } finally {
                if (button) button.disabled = false;
            }
        }

        async function refreshAllMedia() {
            const buttons = Array.from(document.querySelectorAll(".media-category-refresh-btn[data-category]")).filter((button) => !button.disabled);
            if (!buttons.length) return;
            for (const button of buttons) {
                await refreshMediaCategory(button.dataset.category, button);
            }
        }

        return Object.freeze({
            formatNumber,
            mediaRowKey,
            setMediaRefreshState,
            mediaRefreshMessage,
            loadMediaLibraries,
            renderMediaDiagnosticHint,
            renderMediaStats,
            renderMediaStatCard,
            renderMediaLibraries,
            renderMediaLibraryRow,
            mediaStatusForItem,
            renderMediaMappings,
            showMediaLibraryDetail,
            refreshPayloadFromItem,
            refreshMediaItem,
            refreshMediaCategory,
            refreshAllMedia,
        });
    }
    window.FnosAdminMedia = Object.freeze({ create });
})();
