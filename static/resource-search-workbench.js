(function () {
    function resultKey(item = {}) {
        return String(item.result_key || item.public_id || item.resource_id || item.source_url || item.source_url_masked || `${item.source_type || item.source || ""}:${item.title || ""}:${item.size_text || item.size || ""}`).trim();
    }

    function sourceIconKey(item = {}) {
        const raw = [item.source_type, item.source_hint, item.source, item.source_url, item.source_url_masked].filter(Boolean).join(" ").toLowerCase();
        if (raw.includes("139") || raw.includes("mobile") || raw.includes("mcloud") || raw.includes("移动")) return "cloud139";
        if (raw.includes("189") || raw.includes("tianyi") || raw.includes("天翼")) return "cloud189";
        if (raw.includes("magnet") || raw.includes("torrent") || raw.includes("bt") || raw.includes("磁链") || raw.includes("种子")) return "bt";
        if (raw.includes("quark") || raw.includes("夸克") || raw.includes("uc")) return "quark";
        return "other";
    }

    function mergeResults(current, incoming, options = {}) {
        const values = Array.isArray(current) ? current.slice() : [];
        const seen = new Set(values.map(resultKey).filter(Boolean));
        const additions = [];
        (Array.isArray(incoming) ? incoming : []).forEach((item) => {
            const key = resultKey(item);
            if (!key || seen.has(key)) return;
            seen.add(key);
            additions.push(item);
        });
        const merged = values.concat(additions);
        return {
            items: typeof options.sort === "function" ? options.sort(merged) : merged,
            additions,
        };
    }

    function renderCard(options = {}) {
        const escapeHtml = options.escapeHtml || ((value) => String(value ?? ""));
        const classes = ["resource-card", "resource-row", ...(options.classes || [])].filter(Boolean).join(" ");
        const id = escapeHtml(options.id || "");
        const attributes = options.attributes || `data-workbench-resource-id="${id}"`;
        const poster = options.poster
            ? `<div class="resource-poster"><img src="${escapeHtml(options.poster)}" alt="${escapeHtml(options.posterAlt || options.title || "资源封面")}" loading="lazy" referrerpolicy="no-referrer-when-downgrade" onerror="this.closest('.resource-poster').classList.add('poster-error');this.remove();"></div>`
            : `<div class="resource-icon ${escapeHtml(options.iconClass || "")}">${options.iconHtml || ""}</div>`;
        return `
            <article class="${classes}" ${attributes} tabindex="0" role="button" aria-expanded="${options.selected ? "true" : "false"}">
                ${options.badgesHtml || ""}
                ${poster}
                <div class="resource-main">
                    <h3 class="resource-title">${escapeHtml(options.title || "未命名资源")}</h3>
                    <div class="resource-tags">${options.tagsHtml || ""}</div>
                    <div class="resource-foot">${options.metaHtml || ""}</div>
                </div>
                <div class="card-actions">${options.actionsHtml || ""}</div>
            </article>
            ${options.selected ? options.detailHtml || "" : ""}
        `;
    }

    function bindCards(root, handlers = {}) {
        if (!root) return;
        root.querySelectorAll("[data-workbench-resource-id], [data-resource-public-id]").forEach((card) => {
            const id = card.dataset.workbenchResourceId || card.dataset.resourcePublicId || "";
            const activate = (event) => {
                if (event.target.closest("button, a, input, textarea, select, label, summary, [data-file-toggle]")) return;
                handlers.onSelect?.(id, card, event);
            };
            card.addEventListener("click", activate);
            card.addEventListener("keydown", (event) => {
                if (event.key !== "Enter" && event.key !== " ") return;
                event.preventDefault();
                activate(event);
            });
        });
        root.querySelectorAll("[data-workbench-detail-id], [data-detail-public-id]").forEach((button) => {
            button.addEventListener("click", (event) => {
                event.preventDefault();
                event.stopPropagation();
                handlers.onDetail?.(button.dataset.workbenchDetailId || button.dataset.detailPublicId || "", button, event);
            });
        });
        root.querySelectorAll("[data-workbench-submit-id], [data-submit-public-id]").forEach((button) => {
            button.addEventListener("click", (event) => {
                event.preventDefault();
                event.stopPropagation();
                handlers.onSubmit?.(button.dataset.workbenchSubmitId || button.dataset.submitPublicId || "", button, event);
            });
        });
    }

    function bindFileInteractions(root, handlers = {}) {
        if (!root) return;
        root.querySelectorAll("[data-file-toggle]").forEach((button) => button.addEventListener("click", (event) => {
            event.preventDefault();
            event.stopPropagation();
            handlers.onFolderToggle?.(button.dataset.filePublicId, button.dataset.fileFid, button);
        }));
        root.querySelectorAll("[data-quark-select]").forEach((input) => input.addEventListener("change", (event) => handlers.onQuarkSelection?.(input, event)));
        root.querySelectorAll("[data-cloud139-select]").forEach((input) => input.addEventListener("change", (event) => handlers.onCloud139Selection?.(input, event)));
    }

    function detailValue(response = {}) {
        if (response.detail && typeof response.detail === "object") return response.detail;
        return response && typeof response === "object" ? response : {};
    }

    function detailItems(response = {}) {
        const detail = detailValue(response);
        if (Array.isArray(detail.items)) return detail.items;
        if (Array.isArray(detail.inspection?.items)) return detail.inspection.items;
        return [];
    }

    function itemKey(item = {}) {
        return String(item.fid || item.id || item.path || item.name || "").trim();
    }

    function sourceKind(value = "") {
        const type = String(value || "").toLowerCase();
        if (type.includes("quark") || type.includes("uc") || type.includes("夸克")) return "quark";
        if (type.includes("139") || type.includes("mobile") || type.includes("移动")) return "cloud139";
        if (type.includes("magnet") || type.includes("torrent") || type.includes("sixpan") || type.includes("bt")) return "sixpan";
        return "generic";
    }

    function renderFileSelection(response, options = {}) {
        const escapeHtml = options.escapeHtml || ((value) => String(value ?? ""));
        const selected = options.selected || new Set();
        const publicId = String(options.publicId || "");
        const detail = detailValue(response);
        const kind = sourceKind(options.sourceType || detail.source_type || detail.link?.source_type || "");
        const selectionContext = detail._workbench_selection_context || {};
        const items = detailItems(response);
        if (!items.length) return `<div class="empty-cell">${escapeHtml(options.emptyMessage || "未提供可选择的文件列表")}</div>`;
        const contextBar = kind === "quark" && selectionContext.mode === "subdir_items"
            ? `<div class="filter-row" style="margin-bottom:8px;"><button class="secondary mini" type="button" data-workbench-root-public-id="${escapeHtml(publicId)}">返回根目录</button><span class="muted">当前目录：${escapeHtml(selectionContext.base_dir?.name || selectionContext.base_dir?.fid || "-")}</span></div>`
            : "";
        return `${contextBar}<div class="file-preview-list">${items.map((item) => {
            const key = itemKey(item);
            const directory = Boolean(item.is_dir || item.directory || item.type === "dir" || item.type === 1);
            const selectable = kind !== "generic" && (kind !== "quark" || selectionContext.mode === "subdir_items" || directory);
            const checked = selected.has(key);
            return `<label class="file-preview-row">
                <input type="checkbox" data-workbench-file-key="${escapeHtml(key)}" data-workbench-file-public-id="${escapeHtml(publicId)}" ${checked ? "checked" : ""} ${selectable ? "" : "disabled"}>
                <span class="file-preview-main"><strong>${escapeHtml(item.name || item.title || item.path || key || "未命名")}</strong><small>${escapeHtml(directory ? "文件夹" : item.size_text || item.size || "文件")}</small></span>
                ${directory ? `<button class="secondary mini" type="button" data-workbench-folder-public-id="${escapeHtml(publicId)}" data-workbench-folder-fid="${escapeHtml(item.fid || item.id || key)}">展开</button>` : ""}
            </label>`;
        }).join("")}</div>`;
    }

    function selectedItems(response, selected) {
        const chosen = selected || new Set();
        return detailItems(response).filter((item) => chosen.has(itemKey(item)));
    }

    function selectionPayload(response, selected, sourceType = "") {
        const detail = detailValue(response);
        const items = detailItems(response);
        const chosen = selectedItems(response, selected);
        const kind = sourceKind(sourceType || detail.source_type || detail.link?.source_type || "");
        if (kind === "quark") {
            const selectionContext = detail._workbench_selection_context || {};
            if (selectionContext.mode === "subdir_items" && selectionContext.base_dir) {
                return chosen.length ? { quark_selection: { mode: "subdir_items", base_dir: selectionContext.base_dir, selected_items: chosen } } : {};
            }
            const dirs = chosen.filter((item) => item.is_dir || item.directory || item.type === "dir" || item.type === 1);
            return dirs.length ? { quark_selection: { mode: "root_dirs", selected_dirs: dirs } } : {};
        }
        if (kind === "cloud139") {
            const selectedFolders = chosen.filter((item) => item.is_dir || item.directory || item.type === "dir" || item.type === 1);
            const selectedFiles = chosen.filter((item) => !selectedFolders.includes(item));
            return chosen.length ? { cloud139_selection: { mode: "items", selected_files: selectedFiles, selected_folders: selectedFolders } } : {};
        }
        if (kind === "sixpan") {
            if (!chosen.length) return {};
            const ignoreFiles = items.filter((item) => !(item.is_dir || item.directory) && !selected.has(itemKey(item))).map((item) => item.id || item.fid).filter(Boolean);
            return { ignore_files: ignoreFiles, sixpan_selection: { total_count: items.length, selected_count: chosen.length, ignored_count: ignoreFiles.length } };
        }
        return {};
    }

    function createSupplementController(options = {}) {
        let active = false;
        let timer = null;
        let generation = 0;

        function stop() {
            active = false;
            generation += 1;
            if (timer) window.clearTimeout(timer);
            timer = null;
            options.onStatus?.("", false);
        }

        function start(context = {}) {
            stop();
            active = true;
            const token = generation;
            const maxRounds = Math.max(1, Number(context.maxRounds || options.maxRounds || 3));
            const intervalMs = Math.max(250, Number(context.intervalMs || options.intervalMs || 2200));
            let round = 0;
            options.onStatus?.(context.startMessage || "正在补充搜索资源中...", true);
            const poll = async () => {
                if (!active || token !== generation) return;
                round += 1;
                try {
                    const result = await options.search?.(context, round);
                    if (!active || token !== generation) return;
                    const added = Number(options.onResult?.(result, context, round) || 0);
                    options.onStatus?.(added > 0 ? `正在补充搜索资源中... 已新增 ${added} 条` : "正在补充搜索资源中...", true);
                } catch (error) {
                    options.onError?.(error, context, round);
                }
                if (!active || token !== generation) return;
                if (round >= maxRounds) {
                    active = false;
                    timer = null;
                    options.onStatus?.("", false);
                    options.onComplete?.(context, round);
                    return;
                }
                timer = window.setTimeout(poll, intervalMs);
            };
            timer = window.setTimeout(poll, intervalMs);
        }

        return Object.freeze({ start, stop, get active() { return active; } });
    }

    window.FnosResourceSearchWorkbench = Object.freeze({
        resultKey,
        sourceIconKey,
        mergeResults,
        renderCard,
        bindCards,
        bindFileInteractions,
        detailValue,
        detailItems,
        itemKey,
        sourceKind,
        renderFileSelection,
        selectionPayload,
        createSupplementController,
    });
})();
