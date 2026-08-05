(function () {
    function create(context) {
        const {
            state, getElement, api, toast, escapeHtml, statusPill, formatDate, openDrawer,
            focusAdminModal, restoreAdminModalFocus, confirmDialog,
            loadOverview, loadRequests, loadJobs, loadRclone, loadOrganizer, loadUpdates,
        } = context;
        const adminState = state;
        const $ = getElement;

        function openRawLogModal(title, lines) {
            const modal = $("adminRawLogModal");
            const titleBox = $("adminRawLogTitle");
            const body = $("adminRawLogBody");
            if (!modal || !body) {
                openDrawer(title, `<pre class="raw-log-pre">${escapeHtml((lines || []).join("\n") || "暂无日志")}</pre>`);
                return;
            }
            if (titleBox) titleBox.textContent = title;
            body.textContent = (lines || []).join("\n") || "暂无日志";
            modal.classList.add("open");
            body.scrollTop = body.scrollHeight;
            focusAdminModal(modal, $("adminRawLogClose"));
        }

        function closeRawLogModal() {
            const modal = $("adminRawLogModal");
            modal?.classList.remove("open");
            restoreAdminModalFocus(modal);
        }

        async function loadSecurityStatus() {
            const data = await api("/api/admin/security/status");
            adminState.securityStatus = data || {};
            renderSecurityStatus();
        }

        function renderSecurityStatus() {
            const box = $("adminSecurityStatus");
            const status = adminState.securityStatus || {};
            const issues = status.issues || [];
            const defaultPasswordBanner = $("adminDefaultPasswordBanner");
            if (defaultPasswordBanner) {
                defaultPasswordBanner.hidden = status.flags?.default_admin !== true;
            }
            if (!box) return;
            if (!issues.length) {
                box.innerHTML = `
                    <div class="security-status-item level-ok">
                        <strong>未发现明显风险</strong>
                        <p>当前基础安全项检查通过。</p>
                    </div>
                `;
                return;
            }
            box.innerHTML = issues.map((item) => `
                <div class="security-status-item level-${escapeHtml(item.level || "info")}">
                    <strong>${escapeHtml(item.title || "安全提示")}</strong>
                    <p>${escapeHtml(item.message || "")}</p>
                    ${item.action ? `<small>${escapeHtml(item.action)}</small>` : ""}
                </div>
            `).join("");
        }

        async function loadHistoryMaintenanceSummary() {
            const data = await api("/api/admin/maintenance/history-summary");
            adminState.historyMaintenance = data.summary || {};
            renderHistoryMaintenance();
        }

        function renderHistoryMaintenance() {
            const summary = adminState.historyMaintenance || {};
            const cleanupTables = summary.cleanup_tables || {};
            const preservedTables = summary.preserved_tables || {};
            const subscriptions = Array.isArray(summary.preserved_subscriptions) ? summary.preserved_subscriptions : [];
            const sources = Array.isArray(summary.preserved_sources) ? summary.preserved_sources : [];
            const total = Number(summary.cleanup_total || 0);
            if ($("adminHistorySummary")) {
                $("adminHistorySummary").innerHTML = `
                    <strong>可清理历史记录：${escapeHtml(total)} 条</strong>
                    <p>会保留定时追更订阅、追更来源和系统设置。</p>
                `;
            }
            if ($("adminHistoryCleanupTables")) {
                const groups = {
                    "提交与入库记录": ["guest_request_events", "guest_requests", "import_jobs", "job_events"],
                    "搬运与整理记录": ["rclone_runs", "rclone_events", "rclone_file_events", "organizer_tasks", "organizer_runs", "organizer_files", "organizer_mappings", "organizer_operations", "organizer_ai_suggestions", "organizer_tmdb_matches"],
                    "追更历史记录": ["update_runs", "update_candidates", "update_events", "update_seen_items", "update_path_snapshots", "update_preview_cache"],
                    "搜索缓存": ["resources", "search_cache"],
                };
                const entries = Object.entries(groups)
                    .map(([label, keys]) => [label, keys.reduce((sum, key) => sum + Number(cleanupTables[key] || 0), 0)])
                    .filter(([, count]) => Number(count || 0) > 0);
                $("adminHistoryCleanupTables").innerHTML = entries.length
                    ? entries.map(([label, count]) => `
                        <div class="list-item">
                            <div>
                                <strong>${escapeHtml(label)}</strong>
                                <p>${escapeHtml(count)} 条</p>
                            </div>
                        </div>
                    `).join("")
                    : `<div class="empty">历史记录已经是空的。</div>`;
            }
            if ($("adminHistoryPreserved")) {
                const sourcesBySub = sources.reduce((acc, item) => {
                    const key = String(item.subscription_id || "");
                    acc[key] = acc[key] || [];
                    acc[key].push(item);
                    return acc;
                }, {});
                $("adminHistoryPreserved").innerHTML = subscriptions.length
                    ? subscriptions.map((item) => {
                        const linkedSources = sourcesBySub[String(item.id || "")] || [];
                        return `
                            <div class="list-item">
                                <div>
                                    <strong>#${escapeHtml(item.id)} ${escapeHtml(item.title || "-")}</strong>
                                    <p>${statusPill(item.status || "unknown")} · ${escapeHtml(item.category_label || item.category || "-")} · 下一集 ${escapeHtml(item.next_episode || "自动判断")} · 已完成 ${escapeHtml(item.last_success_episode || "-")}</p>
                                    <p class="muted">下次运行：${escapeHtml(formatDate(item.next_run_at))}；来源：${linkedSources.map((source) => escapeHtml(source.name || source.type || "-")).join("、") || "未配置"}</p>
                                </div>
                            </div>
                        `;
                    }).join("")
                    : `<div class="empty">没有保留的追更订阅。</div>`;
            }
        }

        async function cleanupHistoryRecords() {
            const summary = adminState.historyMaintenance?.cleanup_tables ? adminState.historyMaintenance : (await api("/api/admin/maintenance/history-summary")).summary || {};
            adminState.historyMaintenance = summary;
            renderHistoryMaintenance();
            const total = Number(summary.cleanup_total || 0);
            const ok = await confirmDialog({
                title: "清理历史记录",
                message: `将删除 ${total} 条历史记录。\n会自动备份数据库，并保留定时追更订阅和来源。是否继续？`,
                confirmText: "备份并清理",
                tone: "danger",
            });
            if (!ok) return;
            const data = await api("/api/admin/maintenance/cleanup-history", {
                method: "POST",
                body: JSON.stringify({ confirm: true, backup: true, vacuum: true }),
                loadingMessage: "正在备份并清理历史记录...",
                buttonLabel: "清理中...",
                allowFailure: true,
            });
            if (!data.success) {
                toast(data.message || "历史记录清理失败", "error");
                return;
            }
            adminState.historyMaintenance = data.summary || data.result?.after || {};
            renderHistoryMaintenance();
            toast(data.message || "历史记录已清理", "success");
            await Promise.all([loadOverview(), loadRequests(), loadJobs(), loadRclone(), loadOrganizer(), loadUpdates()]);
        }

        function renderProfileSettings() {
            const profile = adminState.profile || {};
            if ($("profileUsername")) $("profileUsername").value = profile.username || "";
            if ($("profileCurrentPassword")) $("profileCurrentPassword").value = "";
            if ($("profileNewPassword")) $("profileNewPassword").value = "";
            renderImagePreview("profileAvatarPreview", profile.avatar_url, "user");
            const logoPreview = $("siteLogoPreview");
            if (logoPreview) {
                logoPreview.style.setProperty("--site-logo-bg", profile.logo_url ? `url("${profile.logo_url}")` : "");
            }
            if ($("adminTopUsername")) $("adminTopUsername").textContent = profile.username || "管理员";
            renderImagePreview("adminTopAvatar", profile.avatar_url, "user");
        }

        function renderImagePreview(id, url, iconName = "user") {
            const box = $(id);
            if (!box) return;
            box.innerHTML = url ? `<img src="${escapeHtml(url)}" alt="">` : `<span class="icon-slot icon-${escapeHtml(iconName)}" aria-hidden="true"></span>`;
        }

        async function saveProfile() {
            const payload = {
                username: $("profileUsername")?.value?.trim() || "",
                current_password: $("profileCurrentPassword")?.value || "",
                new_password: $("profileNewPassword")?.value || "",
            };
            const data = await api("/api/admin/profile", {
                method: "POST",
                body: JSON.stringify(payload),
            });
            adminState.profile = data.profile || {};
            await loadSecurityStatus();
            if ($("profileAvatarFile")?.files?.[0]) {
                await uploadProfileAvatar();
                if ($("profileAvatarFile")) $("profileAvatarFile").value = "";
                return;
            }
            renderProfileSettings();
            toast(data.message || "个人设置已保存", "success");
        }

        async function uploadProfileAvatar() {
            const file = $("profileAvatarFile")?.files?.[0];
            if (!file) {
                toast("请选择头像图片", "error");
                return;
            }
            const form = new FormData();
            form.append("avatar", file);
            const data = await uploadForm("/api/admin/profile/avatar", form);
            adminState.profile = data.profile || {};
            renderProfileSettings();
            toast(data.message || "头像已更新", "success");
        }

        async function uploadSiteLogo() {
            const file = $("siteLogoFile")?.files?.[0];
            if (!file) {
                toast("请选择 Logo 图片", "error");
                return;
            }
            const form = new FormData();
            form.append("logo", file);
            const data = await uploadForm("/api/admin/site-logo", form);
            adminState.profile = data.profile || {};
            renderProfileSettings();
            if (adminState.profile.logo_url) {
                document.documentElement.style.setProperty("--site-logo-bg", `url("${adminState.profile.logo_url}")`);
            }
            toast(data.message || "网站 Logo 已更新", "success");
        }

        async function uploadForm(path, form) {
            return api(path, {
                method: "POST",
                body: form,
                loadingMessage: "正在上传文件...",
                buttonLabel: "上传中...",
            });
        }

        return Object.freeze({
            openRawLogModal,
            closeRawLogModal,
            loadSecurityStatus,
            renderSecurityStatus,
            loadHistoryMaintenanceSummary,
            renderHistoryMaintenance,
            cleanupHistoryRecords,
            renderProfileSettings,
            renderImagePreview,
            saveProfile,
            uploadProfileAvatar,
            uploadSiteLogo,
            uploadForm,
        });
    }
    window.FnosAdminSystem = Object.freeze({ create });
})();
