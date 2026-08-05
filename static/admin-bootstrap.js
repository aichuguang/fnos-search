(function () {
    function create(context) {
        const { state, getElement, api, toast, captureActionButton, loadOverview, loadRequests, loadJobs, loadUpdates, openUpdateEditor, runDueUpdates, loadTrending, runTrending, openTrendingScheduler, startRclone, stopRclone, checkRclone, loadRclone, loadTaskLogs, loadOrganizer, openOrganizerScanDrawer, openOrganizerRunsDrawer, loadAdapters, loadMediaLibraries, refreshAllMedia, saveSettings, saveAllSettingsTransaction, saveProfile, uploadSiteLogo, saveSearchProviders, loadAdvancedConfig, saveAdvancedConfig, loadRcloneWebdavConfig, saveRcloneWebdavConfig, testRcloneWebdavConfig, exportAdvancedConfig, importAdvancedConfig, applyCategoryTemplate, renderCategoryTemplatePreview, loadSecurityStatus, openAdvancedSecretManager, loadHistoryMaintenanceSummary, cleanupHistoryRecords, loadNotificationSettings, bindNotificationSettings, startSixpanDeviceAuth, checkSixpanDeviceAuth, probeSixpan, testBtbtlaProxy, testOrganizerEndpoint, syncAdvancedDependencies, closeRawLogModal, startRcloneLivePolling } = context;
        const adminState = state;
        const $ = getElement;

        function tabLoader(name) {
            return {
                overview: loadOverview,
                requests: loadRequests,
                jobs: loadJobs,
                updates: loadUpdates,
                trending: loadTrending,
                media: loadMediaLibraries,
                organizer: loadOrganizer,
                rclone: loadRclone,
                adapters: loadAdapters,
            }[name];
        }

        function clearTabLoadError(name) {
            $(`tab-${name}`)?.querySelector("[data-tab-load-error]")?.remove();
        }

        function renderTabLoadError(name, error) {
            const panel = $(`tab-${name}`);
            if (!panel) return;
            clearTabLoadError(name);
            const box = document.createElement("div");
            box.className = "notice-box status-error tab-load-error";
            box.dataset.tabLoadError = name;
            box.setAttribute("role", "alert");
            const message = document.createElement("span");
            message.textContent = `加载失败：${error?.message || "未知错误"}`;
            const retry = document.createElement("button");
            retry.type = "button";
            retry.className = "secondary mini";
            retry.textContent = "重试";
            retry.addEventListener("click", () => ensureTabLoaded(name, { force: true }).catch((retryError) => toast(retryError.message, "error")));
            box.append(message, retry);
            panel.prepend(box);
        }

        async function ensureTabLoaded(name, options = {}) {
            const { force = false } = options;
            const loader = tabLoader(name);
            if (!loader) return;
            if (!force && adminState.loadedTabs[name]) return;
            if (adminState.loadingTabs[name]) return adminState.loadingTabs[name];
            clearTabLoadError(name);
            $(`tab-${name}`)?.setAttribute("aria-busy", "true");
            const promise = (async () => {
                try {
                    await loader();
                    adminState.loadedTabs[name] = true;
                } catch (error) {
                    adminState.loadedTabs[name] = false;
                    renderTabLoadError(name, error);
                    throw error;
                } finally {
                    $(`tab-${name}`)?.setAttribute("aria-busy", "false");
                    delete adminState.loadingTabs[name];
                }
            })();
            adminState.loadingTabs[name] = promise;
            return promise;
        }

        function activateTab(name, options = {}) {
            const { load = true } = options;
            adminState.activeTab = name || "overview";
            document.querySelectorAll(".tab").forEach((button) => {
                const active = button.dataset.tab === name;
                button.classList.toggle("active", active);
                button.setAttribute("aria-selected", String(active));
                button.tabIndex = active ? 0 : -1;
            });
            document.querySelectorAll(".admin-tab-panel").forEach((panel) => {
                const active = panel.id === `tab-${name}`;
                panel.classList.toggle("active", active);
                panel.hidden = !active;
            });
            if (name === "adapters") {
                scheduleAdvancedConfigMasonrySettled();
            }
            // 管理后台各页展示的是任务和运行状态，切回页签时必须重新读取服务端数据。
            // loadedTabs 只用于首屏装载记录，不能让后续页签切换一直复用旧快照。
            if (load) ensureTabLoaded(name, { force: true }).catch((error) => toast(error.message, "error"));
        }

        function activateSettingsSection(section = "search") {
            adminState.activeSettingsSection = section || "search";
            document.querySelectorAll("[data-settings-section-target]").forEach((button) => {
                const active = button.dataset.settingsSectionTarget === adminState.activeSettingsSection;
                button.classList.toggle("active", active);
                button.setAttribute("aria-pressed", String(active));
            });
            document.querySelectorAll("[data-settings-section]").forEach((node) => {
                const sections = String(node.dataset.settingsSection || "").split(/\s+/).filter(Boolean);
                node.classList.toggle("settings-section-hidden", sections.length > 0 && !sections.includes(adminState.activeSettingsSection));
            });
            if (adminState.activeSettingsSection === "security") {
                loadSecurityStatus().catch((error) => toast(error.message, "error"));
            }
            if (adminState.activeSettingsSection === "maintenance") {
                loadHistoryMaintenanceSummary().catch((error) => toast(error.message, "error"));
            }
            if (adminState.activeSettingsSection === "notifications") {
                loadNotificationSettings().catch((error) => toast(error.message, "error"));
            }
            scheduleAdvancedConfigMasonrySettled();
        }

        let advancedConfigMasonryTimer = 0;

        function scheduleAdvancedConfigMasonry() {
            window.clearTimeout(advancedConfigMasonryTimer);
            advancedConfigMasonryTimer = window.setTimeout(layoutAdvancedConfigMasonry, 40);
        }

        function scheduleAdvancedConfigMasonrySettled() {
            scheduleAdvancedConfigMasonry();
            window.requestAnimationFrame?.(() => {
                layoutAdvancedConfigMasonry();
                window.setTimeout(layoutAdvancedConfigMasonry, 120);
                window.setTimeout(layoutAdvancedConfigMasonry, 360);
            });
        }

        function layoutAdvancedConfigMasonry() {
            const grid = document.querySelector("#tab-adapters .advanced-config-grid");
            const cards = Array.from(grid?.querySelectorAll(".advanced-config-card") || []);
            if (!grid) return;
            cards.forEach((card) => {
                card.style.gridRowEnd = "";
            });
        }

        function focusAdminModal(modal, preferred = null) {
            window.FnosUI.rememberAndFocus(modal, preferred);
        }

        function restoreAdminModalFocus(modal) {
            window.FnosUI.restoreFocus(modal);
        }

        function trapAdminModalFocus(event, modal) {
            window.FnosUI.trapFocus(event, modal);
        }

        function openDrawer(title, html, options = {}) {
            $("adminDrawerTitle").textContent = title;
            $("adminDrawerBody").innerHTML = html;
            const drawer = $("adminDrawer");
            drawer.className = "drawer-mask";
            if (options.mode === "modal") drawer.classList.add("drawer-modal");
            if (options.className) {
                String(options.className).split(/\s+/).filter(Boolean).forEach((name) => drawer.classList.add(name));
            }
            drawer.classList.add("open");
            focusAdminModal(drawer, $("adminDrawerClose"));
        }

        function closeDrawer() {
            const drawer = $("adminDrawer");
            if (drawer) {
                drawer.className = "drawer-mask";
                restoreAdminModalFocus(drawer);
            }
        }

        async function logout() {
            await api("/api/admin/logout", { method: "POST", body: JSON.stringify({}) });
            window.location.href = "/admin/login";
        }

        async function loadAll() {
            await Promise.all([
                ensureTabLoaded("overview", { force: true }),
                loadSecurityStatus(),
            ]);
        }

        async function saveAllSettings() {
            const button = $("saveSettingsBtn");
            if (button) button.disabled = true;
            try {
                const result = await saveAllSettingsTransaction();
                if (result?.success === false) return;
                toast(result?.message || "系统设置已保存", "success");
            } finally {
                if (button) button.disabled = false;
            }
        }

        function bindEvents() {
            bindNotificationSettings();
            window.FnosUI.bindRovingTabs({
                selector: ".tab",
                dataKey: "tab",
                orientation: "vertical",
                onActivate: (tab) => activateTab(tab),
            });
            document.querySelectorAll("[data-settings-section-target]").forEach((button) => {
                button.addEventListener("click", () => activateSettingsSection(button.dataset.settingsSectionTarget));
            });
            document.querySelectorAll("[data-config-jump]").forEach((button) => {
                button.addEventListener("click", () => activateSettingsSection(button.dataset.configJump));
            });
            document.querySelectorAll("#tab-adapters .advanced-config-card details").forEach((details) => {
                details.addEventListener("toggle", scheduleAdvancedConfigMasonrySettled);
            });
            window.addEventListener("resize", scheduleAdvancedConfigMasonry);
            $("adminDrawerClose")?.addEventListener("click", closeDrawer);
            $("adminDrawer")?.addEventListener("click", (event) => {
                if (event.target.id === "adminDrawer") closeDrawer();
            });
            $("adminRawLogClose")?.addEventListener("click", closeRawLogModal);
            $("adminRawLogModal")?.addEventListener("click", (event) => {
                if (event.target.id === "adminRawLogModal") closeRawLogModal();
            });
            document.addEventListener("keydown", (event) => {
                const rawLogModal = $("adminRawLogModal");
                const drawer = $("adminDrawer");
                const activeModal = rawLogModal?.classList.contains("open") ? rawLogModal : drawer?.classList.contains("open") ? drawer : null;
                trapAdminModalFocus(event, activeModal);
                if (event.key !== "Escape" || !activeModal) return;
                event.preventDefault();
                if (activeModal === rawLogModal) closeRawLogModal();
                else closeDrawer();
            });
            $("adminLogoutBtn")?.addEventListener("click", logout);
            $("adminRefreshBtn")?.addEventListener("click", () => loadAll().catch((error) => toast(error.message, "error")));
            $("adminOverviewShowJobs")?.addEventListener("click", () => activateTab("jobs"));
            $("loadRequestsBtn")?.addEventListener("click", () => loadRequests().catch((error) => toast(error.message, "error")));
            $("requestStatusFilter")?.addEventListener("change", () => {
                adminState.pagination.requests.page = 1;
                loadRequests().catch((error) => toast(error.message, "error"));
            });
            $("loadJobsBtn")?.addEventListener("click", () => {
                adminState.pagination.jobs.page = 1;
                loadJobs().catch((error) => toast(error.message, "error"));
            });
            $("loadUpdatesBtn")?.addEventListener("click", () => {
                adminState.pagination.updates.page = 1;
                loadUpdates().catch((error) => toast(error.message, "error"));
            });
            $("updateStatusFilter")?.addEventListener("change", () => {
                adminState.pagination.updates.page = 1;
                loadUpdates().catch((error) => toast(error.message, "error"));
            });
            $("newUpdateSubscriptionBtn")?.addEventListener("click", () => openUpdateEditor().catch((error) => toast(error.message, "error")));
            $("runDueUpdatesBtn")?.addEventListener("click", () => runDueUpdates().catch((error) => toast(error.message, "error")));
            $("loadTrendingBtn")?.addEventListener("click", () => {
                adminState.pagination.trending.page = 1;
                loadTrending().catch((error) => toast(error.message, "error"));
            });
            ["trendingSourceFilter", "trendingTypeFilter", "trendingStatusFilter"].forEach((id) => {
                $(id)?.addEventListener("change", () => {
                    adminState.pagination.trending.page = 1;
                    loadTrending().catch((error) => toast(error.message, "error"));
                });
            });
            $("runTrendingBtn")?.addEventListener("click", () => runTrending().catch((error) => toast(error.message, "error")));
            $("openTrendingSchedulerBtn")?.addEventListener("click", openTrendingScheduler);
            $("jobStatusFilter")?.addEventListener("change", () => {
                adminState.pagination.jobs.page = 1;
                loadJobs().catch((error) => toast(error.message, "error"));
            });
            $("jobCategoryFilter")?.addEventListener("change", () => {
                adminState.pagination.jobs.page = 1;
                loadJobs().catch((error) => toast(error.message, "error"));
            });
            $("jobKeywordFilter")?.addEventListener("keydown", (event) => {
                if (event.key === "Enter") {
                    adminState.pagination.jobs.page = 1;
                    loadJobs().catch((error) => toast(error.message, "error"));
                }
            });
            $("adminRcloneStartBtn")?.addEventListener("click", () => startRclone().catch((error) => toast(error.message, "error")));
            $("adminRcloneStopBtn")?.addEventListener("click", () => stopRclone().catch((error) => toast(error.message, "error")));
            $("adminRcloneCheckBtn")?.addEventListener("click", () => checkRclone().catch((error) => toast(error.message, "error")));
            $("loadRcloneBtn")?.addEventListener("click", () => loadRclone().catch((error) => toast(error.message, "error")));
            $("taskLogSearchBtn")?.addEventListener("click", () => {
                adminState.pagination.taskLogs.page = 1;
                loadTaskLogs().catch((error) => toast(error.message, "error"));
            });
            $("taskLogStatus")?.addEventListener("change", () => {
                adminState.pagination.taskLogs.page = 1;
                loadTaskLogs().catch((error) => toast(error.message, "error"));
            });
            $("taskLogKeyword")?.addEventListener("keydown", (event) => {
                if (event.key !== "Enter") return;
                adminState.pagination.taskLogs.page = 1;
                loadTaskLogs().catch((error) => toast(error.message, "error"));
            });
            $("loadOrganizerBtn")?.addEventListener("click", () => loadOrganizer().catch((error) => toast(error.message, "error")));
            $("organizerStatusFilter")?.addEventListener("change", () => {
                adminState.pagination.organizerTasks.page = 1;
                loadOrganizer().catch((error) => toast(error.message, "error"));
            });
            $("openOrganizerScanBtn")?.addEventListener("click", openOrganizerScanDrawer);
            $("openOrganizerRunsBtn")?.addEventListener("click", () => openOrganizerRunsDrawer().catch((error) => toast(error.message, "error")));
            $("loadAdaptersBtn")?.addEventListener("click", () => loadAdapters().catch((error) => toast(error.message, "error")));
            $("mediaLoadBtn")?.addEventListener("click", () => loadMediaLibraries().catch((error) => toast(error.message, "error")));
            $("mediaRefreshAllBtn")?.addEventListener("click", () => refreshAllMedia().catch((error) => toast(error.message, "error")));
            $("saveSettingsBtn")?.addEventListener("click", () => saveAllSettings().catch((error) => toast(error.message, "error")));
            $("saveProfileBtn")?.addEventListener("click", () => saveProfile().catch((error) => toast(error.message, "error")));
            $("adminDefaultPasswordAction")?.addEventListener("click", () => {
                activateTab("adapters");
                activateSettingsSection("profile");
                $("profileCurrentPassword")?.focus();
            });
            $("saveSiteLogoBtn")?.addEventListener("click", () => uploadSiteLogo().catch((error) => toast(error.message, "error")));
            $("loadAdvancedConfigBtn")?.addEventListener("click", () => loadAdvancedConfig().catch((error) => toast(error.message, "error")));
            $("saveRcloneWebdavBtn")?.addEventListener("click", () => saveRcloneWebdavConfig().catch((error) => toast(error.message, "error")));
            $("testRcloneWebdavBtn")?.addEventListener("click", () => testRcloneWebdavConfig().catch((error) => toast(error.message, "error")));
            $("advRcloneRemoteName")?.addEventListener("change", () => loadRcloneWebdavConfig().catch((error) => {
                const box = $("rcloneWebdavStatus");
                if (box) {
                    box.className = "notice-box compact status-error";
                    box.textContent = error.message || "WebDAV 配置读取失败";
                }
            }));
            $("exportAdvancedConfigBtn")?.addEventListener("click", () => exportAdvancedConfig().catch((error) => toast(error.message, "error")));
            $("importAdvancedConfigBtn")?.addEventListener("click", () => $("importAdvancedConfigFile")?.click());
            $("importAdvancedConfigFile")?.addEventListener("change", (event) => {
                const file = event.target.files && event.target.files[0];
                if (file) importAdvancedConfig(file).catch((error) => toast(error.message, "error"));
                event.target.value = "";
            });
            $("applyCategoryTemplateBtn")?.addEventListener("click", applyCategoryTemplate);
            [
                "categoryTemplateQuarkRoot",
                "categoryTemplateCloud139Root",
                "categoryTemplateCloud139FnosRoot",
                "categoryTemplateSixpanRoot",
                "categoryTemplateSixpanFnosRoot",
            ].forEach((id) => {
                $(id)?.addEventListener("input", renderCategoryTemplatePreview);
                $(id)?.addEventListener("change", renderCategoryTemplatePreview);
            });
            $("manageAdvancedSecretsBtn")?.addEventListener("click", openAdvancedSecretManager);
            $("securityStatusRefreshBtn")?.addEventListener("click", () => loadSecurityStatus().catch((error) => toast(error.message, "error")));
            $("historySummaryRefreshBtn")?.addEventListener("click", () => loadHistoryMaintenanceSummary().catch((error) => toast(error.message, "error")));
            $("cleanupHistoryBtn")?.addEventListener("click", () => cleanupHistoryRecords().catch((error) => toast(error.message, "error")));
            $("sixpanStartAuthBtn")?.addEventListener("click", () => startSixpanDeviceAuth().catch((error) => toast(error.message, "error")));
            $("sixpanCheckAuthBtn")?.addEventListener("click", () => checkSixpanDeviceAuth().catch((error) => toast(error.message, "error")));
            $("sixpanProbeBtn")?.addEventListener("click", () => probeSixpan().catch((error) => toast(error.message, "error")));
            $("btbtlaProxyTestBtn")?.addEventListener("click", () => testBtbtlaProxy().catch((error) => toast(error.message, "error")));
            $("openlistTestBtn")?.addEventListener("click", () => testOrganizerEndpoint("openlist").catch((error) => toast(error.message, "error")));
            $("tmdbTestBtn")?.addEventListener("click", () => testOrganizerEndpoint("tmdb").catch((error) => toast(error.message, "error")));
            $("aiTestBtn")?.addEventListener("click", () => testOrganizerEndpoint("ai").catch((error) => toast(error.message, "error")));
            ["advAiEnabled", "advOrganizerEnabled", "advOrganizerRefreshFnosAfterApply", "advSixpanParseBeforeAdd", "advRcloneEnabled"].forEach((id) => {
                $(id)?.addEventListener("change", syncAdvancedDependencies);
            });
            $("advBtbtlaProxyEnabled")?.addEventListener("change", () => {
                if ($("advBtbtlaProxyUrl")) $("advBtbtlaProxyUrl").disabled = !$("advBtbtlaProxyEnabled").checked;
            });
            $("advTmdbProxyEnabled")?.addEventListener("change", () => {
                if ($("advTmdbProxyUrl")) $("advTmdbProxyUrl").disabled = !$("advTmdbProxyEnabled").checked;
            });
        }

        function start() {
            document.addEventListener("click", captureActionButton, true);

            document.addEventListener("DOMContentLoaded", () => {
                if ($("requestStatusFilter")) $("requestStatusFilter").value = "";
                bindEvents();
                activateTab(adminState.activeTab, { load: false });
                activateSettingsSection(adminState.activeSettingsSection);
                startRcloneLivePolling();
                loadAll().catch((error) => toast(error.message, "error"));
            });
        }

        return Object.freeze({
            tabLoader,
            clearTabLoadError,
            renderTabLoadError,
            ensureTabLoaded,
            activateTab,
            activateSettingsSection,
            scheduleAdvancedConfigMasonry,
            scheduleAdvancedConfigMasonrySettled,
            layoutAdvancedConfigMasonry,
            focusAdminModal,
            restoreAdminModalFocus,
            trapAdminModalFocus,
            openDrawer,
            closeDrawer,
            logout,
            loadAll,
            bindEvents,
            start
        });
    }
    window.FnosAdminBootstrap = Object.freeze({ create });
})();
