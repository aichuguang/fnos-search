(function () {
    function create(context) {
        const { state, getElement, api, toast, syncSearchSourceOptions, enabledSearchSourceKeys, setPublicSearchStatus, loadCaptcha, bindRecentRequests, bindRequestStatusPage, searchPublicResources, selectedSearchSource, updateSearchSourceActiveState, loadPublicTrending, bindPublicTrending, detectManualPublicLink, submitManualPublicLink, handleSixpanParseClick, handleSixpanParseChange, queryRequestStatus, closePublicSubmitConfirm, closeSixpanSlowWarning, continueSixpanSlowSubmit, updatePublicConfirmWarning, confirmPublicResourceSubmit, trapPublicModalFocus } = context;
        const publicState = state;
        const $ = getElement;

        async function loadPublicConfig() {
            const data = await api("/api/public/config");
            publicState.categories = data.categories || {};
            publicState.security = data.security || {};
            publicState.public = data.public || {};
            publicState.submission = data.submission || {};
            publicState.notifications = data.notifications || {};
            publicState.searchProviders = data.search?.providers || [];
            syncSearchSourceOptions();
            syncCategorySelect(publicState.selectedCategory);
            applyPublicFeatureSettings();
            const notificationStatus = new URLSearchParams(window.location.search).get("notification");
            if (notificationStatus === "verified") toast("邮箱验证成功，后续进度会发送到邮箱", "success");
            if (notificationStatus === "unsubscribed") toast("已停止接收该申请的邮件通知", "success");
            if (notificationStatus === "invalid") toast("通知链接无效或已过期", "error");
            await loadCaptcha();
        }

        function syncCategorySelect(category, options = {}) {
            if (options.user) publicState.categoryTouched = true;
            publicState.selectedCategory = category || "movie";
            if ($("manualPublicCategory")) $("manualPublicCategory").value = publicState.selectedCategory;
            if (options.syncConfirm !== false && $("publicConfirmCategory") && !$("publicSubmitConfirm")?.classList.contains("hidden")) {
                $("publicConfirmCategory").value = publicState.selectedCategory;
                window.syncCustomSelect?.($("publicConfirmCategory"));
            }
            document.querySelectorAll("[data-category-chip]").forEach((button) => {
                button.classList.toggle("active", button.dataset.categoryChip === publicState.selectedCategory);
                button.setAttribute("aria-pressed", String(button.dataset.categoryChip === publicState.selectedCategory));
            });
        }

        function normalizeTab(value) {
            const tab = String(value || "").replace(/^#/, "").replace(/Section$/, "");
            if (tab === "searchSection") return "search";
            if (tab === "linkSection") return "link";
            if (tab === "trendingSection") return "trending";
            if (tab === "querySection") return "query";
            return ["search", "link", "trending", "query"].includes(tab) ? tab : "search";
        }

        function activatePublicTab(tabName, updateHash = true) {
            const tab = normalizeTab(tabName);
            if (tab === "query" && publicState.public?.request_query_enabled === false) {
                toast("提交结果查询已关闭", "error");
                return;
            }
            publicState.activeTab = tab;
            document.body.dataset.activeTab = tab;
            document.querySelectorAll("[data-public-tab]").forEach((button) => {
                const active = button.dataset.publicTab === tab;
                button.classList.toggle("active", active);
                button.setAttribute("aria-selected", String(active));
                button.tabIndex = active ? 0 : -1;
            });
            document.querySelectorAll("[data-public-panel]").forEach((panel) => {
                const active = panel.dataset.publicPanel === tab;
                panel.classList.toggle("active", active);
                panel.hidden = !active;
            });
            if (updateHash) {
                history.replaceState(null, "", `#${tab}`);
            }
            if (tab === "trending") {
                loadPublicTrending().catch((error) => toast(error.message, "error"));
            }
            if (updateHash && window.matchMedia("(max-width: 900px)").matches) {
                window.requestAnimationFrame(() => window.scrollTo({ top: 0, behavior: "auto" }));
            }
        }

        function applyPublicFeatureSettings() {
            const settings = publicState.public || {};
            const noSearchProvider = !enabledSearchSourceKeys().length;
            const searchDisabled = settings.allow_anonymous_search === false || noSearchProvider;
            const queryDisabled = settings.request_query_enabled === false;
            const notificationAvailable = publicState.notifications?.guest_email_available === true;

            $("publicNotificationPanel")?.classList.toggle("hidden", !notificationAvailable);
            if (!notificationAvailable) {
                if ($("publicNotificationEnabled")) $("publicNotificationEnabled").checked = false;
                if ($("publicNotificationEmail")) $("publicNotificationEmail").disabled = true;
            }

            if ($("publicKeywordInput")) $("publicKeywordInput").disabled = searchDisabled;
            if ($("publicSearchBtn")) $("publicSearchBtn").disabled = searchDisabled;
            if (searchDisabled && $("publicSearchStatus")) {
                setPublicSearchStatus(noSearchProvider ? "暂无可用搜索源，请联系管理员开启搜索源。" : "匿名搜索已关闭，请切换到“链接提交”直接提交分享链接。", { tone: "error" });
            }

            document.querySelectorAll("[data-public-tab='query']").forEach((button) => {
                button.disabled = queryDisabled;
                button.classList.toggle("disabled", queryDisabled);
                button.title = queryDisabled ? "提交结果查询已关闭" : "";
            });
            if ($("requestTokenInput")) $("requestTokenInput").disabled = queryDisabled;
            if ($("requestQueryBtn")) $("requestQueryBtn").disabled = queryDisabled;
            if (queryDisabled && $("requestStatusBox")) {
                $("requestStatusBox").innerHTML = `<div class="notice-box">提交结果查询已关闭。如需查询，请联系管理员。</div>`;
            }
        }

        function bindSubmitPage() {
            bindPublicTrending();
            bindRecentRequests();
            window.FnosUI.bindRovingTabs({
                selector: "[data-public-tab]",
                dataKey: "publicTab",
                orientation: "horizontal",
                onActivate: (tab) => activatePublicTab(tab),
            });
            window.addEventListener("hashchange", () => activatePublicTab(location.hash, false));
            activatePublicTab(location.hash || "search", false);

            document.querySelectorAll("[data-category-chip]").forEach((button) => {
                button.addEventListener("click", () => syncCategorySelect(button.dataset.categoryChip, { user: true }));
            });
            $("manualPublicCategory")?.addEventListener("change", (event) => syncCategorySelect(event.target.value, { user: true }));
            $("publicSearchForm")?.addEventListener("submit", (event) => {
                event.preventDefault();
                searchPublicResources();
            });
            document.querySelectorAll('input[name="publicSearchSource"]').forEach((input) => {
                input.addEventListener("change", () => {
                    publicState.searchSource = selectedSearchSource();
                    updateSearchSourceActiveState();
                });
            });
            $("manualPublicDetectBtn")?.addEventListener("click", detectManualPublicLink);
            $("manualPublicForm")?.addEventListener("submit", (event) => {
                event.preventDefault();
                submitManualPublicLink();
            });
            document.addEventListener("click", handleSixpanParseClick);
            document.addEventListener("change", handleSixpanParseChange);
            $("refreshCaptchaBtn")?.addEventListener("click", () => loadCaptcha().catch((error) => toast(error.message, "error")));
            $("requestQueryForm")?.addEventListener("submit", (event) => {
                event.preventDefault();
                queryRequestStatus();
            });
            $("publicConfirmClose")?.addEventListener("click", closePublicSubmitConfirm);
            $("publicConfirmCancel")?.addEventListener("click", closePublicSubmitConfirm);
            $("sixpanSlowCancel")?.addEventListener("click", closeSixpanSlowWarning);
            $("sixpanSlowClose")?.addEventListener("click", closeSixpanSlowWarning);
            $("sixpanSlowContinue")?.addEventListener("click", continueSixpanSlowSubmit);
            $("publicConfirmCategory")?.addEventListener("change", (event) => {
                const value = event.target.value || "movie";
                publicState.pendingSubmit = { ...(publicState.pendingSubmit || {}), category: value };
                syncCategorySelect(value, { user: true, syncConfirm: false });
                updatePublicConfirmWarning();
            });
            $("publicNotificationEnabled")?.addEventListener("change", (event) => {
                if ($("publicNotificationEmail")) {
                    $("publicNotificationEmail").disabled = !event.target.checked;
                    if (event.target.checked) $("publicNotificationEmail").focus();
                }
            });
            $("publicConfirmSubmit")?.addEventListener("click", () => {
                const pending = publicState.pendingSubmit;
                if (!pending?.publicId) return;
                confirmPublicResourceSubmit(pending.publicId, $("publicConfirmCategory")?.value).catch((error) => toast(error.message, "error"));
            });
            $("publicSubmitConfirm")?.addEventListener("click", (event) => {
                if (event.target.id === "publicSubmitConfirm") closePublicSubmitConfirm();
            });
            $("sixpanSlowSubmitConfirm")?.addEventListener("click", (event) => {
                if (event.target.id === "sixpanSlowSubmitConfirm") closeSixpanSlowWarning();
            });
            document.addEventListener("keydown", (event) => {
                const activeModal = !$("sixpanSlowSubmitConfirm")?.classList.contains("hidden")
                    ? $("sixpanSlowSubmitConfirm")
                    : !$("publicSubmitConfirm")?.classList.contains("hidden")
                        ? $("publicSubmitConfirm")
                        : null;
                trapPublicModalFocus(event, activeModal);
                if (event.key === "Escape") {
                    if (!$("sixpanSlowSubmitConfirm")?.classList.contains("hidden")) {
                        closeSixpanSlowWarning();
                        return;
                    }
                    if (!$("publicSubmitConfirm")?.classList.contains("hidden")) {
                        closePublicSubmitConfirm();
                    }
                }
            });
        }

        function start() {
            document.addEventListener("DOMContentLoaded", async () => {
                try {
                    await loadPublicConfig();
                } catch (error) {
                    toast(error.message, "error");
                }
                const page = document.body.dataset.page;
                if (page === "submit") bindSubmitPage();
                if (page === "request-status") bindRequestStatusPage();
            });
        }

        return Object.freeze({
            loadPublicConfig,
            syncCategorySelect,
            normalizeTab,
            activatePublicTab,
            applyPublicFeatureSettings,
            bindSubmitPage,
            start
        });
    }
    window.FnosPublicBootstrap = Object.freeze({ create });
})();
