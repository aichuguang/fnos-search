(function () {
    function create(context) {
        const { state, getElement, api, toast, escapeHtml, activatePublicTab, searchPublicResources } = context;
        const $ = getElement;
        const categoryOrder = ["tv", "movie", "variety", "anime"];
        const categoryMeta = {
            tv: { label: "电视剧", note: "热播剧集", icon: "tv" },
            movie: { label: "电影", note: "热门影片", icon: "film" },
            variety: { label: "综艺", note: "人气综艺", icon: "star" },
            anime: { label: "动漫", note: "热门动漫", icon: "anime" },
        };
        const sourceLabels = {
            tencent: "腾讯视频",
            iqiyi: "爱奇艺",
            youku: "优酷",
        };
        let activeCategory = "tv";
        let loaded = false;
        let loading = false;
        let groups = {};
        let bound = false;

        function safeImageUrl(value) {
            const url = String(value || "").trim();
            return /^(?:https?:\/\/|\/)/i.test(url) ? url : "";
        }

        function sourceRows(item) {
            if (Array.isArray(item.sources) && item.sources.length) return item.sources;
            if (item.platform_ranks && typeof item.platform_ranks === "object") {
                return Object.entries(item.platform_ranks).map(([source, rank]) => ({ source, rank }));
            }
            return [];
        }

        function sourceSummary(item) {
            return Array.from(new Set(sourceRows(item).map((row) => sourceLabels[row.source] || row.source).filter(Boolean))).join(" / ") || "-";
        }

        function platformRanks(item) {
            return sourceRows(item)
                .map((row) => `${sourceLabels[row.source] || row.source || "-"} #${row.rank || "-"}`)
                .join(" · ") || "-";
        }

        function availability(item) {
            return item.media_exists
                ? '<span class="public-trending-available"><span class="public-trending-state-icon" aria-hidden="true">▶</span>已可观看</span>'
                : '<span class="public-trending-search"><span class="icon-slot icon-search" aria-hidden="true"></span>搜索资源</span>';
        }

        function poster(item, prominent = false) {
            const url = safeImageUrl(item.image_url);
            const image = url
                ? `<img src="${escapeHtml(url)}" alt="" loading="lazy" referrerpolicy="no-referrer">`
                : `<span class="icon-slot icon-${escapeHtml(categoryMeta[item.media_type]?.icon || "film")}" aria-hidden="true"></span>`;
            return `<span class="public-trending-poster${prominent ? " prominent" : ""}">${image}</span>`;
        }

        function topCard(item, index) {
            const rank = Number(item.rank || index + 1);
            return `<button class="public-trending-top-card rank-${rank}" type="button" data-trending-title="${escapeHtml(item.title || "")}" data-trending-available="${item.media_exists ? "true" : "false"}">
                ${poster(item, true)}
                <span class="public-trending-card-shade" aria-hidden="true"></span>
                <span class="public-trending-rank-number">${escapeHtml(rank)}</span>
                <span class="public-trending-card-copy">
                    <strong>${escapeHtml(item.title || "-")}</strong>
                    <span>${escapeHtml([item.year, sourceSummary(item)].filter(Boolean).join(" · "))}</span>
                    <small>${escapeHtml(platformRanks(item))}</small>
                    ${availability(item)}
                </span>
            </button>`;
        }

        function listRow(item, index) {
            const rank = Number(item.rank || index + 4);
            return `<button class="public-trending-list-row" type="button" data-trending-title="${escapeHtml(item.title || "")}" data-trending-available="${item.media_exists ? "true" : "false"}">
                <span class="public-trending-list-rank">${escapeHtml(rank)}</span>
                ${poster(item)}
                <span class="public-trending-list-copy">
                    <strong>${escapeHtml(item.title || "-")}</strong>
                    <span>${escapeHtml([item.year, sourceSummary(item)].filter(Boolean).join(" · "))}</span>
                </span>
                <small class="public-trending-platform-ranks">${escapeHtml(platformRanks(item))}</small>
                ${availability(item)}
            </button>`;
        }

        function render() {
            const content = $("publicTrendingContent");
            if (!content) return;
            document.querySelectorAll("[data-trending-category]").forEach((button) => {
                const active = button.dataset.trendingCategory === activeCategory;
                button.classList.toggle("active", active);
                button.setAttribute("aria-selected", String(active));
                button.tabIndex = active ? 0 : -1;
            });
            const items = Array.isArray(groups[activeCategory]?.items) ? groups[activeCategory].items.slice(0, 25) : [];
            const meta = categoryMeta[activeCategory];
            if (!items.length) {
                content.innerHTML = `<div class="public-trending-empty"><span class="icon-slot icon-${escapeHtml(meta.icon)}" aria-hidden="true"></span><strong>暂无${escapeHtml(meta.label)}榜单</strong><p>稍后刷新看看吧。</p></div>`;
                return;
            }
            content.innerHTML = `
                <div class="public-trending-section-head">
                    <div><span>${escapeHtml(meta.note)}</span><h2>${escapeHtml(meta.label)} TOP ${escapeHtml(items.length)}</h2></div>
                </div>
                <div class="public-trending-podium">${items.slice(0, 3).map(topCard).join("")}</div>
                <div class="public-trending-list">${items.slice(3).map(listRow).join("")}</div>
            `;
        }

        function setStatus(message, tone = "") {
            const status = $("publicTrendingStatus");
            if (!status) return;
            status.className = `notice-box public-trending-status${tone ? ` status-${tone}` : ""}${message ? "" : " hidden"}`;
            status.textContent = message || "";
        }

        function formatUpdatedAt(value) {
            if (!value) return "每日更新";
            const date = new Date(String(value).replace(" ", "T") + (String(value).includes("Z") ? "" : "Z"));
            if (Number.isNaN(date.getTime())) return "每日更新";
            return `更新于 ${date.toLocaleString("zh-CN", { hour12: false, month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" })}`;
        }

        async function load() {
            if (loading || loaded) return;
            loading = true;
            if ($("publicTrendingHero")) $("publicTrendingHero").dataset.day = String(new Date().getDate()).padStart(2, "0");
            setStatus("正在加载今日热播榜单...");
            try {
                const data = await api("/api/public/trending", { allowFailure: true });
                if (data.success === false) throw new Error(data.message || "热播榜单加载失败");
                groups = data.groups || {};
                loaded = true;
                if ($("publicTrendingUpdatedAt")) $("publicTrendingUpdatedAt").textContent = formatUpdatedAt(data.updated_at);
                setStatus("");
                render();
            } catch (error) {
                setStatus(error.message || "热播榜单加载失败", "error");
            } finally {
                loading = false;
            }
        }

        function openSearch(title) {
            const input = $("publicKeywordInput");
            if (!input || input.disabled) {
                toast("当前未开启访客搜索，请联系管理员。", "error");
                return;
            }
            input.value = title;
            input.dispatchEvent(new Event("input", { bubbles: true }));
            activatePublicTab("search");
            toast(`正在为你搜索《${title}》`);
            searchPublicResources();
        }

        function bind() {
            if (bound) return;
            bound = true;
            window.FnosUI.bindRovingTabs({
                selector: "[data-trending-category]",
                dataKey: "trendingCategory",
                orientation: "horizontal",
                onActivate: (category) => {
                    activeCategory = categoryOrder.includes(category) ? category : "tv";
                    render();
                },
            });
            $("publicTrendingContent")?.addEventListener("click", (event) => {
                const button = event.target.closest("[data-trending-title]");
                if (!button) return;
                const title = String(button.dataset.trendingTitle || "").trim();
                if (!title) return;
                if (button.dataset.trendingAvailable === "true") {
                    toast(`《${title}》已经可以观看了`, "success");
                    return;
                }
                openSearch(title);
            });
        }

        return Object.freeze({ load, bind, render });
    }

    window.FnosPublicTrending = Object.freeze({ create });
})();
