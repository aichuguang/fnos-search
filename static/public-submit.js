(function () {
    function create(context) {
        const {
            state, getElement, api, toast, escapeHtml, icon, formatDate,
            categoryLabel, sourceLabel, publicRouteLabel, publicStatusForDisplay, publicDefaultMessage, publicMessageForDisplay,
            showGlobalLoading, hideGlobalLoading, showPublicModal, hidePublicModal,
            resetNoticeBox,
            syncCategorySelect, activatePublicTab, categoryIcons,
            publicResourceById, isBtbtlaCandidate, isSixpanCandidate, isQuarkCandidate, isCloud139Candidate,
            recommendedCategoryForItem, isResourceInvalid, ensurePublicResourceDetail, updatePublicResourceState,
            refreshPublicResourceViews, renderPublicResults, categoryConfidenceText, knownSizeText,
            bindPreviewInteractions, sixpanSubmitSpeed, isInspectionInvalid,
            categoryForPublicResource, selectPublicResource, sixpanHasSelectableFiles,
        } = context;
        const publicState = state;
        const $ = getElement;
        const workbench = window.FnosResourceSearchWorkbench;
        const CATEGORY_ICON = categoryIcons;
        const RECENT_REQUESTS_STORAGE_KEY = "fnos.public.recentRequests.v1";
        const RECENT_REQUESTS_LIMIT = 5;

        function loadRecentRequests() {
            try {
                const value = window.localStorage.getItem(RECENT_REQUESTS_STORAGE_KEY);
                const items = value ? JSON.parse(value) : [];
                if (!Array.isArray(items)) return [];
                return items
                    .map((item) => ({
                        title: String(item?.title || "").trim().slice(0, 300),
                        token: String(item?.token || "").trim().slice(0, 80),
                    }))
                    .filter((item) => item.title && item.token)
                    .slice(0, RECENT_REQUESTS_LIMIT);
            } catch (_error) {
                return [];
            }
        }

        function renderRecentRequests() {
            const section = $("requestRecentSection");
            const list = $("requestRecentList");
            if (!section || !list) return;
            const items = loadRecentRequests();
            section.hidden = items.length === 0;
            list.innerHTML = items.map((item) => `
                <button class="request-recent-row" type="button" data-recent-request-token="${escapeHtml(item.token)}">
                    <span class="request-recent-name">${escapeHtml(item.title)}</span>
                    <span class="request-recent-token">${escapeHtml(item.token)}</span>
                </button>
            `).join("");
        }

        function saveRecentRequest(titleValue, tokenValue) {
            const title = String(titleValue || "").trim().slice(0, 300);
            const token = String(tokenValue || "").trim().slice(0, 80);
            if (!title || !token) return;
            const items = loadRecentRequests().filter((item) => item.token !== token);
            items.unshift({ title, token });
            try {
                window.localStorage.setItem(RECENT_REQUESTS_STORAGE_KEY, JSON.stringify(items.slice(0, RECENT_REQUESTS_LIMIT)));
            } catch (_error) {
                return;
            }
            renderRecentRequests();
        }

        function bindRecentRequests() {
            const list = $("requestRecentList");
            renderRecentRequests();
            list?.addEventListener("click", (event) => {
                const row = event.target.closest("[data-recent-request-token]");
                if (!row || !list.contains(row)) return;
                const token = String(row.dataset.recentRequestToken || "").trim();
                if (!token) return;
                if ($("requestTokenInput")) $("requestTokenInput").value = token;
                queryRequestStatus(token);
            });
        }

        function isPublicSubmitConfirmOpen(publicId = "") {
            const modal = $("publicSubmitConfirm");
            if (!modal || modal.classList.contains("hidden")) return false;
            if (!publicId) return Boolean(publicState.pendingSubmit?.publicId);
            return publicState.pendingSubmit?.publicId === publicId;
        }

        function renderPublicResourceDetail(state) {
            if (!state || state.loading) {
                return `
                    <section class="resource-detail-panel glass-panel soft">
                        <div class="detail-loading">${icon("refresh")}<span>正在读取资源详情，请稍候...</span></div>
                    </section>
                `;
            }
            if (state.error) {
                return `<section class="resource-detail-panel glass-panel soft"><div class="notice-box status-error">${escapeHtml(state.error)}</div></section>`;
            }
            const detail = workbench.detailValue(state.detail || {});
            const suggestion = detail.category_suggestion || {};
            const inspection = detail.inspection || {};
            const summary = inspection.summary || {};
            const items = inspection.items || [];
            const link = detail.link || {};
            const capability = detail.detail_capability || {};
            const previewPublicId = detail.public_id || publicState.selectedPublicId;
            const previewItems = Array.isArray(items) ? items : [];
            const detailSize = knownSizeText(summary.total_size_text, detail.size_text);
            const detailDate = formatDate(detail.datetime);
            const btbtlaDetail = isBtbtlaCandidate(detail) || isBtbtlaCandidate(link) || inspection.provider === "btbtla";
            const sixpanDetail = !btbtlaDetail && (isSixpanCandidate(detail) || isSixpanCandidate(link));
            const quarkDetail = isQuarkCandidate(detail) || isQuarkCandidate(link) || inspection.provider === "quark";
            const cloud139Detail = isCloud139Candidate(detail) || isCloud139Candidate(link) || inspection.provider === "cloud139";
            const sixpanState = sixpanDetail ? sixpanParseState(sixpanPublicKey(previewPublicId)) : null;
            const statusClass = sixpanDetail
                ? sixpanState?.success ? "ok" : "warn"
                : inspection.success ? "ok" : inspection.status === "reserved" ? "warn" : "error";
            const statusText = sixpanDetail
                ? sixpanState?.success ? "可快速入库" : (sixpanState?.slow || sixpanState?.error) ? "慢速入库" : "内容预览中"
                : inspection.status === "reserved" ? "暂不支持预览" : inspection.success ? "检测通过" : "检测未通过";
            return `
                <section class="resource-detail-panel glass-panel soft">
                    <div class="section-title compact">
                        <div>
                            <h3>资源详情</h3>
                            <p>已读取基础信息</p>
                        </div>
                        <span class="pill ${statusClass}">${escapeHtml(statusText)}</span>
                    </div>
                    <div class="resource-detail-grid">
                        <div><span>标题</span><strong>${escapeHtml(detail.title || "-")}</strong></div>
                        <div><span>来源</span><strong>${escapeHtml(sourceLabel(detail.source_type || link.source_type || ""))}</strong></div>
                        <div><span>入库方式</span><strong>${escapeHtml(publicRouteLabel(detail.route || link.route || "", detail.source_type || link.source_type || ""))}</strong></div>
                        <div><span>资源大小</span><strong>${escapeHtml(detailSize || "未知")}</strong></div>
                        <div><span>文件数量</span><strong>${escapeHtml(summary.file_count || "-")}</strong></div>
                        <div><span>建议分类</span><strong>${escapeHtml(suggestion.label || categoryLabel(suggestion.key || publicState.selectedCategory || "movie"))}${suggestion.confidence ? ` · ${escapeHtml(categoryConfidenceText(suggestion.confidence))}` : ""}</strong></div>
                        <div><span>当前提交分类</span><strong>${escapeHtml(categoryLabel(publicState.selectedCategory || "movie"))}</strong></div>
                        <div><span>更新时间</span><strong>${escapeHtml(detailDate)}</strong></div>
                    </div>
                    ${btbtlaDetail ? renderBtbtlaResourcePreview(previewPublicId, inspection) : sixpanDetail ? renderSixpanParseHtml(sixpanPublicKey(previewPublicId), { context: "detail" }) : previewItems.length ? (quarkDetail ? renderQuarkFilePreview(previewPublicId, previewItems) : cloud139Detail ? renderCloud139FilePreview(previewPublicId, previewItems) : renderFilePreview(previewPublicId, previewItems)) : renderEmptyFilePreview(inspection, capability)}
                </section>
            `;
        }

        function renderBtbtlaResourcePreview(publicId, inspection = {}) {
            const items = Array.isArray(inspection.items) ? inspection.items : [];
            if (!items.length) {
                return `<div class="sixpan-empty-note">${escapeHtml(publicMessageForDisplay(inspection.message || "未找到可解析的 BT 下载资源。", inspection.status || "检测未通过", "未找到可解析的 BT 下载资源。"))}</div>`;
            }
            const recommendedId = String(inspection.recommended?.id || inspection.summary?.recommended_id || items[0]?.id || "");
            const recommended = items.find((item) => String(item.id || "") === recommendedId) || items[0];
            const others = items.filter((item) => String(item.id || "") !== String(recommended?.id || ""));
            const hasResolveError = items.some((item) => btbtlaResolveState(publicId, item).error);
            return `
                <div class="btbtla-preview">
                    <div class="sixpan-parse-head">
                        <div>
                            <strong>选择下载资源并解析磁链</strong>
                            <small>已按整季/质量/下载量/大小综合推荐；解析成功后进入 BT 文件预览。</small>
                        </div>
                    </div>
                    <div class="btbtla-recommend">
                        <div class="btbtla-section-title">推荐资源</div>
                        ${renderBtbtlaResourceRow(publicId, recommended, true)}
                    </div>
                    ${others.length ? `
                        <details class="advanced-details btbtla-other" ${hasResolveError ? "open" : ""}>
                            <summary>查看其他下载资源（${escapeHtml(others.length)} 条）</summary>
                            <div class="btbtla-resource-list">
                                ${others.map((item) => renderBtbtlaResourceRow(publicId, item, false)).join("")}
                            </div>
                        </details>
                    ` : ""}
                </div>
            `;
        }

        function btbtlaResolveKey(publicId, item = {}) {
            return `${publicId || ""}:${item.id || item.url || item.title || ""}`;
        }

        function btbtlaResolveState(publicId, item = {}) {
            return publicState.btbtlaResolves[btbtlaResolveKey(publicId, item)] || {};
        }

        function setBtbtlaResolveState(publicId, item = {}, patch = {}) {
            const key = btbtlaResolveKey(publicId, item);
            publicState.btbtlaResolves[key] = { ...(publicState.btbtlaResolves[key] || {}), ...patch };
            refreshPublicResourceViews(publicId);
        }

        function renderBtbtlaResourceRow(publicId, item = {}, recommended = false) {
            const reasons = Array.isArray(item.reasons) ? item.reasons.filter(Boolean).slice(0, 4) : [];
            const state = btbtlaResolveState(publicId, item);
            const loading = Boolean(state.loading);
            const error = String(state.error || "");
            const success = Boolean(state.success);
            const buttonText = loading ? "解析中..." : error ? "重试解析" : success ? "重新解析" : "解析并预览";
            return `
                <div class="btbtla-resource-row ${recommended ? "recommended" : ""} ${error ? "resolve-failed" : ""} ${success ? "resolve-success" : ""}">
                    <div class="btbtla-resource-main">
                        <strong>${recommended ? "✅ " : ""}${escapeHtml(item.title || "-")}</strong>
                        <small>
                            ${item.quality ? `<span>${escapeHtml(item.quality)}</span>` : ""}
                            ${item.size_text ? `<span>${escapeHtml(item.size_text)}</span>` : ""}
                            ${item.download_count ? `<span>下载 ${escapeHtml(item.download_count)}</span>` : ""}
                            ${item.score ? `<span>推荐分 ${escapeHtml(item.score)}</span>` : ""}
                        </small>
                        ${reasons.length ? `<p>${escapeHtml(reasons.join(" / "))}</p>` : ""}
                        ${error ? `<p class="btbtla-resolve-error">解析失败：${escapeHtml(error)}。可以重试本条，或继续尝试其他下载资源。</p>` : ""}
                        ${success ? `<p class="btbtla-resolve-success">已解析成功。</p>` : ""}
                    </div>
                    <button class="${recommended ? "" : "secondary"} mini" type="button"
                        data-btbtla-resolve="1"
                        data-public-id="${escapeHtml(publicId)}"
                        data-resource-id="${escapeHtml(item.id || "")}"
                        data-resource-url="${escapeHtml(item.url || "")}"
                        data-resource-title="${escapeHtml(item.title || "")}"
                        ${loading ? "disabled" : ""}>
                        ${escapeHtml(buttonText)}
                    </button>
                </div>
            `;
        }

        async function resolveBtbtlaResource(button) {
            const publicId = button.dataset.publicId || "";
            if (!publicId) return;
            const resourceId = button.dataset.resourceId || "";
            const resourceUrl = button.dataset.resourceUrl || "";
            const resourceTitle = button.dataset.resourceTitle || "";
            const resourceStateItem = { id: resourceId, url: resourceUrl, title: resourceTitle };
            const key = sixpanPublicKey(publicId);
            const originalItem = publicResourceById(publicId) ? { ...publicResourceById(publicId) } : null;
            const originalDetail = publicState.btbtlaDetails[publicId] || publicState.details[publicId]?.detail || null;
            if (originalDetail && (originalDetail.inspection?.provider === "btbtla" || isBtbtlaCandidate(originalDetail))) {
                publicState.btbtlaDetails[publicId] = originalDetail;
            }
            setBtbtlaResolveState(publicId, resourceStateItem, { loading: true, error: "", success: false });
            try {
                const data = await api("/api/public/btbtla/resolve", {
                    method: "POST",
                    body: JSON.stringify({ public_id: publicId, resource_id: resourceId, resource_url: resourceUrl, resource_title: resourceTitle }),
                    allowFailure: true,
                });
                if (data.success === false) throw new Error(data.message || "磁链解析失败");
                const current = publicResourceById(publicId);
                if (data.item) {
                    if (current) Object.assign(current, data.item);
                }
                publicState.details[publicId] = { loading: false, detail: data.detail || {}, success: true };
                publicState.sixpanParses[key] = { loading: true, items: [], selected: {}, recommended: {}, source: "public", publicId };
                refreshPublicResourceViews(publicId);
                await loadSixpanParseForPublic(publicId, publicResourceById(publicId), true);
                const parseState = sixpanParseState(key);
                if (!parseState?.success || !sixpanHasSelectableFiles(parseState)) {
                    const message = parseState?.error || parseState?.message || "该磁链暂时无法预览可入库内容";
                    if (originalItem && current) Object.assign(current, originalItem);
                    if (publicState.btbtlaDetails[publicId]) {
                        publicState.details[publicId] = { loading: false, detail: publicState.btbtlaDetails[publicId], success: true };
                    }
                    delete publicState.sixpanParses[key];
                    throw new Error(message);
                }
                setBtbtlaResolveState(publicId, resourceStateItem, { loading: false, error: "", success: true });
                toast(data.message || "磁链解析成功", "success");
                openPublicSubmitConfirm(publicId, { skipSlowWarning: true });
            } catch (error) {
                const current = publicResourceById(publicId);
                if (originalItem && current) Object.assign(current, originalItem);
                if (publicState.btbtlaDetails[publicId]) {
                    publicState.details[publicId] = { loading: false, detail: publicState.btbtlaDetails[publicId], success: true };
                }
                delete publicState.sixpanParses[key];
                setBtbtlaResolveState(publicId, resourceStateItem, { loading: false, error: error.message || "解析失败", success: false });
                throw error;
            }
        }

        function renderEmptyFilePreview(inspection = {}, capability = {}) {
            return "";
        }

        function previewKey(publicId, fid) {
            return `${publicId || ""}:${fid || ""}`;
        }

        function closePreviewBranch(publicId, fid, visited = null) {
            if (!publicId || !fid) return;
            const seen = visited instanceof Set ? visited : new Set();
            const key = previewKey(publicId, fid);
            if (seen.has(key)) return;
            seen.add(key);
            const current = publicState.filePreviews[key];
            if (!current) return;
            publicState.filePreviews[key] = { ...current, open: false };
            (Array.isArray(current.items) ? current.items : []).forEach((item) => {
                const childFid = String(item?.fid || "");
                if (childFid) closePreviewBranch(publicId, childFid, seen);
            });
        }

        function renderFilePreview(publicId, items, level = 0) {
            const safeItems = Array.isArray(items) ? items : [];
            const expandable = safeItems.some((file) => file && file.can_expand && file.fid);
            return `
                <div class="file-preview ${level ? "nested" : ""}">
                    ${level ? "" : `<div class="file-preview-title">
                        <span>目录/文件预览（前 ${safeItems.length} 项）</span>
                        <small>${expandable ? "包含可展开文件夹" : "目录概览"}</small>
                    </div>`}
                    <ol>
                        ${safeItems.map((file) => renderFilePreviewItem(publicId, file, level)).join("")}
                    </ol>
                </div>
            `;
        }

        function renderFilePreviewItem(publicId, file, level = 0) {
            const isDir = Boolean(file.is_dir);
            const canExpand = Boolean(file.can_expand && file.fid);
            const key = previewKey(publicId, file.fid);
            const state = publicState.filePreviews[key] || {};
            const open = Boolean(state.open);
            const rowContent = `
                <span>${isDir ? icon(open ? "folder-open" : "folder") : icon("file")}${escapeHtml(file.name || "-")}</span>
                <small>${escapeHtml(isDir ? "文件夹" : file.size_text || "-")}</small>
                ${canExpand ? `<i aria-hidden="true">${open ? "收起" : "展开"}</i>` : ""}
            `;
            return `
                <li class="${isDir ? "is-dir" : ""} ${open ? "open" : ""}">
                    ${canExpand ? `
                        <button class="file-preview-row" type="button" data-file-toggle="1" data-file-public-id="${escapeHtml(publicId)}" data-file-fid="${escapeHtml(file.fid)}" aria-expanded="${open ? "true" : "false"}">
                            ${rowContent}
                        </button>
                    ` : `<div class="file-preview-row">${rowContent}</div>`}
                    ${open ? renderFolderChildren(publicId, file.fid, state, level + 1) : ""}
                </li>
            `;
        }

        function renderFolderChildren(publicId, fid, state, level) {
            if (state.loading) {
                return `<div class="folder-children loading">${icon("refresh")}正在读取目录...</div>`;
            }
            if (state.error) {
                return `<div class="folder-children error">${escapeHtml(state.error)}</div>`;
            }
            const items = state.items || [];
            if (!items.length) {
                return `<div class="folder-children empty">该目录暂无可预览条目。</div>`;
            }
            return `<div class="folder-children">${renderFilePreview(publicId, items, level)}</div>`;
        }

        async function toggleResourceFolder(publicId, fid) {
            if (!publicId || !fid) return;
            const key = previewKey(publicId, fid);
            const current = publicState.filePreviews[key] || {};
            if (current.open && (current.items || current.error)) {
                closePreviewBranch(publicId, fid);
                refreshPublicResourceViews(publicId);
                return;
            }
            publicState.filePreviews[key] = { ...current, open: true, loading: !current.items };
            refreshPublicResourceViews(publicId);
            if (current.items) return;
            try {
                const data = await api(`/api/public/resources/${encodeURIComponent(publicId)}/files?fid=${encodeURIComponent(fid)}`, { allowFailure: true });
                publicState.filePreviews[key] = {
                    open: true,
                    loading: false,
                    items: data.items || [],
                    message: data.message || "",
                    success: data.success !== false,
                };
            } catch (error) {
                publicState.filePreviews[key] = { open: true, loading: false, error: error.message };
            }
            refreshPublicResourceViews(publicId);
        }

        function quarkSelectionState(publicId) {
            const state = publicState.quarkSelections[publicId];
            if (state && typeof state === "object") return state;
            return { mode: "all", rootDirs: {}, baseDir: null, items: {} };
        }

        function setQuarkSelectionState(publicId, patch = {}) {
            const current = quarkSelectionState(publicId);
            publicState.quarkSelections[publicId] = { ...current, ...patch };
        }

        function quarkItemPayloadFromDataset(dataset = {}) {
            const fid = String(dataset.quarkFid || "").trim();
            const name = String(dataset.quarkName || fid || "未命名文件").trim();
            const isDir = dataset.quarkDir === "1";
            return { fid, name, type: isDir ? "dir" : "file", is_dir: isDir };
        }

        function quarkDirPayload(item = {}) {
            const fid = String(item.fid || item.id || "").trim();
            const name = String(item.name || fid || "未命名文件夹").trim();
            return { fid, name, type: "dir", is_dir: true };
        }

        function quarkLoadedChildItems(publicId, baseFid) {
            if (!publicId || !baseFid) return [];
            const preview = publicState.filePreviews[previewKey(publicId, baseFid)] || {};
            return (Array.isArray(preview.items) ? preview.items : []).filter((item) => item?.fid);
        }

        function quarkSubdirSelectionCoversParent(publicId, state, selectedItems = null) {
            const baseFid = state?.baseDir?.fid || "";
            if (!baseFid) return false;
            const children = quarkLoadedChildItems(publicId, baseFid);
            if (!children.length) return false;
            const selected = selectedItems || state.items || {};
            return children.every((item) => Boolean(selected[item.fid]));
        }

        function collapseQuarkSubdirSelectionIfComplete(publicId, selectedItems = null) {
            const state = quarkSelectionState(publicId);
            if (state.mode !== "subdir_items" || !quarkSubdirSelectionCoversParent(publicId, state, selectedItems)) return false;
            const parentDir = quarkDirPayload(state.baseDir || {});
            if (!parentDir.fid) return false;
            publicState.quarkSelections[publicId] = {
                mode: "root_dirs",
                rootDirs: { [parentDir.fid]: parentDir },
                baseDir: null,
                items: {},
            };
            return true;
        }

        function quarkSelectionSummaryText(publicId) {
            const state = quarkSelectionState(publicId);
            if (state.mode === "root_dirs") {
                const count = Object.values(state.rootDirs || {}).filter(Boolean).length;
                return count ? `只保存已勾选的 ${count} 个根目录文件夹` : "默认保存整个分享";
            }
            if (state.mode === "subdir_items") {
                const count = Object.values(state.items || {}).filter(Boolean).length;
                const baseName = state.baseDir?.name || "指定文件夹";
                return count ? `只保存「${baseName}」中已勾选的 ${count} 个条目` : "未勾选时默认保存整个分享";
            }
            return "默认保存整个分享";
        }

        const QUARK_PREVIEW_MAX_DEPTH = 20;

        function renderQuarkFilePreview(publicId, items, level = 0, parent = null, ancestors = null) {
            const safeItems = Array.isArray(items) ? items : [];
            const branchAncestors = ancestors instanceof Set ? ancestors : new Set();
            return `
                <div class="file-preview cloud-file-preview quark-file-preview ${level ? "nested" : ""}">
                    ${level ? "" : `<div class="file-preview-title compact">
                        <span>目录/文件预览（前 ${safeItems.length} 项）</span>
                        <small>${escapeHtml(quarkSelectionSummaryText(publicId))}</small>
                    </div>`}
                    <ol>
                        ${safeItems.map((file) => renderQuarkFilePreviewItem(publicId, file, level, parent, branchAncestors)).join("")}
                    </ol>
                </div>
            `;
        }

        function renderQuarkFilePreviewItem(publicId, file, level = 0, parent = null, ancestors = null) {
            const state = quarkSelectionState(publicId);
            const isDir = Boolean(file.is_dir);
            const fid = String(file.fid || "");
            const name = String(file.name || "-");
            const branchAncestors = ancestors instanceof Set ? ancestors : new Set();
            const cyclic = Boolean(fid && branchAncestors.has(fid));
            const depthLimited = level >= QUARK_PREVIEW_MAX_DEPTH;
            const canExpand = Boolean(file.can_expand && fid && !cyclic && !depthLimited);
            const key = previewKey(publicId, fid);
            const childState = publicState.filePreviews[key] || {};
            const baseFid = state.baseDir?.fid || "";
            const open = canExpand && Boolean(childState.open || (state.mode === "subdir_items" && baseFid && baseFid === fid));
            const isRoot = level === 0;
            const isBaseChildren = state.mode === "subdir_items" && parent?.fid && parent.fid === baseFid && level === 1;
            const rootChecked = Boolean(state.mode === "root_dirs" && state.rootDirs?.[fid]);
            const itemChecked = Boolean(state.mode === "subdir_items" && state.items?.[fid]);
            const rootSelectable = isRoot && isDir && fid;
            const childSelectable = isBaseChildren && fid;
            const rowClasses = [
                isDir ? "is-dir" : "",
                open ? "open" : "",
                rootChecked || itemChecked ? "selected" : "",
                state.mode === "subdir_items" && baseFid === fid ? "active-base" : "",
                cyclic ? "cycle-guard" : "",
                depthLimited ? "depth-guard" : "",
            ].filter(Boolean).join(" ");
            const checkbox = rootSelectable
                ? `<input type="checkbox" ${rootChecked ? "checked" : ""} data-quark-select="root_dir" data-quark-public-id="${escapeHtml(publicId)}" data-quark-fid="${escapeHtml(fid)}" data-quark-name="${escapeHtml(name)}" data-quark-dir="1" title="保存此文件夹">`
                : childSelectable
                    ? `<input type="checkbox" ${itemChecked ? "checked" : ""} data-quark-select="subdir_item" data-quark-public-id="${escapeHtml(publicId)}" data-quark-fid="${escapeHtml(fid)}" data-quark-name="${escapeHtml(name)}" data-quark-dir="${isDir ? "1" : "0"}" title="保存此条目">`
                    : `<span class="cloud-checkbox-placeholder quark-checkbox-placeholder" aria-hidden="true"></span>`;
            const metaText = isDir
                ? cyclic
                    ? "已在上层展示"
                    : depthLimited
                        ? "层级过深，已停止预览"
                        : canExpand
                            ? (rootSelectable && state.mode === "subdir_items" && baseFid === fid ? "当前文件夹" : "文件夹")
                            : "文件夹"
                : (file.size_text || "-");
            const openAttrs = canExpand
                ? `data-quark-open="1" data-quark-public-id="${escapeHtml(publicId)}" data-quark-fid="${escapeHtml(fid)}" data-quark-name="${escapeHtml(name)}" data-quark-dir="1" data-quark-level="${escapeHtml(level)}" role="button" tabindex="0" aria-expanded="${open ? "true" : "false"}"`
                : "";
            return `
                <li class="${rowClasses}">
                    <div class="file-preview-row cloud-file-row quark-file-row" ${openAttrs}>
                        ${checkbox}
                        <span>${isDir ? icon(open ? "folder-open" : "folder") : icon("file")}${escapeHtml(name)}</span>
                        <small>${escapeHtml(metaText)}</small>
                    </div>
                    ${open ? renderQuarkFolderChildren(publicId, fid, childState, level + 1, { fid, name, is_dir: isDir }, branchAncestors) : ""}
                </li>
            `;
        }

        function renderQuarkFolderChildren(publicId, fid, state, level, parent, ancestors = null) {
            if (state.loading) {
                return `<div class="folder-children loading">${icon("refresh")}正在读取目录...</div>`;
            }
            if (state.error) {
                return `<div class="folder-children error">${escapeHtml(state.error)}</div>`;
            }
            const items = state.items || [];
            if (!items.length) {
                return `<div class="folder-children empty">该目录暂无可预览条目。</div>`;
            }
            const nextAncestors = new Set(ancestors instanceof Set ? ancestors : []);
            if (fid) nextAncestors.add(String(fid));
            return `<div class="folder-children">${renderQuarkFilePreview(publicId, items, level, parent, nextAncestors)}</div>`;
        }

        async function ensureResourceFolderLoaded(publicId, fid) {
            if (!publicId || !fid) return null;
            const key = previewKey(publicId, fid);
            const current = publicState.filePreviews[key] || {};
            if (current.items && !current.loading) {
                publicState.filePreviews[key] = { ...current, open: true };
                refreshPublicResourceViews(publicId);
                return publicState.filePreviews[key];
            }
            publicState.filePreviews[key] = { ...current, open: true, loading: true };
            refreshPublicResourceViews(publicId);
            try {
                const data = await api(`/api/public/resources/${encodeURIComponent(publicId)}/files?fid=${encodeURIComponent(fid)}`, { allowFailure: true });
                publicState.filePreviews[key] = {
                    open: true,
                    loading: false,
                    items: data.items || [],
                    message: data.message || "",
                    success: data.success !== false,
                };
            } catch (error) {
                publicState.filePreviews[key] = { open: true, loading: false, error: error.message };
            }
            refreshPublicResourceViews(publicId);
            return publicState.filePreviews[key];
        }

        async function handleQuarkSelectionAction(button) {
            const publicId = button.dataset.quarkPublicId || "";
            const action = button.dataset.quarkAction || "";
            if (!publicId || !action) return;
            if (action === "all") {
                publicState.quarkSelections[publicId] = { mode: "all", rootDirs: {}, baseDir: null, items: {} };
                refreshPublicResourceViews(publicId);
                return;
            }
            if (action === "root-clear") {
                publicState.quarkSelections[publicId] = { mode: "all", rootDirs: {}, baseDir: null, items: {} };
                refreshPublicResourceViews(publicId);
                return;
            }
            if (action === "root-all") {
                const rootItems = quarkRootPreviewItems(publicId).filter((item) => item?.is_dir && item?.fid);
                const rootDirs = {};
                rootItems.forEach((item) => {
                    rootDirs[item.fid] = { fid: item.fid, name: item.name || item.fid, type: "dir", is_dir: true };
                });
                publicState.quarkSelections[publicId] = { mode: Object.keys(rootDirs).length ? "root_dirs" : "all", rootDirs, baseDir: null, items: {} };
                refreshPublicResourceViews(publicId);
                return;
            }
            if (action === "enter") {
                const baseDir = quarkItemPayloadFromDataset(button.dataset);
                if (!baseDir.fid) return;
                publicState.quarkSelections[publicId] = { mode: "subdir_items", rootDirs: {}, baseDir, items: {} };
                await ensureResourceFolderLoaded(publicId, baseDir.fid);
                return;
            }
            if (action === "subdir-clear") {
                setQuarkSelectionState(publicId, { items: {} });
                refreshPublicResourceViews(publicId);
                return;
            }
            if (action === "subdir-all") {
                const state = quarkSelectionState(publicId);
                const parentDir = quarkDirPayload(state.baseDir || {});
                if (parentDir.fid) {
                    publicState.quarkSelections[publicId] = {
                        mode: "root_dirs",
                        rootDirs: { [parentDir.fid]: parentDir },
                        baseDir: null,
                        items: {},
                    };
                }
                refreshPublicResourceViews(publicId);
            }
        }

        async function handleQuarkFolderOpen(row) {
            const publicId = row.dataset.quarkPublicId || "";
            const baseDir = quarkItemPayloadFromDataset(row.dataset);
            const level = Number(row.dataset.quarkLevel || 0) || 0;
            if (!publicId || !baseDir.fid) return;
            if (level > 0) {
                await toggleResourceFolder(publicId, baseDir.fid);
                return;
            }
            const current = quarkSelectionState(publicId);
            if (current.mode === "subdir_items" && current.baseDir?.fid === baseDir.fid) {
                closePreviewBranch(publicId, baseDir.fid);
                publicState.quarkSelections[publicId] = { mode: "all", rootDirs: {}, baseDir: null, items: {} };
                refreshPublicResourceViews(publicId);
                return;
            }
            publicState.quarkSelections[publicId] = { mode: "subdir_items", rootDirs: {}, baseDir, items: {} };
            await ensureResourceFolderLoaded(publicId, baseDir.fid);
        }

        function handleQuarkSelectionChange(input) {
            const publicId = input.dataset.quarkPublicId || "";
            const type = input.dataset.quarkSelect || "";
            if (!publicId || !type) return;
            const item = quarkItemPayloadFromDataset(input.dataset);
            if (!item.fid) return;
            if (type === "root_dir") {
                const current = quarkSelectionState(publicId);
                const rootDirs = { ...(current.rootDirs || {}) };
                if (input.checked) rootDirs[item.fid] = item;
                else delete rootDirs[item.fid];
                const hasSelected = Object.keys(rootDirs).length > 0;
                publicState.quarkSelections[publicId] = { mode: hasSelected ? "root_dirs" : "all", rootDirs, baseDir: null, items: {} };
                refreshPublicResourceViews(publicId);
                return;
            }
            if (type === "subdir_item") {
                const current = quarkSelectionState(publicId);
                const items = { ...(current.items || {}) };
                if (input.checked) items[item.fid] = item;
                else delete items[item.fid];
                if (input.checked && collapseQuarkSubdirSelectionIfComplete(publicId, items)) {
                    refreshPublicResourceViews(publicId);
                    return;
                }
                setQuarkSelectionState(publicId, { mode: "subdir_items", items });
                refreshPublicResourceViews(publicId);
            }
        }

        function quarkRootPreviewItems(publicId) {
            const detailState = publicState.details[publicId] || {};
            const detail = detailState.detail || {};
            const inspection = detail.inspection || {};
            return Array.isArray(inspection.items) ? inspection.items : [];
        }

        function quarkSelectionPayload(publicId) {
            const state = quarkSelectionState(publicId);
            if (state.mode === "root_dirs") {
                const selectedDirs = Object.values(state.rootDirs || {}).filter((item) => item?.fid && item?.is_dir);
                if (!selectedDirs.length) return {};
                return {
                    quark_selection: {
                        mode: "root_dirs",
                        selected_dirs: selectedDirs,
                    },
                };
            }
            if (state.mode === "subdir_items") {
                const baseDir = state.baseDir;
                const selectedItems = Object.values(state.items || {}).filter((item) => item?.fid);
                if (!baseDir?.fid) return {};
                // 文件夹展开只用于预览；未勾选任何条目时仍按完整分享提交。
                if (!selectedItems.length) return {};
                if (quarkSubdirSelectionCoversParent(publicId, state)) {
                    return {
                        quark_selection: {
                            mode: "root_dirs",
                            selected_dirs: [quarkDirPayload(baseDir)],
                        },
                    };
                }
                return {
                    quark_selection: {
                        mode: "subdir_items",
                        base_dir: baseDir,
                        selected_items: selectedItems,
                    },
                };
            }
            return {};
        }

        function cloud139SelectionState(publicId) {
            const state = publicState.cloud139Selections[publicId];
            if (state && typeof state === "object") return state;
            return { mode: "items", files: {}, folders: {} };
        }

        function cloud139SelectionKey(item = {}) {
            return String(item.path || item.share_fid_token || item.fid || item.id || "").trim();
        }

        function cloud139SelectionSummaryText(publicId) {
            const state = cloud139SelectionState(publicId);
            const fileCount = Object.values(state.files || {}).filter(Boolean).length;
            const folderCount = Object.values(state.folders || {}).filter(Boolean).length;
            const parts = [];
            if (fileCount) parts.push(`${fileCount} 个文件`);
            if (folderCount) parts.push(`${folderCount} 个文件夹`);
            return parts.length ? `只保存已勾选的 ${parts.join("、")}` : "未勾选时默认保存整个分享";
        }

        function renderCloud139FilePreview(publicId, items, level = 0) {
            const safeItems = Array.isArray(items) ? items : [];
            return `
                <div class="file-preview cloud-file-preview cloud139-file-preview ${level ? "nested" : ""}">
                    ${level ? "" : `<div class="file-preview-title compact">
                        <span>目录/文件预览（前 ${safeItems.length} 项）</span>
                        <small>${escapeHtml(cloud139SelectionSummaryText(publicId))}</small>
                    </div>`}
                    <ol>
                        ${safeItems.map((file) => renderCloud139FilePreviewItem(publicId, file, level)).join("")}
                    </ol>
                </div>
            `;
        }

        function renderCloud139FilePreviewItem(publicId, file, level = 0) {
            const state = cloud139SelectionState(publicId);
            const isDir = Boolean(file.is_dir);
            const fid = String(file.fid || "");
            const name = String(file.name || "-");
            const path = String(file.path || "");
            const shareFidToken = String(file.share_fid_token || "");
            const selectionItem = { fid, name, path, share_fid_token: shareFidToken, type: isDir ? "dir" : "file", is_dir: isDir };
            const selectionKey = cloud139SelectionKey(selectionItem);
            const selectable = Boolean(selectionKey);
            const checked = Boolean(isDir ? state.folders?.[selectionKey] : state.files?.[selectionKey]);
            const displayLevel = Math.max(0, Number(file.level || level || 0) || 0);
            const hasRootFiles = file.has_root_files === true || file.hasRootFiles === true;
            const canExpand = Boolean(isDir && file.can_expand && fid);
            const childState = publicState.filePreviews[previewKey(publicId, fid)] || {};
            const open = Boolean(canExpand && childState.open);
            const metaText = isDir ? (hasRootFiles ? "文件夹 · 含根层文件" : "文件夹") : (file.size_text || "-");
            const openAttrs = canExpand
                ? `data-cloud139-open="1" data-file-public-id="${escapeHtml(publicId)}" data-file-fid="${escapeHtml(fid)}" role="button" tabindex="0" aria-expanded="${open ? "true" : "false"}"`
                : "";
            return `
                <li class="${isDir ? "is-dir" : ""} ${open ? "open" : ""} ${checked ? "selected" : ""}" style="--preview-indent:${Math.min(displayLevel, 6)}">
                    <div class="file-preview-row cloud-file-row cloud139-file-row" ${openAttrs}>
                        ${selectable ? `<input type="checkbox" ${checked ? "checked" : ""} data-cloud139-select="${isDir ? "folder" : "file"}" data-cloud139-public-id="${escapeHtml(publicId)}" data-cloud139-fid="${escapeHtml(fid)}" data-cloud139-name="${escapeHtml(name)}" data-cloud139-path="${escapeHtml(path)}" data-cloud139-share-token="${escapeHtml(shareFidToken)}" data-cloud139-dir="${isDir ? "1" : "0"}" title="保存此${isDir ? "文件夹" : "文件"}">` : `<span class="cloud-checkbox-placeholder quark-checkbox-placeholder" aria-hidden="true"></span>`}
                        <span>${isDir ? icon(open ? "folder-open" : "folder") : icon("file")}${escapeHtml(name)}</span>
                        <small>${escapeHtml(metaText)}</small>
                    </div>
                    ${open ? renderCloud139FolderChildren(publicId, fid, childState, level + 1) : ""}
                </li>
            `;
        }

        function renderCloud139FolderChildren(publicId, fid, state, level) {
            if (state.loading) {
                return `<div class="folder-children loading">${icon("refresh")}正在读取目录...</div>`;
            }
            if (state.error) {
                return `<div class="folder-children error">${escapeHtml(state.error)}</div>`;
            }
            const items = state.items || [];
            if (!items.length) {
                return `<div class="folder-children empty">该目录暂无可预览条目。</div>`;
            }
            return `<div class="folder-children">${renderCloud139FilePreview(publicId, items, level)}</div>`;
        }

        function cloud139ItemPayloadFromDataset(dataset = {}) {
            const fid = String(dataset.cloud139Fid || "").trim();
            const name = String(dataset.cloud139Name || fid || "未命名文件").trim();
            const path = String(dataset.cloud139Path || "").trim();
            const shareFidToken = String(dataset.cloud139ShareToken || "").trim();
            const isDir = dataset.cloud139Dir === "1" || dataset.cloud139Select === "folder";
            return { fid, name, path, share_fid_token: shareFidToken, type: isDir ? "dir" : "file", is_dir: isDir };
        }

        function handleCloud139SelectionChange(input) {
            const publicId = input.dataset.cloud139PublicId || "";
            const item = cloud139ItemPayloadFromDataset(input.dataset);
            const key = cloud139SelectionKey(item);
            if (!publicId || !key) return;
            const current = cloud139SelectionState(publicId);
            const files = { ...(current.files || {}) };
            const folders = { ...(current.folders || {}) };
            if (item.is_dir) {
                if (input.checked) folders[key] = item;
                else delete folders[key];
            } else {
                if (input.checked) files[key] = item;
                else delete files[key];
            }
            publicState.cloud139Selections[publicId] = { mode: "items", files, folders };
            refreshPublicResourceViews(publicId);
        }

        function cloud139SelectionPayload(publicId) {
            const state = cloud139SelectionState(publicId);
            const selectedFiles = Object.values(state.files || {}).filter((item) => cloud139SelectionKey(item));
            const selectedFolders = Object.values(state.folders || {}).filter((item) => cloud139SelectionKey(item));
            if (!selectedFiles.length && !selectedFolders.length) {
                return {};
            }
            return {
                cloud139_selection: {
                    mode: "items",
                    selected_files: selectedFiles,
                    selected_folders: selectedFolders,
                },
            };
        }

        const SIXPAN_MANUAL_KEY = "manual";

        function sixpanPublicKey(publicId) {
            return `public:${publicId || ""}`;
        }

        function sixpanParseState(key) {
            return publicState.sixpanParses[key] || null;
        }

        function setSixpanParseState(key, state) {
            publicState.sixpanParses[key] = state;
            rerenderSixpanParse(key);
        }

        function initSixpanParseSelection(items) {
            const selected = {};
            const recommended = {};
            (Array.isArray(items) ? items : []).forEach((item) => {
                if (!item?.selectable || !item.id) return;
                const checked = item.default_selected !== false;
                selected[item.id] = checked;
                recommended[item.id] = checked;
            });
            return { selected, recommended };
        }

        async function loadSixpanParseForPublic(publicId, item, force = false) {
            if (!isSixpanCandidate(item || {})) {
                delete publicState.sixpanParses[sixpanPublicKey(publicId)];
                renderPublicConfirmPreview(publicId);
                return;
            }
            const key = sixpanPublicKey(publicId);
            const existing = sixpanParseState(key);
            if (!force && existing && !existing.error) {
                renderPublicConfirmPreview(publicId);
                return;
            }
            updatePublicResourceState(publicId, { availability_status: "checking", availability_message: "正在确认是否支持快速入库" }, { render: false });
            setSixpanParseState(key, { loading: true, items: [], selected: {}, recommended: {}, source: "public", publicId });
            try {
                const data = await api("/api/public/sixpan/parse", {
                    method: "POST",
                    body: JSON.stringify({ public_id: publicId, title: item?.title || "" }),
                    allowFailure: true,
                });
                if (data.success === false) throw new Error(data.message || "内容预览失败");
                const items = Array.isArray(data.items) ? data.items : [];
                const selection = initSixpanParseSelection(items);
                const fastAvailable = data.fast_available !== false && items.some((entry) => entry?.selectable && entry?.id);
                const parseStatus = fastAvailable ? "files_ready" : "empty_files";
                setSixpanParseState(key, {
                    loading: false,
                    success: fastAvailable,
                    slow: !fastAvailable,
                    parse_status: parseStatus,
                    fast_available: fastAvailable,
                    items,
                    summary: data.summary || {},
                    selected: selection.selected,
                    recommended: selection.recommended,
                    source: "public",
                    publicId,
                    message: data.message || "",
                });
                updatePublicResourceState(
                    publicId,
                    fastAvailable
                        ? { availability_status: "parse_ok", availability_message: "已找到可入库内容，可选择后提交" }
                        : { availability_status: "parse_empty", availability_message: data.message || "暂未找到可快速入库内容，已切换为慢速入库" },
                );
            } catch (error) {
                setSixpanParseState(key, { loading: false, error: error.message, slow: true, parse_status: "parse_failed", items: [], selected: {}, recommended: {}, source: "public", publicId });
                updatePublicResourceState(publicId, { availability_status: "parse_failed", availability_message: error.message || "暂时无法预览内容，已切换为慢速入库" });
            }
        }

        async function loadSixpanParseForManual(link = publicState.manualLink, force = false) {
            const url = $("manualPublicUrl")?.value.trim() || "";
            const title = $("manualPublicTitle")?.value.trim() || "";
            if (!url || !isSixpanCandidate(link || { source_type: "magnet", source_url: url })) {
                delete publicState.sixpanParses[SIXPAN_MANUAL_KEY];
                renderManualPublicStatus(link);
                return;
            }
            const existing = sixpanParseState(SIXPAN_MANUAL_KEY);
            if (!force && existing && !existing.error) {
                renderManualPublicStatus(link);
                return;
            }
            setSixpanParseState(SIXPAN_MANUAL_KEY, { loading: true, items: [], selected: {}, recommended: {}, source: "manual" });
            try {
                const data = await api("/api/public/sixpan/parse", {
                    method: "POST",
                    body: JSON.stringify({ url, title }),
                    allowFailure: true,
                });
                if (data.success === false) throw new Error(data.message || "内容预览失败");
                const items = Array.isArray(data.items) ? data.items : [];
                const selection = initSixpanParseSelection(items);
                const fastAvailable = data.fast_available !== false && items.some((entry) => entry?.selectable && entry?.id);
                setSixpanParseState(SIXPAN_MANUAL_KEY, {
                    loading: false,
                    success: fastAvailable,
                    slow: !fastAvailable,
                    parse_status: fastAvailable ? "files_ready" : "empty_files",
                    fast_available: fastAvailable,
                    items,
                    summary: data.summary || {},
                    selected: selection.selected,
                    recommended: selection.recommended,
                    source: "manual",
                    message: data.message || "",
                });
            } catch (error) {
                setSixpanParseState(SIXPAN_MANUAL_KEY, { loading: false, error: error.message, slow: true, parse_status: "parse_failed", items: [], selected: {}, recommended: {}, source: "manual" });
            }
        }

        function renderPublicConfirmPreview(publicId) {
            const box = $("publicConfirmSixpanParseBox");
            if (!box) return;
            const item = publicResourceById(publicId);
            if (isSixpanCandidate(item || {})) {
                box.innerHTML = renderSixpanParseHtml(sixpanPublicKey(publicId), { context: "confirm" });
                return;
            }
            const detailState = publicState.details[publicId] || {};
            if (detailState.loading) {
                box.innerHTML = `<div class="manual-preview-box notice-box">${icon("refresh")}<span><strong>正在预览资源内容...</strong><small>预览完成后可选择要保存的内容。</small></span></div>`;
                return;
            }
            if (detailState.error) {
                box.innerHTML = `<div class="manual-preview-box notice-box status-error">${icon("warning")}<span><strong>内容预览失败</strong><small>${escapeHtml(detailState.error)}</small></span></div>`;
                return;
            }
            const detail = detailState.detail || {};
            const inspection = detail.inspection || {};
            const items = Array.isArray(inspection.items) ? inspection.items : [];
            if (isQuarkCandidate(item || {})) {
                const payload = quarkSelectionPayload(publicId);
                box.innerHTML = `
                    <div class="manual-preview-box">
                        <div class="sixpan-parse-head">
                            <div>
                                <strong>预览并选择要入库的内容</strong>
                                <small>${escapeHtml(quarkSelectionSummaryText(publicId))}</small>
                            </div>
                        </div>
                        ${payload.error ? `<div class="notice-box warning">${icon("warning")}<span><strong>夸克选择未完成</strong><small>${escapeHtml(payload.error)}</small></span></div>` : ""}
                        ${items.length ? renderQuarkFilePreview(publicId, items) : renderEmptyManualPreview(inspection)}
                    </div>
                `;
                bindPreviewInteractions(box);
                return;
            }
            if (isCloud139Candidate(item || {})) {
                const payload = cloud139SelectionPayload(publicId);
                box.innerHTML = `
                    <div class="manual-preview-box">
                        <div class="sixpan-parse-head">
                            <div>
                                <strong>预览并选择要入库的内容</strong>
                                <small>${escapeHtml(cloud139SelectionSummaryText(publicId))}</small>
                            </div>
                        </div>
                        ${payload.error ? `<div class="notice-box warning">${icon("warning")}<span><strong>文件选择未完成</strong><small>${escapeHtml(publicMessageForDisplay(payload.error, "检测未通过", payload.error))}</small></span></div>` : ""}
                        ${items.length ? renderCloud139FilePreview(publicId, items) : renderEmptyManualPreview(inspection)}
                    </div>
                `;
                bindPreviewInteractions(box);
                return;
            }
            box.innerHTML = `
                <div class="manual-preview-box notice-box">
                    ${icon("info")}<span><strong>该来源暂不支持目录勾选预览</strong><small>${escapeHtml(publicMessageForDisplay(inspection.message || item?.reason || "将按完整资源提交。", inspection.status || "", "将按完整资源提交。"))}</small></span>
                </div>
            `;
        }

        function renderEmptyManualPreview(inspection = {}) {
            const message = publicMessageForDisplay(inspection.message || "暂未返回可勾选的目录/文件，确认后将按完整资源提交。", inspection.status || "", "暂未返回可勾选的目录/文件，确认后将按完整资源提交。");
            return `<div class="sixpan-empty-note">${escapeHtml(message)}</div>`;
        }

        function renderManualPublicStatus(link = publicState.manualLink) {
            const box = $("manualPublicStatus");
            if (!box) return;
            if (!link) return;
            resetNoticeBox(box);
            const sixpanHtml = isSixpanCandidate(link) ? renderSixpanParseHtml(SIXPAN_MANUAL_KEY) : "";
            box.innerHTML = `
                <div class="manual-detect-result">
                    ${icon(link.supported ? "check" : "info")}
                    <span>
                        <strong>${link.supported ? "已识别链接类型" : "链接会进入审核或暂不支持"}</strong>
                        <small>识别来源：${escapeHtml(sourceLabel(link.source_type || "unknown"))}；提交时会再次检测资源可用性。</small>
                    </span>
                </div>
                ${sixpanHtml}
            `;
        }

        function renderSixpanParseHtml(key, options = {}) {
            const state = sixpanParseState(key);
            const context = options.context || "";
            if (!state) {
                return `
                    <div class="sixpan-parse-box notice-box">
                        ${icon("info")}<span><strong>尚未预览可入库内容</strong><small>${context === "confirm" ? "系统会先读取详情并预览内容，再允许确认提交。" : "正在准备内容预览，完成后可选择要保存的内容。"}</small></span>
                    </div>
                `;
            }
            if (state.loading) {
                return `
                    <div class="sixpan-parse-box notice-box">
                        ${icon("refresh")}<span><strong>正在预览可入库内容...</strong><small>预览完成后可选择要保存的内容。</small></span>
                    </div>
                `;
            }
            if (state.error) {
                return `
                    <div class="sixpan-parse-box notice-box status-error">
                        <div class="sixpan-parse-head">
                            <strong>暂未找到可快速入库内容</strong>
                            <button class="secondary mini" type="button" data-sixpan-action="retry" data-sixpan-key="${escapeHtml(key)}">重新预览</button>
                        </div>
                        <p>${escapeHtml(state.error)}；此资源暂不支持快速入库，可继续按慢速入库提交，但不保证成功入库。</p>
                    </div>
                `;
            }
            const items = Array.isArray(state.items) ? state.items : [];
            const selectable = items.filter((item) => item.selectable);
            const selectedCount = selectable.filter((item) => state.selected?.[item.id]).length;
            const ignoredCount = Math.max(0, selectable.length - selectedCount);
            const allSelected = selectable.length > 0 && selectedCount === selectable.length;
            if (state.slow || !selectable.length) {
                return `
                    <div class="sixpan-parse-box notice-box warning">
                        <div class="sixpan-parse-head">
                            <strong>暂未找到可快速入库内容</strong>
                            <button class="secondary mini" type="button" data-sixpan-action="retry" data-sixpan-key="${escapeHtml(key)}">重新预览</button>
                        </div>
                        <p>${escapeHtml(state.message || "暂未找到可快速入库内容。")} 可继续提交为慢速入库，后台会尽快搬运，但不保证成功。</p>
                    </div>
                `;
            }
            return `
                <div class="sixpan-parse-box">
                    <div class="sixpan-parse-head">
                        <div>
                            <strong>选择要入库的内容</strong>
                            <small data-sixpan-selection-summary="1" data-sixpan-key="${escapeHtml(key)}">已选 ${selectedCount}/${selectable.length} 个文件，忽略 ${ignoredCount} 个</small>
                        </div>
                        <div class="sixpan-parse-actions">
                            <button class="secondary mini" type="button" data-sixpan-action="recommended" data-sixpan-key="${escapeHtml(key)}">恢复推荐</button>
                            <button class="secondary mini" type="button" data-sixpan-action="video" data-sixpan-key="${escapeHtml(key)}">只选视频/字幕</button>
                            <button class="secondary mini" type="button" data-sixpan-select-toggle="1" data-sixpan-action="${allSelected ? "none" : "all"}" data-sixpan-key="${escapeHtml(key)}">${allSelected ? "取消全选" : "全选"}</button>
                        </div>
                    </div>
                    ${state.message ? `<p class="sixpan-parse-message">${escapeHtml(state.message)}</p>` : ""}
                    ${items.length ? `<div class="sixpan-file-list">${items.map((item) => renderSixpanFileRow(key, item, state)).join("")}</div>` : `<div class="sixpan-empty-note">暂未找到可快速入库内容，已切换为慢速入库。</div>`}
                </div>
            `;
        }

        function renderSixpanFileRow(key, item, state) {
            const selectable = Boolean(item.selectable && item.id);
            const checked = Boolean(state.selected?.[item.id]);
            const typeLabel = item.directory
                ? "目录"
                : item.reason === "small_video_ad" ? "广告小视频"
                  : item.media_type === "video" ? "视频"
                    : item.media_type === "subtitle" ? "字幕"
                      : item.media_type === "image" ? "图片"
                        : item.reason === "noise" ? "疑似垃圾"
                          : item.reason === "metadata" ? "元数据"
                            : "其他";
            return `
                <label class="sixpan-file-row ${item.directory ? "is-dir" : ""} ${!selectable ? "disabled" : ""}">
                    <input type="checkbox" ${selectable ? "" : "disabled"} ${checked ? "checked" : ""} data-sixpan-file="1" data-sixpan-key="${escapeHtml(key)}" data-sixpan-id="${escapeHtml(item.id || "")}">
                    <span class="sixpan-file-main">
                        <strong>${escapeHtml(item.path || item.name || "-")}</strong>
                        <small>${escapeHtml(typeLabel)}${item.size_text ? ` · ${escapeHtml(item.size_text)}` : ""}</small>
                    </span>
                </label>
            `;
        }

        function rerenderSixpanParse(key) {
            if (key === SIXPAN_MANUAL_KEY) {
                renderManualPublicStatus(publicState.manualLink);
                return;
            }
            if (key.startsWith("public:")) {
                const publicId = key.slice("public:".length);
                renderPublicConfirmPreview(publicId);
                if (publicState.selectedPublicId === publicId) {
                    renderPublicResults();
                }
            }
        }

        function sixpanSelectionPayload(key) {
            const state = sixpanParseState(key);
            if (!state || state.loading || state.error) {
                return {};
            }
            const items = Array.isArray(state.items) ? state.items : [];
            const sourceFiles = items.filter((item) => item.id && !item.directory);
            const ignoreFiles = sourceFiles.filter((item) => !state.selected?.[item.id]).map((item) => item.id);
            const selectedCount = sourceFiles.filter((item) => state.selected?.[item.id]).length;
            return {
                ignore_files: ignoreFiles,
                sixpan_selection: {
                    total_count: sourceFiles.length,
                    selected_count: selectedCount,
                    ignored_count: ignoreFiles.length,
                    parse_status: state.parse_status || "",
                    parse_error: state.error || "",
                    slow: Boolean(state.slow),
                },
            };
        }

        function applySixpanAction(key, action) {
            const state = sixpanParseState(key);
            if (!state || state.loading) return;
            const selected = { ...(state.selected || {}) };
            const items = Array.isArray(state.items) ? state.items : [];
            items.forEach((item) => {
                if (!item.selectable || !item.id) return;
                if (action === "all") selected[item.id] = true;
                if (action === "none") selected[item.id] = false;
                if (action === "recommended") selected[item.id] = state.recommended?.[item.id] !== false;
                if (action === "video") selected[item.id] = isSixpanPreferredMedia(item);
            });
            publicState.sixpanParses[key] = { ...state, selected };
            syncSixpanSelectionUi(key);
        }

        function syncSixpanSelectionUi(key) {
            const state = sixpanParseState(key);
            if (!state) return;
            const selectable = (Array.isArray(state.items) ? state.items : []).filter((item) => item.selectable && item.id);
            const selectedCount = selectable.filter((item) => state.selected?.[item.id]).length;
            const ignoredCount = Math.max(0, selectable.length - selectedCount);
            const allSelected = selectable.length > 0 && selectedCount === selectable.length;

            document.querySelectorAll("[data-sixpan-key]").forEach((element) => {
                if (element.dataset.sixpanKey !== key) return;
                if (element.matches("[data-sixpan-file]")) {
                    element.checked = Boolean(state.selected?.[element.dataset.sixpanId || ""]);
                } else if (element.matches("[data-sixpan-selection-summary]")) {
                    element.textContent = `已选 ${selectedCount}/${selectable.length} 个文件，忽略 ${ignoredCount} 个`;
                } else if (element.matches("[data-sixpan-select-toggle]")) {
                    element.dataset.sixpanAction = allSelected ? "none" : "all";
                    element.textContent = allSelected ? "取消全选" : "全选";
                }
            });
        }

        function isSixpanPreferredMedia(item) {
            if (!item || !item.selectable) return false;
            if (item.media_type === "subtitle") return true;
            if (item.media_type !== "video") return false;
            const size = Number(item.size || 0);
            return !size || size >= 20 * 1024 * 1024;
        }

        function handleSixpanParseClick(event) {
            const button = event.target.closest("[data-sixpan-action]");
            if (!button) return;
            event.preventDefault();
            const key = button.dataset.sixpanKey || "";
            const action = button.dataset.sixpanAction || "";
            if (action === "retry") {
                if (key === SIXPAN_MANUAL_KEY) {
                    loadSixpanParseForManual(publicState.manualLink, true).catch((error) => toast(error.message, "error"));
                } else if (key.startsWith("public:")) {
                    const publicId = key.slice("public:".length);
                    const item = publicResourceById(publicId);
                    loadSixpanParseForPublic(publicId, item, true).catch((error) => toast(error.message, "error"));
                }
                return;
            }
            applySixpanAction(key, action);
        }

        function handleSixpanParseChange(event) {
            const input = event.target.closest("[data-sixpan-file]");
            if (!input) return;
            const key = input.dataset.sixpanKey || "";
            const id = input.dataset.sixpanId || "";
            const state = sixpanParseState(key);
            if (!state || !id) return;
            publicState.sixpanParses[key] = {
                ...state,
                selected: { ...(state.selected || {}), [id]: Boolean(input.checked) },
            };
            syncSixpanSelectionUi(key);
        }

        function shouldWarnSlowSixpanSubmit(publicId, item = publicResourceById(publicId)) {
            return Boolean(item && isSixpanCandidate(item) && sixpanSubmitSpeed(publicId) === "slow");
        }

        function openSixpanSlowWarning(publicId) {
            const item = publicResourceById(publicId);
            if (!item) {
                toast("资源不存在或已过期，请重新搜索", "error");
                return;
            }
            const modal = $("sixpanSlowSubmitConfirm");
            if (!modal) {
                openPublicSubmitConfirm(publicId, { skipSlowWarning: true });
                return;
            }
            publicState.pendingSlowSubmit = { publicId };
            if ($("sixpanSlowResourceName")) $("sixpanSlowResourceName").textContent = item.title || "未命名资源";
            const state = sixpanParseState(sixpanPublicKey(publicId));
            if ($("sixpanSlowReason")) {
                const slowReason = state?.error || item.availability_message || state?.message || "此资源暂未确认可快速入库，处理时间可能较长，且不保证成功。";
                $("sixpanSlowReason").textContent = publicMessageForDisplay(slowReason, "处理中", "此资源暂未确认可快速入库，处理时间可能较长，且不保证成功。");
            }
            showPublicModal(modal, $("sixpanSlowCancel"));
        }

        function closeSixpanSlowWarning(options = {}) {
            hidePublicModal($("sixpanSlowSubmitConfirm"), options);
            publicState.pendingSlowSubmit = null;
        }

        function continueSixpanSlowSubmit() {
            const pending = publicState.pendingSlowSubmit;
            closeSixpanSlowWarning({ restoreFocus: false });
            if (pending?.publicId) {
                openPublicSubmitConfirm(pending.publicId, { skipSlowWarning: true });
            }
        }

        function openPublicSubmitConfirm(publicId, options = {}) {
            const item = publicResourceById(publicId);
            if (!item) {
                toast("资源不存在或已过期，请重新搜索", "error");
                return;
            }
            if (isResourceInvalid(item)) {
                toast(publicMessageForDisplay(item.availability_message || "详情检测未通过，当前资源不可提交", item.availability_status || "检测未通过", "详情检测未通过，当前资源不可提交"), "error");
                return;
            }
            if (!options.skipSlowWarning && shouldWarnSlowSixpanSubmit(publicId, item)) {
                openSixpanSlowWarning(publicId);
                return;
            }
            const modal = $("publicSubmitConfirm");
            if (!modal) {
                confirmPublicResourceSubmit(publicId, recommendedCategoryForItem(item)).catch((error) => toast(error.message, "error"));
                return;
            }
            const previousPendingCategory = publicState.pendingSubmit?.publicId === publicId ? publicState.pendingSubmit.category : "";
            setPublicConfirmError(null);
            const category = previousPendingCategory
                || (publicState.manualPreview?.publicId === publicId
                ? ($("manualPublicCategory")?.value || publicState.selectedCategory || recommendedCategoryForItem(item))
                : recommendedCategoryForItem(item));
            publicState.pendingSubmit = { publicId, item, category };
            if ($("publicConfirmResourceName")) $("publicConfirmResourceName").textContent = item.title || "未命名资源";
            if ($("publicConfirmCategory")) {
                $("publicConfirmCategory").value = category;
                window.syncCustomSelect?.($("publicConfirmCategory"));
            }
            updatePublicConfirmWarning();
            renderPublicConfirmPreview(publicId);
            const categoryTrigger = $("publicConfirmCategory")?.__fnSelectRoot?.querySelector(".fn-select-trigger");
            showPublicModal(modal, categoryTrigger || $("publicConfirmSubmit"));
        }

        function closePublicSubmitConfirm() {
            hidePublicModal($("publicSubmitConfirm"));
            setPublicConfirmError(null);
            if ($("publicConfirmSixpanParseBox")) $("publicConfirmSixpanParseBox").innerHTML = "";
            if ($("publicConfirmSubmit")) $("publicConfirmSubmit").disabled = false;
        }

        function setPublicConfirmError(dataOrMessage) {
            const box = $("publicConfirmError");
            if (!box) return;
            if (!dataOrMessage) {
                box.innerHTML = "";
                box.classList.add("hidden");
                return;
            }
            box.classList.remove("hidden");
            box.innerHTML = typeof dataOrMessage === "string"
                ? `<div class="preflight-failure">${icon("warning")}<div><strong>${escapeHtml(publicMessageForDisplay(dataOrMessage, "处理失败", dataOrMessage))}</strong></div></div>`
                : renderPreflightFailureHtml(dataOrMessage);
        }

        function updatePublicConfirmWarning() {
            const category = $("publicConfirmCategory")?.value || "movie";
            const label = categoryLabel(category);
            if ($("publicConfirmWarning")) {
                $("publicConfirmWarning").textContent = `提示：请再次确认以【${label}】分类入库资源，分类错误将影响入库！`;
            }
        }

        async function submitPublicResource(publicId) {
            const item = publicResourceById(publicId);
            if (isBtbtlaCandidate(item)) {
                if (publicState.selectedPublicId !== publicId) await selectPublicResource(publicId);
                toast("请先选择下载资源，解析磁链后再提交", "info");
                return;
            }
            const detailState = await ensurePublicResourceDetail(publicId, { loadingMessage: "正在先读取资源详情，确认可入库内容..." });
            if (detailState?.error) {
                toast(detailState.error || "资源详情读取失败，暂不能提交入库", "error");
                return;
            }
            const refreshedItem = publicResourceById(publicId);
            if (isResourceInvalid(refreshedItem)) {
                toast(publicMessageForDisplay(refreshedItem?.availability_message || "详情检测未通过，当前资源不可提交", refreshedItem?.availability_status || "检测未通过", "详情检测未通过，当前资源不可提交"), "error");
                return;
            }
            openPublicSubmitConfirm(publicId);
        }

        async function confirmPublicResourceSubmit(publicId, categoryValue) {
            const category = categoryValue || $("publicConfirmCategory")?.value || categoryForPublicResource(publicId) || $("manualPublicCategory")?.value || "movie";
            const note = $("manualPublicNote")?.value || "";
            const captcha = captchaPayload();
            if (captcha.required && !captcha.valid) return;
            const notification = notificationPayload();
            if (notification === null) return;
            setPublicConfirmError(null);
            if ($("publicConfirmSubmit")) $("publicConfirmSubmit").disabled = true;
            publicState.pendingSubmit = { ...(publicState.pendingSubmit || {}), publicId, category };
            try {
                const quarkPayload = quarkSelectionPayload(publicId);
                if (quarkPayload.error) {
                    setPublicConfirmError(quarkPayload.error);
                    toast(quarkPayload.error, "error");
                    return;
                }
                const sixpanPayload = sixpanSelectionPayload(sixpanPublicKey(publicId));
                const cloud139Payload = cloud139SelectionPayload(publicId);
                if (cloud139Payload.error) {
                    setPublicConfirmError(cloud139Payload.error);
                    toast(cloud139Payload.error, "error");
                    return;
                }
                showGlobalLoading("正在提交入库并检测资源有效性...");
                const data = await api("/api/public/submit", {
                    method: "POST",
                    body: JSON.stringify({ public_id: publicId, category, note, ...sixpanPayload, ...quarkPayload, ...cloud139Payload, ...captcha.data, ...notification }),
                    allowFailure: true,
                });
                closePublicSubmitConfirm();
                handleSubmitResponse(data);
                if (data.success !== false) await loadCaptcha();
            } catch (error) {
                closePublicSubmitConfirm();
                setPublicConfirmError(error.message);
                toast(error.message, "error");
            } finally {
                hideGlobalLoading();
                if ($("publicConfirmSubmit")) $("publicConfirmSubmit").disabled = false;
            }
        }

        function notificationPayload() {
            if (publicState.notifications?.guest_email_available !== true) return {};
            if (!$("publicNotificationEnabled")?.checked) return {};
            const email = $("publicNotificationEmail")?.value.trim() || "";
            if (email.length > 254 || !/^[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9-]+(?:\.[A-Z0-9-]+)+$/i.test(email)) {
                $("publicNotificationEmail")?.focus();
                toast("请输入有效的通知邮箱", "error");
                return null;
            }
            return { notification_email_enabled: true, notification_email: email };
        }

        async function detectManualPublicLink() {
            const url = $("manualPublicUrl")?.value.trim() || "";
            const password = $("manualPublicPassword")?.value.trim() || "";
            const box = $("manualPublicStatus");
            if (!url) {
                toast("请输入分享链接", "error");
                return;
            }
            resetNoticeBox(box);
            if (box) box.textContent = "识别中...";
            try {
                showGlobalLoading("正在识别分享链接...");
                const data = await api("/api/public/detect", {
                    method: "POST",
                    body: JSON.stringify({ url, password }),
                });
                const link = data.link || {};
                publicState.manualLink = link;
                if (isSixpanCandidate(link)) {
                    publicState.sixpanParses[SIXPAN_MANUAL_KEY] = { loading: true, items: [], selected: {}, recommended: {}, source: "manual" };
                }
                renderManualPublicStatus(link);
                if (isSixpanCandidate(link)) await loadSixpanParseForManual(link, true);
                else delete publicState.sixpanParses[SIXPAN_MANUAL_KEY];
            } catch (error) {
                if (box) box.textContent = error.message;
                toast(error.message, "error");
            } finally {
                hideGlobalLoading();
            }
        }

        async function submitManualPublicLink() {
            const payload = {
                title: $("manualPublicTitle")?.value.trim() || "",
                preferred_title: $("manualPublicTitle")?.value.trim() || "",
                category: $("manualPublicCategory")?.value || publicState.selectedCategory || "movie",
                url: $("manualPublicUrl")?.value.trim() || "",
                password: $("manualPublicPassword")?.value.trim() || "",
                note: $("manualPublicNote")?.value.trim() || "",
            };
            if (!validateManualPublicForm(payload)) return;
            try {
                showGlobalLoading("正在预览链接内容...");
                const data = await api("/api/public/manual/preview", {
                    method: "POST",
                    body: JSON.stringify(payload),
                    allowFailure: true,
                });
                if (data.success === false) throw new Error(data.message || "内容预览失败");
                await openManualPreviewConfirm(data);
            } catch (error) {
                toast(error.message, "error");
            } finally {
                hideGlobalLoading();
            }
        }

        async function openManualPreviewConfirm(data) {
            const item = data.item || {};
            const detail = data.detail || {};
            const publicId = data.public_id || item.public_id || detail.public_id || "";
            if (!publicId) {
                toast("内容预览失败：缺少临时资源编号", "error");
                return;
            }
            item.public_id = publicId;
            publicState.manualPreview = { publicId, item, detail, link: data.link || {} };
            publicState.manualLink = data.link || publicState.manualLink;
            publicState.details[publicId] = { loading: false, detail, success: data.success !== false };
            if (isSixpanCandidate(item) || isSixpanCandidate(data.link || detail || {})) {
                publicState.sixpanParses[sixpanPublicKey(publicId)] = { loading: true, items: [], selected: {}, recommended: {}, source: "public", publicId };
                openPublicSubmitConfirm(publicId, { skipSlowWarning: true });
                await loadSixpanParseForPublic(publicId, item, true);
                return;
            }
            openPublicSubmitConfirm(publicId, { skipSlowWarning: true });
        }

        function validateManualPublicForm(payload) {
            const urlInput = $("manualPublicUrl");
            const titleInput = $("manualPublicTitle");
            const categoryInput = $("manualPublicCategory");
            if (!payload.url) {
                urlInput?.focus();
                toast("请输入分享链接", "error");
                return false;
            }
            if (!payload.title) {
                titleInput?.focus();
                toast("请输入资源名称", "error");
                return false;
            }
            if (!payload.category) {
                categoryInput?.focus();
                toast("请选择资源分类", "error");
                return false;
            }
            return true;
        }

        function handleSubmitResponse(data) {
            const responseStatus = data.status || data.request?.status || (data.success === false ? "处理失败" : "处理中");
            const responseMessage = publicMessageForDisplay(
                data.success === false ? data.message || "提交未进入自动处理" : data.message || "提交成功，请保存编号",
                responseStatus,
                data.success === false ? "提交未进入自动处理" : "提交成功，请保存编号",
            );
            toast(responseMessage, data.success === false ? "error" : "success");
            if (data.success === false && data.inspection && !(data.request_token || data.token || data.request?.token)) {
                const publicId = data.public_id || (publicState.activeTab === "search" ? (publicState.pendingSubmit?.publicId || publicState.selectedPublicId || "") : "");
                if (publicId && isInspectionInvalid(data.inspection)) {
                    updatePublicResourceState(
                        publicId,
                        {
                            availability_status: "invalid",
                            availability_message: data.inspection.message || data.message || "提交前检测未通过，资源可能已失效",
                        },
                        { render: false },
                    );
                    renderPublicResults();
                }
                renderPreflightFailure(data);
                if (!$("publicSubmitConfirm")?.classList.contains("hidden")) {
                    setPublicConfirmError(data);
                }
                return;
            }
            renderRequestResult(data);
            if (document.body.dataset.page === "submit" && publicState.public?.request_query_enabled !== false && (data.request_token || data.token || data.request?.token)) {
                activatePublicTab("query");
            }
        }

        function renderPreflightFailure(data) {
            const box = publicState.activeTab === "link" ? $("manualPublicStatus") : $("publicSearchStatus");
            if (!box) return;
            box.classList.add("status-error", "preflight-status");
            box.classList.remove("hidden");
            box.setAttribute("role", "alert");
            box.setAttribute("aria-live", "assertive");
            box.innerHTML = renderPreflightFailureHtml(data);
        }

        function renderPreflightFailureHtml(data) {
            const inspection = data.inspection || {};
            const summary = inspection.summary || {};
            const message = publicMessageForDisplay(data.message || "资源检测未通过", data.status || "检测未通过", "资源检测未通过");
            const inspectionMessage = publicMessageForDisplay(inspection.message || "提交前检测未通过，请确认分享链接有效后再试。", data.status || "检测未通过", "提交前检测未通过，请确认分享链接有效后再试。");
            return `
                <div class="preflight-failure">
                    ${icon("warning")}
                    <div>
                        <strong>${escapeHtml(message)}</strong>
                        <p>${escapeHtml(inspectionMessage)}</p>
                        <p>来源：${escapeHtml(sourceLabel(data.link?.source_type || ""))}；文件数：${escapeHtml(summary.file_count || "-")}；大小：${escapeHtml(summary.total_size_text || "-")}</p>
                    </div>
                </div>
            `;
        }

        function renderRequestResult(data) {
            const token = data.request_token || data.token || data.request?.token || "";
            const title = data.request?.title || publicState.pendingSubmit?.item?.title || $("manualPublicTitle")?.value.trim() || "已提交资源";
            const categoryKey = data.request?.category || publicState.pendingSubmit?.category || publicState.selectedCategory;
            const category = data.request?.category_label || categoryLabel(categoryKey);
            const status = publicStatusForDisplay(data.status || data.request?.status || (data.success === false ? "处理失败" : "处理中"));
            if (token) saveRecentRequest(title, token);
            const box = $("requestStatusBox") || $("manualPublicStatus");
            if (!box) return;
            box.innerHTML = renderStatusCard({
                success: data.success !== false,
                title,
                category_key: categoryKey,
                category,
                token,
                status,
                created_at: new Date().toISOString(),
                updated_at: "",
                message: publicMessageForDisplay(data.message, status, publicDefaultMessage(status)),
            });
            if ($("requestTokenInput") && token) $("requestTokenInput").value = token;
            bindStatusCardActions(box);
        }

        async function queryRequestStatus(tokenValue) {
            const token = (tokenValue || $("requestTokenInput")?.value || document.body.dataset.requestToken || "").trim();
            const box = $("requestStatusBox");
            if (!token) {
                toast("请输入提交编号", "error");
                return;
            }
            if (publicState.public?.request_query_enabled === false) {
                if (box) box.innerHTML = `<div class="notice-box">提交结果查询已关闭。如需查询，请联系管理员。</div>`;
                toast("提交结果查询已关闭", "error");
                return;
            }
            if (box) box.textContent = "查询中...";
            try {
                showGlobalLoading("正在查询提交进度...");
                const data = await api(`/api/public/request/${encodeURIComponent(token)}`);
                renderRequestStatus(data.request || {});
            } catch (error) {
                if (box) box.innerHTML = `<div class="notice-box status-error">${escapeHtml(error.message)}</div>`;
                toast(error.message, "error");
            } finally {
                hideGlobalLoading();
            }
        }

        function renderRequestStatus(item) {
            const box = $("requestStatusBox");
            if (!box) return;
            const status = publicStatusForDisplay(item.status || "处理中");
            box.innerHTML = renderStatusCard({
                success: !isFailedStatus(status),
                title: item.title || "-",
                category_key: item.category || "",
                category: item.category_label || categoryLabel(item.category),
                token: item.token || document.body.dataset.requestToken || "-",
                status,
                created_at: item.created_at,
                updated_at: item.updated_at,
                message: publicMessageForDisplay(item.message, status, publicDefaultMessage(status)),
            });
            bindStatusCardActions(box);
        }

        function renderStatusCard(item) {
            const status = publicStatusForDisplay(item.status || "处理中");
            const message = publicMessageForDisplay(item.message, status, publicDefaultMessage(status));
            const failed = isFailedStatus(status) || item.success === false;
            const done = isDoneStatus(status);
            const processing = !failed && !done;
            const statusClass = failed ? "error" : done ? "ok" : "warn";
            const iconKey = item.category_key || publicState.selectedCategory;
            return `
                <div class="status-success-card ${failed ? "is-failed" : ""}">
                    <div class="status-success-main">
                        <div class="status-big-mark ${failed ? "error" : "ok"}">${icon(failed ? "info" : "check")}</div>
                        <div class="status-summary">
                            <h2>${failed ? "提交未完成" : "提交成功"}</h2>
                            <p class="status-line">${icon("film")}<span>资源：${escapeHtml(item.title || "-")}</span></p>
                            <p class="status-line">${icon(CATEGORY_ICON[iconKey] || "grid")}<span>分类：${escapeHtml(item.category || "-")}</span></p>
                            <p class="status-line">${icon("file")}<span>提交编号：<code>${escapeHtml(item.token || "-")}</code></span>${item.token ? `<button class="copy-inline" type="button" data-copy-token="${escapeHtml(item.token)}">${icon("copy")}</button>` : ""}</p>
                        </div>
                    </div>
                    <p class="status-message">${escapeHtml(message)}</p>
                    <div class="status-steps">
                        <div class="status-step done">
                            <div class="status-dot">${icon("check")}</div>
                            <strong>已提交</strong>
                            <small>${escapeHtml(formatDate(item.created_at))}</small>
                        </div>
                        <div class="status-step ${processing ? "active" : "done"}">
                            <div class="status-dot">${processing ? "" : icon("check")}</div>
                            <strong>处理中</strong>
                            <small>${processing ? "排队中..." : failed ? "已结束" : "已处理"}</small>
                        </div>
                        <div class="status-step ${done ? "done" : failed ? "active error" : ""}">
                            <div class="status-dot">${done ? icon("check") : failed ? "!" : "3"}</div>
                            <strong>${failed ? "未完成" : "已完成"}</strong>
                            <small>${done ? "可查看" : failed ? escapeHtml(status || "失败") : "待完成"}</small>
                        </div>
                    </div>
                    <div class="status-current">当前状态：<span class="pill ${statusClass}">${escapeHtml(status || "处理中")}</span></div>
                </div>
            `;
        }

        function isDoneStatus(status) {
            const text = publicStatusForDisplay(status);
            return text.includes("完成") || text === "done" || text === "success";
        }

        function isFailedStatus(status) {
            const text = publicStatusForDisplay(status);
            return text.includes("失败") || text.includes("未通过") || text.includes("暂不支持") || text.includes("取消") || text === "failed" || text === "rejected" || text === "unsupported" || text === "cancelled";
        }

        function bindStatusCardActions(root) {
            root.querySelector("[data-copy-token]")?.addEventListener("click", (event) => copyText(event.currentTarget.dataset.copyToken));
        }

        async function copyText(text) {
            const value = String(text || "");
            if (!value) {
                toast("没有可复制的提交编号", "error");
                return;
            }
            const legacyCopy = () => {
                const textarea = document.createElement("textarea");
                textarea.value = value;
                textarea.setAttribute("readonly", "readonly");
                textarea.style.position = "fixed";
                textarea.style.left = "-9999px";
                textarea.style.top = "0";
                document.body.appendChild(textarea);
                textarea.focus();
                textarea.select();
                textarea.setSelectionRange(0, textarea.value.length);
                let ok = false;
                try {
                    ok = document.execCommand("copy");
                } finally {
                    document.body.removeChild(textarea);
                }
                return ok;
            };
            try {
                if (legacyCopy()) {
                    toast("已复制提交编号", "success");
                    return;
                }
                if (navigator.clipboard && window.isSecureContext) {
                    await navigator.clipboard.writeText(value);
                    toast("已复制提交编号", "success");
                    return;
                }
                throw new Error("copy failed");
            } catch {
                toast("复制失败，请手动复制", "error");
            }
        }

        async function loadCaptcha() {
            const captcha = (publicState.security || {}).captcha || {};
            const panel = $("captchaPanel");
            const box = $("captchaBox");
            if (!panel || !box) return;
            if (!captcha.enabled) {
                panel.classList.add("hidden");
                box.innerHTML = "";
                return;
            }
            panel.classList.remove("hidden");
            const data = await api("/api/public/captcha");
            box.innerHTML = `
                <div class="form-row icon-row">
                    <span class="field-label">${escapeHtml(data.question || "请输入验证码")}</span>
                    <input id="captchaAnswerInput" type="text" inputmode="numeric" autocomplete="off" placeholder="请输入答案">
                </div>
            `;
        }

        function captchaPayload() {
            const captcha = (publicState.security || {}).captcha || {};
            if (!captcha.enabled) return { required: false, valid: true, data: {} };
            const answer = $("captchaAnswerInput")?.value.trim() || "";
            if (!answer) {
                toast("请先完成人机验证", "error");
                return { required: true, valid: false, data: {} };
            }
            return { required: true, valid: true, data: { captcha_answer: answer } };
        }

        function bindRequestStatusPage() {
            const token = document.body.dataset.requestToken || "";
            if (publicState.public?.request_query_enabled === false) {
                if ($("requestStatusBox")) $("requestStatusBox").innerHTML = `<div class="notice-box">提交结果查询已关闭。如需查询，请联系管理员。</div>`;
                if ($("requestRefreshBtn")) $("requestRefreshBtn").disabled = true;
                if ($("requestRefreshMirrorBtn")) $("requestRefreshMirrorBtn").disabled = true;
                return;
            }
            $("requestRefreshBtn")?.addEventListener("click", () => queryRequestStatus(token));
            $("requestStatusRefreshForm")?.addEventListener("submit", (event) => {
                event.preventDefault();
                queryRequestStatus(token);
            });
            if (token) queryRequestStatus(token);
        }

        return Object.freeze({
            isPublicSubmitConfirmOpen,
            renderPublicResourceDetail,
            renderBtbtlaResourcePreview,
            btbtlaResolveKey,
            btbtlaResolveState,
            setBtbtlaResolveState,
            renderBtbtlaResourceRow,
            resolveBtbtlaResource,
            renderEmptyFilePreview,
            previewKey,
            closePreviewBranch,
            renderFilePreview,
            renderFilePreviewItem,
            renderFolderChildren,
            toggleResourceFolder,
            quarkSelectionState,
            setQuarkSelectionState,
            quarkItemPayloadFromDataset,
            quarkDirPayload,
            quarkLoadedChildItems,
            quarkSubdirSelectionCoversParent,
            collapseQuarkSubdirSelectionIfComplete,
            quarkSelectionSummaryText,
            renderQuarkFilePreview,
            renderQuarkFilePreviewItem,
            renderQuarkFolderChildren,
            ensureResourceFolderLoaded,
            handleQuarkSelectionAction,
            handleQuarkFolderOpen,
            handleQuarkSelectionChange,
            quarkRootPreviewItems,
            quarkSelectionPayload,
            cloud139SelectionState,
            cloud139SelectionKey,
            cloud139SelectionSummaryText,
            renderCloud139FilePreview,
            renderCloud139FilePreviewItem,
            renderCloud139FolderChildren,
            cloud139ItemPayloadFromDataset,
            handleCloud139SelectionChange,
            cloud139SelectionPayload,
            sixpanPublicKey,
            sixpanParseState,
            setSixpanParseState,
            initSixpanParseSelection,
            loadSixpanParseForPublic,
            loadSixpanParseForManual,
            renderPublicConfirmPreview,
            renderEmptyManualPreview,
            renderManualPublicStatus,
            renderSixpanParseHtml,
            renderSixpanFileRow,
            rerenderSixpanParse,
            sixpanSelectionPayload,
            applySixpanAction,
            syncSixpanSelectionUi,
            isSixpanPreferredMedia,
            handleSixpanParseClick,
            handleSixpanParseChange,
            shouldWarnSlowSixpanSubmit,
            openSixpanSlowWarning,
            closeSixpanSlowWarning,
            continueSixpanSlowSubmit,
            openPublicSubmitConfirm,
            closePublicSubmitConfirm,
            setPublicConfirmError,
            updatePublicConfirmWarning,
            submitPublicResource,
            confirmPublicResourceSubmit,
            detectManualPublicLink,
            submitManualPublicLink,
            openManualPreviewConfirm,
            validateManualPublicForm,
            handleSubmitResponse,
            renderPreflightFailure,
            renderPreflightFailureHtml,
            renderRequestResult,
            queryRequestStatus,
            renderRequestStatus,
            renderStatusCard,
            isDoneStatus,
            isFailedStatus,
            bindStatusCardActions,
            copyText,
            loadCaptcha,
            captchaPayload,
            loadRecentRequests,
            renderRecentRequests,
            saveRecentRequest,
            bindRecentRequests,
            bindRequestStatusPage,
        });
    }
    window.FnosPublicSubmit = Object.freeze({ create });
})();
