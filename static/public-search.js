(function () {
    function create(context) {
        const {
            state,
            getElement,
            api,
            toast,
            escapeHtml,
            icon,
            formatDate,
            categoryLabel,
            sourceLabel,
            publicMessageForDisplay,
            showGlobalLoading,
            hideGlobalLoading,
            showPublicSearchTransition,
            hidePublicSearchTransition,
            resetNoticeBox,
            setPublicSearchStatus,
            loadSixpanParseForPublic,
            resolveBtbtlaResource,
            toggleResourceFolder,
            handleQuarkSelectionAction,
            handleQuarkFolderOpen,
            handleQuarkSelectionChange,
            handleCloud139SelectionChange,
            handleTianyiSelectionChange,
            submitPublicResource,
            renderPublicResourceDetail,
            isPublicSubmitConfirmOpen,
            searchSourceMetaDefaults,
        } = context;
        const publicState = state;
        const $ = getElement;
        const SEARCH_SOURCE_META = searchSourceMetaDefaults;
        const workbench = window.FnosResourceSearchWorkbench;
        let publicSupplementController = null;

        function tmdbPanel() {
            return $("publicTmdbPanel");
        }

        function setTmdbPanelVisibility(visible) {
            const panel = tmdbPanel();
            if (!panel) return;
            panel.classList.toggle("hidden", !visible);
            document.body.classList.toggle("public-tmdb-modal-open", Boolean(visible));
            document.documentElement.classList.toggle("is-tmdb-modal-open", Boolean(visible));
        }

        function hideTmdbPanel() {
            setTmdbPanelVisibility(false);
            const candidates = $("publicTmdbCandidates");
            if (candidates) candidates.innerHTML = "";
        }

        function setTmdbPanelState(state, title) {
            const panel = tmdbPanel();
            if (!panel) return;
            panel.dataset.state = state || "";
            if ($("publicTmdbTitle")) $("publicTmdbTitle").textContent = title || "请选择匹配的影视";
        }

        function renderTmdbCandidates(items) {
            const panel = tmdbPanel();
            const box = $("publicTmdbCandidates");
            if (!panel || !box) return;
            setTmdbPanelState("ready", "请选择匹配的影视");
            box.innerHTML = (Array.isArray(items) ? items : []).map((item, index) => {
                const title = String(item?.title || "未命名影视");
                const poster = String(item?.poster_url || "").trim();
                const type = item?.media_type === "tv" ? "电视剧" : "电影";
                const year = String(item?.year || "").trim();
                const overview = String(item?.overview || "").trim();
                const score = Number(item?.vote_average || 0);
                const scoreText = Number.isFinite(score) && score > 0 ? score.toFixed(1) : "";
                return `<button class="public-tmdb-candidate" type="button" data-tmdb-index="${index}" aria-label="选择 ${escapeHtml(title)}">
                    <span class="public-tmdb-candidate-poster${poster ? " has-poster" : ""}">
                        ${poster ? `<img src="${escapeHtml(poster)}" alt="${escapeHtml(title)}" loading="lazy">` : `<span>${escapeHtml(title.slice(0, 1))}</span>`}
                        <span class="public-tmdb-select-mark" aria-hidden="true"><span class="icon-slot icon-check"></span></span>
                    </span>
                    <span class="public-tmdb-candidate-copy">
                        <span class="public-tmdb-candidate-meta"><small>${escapeHtml(type)}</small>${year ? `<small>${escapeHtml(year)}</small>` : ""}${scoreText ? `<small class="is-score">★ ${escapeHtml(scoreText)}</small>` : ""}</span>
                        <strong class="public-tmdb-candidate-title">${escapeHtml(title)}</strong>
                        ${item?.original_title && item.original_title !== title ? `<em>${escapeHtml(item.original_title)}</em>` : ""}
                        <span class="public-tmdb-candidate-overview${overview ? "" : " is-empty"}">${escapeHtml(overview || "暂无剧情简介，可根据海报、年份和类型确认。")}</span>
                    </span>
                </button>`;
            }).join("");
            setTmdbPanelVisibility(true);
            const modal = tmdbPanel();
            if (modal) modal.dataset.state = "ready";
        }

        function renderTmdbManualState({ title, message, tone = "info", detail }) {
            const panel = tmdbPanel();
            const box = $("publicTmdbCandidates");
            if (!panel || !box) return;
            setTmdbPanelState("empty", title);
            setTmdbStatus(message, tone);
            box.innerHTML = `<div class="public-tmdb-empty">
                <span class="public-tmdb-empty-icon" aria-hidden="true"><span class="icon-slot icon-search"></span></span>
                <span class="public-tmdb-empty-copy">
                    <strong>继续搜索原关键词</strong>
                    <span>${escapeHtml(detail)}</span>
                </span>
                <button class="public-tmdb-manual-action" type="button" data-tmdb-action="manual">搜索资源</button>
            </div>`;
            setTmdbPanelVisibility(true);
        }

        function setTmdbStatus(message, tone = "") {
            const box = $("publicTmdbStatus");
            if (!box) return;
            box.className = `public-tmdb-status${tone ? ` status-${tone}` : ""}`;
            box.textContent = message || "";
        }

        async function searchTmdbCandidates(keyword) {
            const sequence = (publicState.tmdbSearchSequence || 0) + 1;
            publicState.tmdbSearchSequence = sequence;
            publicState.tmdbQuery = keyword;
            publicState.tmdbSelection = null;
            publicState.tmdbDecision = "pending";
            stopPublicSearchSupplement();
            publicState.searchSequence = (publicState.searchSequence || 0) + 1;
            publicState.results = [];
            publicState.details = {};
            publicState.filePreviews = {};
            publicState.selectedPublicId = "";
            if ($("publicResults")) $("publicResults").innerHTML = "";
            setPublicSearchStatus("正在识别影视，随后开始搜索资源...", { tone: "info" });
            setTmdbPanelState("loading", "正在识别影视");
            setTmdbStatus("正在匹配影视信息...");
            const panel = tmdbPanel();
            setTmdbPanelVisibility(true);
            const box = $("publicTmdbCandidates");
            if (box) box.innerHTML = `<div class="public-tmdb-loading">正在查询 TMDB...</div>`;
            try {
                const data = await api(`/api/public/tmdb/search?q=${encodeURIComponent(keyword)}`, { allowFailure: true });
                if (sequence !== publicState.tmdbSearchSequence) return false;
                const status = String(data.status || "empty");
                publicState.tmdbCandidates = Array.isArray(data.items) ? data.items : [];
                if (status === "ready" && publicState.tmdbCandidates.length) {
                    setTmdbStatus("");
                    renderTmdbCandidates(publicState.tmdbCandidates);
                } else if (status === "not_configured") {
                    renderTmdbManualState({
                        title: "影视识别暂未启用",
                        message: "当前未配置 TMDB，不影响继续查找资源。",
                        detail: "系统会直接使用你输入的片名查找网盘与磁链资源。",
                    });
                } else if (status === "unavailable") {
                    renderTmdbManualState({
                        title: "影视识别暂时不可用",
                        message: "TMDB 服务暂时没有响应，资源搜索仍可正常使用。",
                        tone: "error",
                        detail: "跳过影视识别，直接按当前关键词继续搜索。",
                    });
                } else {
                    renderTmdbManualState({
                        title: "没有找到匹配的影视",
                        message: "TMDB 中暂无匹配条目，你仍可以继续搜索资源。",
                        detail: "将保留当前关键词，不会因为缺少影视信息阻断搜索。",
                    });
                }
                return true;
            } catch (error) {
                if (sequence !== publicState.tmdbSearchSequence) return false;
                renderTmdbManualState({
                    title: "影视识别暂时不可用",
                    message: "TMDB 服务暂时没有响应，资源搜索仍可正常使用。",
                    tone: "error",
                    detail: "跳过影视识别，直接按当前关键词继续搜索。",
                });
                return false;
            }
        }

        async function beginResourceSearch(keyword, candidate = null) {
            const item = candidate && typeof candidate === "object" ? candidate : null;
            // 只用 TMDB 的标准片名检索：PanSou 按资源标题做字面匹配，拼上年份会让命中数急剧下降。
            // 影视消歧仍由提交阶段的 tmdb_selection_token 承担，不依赖搜索词里的年份。
            const effectiveKeyword = String(item?.title || keyword || "").trim();
            publicState.resourceSearchKeyword = effectiveKeyword;
            hideTmdbPanel();
            showPublicSearchTransition(item || { title: keyword }, item ? "正在使用标准片名匹配资源..." : "正在按原关键词匹配资源...");
            try {
                await performResourceSearch(effectiveKeyword.trim() || keyword);
            } finally {
                await new Promise((resolve) => window.setTimeout(resolve, 850));
                hidePublicSearchTransition();
            }
        }

        function selectedSearchSource() {
            const checked = document.querySelector('input[name="publicSearchSource"]:checked');
            return checked?.value || publicState.searchSource || "pansou";
        }

        function searchSourcePayload(source = selectedSearchSource()) {
            const value = String(source || "pansou").toLowerCase();
            return value ? [value] : [];
        }

        function searchSourceLabel(source = selectedSearchSource()) {
            const key = String(source || "pansou").toLowerCase();
            const provider = (Array.isArray(publicState.searchProviders) ? publicState.searchProviders : []).find((item) => String(item?.key || "").toLowerCase() === key);
            const meta = searchSourceMeta(key, provider);
            return meta.label || meta.title || provider?.name || key;
        }

        function enabledSearchSourceKeys() {
            return enabledSearchSources().map((item) => item.key);
        }

        function enabledSearchSources() {
            const providers = Array.isArray(publicState.searchProviders) ? publicState.searchProviders : [];
            const items = providers
                .filter((item) => item && item.enabled !== false && item.configured !== false)
                .map((item) => ({ ...item, key: String(item.key || "").toLowerCase() }))
                .filter((item) => item.key);
            if (!providers.length) {
                return [{ key: "pansou", name: "PanSou", enabled: true, configured: true, priority: 10 }];
            }
            return items;
        }

        function searchSourceMeta(key, provider = {}) {
            const sourceKey = String(key || "").toLowerCase();
            const base = SEARCH_SOURCE_META[sourceKey] || {};
            const providerName = String(provider?.name || sourceKey || "搜索源").trim();
            return {
                key: sourceKey,
                title: base.title || providerName,
                label: base.label || providerName,
                badge: base.badge || "",
                desc: base.desc || String(provider?.message || "使用该搜索源检索资源").trim(),
                icon: base.icon || sourceKey || "search",
            };
        }

        function renderSearchSourceOption(provider, activeKey) {
            const key = String(provider?.key || "").toLowerCase();
            const meta = searchSourceMeta(key, provider);
            const active = key === activeKey;
            return `
                <label class="source-option ${active ? "active" : ""}" data-search-source-option="${escapeHtml(key)}">
                    <input type="radio" name="publicSearchSource" value="${escapeHtml(key)}" ${active ? "checked" : ""}>
                    <span class="source-option-icon"><span class="icon-slot icon-${escapeHtml(meta.icon)}" aria-hidden="true"></span></span>
                    <span class="source-option-body">
                        <span class="source-option-title">${escapeHtml(meta.title)}${meta.badge ? ` <small>${escapeHtml(meta.badge)}</small>` : ""}</span>
                        <span class="source-option-desc">${escapeHtml(meta.desc)}</span>
                    </span>
                </label>
            `;
        }

        function syncSearchSourceOptions() {
            const enabledSources = enabledSearchSources();
            const keys = enabledSources.map((item) => item.key);
            const allowed = new Set(keys);
            const group = document.querySelector(".search-source-toggle");
            const panel = document.querySelector("[data-search-source-panel]");
            let current = selectedSearchSource();
            if (!allowed.has(current)) {
                current = allowed.has("pansou") ? "pansou" : keys[0] || "";
            }
            publicState.searchSource = current;
            if (group) {
                group.style.setProperty("--search-source-columns", String(Math.min(Math.max(keys.length, 1), 4)));
                group.innerHTML = enabledSources.map((item) => renderSearchSourceOption(item, current)).join("");
            }
            if (current) {
                const selected = document.querySelector(`input[name="publicSearchSource"][value="${current}"]`);
                if (selected) selected.checked = true;
            }
            updateSearchSourceActiveState();
            const shouldHide = allowed.size <= 1;
            if (panel) panel.hidden = shouldHide;
            if (group) group.hidden = shouldHide;
        }

        function updateSearchSourceActiveState() {
            document.querySelectorAll(".search-source-toggle .source-option").forEach((label) => {
                const input = label.querySelector('input[name="publicSearchSource"]');
                label.classList.toggle("active", Boolean(input?.checked));
            });
        }

        function sourceDescriptor(item = {}) {
            const link = item?.link && typeof item.link === "object" ? item.link : {};
            return {
                providerKind: String(item?.provider_kind || link.provider_kind || "").toLowerCase(),
                sourceForm: String(item?.source_form || link.source_form || "").toLowerCase(),
                sourceType: String(item?.source_type || link.source_type || "").toLowerCase(),
                searchSourceId: String(item?.search_source_id || link.search_source_id || "").toLowerCase(),
            };
        }

        function sourceIconKey(item) {
            const source = sourceDescriptor(item);
            if (source.providerKind === "mobile" || source.sourceType === "cloud139") return "cloud139";
            if (source.providerKind === "tianyi" || source.sourceType === "cloud189") return "cloud189";
            if (source.providerKind === "sixpan" || source.sourceForm === "magnet" || source.providerKind === "btbtla" || ["magnet", "torrent"].includes(source.sourceType)) return "bt";
            if (source.providerKind === "quark") return "quark";
            return "other";
        }

        function sourceTagMeta(item) {
            const source = sourceDescriptor(item);
            if (source.providerKind === "quark" || source.sourceType === "quark") return { label: "夸克网盘", className: "tag-source-quark" };
            if (source.providerKind === "mobile" || source.sourceType === "cloud139") return { label: "移动云", className: "tag-source-mobile" };
            if (source.providerKind === "tianyi" || source.sourceType === "cloud189") return { label: "天翼云盘", className: "tag-source-tianyi" };
            if (source.providerKind === "btbtla" || source.providerKind === "sixpan" || source.sourceForm === "magnet" || ["magnet", "torrent"].includes(source.sourceType)) {
                return { label: "磁链 / BT", className: "tag-source-bt" };
            }
            return {
                label: sourceLabel(source.providerKind || source.searchSourceId),
                className: "tag-source-unknown",
            };
        }

        function resultProviderBadgeKey(item) {
            const source = sourceDescriptor(item);
            if (source.providerKind === "quark" || source.sourceType === "quark") return "quark";
            if (source.providerKind === "mobile" || source.sourceType === "cloud139") return "mobile";
            if (source.providerKind === "tianyi" || source.sourceType === "cloud189") return "tianyi";
            if (
                source.providerKind === "btbtla"
                || source.providerKind === "sixpan"
                || source.providerKind === "magnet"
                || source.sourceForm === "magnet"
                || ["magnet", "torrent"].includes(source.sourceType)
            ) return "magnet";
            return "";
        }

        function resultProviderBadgeMeta(item, options = {}) {
            if (isResourceInvalid(item)) return null;
            const provider = resultProviderBadgeKey(item);
            if (!provider) return null;
            const badges = publicState.resultProviderBadges && typeof publicState.resultProviderBadges === "object"
                ? publicState.resultProviderBadges
                : {};
            const config = badges[provider] && typeof badges[provider] === "object" ? badges[provider] : {};
            const text = String(config.text || "").trim();
            if (config.enabled !== true || !/^[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]{2,5}$/u.test(text)) return null;
            if (options.slowImport) {
                return { provider: "magnet", text: "慢速入库", className: "tag-source-bt" };
            }
            return { provider, text, className: sourceTagMeta(item).className };
        }

        function publicPosterUrl(item = {}) {
            const raw = item && typeof item === "object" ? item : {};
            const preview = raw.search_preview && typeof raw.search_preview === "object" ? raw.search_preview : {};
            return normalizePublicPosterUrl(raw.poster || raw.cover || raw.image_url || preview.poster || preview.cover || preview.image_url || "");
        }

        function normalizePublicPosterUrl(value = "") {
            const text = String(value || "").trim().replaceAll("&amp;", "&");
            if (!text) return "";
            try {
                const url = new URL(text, window.location.origin);
                if (url.hostname.toLowerCase().endsWith("sogoucdn.com")) {
                    const realUrl = url.searchParams.get("url") || "";
                    if (realUrl) {
                        return normalizePublicPosterUrl(/^https?:\/\//i.test(realUrl) ? realUrl : `https://${realUrl}`);
                    }
                }
                return url.href;
            } catch {
                if (/^(?:image\.tmdb\.org|img\d*\.doubanio\.com|p\d+\.ssl\.qhimgs\d*\.com|ps\.ssl\.qhmsg\.com)\//i.test(text)) {
                    return `https://${text}`;
                }
                return text;
            }
        }

        function publicSourceOrigin(item = {}) {
            const raw = item && typeof item === "object" ? item : {};
            const preview = raw.search_preview && typeof raw.search_preview === "object" ? raw.search_preview : {};
            const direct = String(raw.source_origin || raw.referer || preview.source_origin || preview.referer || "").trim();
            if (direct) return direct.replace(/^https?:\/\//i, "").replace(/\/.*$/, "");
            const source = String(raw.source_url || raw.source_url_masked || raw.url || "").trim();
            const match = source.match(/^(?:https?:\/\/)?([^/]+)/i);
            return match ? match[1] : "";
        }

        function isSixpanCandidate(item) {
            if (isBtbtlaCandidate(item)) return false;
            const source = sourceDescriptor(item);
            return source.providerKind === "sixpan" && source.sourceForm === "magnet";
        }

        function isBtbtlaCandidate(item) {
            const source = sourceDescriptor(item);
            return source.providerKind === "btbtla" || source.searchSourceId === "btbtla";
        }

        function isQuarkCandidate(item) {
            return sourceDescriptor(item).providerKind === "quark";
        }

        function isCloud139Candidate(item) {
            return sourceDescriptor(item).providerKind === "mobile";
        }

        function isInstantImportResource(item) {
            const sourceType = String(item?.source_type || "").trim().toLowerCase();
            return item?.instant_import === true || isCloud139Candidate(item) || sourceType === "cloud139";
        }

        function isTianyiCandidate(item) {
            return sourceDescriptor(item).providerKind === "tianyi";
        }

        function sortPublicResources(items) {
            return [...(Array.isArray(items) ? items : [])].sort((a, b) => {
                const scoreDelta = Number(b.ranking_score || 0) - Number(a.ranking_score || 0);
                if (scoreDelta) return scoreDelta;
                const aRank = Number(a.rank || Number.MAX_SAFE_INTEGER);
                const bRank = Number(b.rank || Number.MAX_SAFE_INTEGER);
                return aRank - bRank;
            });
        }

        function inferQuality(title) {
            const text = String(title || "").toUpperCase();
            if (text.includes("8K")) return "8K";
            if (text.includes("2160") || text.includes("4K") || text.includes("UHD")) return "4K";
            if (text.includes("1080")) return "1080P";
            if (text.includes("720")) return "720P";
            return "";
        }

        function categoryConfidenceText(value) {
            const number = Number(value || 0);
            if (!number) return "低";
            return `${Math.round(number * 100)}%`;
        }

        function knownSizeText(...values) {
            for (const value of values) {
                const text = String(value ?? "").trim();
                if (!text) continue;
                if (/^(0|0\s*b|大小未知|未知|-|none|null)$/i.test(text)) continue;
                return text;
            }
            return "";
        }

        function categoryForPublicResource(publicId) {
            const item = publicResourceById(publicId);
            if (publicState.categoryTouched) return publicState.selectedCategory || "movie";
            return item?.category_suggestion?.key || publicState.selectedCategory || "movie";
        }

        function recommendedCategoryForItem(item) {
            const suggestion = item?.category_suggestion || {};
            if (publicState.categoryTouched) return publicState.selectedCategory || suggestion.key || "movie";
            return suggestion.key || publicState.selectedCategory || "movie";
        }

        function publicResourceById(publicId) {
            const id = String(publicId || "");
            if (publicState.manualPreview?.item?.public_id === id) return publicState.manualPreview.item;
            return publicState.results.find((entry) => entry.public_id === id) || null;
        }

        function isPublicDetailCached(state) {
            return Boolean(state && !state.loading && (state.detail || state.error));
        }

        function publicDetailButtonLabel(selected, state) {
            if (!selected) return "查看详情";
            if (state?.loading) return "读取中";
            return "收起详情";
        }

        async function ensurePublicResourceDetail(publicId, options = {}) {
            const item = publicResourceById(publicId);
            if (!item) return { loading: false, error: "资源不存在或已过期，请重新搜索" };
            const cachedState = publicState.details[publicId];
            if (cachedState?.detail) return cachedState;
            if (cachedState?.loading && publicState.detailPromises[publicId]) {
                return publicState.detailPromises[publicId];
            }

            const render = options.render !== false;
            const loadingMessage = options.loadingMessage || "";
            publicState.details[publicId] = { loading: true };
            updatePublicResourceState(publicId, { availability_status: "checking", availability_message: "正在读取详情并检测资源有效性" }, { render: false });
            if (render) renderPublicResults();

            const promise = (async () => {
                try {
                    if (loadingMessage) showGlobalLoading(loadingMessage);
                    const data = await api(`/api/public/resources/${encodeURIComponent(publicId)}/detail`, { allowFailure: true });
                    if (data.success === false) throw new Error(data.message || "资源详情读取失败");
                    const detail = data.detail || {};
                    const inspection = detail.inspection || {};
                    publicState.details[publicId] = { loading: false, detail, success: data.success !== false };
                    updatePublicResourceState(
                        publicId,
                        {
                            provider_kind: detail.provider_kind || detail.link?.provider_kind || item.provider_kind,
                            source_form: detail.source_form || detail.link?.source_form || item.source_form,
                            search_source_id: detail.search_source_id || detail.link?.search_source_id || item.search_source_id,
                            supported: detail.supported ?? item.supported,
                            category_suggestion: detail.category_suggestion || item.category_suggestion,
                            size_text: knownSizeText(detail.size_text, item.size_text, item.size),
                        },
                        { render: false },
                    );
                    if (isInspectionInvalid(inspection)) {
                        updatePublicResourceState(
                            publicId,
                            {
                                availability_status: "invalid",
                                availability_message: publicMessageForDisplay(inspection.message || "详情检测未通过，资源可能已失效", inspection.status || "检测未通过", "详情检测未通过，资源可能已失效"),
                            },
                            { render: false },
                        );
                    } else if (inspection.success === true) {
                        updatePublicResourceState(
                            publicId,
                            {
                                availability_status: "checked",
                                availability_message: publicMessageForDisplay(inspection.message || "详情检测通过", inspection.status || "检测通过", "详情检测通过"),
                            },
                            { render: false },
                        );
                    } else {
                        updatePublicResourceState(publicId, { availability_status: "", availability_message: "" }, { render: false });
                    }
                    if (isSixpanCandidate(item || detail || {}) && !isInspectionInvalid(inspection)) {
                        await loadSixpanParseForPublic(publicId, item || detail);
                    }
                    return publicState.details[publicId];
                } catch (error) {
                    const message = publicMessageForDisplay(error.message || "详情检测失败", "检测未通过", "详情检测失败");
                    publicState.details[publicId] = { loading: false, error: message };
                    updatePublicResourceState(publicId, { availability_status: "invalid", availability_message: message }, { render: false });
                    return publicState.details[publicId];
                } finally {
                    if (loadingMessage) hideGlobalLoading();
                    delete publicState.detailPromises[publicId];
                    if (render) renderPublicResults();
                }
            })();

            publicState.detailPromises[publicId] = promise;
            return promise;
        }

        function updatePublicResourceState(publicId, patch = {}, options = {}) {
            const item = publicResourceById(publicId);
            if (!item) return null;
            Object.assign(item, patch);
            if (options.render !== false) renderPublicResults();
            return item;
        }

        function isResourceInvalid(item) {
            return String(item?.availability_status || "").toLowerCase() === "invalid";
        }

        function isInspectionInvalid(inspection = {}) {
            const status = String(inspection.status || "").toLowerCase();
            if (!inspection || inspection.success !== false) return false;
            if (status === "reserved" || status === "unconfigured") return false;
            return ["failed", "error", "invalid", "expired", "forbidden"].includes(status);
        }

        function resourceStateTag(item) {
            const status = String(item?.availability_status || "").toLowerCase();
            const message = publicMessageForDisplay(item?.availability_message || "", status, item?.availability_message || "");
            if (status === "invalid") {
                return `<span class="tag tag-red" title="${escapeHtml(message)}">资源失效</span>`;
            }
            if (status === "parse_empty") {
                return `<span class="tag tag-orange" title="${escapeHtml(message)}">慢速入库</span>`;
            }
            if (status === "parse_failed") {
                return `<span class="tag tag-orange" title="${escapeHtml(message)}">慢速入库</span>`;
            }
            if (status === "parse_ok") {
                return `<span class="tag tag-green">可快速入库</span>`;
            }
            if (status === "checked") {
                return `<span class="tag tag-green">详情检测通过</span>`;
            }
            if (status === "checking") {
                return `<span class="tag tag-orange">检测中</span>`;
            }
            if (item?.supported === false) {
                return `<span class="tag tag-orange">需人工处理</span>`;
            }
            return "";
        }

        function sixpanHasSelectableFiles(state) {
            return Array.isArray(state?.items) && state.items.some((item) => item?.selectable && item?.id);
        }

        function sixpanSubmitSpeed(publicId) {
            const state = sixpanParseState(sixpanPublicKey(publicId));
            if (state?.loading) return "checking";
            if (!state) return "unknown";
            if (state.error || state.slow || state.parse_status === "empty_files") return "slow";
            if (state.success && sixpanHasSelectableFiles(state)) return "fast";
            if (state.success) return "slow";
            return "unknown";
        }

        function publicSubmitButtonMeta(item) {
            const invalid = isResourceInvalid(item);
            if (invalid) {
                return {
                    label: "资源失效",
                    className: "secondary",
                    disabled: true,
                    title: publicMessageForDisplay(
                        item?.availability_message || "详情检测未通过，当前资源不可提交",
                        item?.availability_status || "invalid",
                        "分享链接已失效",
                    ),
                };
            }
            if (!item?.supported) {
                return { label: "提交审核", className: "secondary", disabled: false, title: "" };
            }
            if (isBtbtlaCandidate(item)) {
                return { label: "选择资源", className: "secondary", disabled: false, title: "先选择下载资源并解析磁链" };
            }
            if (isSixpanCandidate(item)) {
                const speed = sixpanSubmitSpeed(item.public_id);
                if (speed === "fast") {
                    return { label: "快速入库", className: "submit-fast", disabled: false, title: "已找到可入库内容，可快速入库" };
                }
                if (speed === "slow") {
                    return { label: "慢速入库", className: "submit-slow secondary", disabled: false, title: "暂未找到可快速入库内容，将按慢速入库处理" };
                }
                if (speed === "checking") {
                    return { label: "预览中", className: "secondary", disabled: false, title: "请稍等，正在确认是否可快速入库" };
                }
                return { label: "提交入库", className: "", disabled: false, title: "点击后会先读取详情，确认可入库内容" };
            }
            return { label: "提交入库", className: "", disabled: false, title: "" };
        }

        async function performResourceSearch(keyword) {
            const searchSource = selectedSearchSource();
            const sources = searchSourcePayload(searchSource);
            const resultsBox = $("publicResults");
            if (!keyword) {
                setPublicSearchStatus("请输入搜索关键词。", { tone: "error" });
                $("publicKeywordInput")?.focus();
                toast("请输入搜索关键词", "error");
                return;
            }
            if (publicState.public?.allow_anonymous_search === false) {
                setPublicSearchStatus("匿名搜索已关闭，请使用链接提交。", { tone: "error" });
                toast("匿名搜索已关闭，请使用链接提交", "error");
                return;
            }
            if (!enabledSearchSourceKeys().length || !sources.length || !searchSource) {
                setPublicSearchStatus("暂无可用搜索源，请联系管理员开启搜索源。", { tone: "error" });
                toast("暂无可用搜索源，请联系管理员开启搜索源", "error");
                return;
            }
            stopPublicSearchSupplement();
            const sequence = publicState.searchSequence + 1;
            publicState.searchSequence = sequence;
            resetNoticeBox($("publicSearchStatus"));
            setSearchSupplementStatus("", false);
            setPublicSearchStatus(`${searchSourceLabel(searchSource)}搜索中，正在返回首批资源...`, { tone: "info" });
            if (resultsBox) resultsBox.innerHTML = "";
            let loadingVisible = false;
            try {
                const data = await api("/api/public/search", {
                    method: "POST",
                    body: JSON.stringify({ keyword, sources, async_poll: false }),
                });
                if (sequence !== publicState.searchSequence) return;
                publicState.results = sortPublicResources(data.items || []);
                publicState.details = {};
                publicState.filePreviews = {};
                publicState.selectedPublicId = "";
                const statusHtml = `${escapeHtml(searchSourceLabel(searchSource))}已返回首批结果：共 <strong>${publicState.results.length}</strong> 条，临时结果有效期 ${escapeHtml(data.expires_in_minutes || 60)} 分钟。${publicState.results.length === 0 && searchSource === "pansou" && enabledSearchSourceKeys().includes("btbtla") ? ` <button class="link-button inline" type="button" data-switch-search-source="btbtla">切换到 BT 磁链搜索</button>` : ""}`;
                setPublicSearchStatus(statusHtml, { html: true, tone: publicState.results.length ? "ok" : "info" });
                renderPublicResults();
                bindSearchSourceSwitchActions($("publicSearchStatus"));
                if (searchSource === "pansou") {
                    startPublicSearchSupplement(keyword, sequence, data.expires_in_minutes || 60, sources);
                }
            } catch (error) {
                setPublicSearchStatus(error.message, { tone: "error" });
                toast(error.message, "error");
            } finally {
                if (loadingVisible) hideGlobalLoading();
            }
        }

        async function searchPublicResources() {
            const keyword = $("publicKeywordInput")?.value.trim() || "";
            if (!keyword) {
                setPublicSearchStatus("请输入搜索关键词。", { tone: "error" });
                $("publicKeywordInput")?.focus();
                toast("请输入搜索关键词", "error");
                return;
            }
            if (publicState.tmdbQuery !== keyword || !["selected", "manual"].includes(publicState.tmdbDecision)) {
                await searchTmdbCandidates(keyword);
                return;
            }
            await beginResourceSearch(keyword, publicState.tmdbSelection);
        }

        function handleTmdbAction(target) {
            const action = target?.dataset?.tmdbAction || "";
            if (action === "manual") {
                publicState.tmdbDecision = "manual";
                publicState.tmdbSelection = null;
                const keyword = $("publicKeywordInput")?.value.trim() || publicState.tmdbQuery || "";
                if (keyword) beginResourceSearch(keyword).catch((error) => toast(error.message, "error"));
                return;
            }
            const index = Number(target?.dataset?.tmdbIndex);
            if (!Number.isInteger(index) || !publicState.tmdbCandidates?.[index]) return;
            publicState.tmdbDecision = "selected";
            publicState.tmdbSelection = publicState.tmdbCandidates[index];
            const keyword = $("publicKeywordInput")?.value.trim() || publicState.tmdbQuery || "";
            beginResourceSearch(keyword, publicState.tmdbSelection).catch((error) => toast(error.message, "error"));
        }

        function bindSearchSourceSwitchActions(root) {
            root?.querySelectorAll("[data-switch-search-source]").forEach((button) => {
                button.addEventListener("click", () => {
                    const value = button.dataset.switchSearchSource || "pansou";
                    const input = document.querySelector(`input[name="publicSearchSource"][value="${value}"]`);
                    if (input && !input.disabled) {
                        input.checked = true;
                        publicState.searchSource = value;
                        updateSearchSourceActiveState();
                        searchPublicResources();
                    }
                });
            });
        }

        function setSearchSupplementStatus(message, visible = true) {
            const box = $("publicSearchSupplementStatus");
            if (!box) return;
            if (!visible || !message) {
                box.textContent = "";
                box.classList.add("hidden");
                return;
            }
            box.textContent = message;
            box.classList.remove("hidden");
        }

        function stopPublicSearchSupplement() {
            publicSupplementController?.stop();
            publicState.supplementing = false;
            if (publicState.supplementTimer) {
                window.clearTimeout(publicState.supplementTimer);
                publicState.supplementTimer = null;
            }
            setSearchSupplementStatus("", false);
        }

        function publicResultKey(item) {
            return workbench.resultKey(item);
        }

        function mergePublicSearchResults(items) {
            const merged = workbench.mergeResults(publicState.results, items, { sort: sortPublicResources });
            if (merged.additions.length) {
                publicState.results = merged.items;
                renderPublicResults();
            }
            return merged.additions.length;
        }

        function startPublicSearchSupplement(keyword, sequence, expiresInMinutes, sources = searchSourcePayload("pansou")) {
            publicState.supplementing = true;
            publicSupplementController?.stop();
            publicSupplementController = workbench.createSupplementController({
                maxRounds: 3,
                intervalMs: 2200,
                search: () => api("/api/public/search", {
                        method: "POST",
                        body: JSON.stringify({ keyword, sources, async_poll: false, background: true }),
                        allowFailure: true,
                    }),
                onResult: (data) => sequence === publicState.searchSequence && data.success !== false ? mergePublicSearchResults(data.items || []) : 0,
                onStatus: setSearchSupplementStatus,
                onComplete: () => {
                    publicState.supplementing = false;
                    publicState.supplementTimer = null;
                    const status = $("publicSearchStatus");
                    if (status) {
                        status.innerHTML = `搜索完成：共 <strong>${publicState.results.length}</strong> 条结果，临时结果有效期 ${expiresInMinutes || 60} 分钟。`;
                    }
                },
            });
            publicSupplementController.start({});
        }

        function renderPublicResults() {
            const resultsBox = $("publicResults");
            if (!resultsBox) return;
            if (!publicState.results.length) {
                const canFallback = Boolean(publicState.tmdbSelection && publicState.tmdbQuery);
                resultsBox.innerHTML = `<div class="empty glass-panel soft">暂无结果，可尝试更换关键词或直接粘贴链接提交。${canFallback ? `<div><button class="link-button" type="button" data-tmdb-action="manual">使用原关键词“${escapeHtml(publicState.tmdbQuery)}”重新搜索</button></div>` : ""}</div>`;
                resultsBox.querySelector("[data-tmdb-action='manual']")?.addEventListener("click", (event) => handleTmdbAction(event.currentTarget));
                return;
            }
            resultsBox.innerHTML = publicState.results
                .map((item, index) => renderPublicResultItem(item, index))
                .join("");
            bindPreviewInteractions(resultsBox);
            workbench.bindCards(resultsBox, {
                onSelect: selectPublicResource,
                onDetail: selectPublicResource,
                onSubmit: submitPublicResource,
            });
        }

        function bindPreviewInteractions(root) {
            if (!root) return;
            workbench.bindFileInteractions(root, {
                onFolderToggle: toggleResourceFolder,
                onQuarkSelection: (input, event) => {
                    event.stopPropagation();
                    handleQuarkSelectionChange(input);
                },
                onCloud139Selection: (input, event) => {
                    event.stopPropagation();
                    handleCloud139SelectionChange(input);
                },
                onTianyiSelection: (input, event) => {
                    event.stopPropagation();
                    handleTianyiSelectionChange(input);
                },
            });
            root.querySelectorAll("[data-quark-action]").forEach((button) => {
                button.addEventListener("click", (event) => {
                    event.preventDefault();
                    event.stopPropagation();
                    handleQuarkSelectionAction(button).catch((error) => toast(error.message, "error"));
                });
            });
            root.querySelectorAll("[data-quark-open]").forEach((row) => {
                row.addEventListener("click", (event) => {
                    if (event.target.closest("input, label")) return;
                    event.preventDefault();
                    event.stopPropagation();
                    handleQuarkFolderOpen(row).catch((error) => toast(error.message, "error"));
                });
                row.addEventListener("keydown", (event) => {
                    if (event.key !== "Enter" && event.key !== " ") return;
                    event.preventDefault();
                    event.stopPropagation();
                    handleQuarkFolderOpen(row).catch((error) => toast(error.message, "error"));
                });
            });
            root.querySelectorAll("[data-cloud139-open]").forEach((row) => {
                row.addEventListener("click", (event) => {
                    if (event.target.closest("input, label")) return;
                    event.preventDefault();
                    event.stopPropagation();
                    toggleResourceFolder(row.dataset.filePublicId, row.dataset.fileFid);
                });
                row.addEventListener("keydown", (event) => {
                    if (event.key !== "Enter" && event.key !== " ") return;
                    event.preventDefault();
                    event.stopPropagation();
                    toggleResourceFolder(row.dataset.filePublicId, row.dataset.fileFid);
                });
            });
            root.querySelectorAll("[data-tianyi-open]").forEach((row) => {
                row.addEventListener("click", (event) => {
                    if (event.target.closest("input, label")) return;
                    event.preventDefault();
                    event.stopPropagation();
                    toggleResourceFolder(row.dataset.filePublicId, row.dataset.fileFid);
                });
                row.addEventListener("keydown", (event) => {
                    if (event.key !== "Enter" && event.key !== " ") return;
                    event.preventDefault();
                    event.stopPropagation();
                    toggleResourceFolder(row.dataset.filePublicId, row.dataset.fileFid);
                });
            });
            root.querySelectorAll("[data-btbtla-resolve]").forEach((button) => {
                button.addEventListener("click", (event) => {
                    event.preventDefault();
                    event.stopPropagation();
                    resolveBtbtlaResource(button).catch((error) => toast(error.message, "error"));
                });
            });
        }

        function refreshPublicResourceViews(publicId = "") {
            renderPublicResults();
            if (isPublicSubmitConfirmOpen(publicId)) {
                renderPublicConfirmPreview(publicId || publicState.pendingSubmit?.publicId || "");
            }
        }

        function renderPublicResultItem(item, index) {
            const selected = publicState.selectedPublicId === item.public_id;
            const sourceIcon = sourceIconKey(item);
            const quality = item.quality || inferQuality(item.title);
            const sourceTag = sourceTagMeta(item);
            const updatedAt = formatDate(item.datetime || item.created_at);
            const size = knownSizeText(item.size_text, item.size);
            const detailState = publicState.details[item.public_id];
            const sixpanCandidate = isSixpanCandidate(item);
            const sixpanSpeed = sixpanSubmitSpeed(item.public_id);
            const slowImport = sixpanCandidate && sixpanSpeed === "slow";
            const invalid = isResourceInvalid(item);
            const providerBadge = resultProviderBadgeMeta(item, { slowImport });
            const stateTag = resourceStateTag(item);
            const submitMeta = publicSubmitButtonMeta(item);
            const poster = publicPosterUrl(item);
            const sourceOrigin = publicSourceOrigin(item);
            const instantImport = !invalid && isInstantImportResource(item);
            const fastImport = !invalid && sixpanCandidate && !slowImport;
            const slowImportHighlight = !invalid && sixpanCandidate && slowImport;
            // 来源色边框优先：配置了来源角标就用自己 tag 的颜色描边；
            // 角标全部关闭时才退回能力态（秒入库/快速入库/慢速入库）配色。
            const capabilityClasses = providerBadge ? [] : [
                instantImport ? "is-instant-import" : "",
                fastImport ? "is-fast-import" : "",
                slowImportHighlight ? "is-slow-import" : "",
            ].filter(Boolean);
            const providerHighlightClass = providerBadge
                ? `has-provider-highlight provider-highlight-${providerBadge.provider}`
                : "";
            return workbench.renderCard({
                id: item.public_id,
                attributes: `data-resource-public-id="${escapeHtml(item.public_id)}"`,
                escapeHtml,
                selected,
                title: item.title || "未命名资源",
                poster,
                posterAlt: item.title || "资源封面",
                iconClass: `tile-source-${escapeHtml(sourceIcon)}`,
                iconHtml: icon(sourceIcon),
                classes: [
                    poster ? "has-poster" : "",
                    selected ? "selected" : "",
                    providerBadge ? "has-provider-badge" : "",
                    ...capabilityClasses,
                    providerHighlightClass,
                    invalid ? "is-invalid-resource" : "",
                ],
                badgesHtml: providerBadge ? `<div class="provider-capability-badge ${escapeHtml(providerBadge.className)}">${escapeHtml(providerBadge.text)}</div>` : "",
                tagsHtml: `<span class="tag tag-source ${escapeHtml(sourceTag.className)}">${escapeHtml(sourceTag.label)}</span>${quality ? `<span class="tag tag-gold">${escapeHtml(quality)}</span>` : ""}${stateTag}`,
                metaHtml: `${size ? `<span>文件大小：${escapeHtml(size)}</span>` : ""}<span>更新时间：${escapeHtml(updatedAt)}</span>${invalid ? `<span class="resource-invalid-message">${escapeHtml(publicMessageForDisplay(item.availability_message || "详情检测未通过", item.availability_status || "详情检测未通过", "详情检测未通过"))}</span>` : ""}`,
                actionsHtml: `<button class="secondary detail-trigger" type="button" data-detail-public-id="${escapeHtml(item.public_id)}">${publicDetailButtonLabel(selected, detailState)}</button><button type="button" class="${escapeHtml(submitMeta.className)}" data-submit-public-id="${escapeHtml(item.public_id)}" ${submitMeta.disabled ? "disabled" : ""} title="${escapeHtml(submitMeta.title)}">${escapeHtml(submitMeta.label)}</button>`,
                detailHtml: renderPublicResourceDetail(detailState),
            });
        }

        async function selectPublicResource(publicId) {
            if (!publicId) return;
            if (publicState.selectedPublicId === publicId) {
                publicState.selectedPublicId = "";
                renderPublicResults();
                return;
            }
            publicState.selectedPublicId = publicId;
            const cachedDetailState = publicState.details[publicId];
            if (cachedDetailState?.loading || isPublicDetailCached(cachedDetailState)) {
                renderPublicResults();
                return;
            }
            await ensurePublicResourceDetail(publicId, { loadingMessage: "正在读取资源详情并检测分享有效性..." });
        }

        return Object.freeze({
            selectedSearchSource,
            searchSourcePayload,
            searchSourceLabel,
            enabledSearchSourceKeys,
            enabledSearchSources,
            searchSourceMeta,
            renderSearchSourceOption,
            syncSearchSourceOptions,
            updateSearchSourceActiveState,
            sourceIconKey,
            sourceTagMeta,
            resultProviderBadgeKey,
            resultProviderBadgeMeta,
            publicPosterUrl,
            normalizePublicPosterUrl,
            publicSourceOrigin,
            isSixpanCandidate,
            isBtbtlaCandidate,
            isQuarkCandidate,
            isCloud139Candidate,
            isInstantImportResource,
            isTianyiCandidate,
            sortPublicResources,
            inferQuality,
            categoryConfidenceText,
            knownSizeText,
            categoryForPublicResource,
            recommendedCategoryForItem,
            publicResourceById,
            isPublicDetailCached,
            publicDetailButtonLabel,
            ensurePublicResourceDetail,
            updatePublicResourceState,
            isResourceInvalid,
            isInspectionInvalid,
            resourceStateTag,
            sixpanHasSelectableFiles,
            sixpanSubmitSpeed,
            publicSubmitButtonMeta,
            searchTmdbCandidates,
            beginResourceSearch,
            handleTmdbAction,
            searchPublicResources,
            bindSearchSourceSwitchActions,
            setSearchSupplementStatus,
            stopPublicSearchSupplement,
            publicResultKey,
            mergePublicSearchResults,
            startPublicSearchSupplement,
            renderPublicResults,
            bindPreviewInteractions,
            refreshPublicResourceViews,
            renderPublicResultItem,
            selectPublicResource,
        });
    }

    window.FnosPublicSearch = Object.freeze({ create });
})();
