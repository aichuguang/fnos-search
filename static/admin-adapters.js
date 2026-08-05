(function () {
    function create(context) {
        const {
            state, getElement, api, toast, escapeHtml, statusPill,
            collectSearchProviders, loadSettings, loadAdvancedConfig, loadSecurityStatus, saveAdvancedConfig,
        } = context;
        const adminState = state;
        const $ = getElement;

        async function loadAdapters() {
            const tasks = [
                ["基础设置", loadSettings],
                ["搜索源", loadSearchProviders],
                ["高级配置", loadAdvancedConfig],
                ["安全状态", loadSecurityStatus],
            ];
            const results = await Promise.allSettled(tasks.map(([, loader]) => loader()));
            const failures = results
                .map((result, index) => result.status === "rejected" ? `${tasks[index][0]}：${result.reason?.message || "加载失败"}` : "")
                .filter(Boolean);
            if (failures.length && $("advancedConfigStatus")) {
                $("advancedConfigStatus").classList.add("status-error");
                $("advancedConfigStatus").textContent = `部分配置加载失败：${failures.join("；")}`;
            }
        }

        async function loadSearchProviders() {
            const data = await api("/api/admin/search/providers");
            adminState.searchProviders = data.items || [];
            renderSearchProviders();
        }

        function renderSearchProviders() {
            const box = $("adminSearchProviders");
            if (!box) return;
            if (!adminState.searchProviders.length) {
                box.innerHTML = `<div class="empty">暂无搜索源</div>`;
                return;
            }
            box.innerHTML = adminState.searchProviders.map((item) => `
                <div class="list-item search-provider-row" data-search-provider="${escapeHtml(item.key)}">
                    <div>
                        <strong>${escapeHtml(item.name || item.key)}</strong>
                        <p>${statusPill(item.enabled ? "ready" : "unconfigured")} · ${item.configured ? "配置已填写" : "未配置"} · 优先级 ${escapeHtml(item.priority ?? "-")}</p>
                        <p>${escapeHtml(item.message || "")}</p>
                    </div>
                    <div class="table-actions">
                        <label class="mini-check"><input type="checkbox" data-provider-enabled ${item.enabled ? "checked" : ""}>启用</label>
                        <input class="mini-input" type="number" min="1" max="999" value="${escapeHtml(item.priority || 100)}" data-provider-priority>
                    </div>
                </div>
            `).join("");
        }

        async function saveSearchProviders(options = {}) {
            const { silent = false } = options;
            const providers = collectSearchProviders(adminState.searchProviders);
            const data = await api("/api/admin/search/providers", {
                method: "POST",
                body: JSON.stringify({ providers }),
            });
            adminState.searchProviders = data.items || [];
            renderSearchProviders();
            if (!silent) toast(data.message || "搜索源设置已保存", "success");
            return data;
        }

        function renderSixpanAuthStatus(sixpan = {}) {
            const box = $("sixpanAuthStatus");
            if (!box) return;
            const hasClient = Boolean(sixpan.client_id && sixpan.client_secret);
            const authorized = Boolean(sixpan.access_token || sixpan.refresh_token);
            const oauth = adminState.sixpanOAuth || {};
            if (authorized) {
                box.innerHTML = `<strong>已授权</strong><p>六盘 token 已保存，磁链 / 种子可提交离线任务。</p>`;
                return;
            }
            if (oauth.user_code || oauth.verification_uri) {
                box.innerHTML = `
                    <strong>等待六盘授权</strong>
                    <p>验证码：<code>${escapeHtml(oauth.user_code || "-")}</code></p>
                    <p>${oauth.verification_uri ? `<a href="${escapeHtml(oauth.verification_uri)}" target="_blank" rel="noreferrer">打开授权页面</a>` : "请在六盘授权页面输入验证码。"}</p>
                `;
                return;
            }
            if (hasClient) {
                box.innerHTML = `<strong>未授权</strong><p>ClientID / ClientSecret 已填写。</p>`;
                return;
            }
            box.innerHTML = `<strong>未配置</strong>`;
        }

        async function startSixpanDeviceAuth() {
            const data = await api("/api/admin/sixpan/oauth/device-code", {
                method: "POST",
                body: JSON.stringify({
                    device: "fnos-media-import/admin",
                    credentials: {
                        client_id: $("advSixpanClientId")?.value?.trim() || "",
                        client_secret: $("advSixpanClientSecret")?.value?.trim() || "",
                    },
                }),
                allowFailure: true,
            });
            if (!data.success) {
                toast(data.message || "六盘授权入口创建失败", "error");
                return;
            }
            if (data.config) {
                adminState.advancedConfig = data.config;
                adminState.advancedStoredConfig = data.stored || adminState.advancedStoredConfig;
                adminState.advancedConfigMeta = data.meta || adminState.advancedConfigMeta;
                await loadAdvancedConfig();
            }
            adminState.sixpanOAuth = data.auth || {};
            renderSixpanAuthStatus(adminState.advancedConfig.sixpan || {});
            const auth = data.auth || {};
            if (auth.verification_uri) {
                window.open(auth.verification_uri, "_blank", "noopener,noreferrer");
            }
            toast(data.message || "六盘授权入口已创建", "success");
        }

        async function checkSixpanDeviceAuth() {
            const data = await api("/api/admin/sixpan/oauth/device-code/check", {
                method: "POST",
                body: JSON.stringify({}),
                allowFailure: true,
            });
            toast(data.message || "六盘授权状态已检查", data.authorized ? "success" : data.success ? "" : "error");
            if (data.authorized) {
                adminState.sixpanOAuth = {};
                await Promise.all([loadAdvancedConfig(), loadAdapters()]);
            } else if (data.state) {
                const statusText = data.status ? `状态：${data.status}` : "仍在等待授权";
                if ($("sixpanAuthStatus")) $("sixpanAuthStatus").innerHTML = `<strong>${escapeHtml(statusText)}</strong><p>${escapeHtml(data.message || "")}</p>`;
            }
        }

        async function probeSixpan() {
            const data = await api("/api/admin/sixpan/probe", {
                method: "POST",
                body: JSON.stringify({}),
                allowFailure: true,
            });
            toast(data.message || "六盘账号检测完成", data.success ? "success" : "error");
        }

        return Object.freeze({
            loadAdapters,
            loadSearchProviders,
            renderSearchProviders,
            saveSearchProviders,
            renderSixpanAuthStatus,
            startSixpanDeviceAuth,
            checkSixpanDeviceAuth,
            probeSixpan,
        });
    }
    window.FnosAdminAdapters = Object.freeze({ create });
})();
