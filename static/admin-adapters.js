(function () {
    function create(context) {
        const {
            state, getElement, api, toast, escapeHtml, statusPill,
            collectSearchProviders, loadSettings, loadAdvancedConfig, loadSecurityStatus,
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
            probeSixpan,
        });
    }
    window.FnosAdminAdapters = Object.freeze({ create });
})();
