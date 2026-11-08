(function () {
    function create(context) {
        const { getElement, api, toast, escapeHtml } = context;
        const $ = getElement;
        let bound = false;

        function statusBox(message, tone) {
            const box = $("agentStatusBox");
            if (!box) return;
            box.className = `notice-box${tone ? ` status-${tone}` : ""}`;
            box.textContent = message;
        }

        function agentDisabledHint(message) {
            const text = String(message || "");
            if (text.includes("未启用")) {
                return `${text}：在 .env 设置 FNOS_AGENT_ENABLED=true 后重启 Web 容器，再刷新本页。`;
            }
            if (text.includes("not found") || text.includes("404")) {
                return `${text}：当前镜像可能还不是包含 Agent 接口的版本，请用源码重新构建后再试。`;
            }
            return text;
        }

        function profileLabel(name) {
            return { read: "只读", operator: "可写（不放行整理）", full: "完全（含整理执行）" }[name] || name;
        }

        // -- expiry picker (day granularity, project select component) --------
        function syncExpiryDays() {
            const year = $("agentCreateExpiryYear");
            const month = $("agentCreateExpiryMonth");
            const day = $("agentCreateExpiryDay");
            if (!year || !month || !day) return;
            const y = Number(year.value) || new Date().getFullYear();
            const m = Number(month.value) || (new Date().getMonth() + 1);
            const maxDay = new Date(y, m, 0).getDate();
            const previous = Number(day.value) || maxDay;
            day.innerHTML = "";
            for (let d = 1; d <= maxDay; d += 1) {
                const option = document.createElement("option");
                option.value = String(d);
                option.textContent = String(d);
                day.appendChild(option);
            }
            day.value = String(Math.min(previous, maxDay));
            day.dispatchEvent(new Event("change", { bubbles: true }));
        }

        function initExpiryPicker() {
            const year = $("agentCreateExpiryYear");
            const month = $("agentCreateExpiryMonth");
            const day = $("agentCreateExpiryDay");
            if (!year || !month || !day || year.dataset.ready === "1") return;
            const now = new Date();
            for (let y = now.getFullYear(); y <= now.getFullYear() + 5; y += 1) {
                const option = document.createElement("option");
                option.value = String(y);
                option.textContent = `${y} 年`;
                year.appendChild(option);
            }
            for (let m = 1; m <= 12; m += 1) {
                const option = document.createElement("option");
                option.value = String(m);
                option.textContent = `${m} 月`;
                month.appendChild(option);
            }
            const target = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 30);
            year.value = String(target.getFullYear());
            month.value = String(target.getMonth() + 1);
            year.dataset.ready = "1";
            syncExpiryDays();
            day.value = String(target.getDate());
            [year, month, day].forEach((el) => el.dispatchEvent(new Event("change", { bubbles: true })));
        }

        function toggleExpiryPicker() {
            const mode = String($("agentCreateExpiryMode")?.value || "never");
            const wrapper = $("agentCreateExpiryDate");
            if (!wrapper) return;
            const show = mode === "date";
            wrapper.style.display = show ? "inline-flex" : "none";
            if (show) initExpiryPicker();
        }

        function expiryValue() {
            const mode = String($("agentCreateExpiryMode")?.value || "never");
            if (mode !== "date") return "";
            const y = Number($("agentCreateExpiryYear")?.value);
            const m = Number($("agentCreateExpiryMonth")?.value);
            const d = Number($("agentCreateExpiryDay")?.value);
            if (!y || !m || !d) return "";
            // End of the selected local day, stored as UTC ISO.
            return new Date(y, m - 1, d, 23, 59, 59, 999).toISOString();
        }

        // -- rendering -------------------------------------------------------
        function renderProfiles(data) {
            const select = $("agentCreateProfile");
            if (!select) return;
            const profiles = data?.profiles || {};
            const scopes = data?.scopes || [];
            const current = select.value || "read";
            select.innerHTML = Object.keys(profiles)
                .map((name) => {
                    const count = (profiles[name] || []).length;
                    return `<option value="${escapeHtml(name)}">${escapeHtml(profileLabel(name))} · ${count} 项权限</option>`;
                })
                .join("");
            select.value = profiles[current] ? current : Object.keys(profiles)[0] || "read";
            const hint = $("agentProfileHint");
            if (hint) {
                const chosen = profiles[select.value] || scopes;
                hint.textContent = `包含：${chosen.join(" / ") || "-"}`;
            }
        }

        function renderTokens(items) {
            const body = $("agentTokenBody");
            if (!body) return;
            if (!items.length) {
                body.innerHTML = '<tr><td colspan="6" class="empty-cell">还没有 Agent 令牌。</td></tr>';
                return;
            }
            body.innerHTML = items
                .map((item) => {
                    const revoked = Boolean(item.revoked_at);
                    const state = revoked ? '<span class="pill error">已撤销</span>' : '<span class="pill ok">有效</span>';
                    const action = revoked
                        ? '<span class="muted">-</span>'
                        : `<button class="secondary mini" type="button" data-agent-revoke="${item.id}">撤销</button>`;
                    return `
                        <tr>
                            <td>${escapeHtml(item.name)}</td>
                            <td><code>${escapeHtml(item.token_prefix)}…</code></td>
                            <td>${escapeHtml((item.scopes || []).join(", "))}</td>
                            <td>${state}</td>
                            <td>${escapeHtml(item.last_used_at || "-")}</td>
                            <td>${action}</td>
                        </tr>`;
                })
                .join("");
            body.querySelectorAll("[data-agent-revoke]").forEach((button) => {
                button.addEventListener("click", () => revokeAgentToken(button.dataset.agentRevoke));
            });
        }

        function renderAudit(items) {
            const body = $("agentAuditBody");
            if (!body) return;
            if (!items.length) {
                body.innerHTML = '<tr><td colspan="5" class="empty-cell">暂无调用记录。</td></tr>';
                return;
            }
            body.innerHTML = items
                .map(
                    (item) => `
                    <tr>
                        <td>${escapeHtml(item.created_at || "-")}</td>
                        <td>${escapeHtml(item.token_name || "-")}</td>
                        <td><code>${escapeHtml(item.tool)}</code></td>
                        <td>${escapeHtml(item.outcome)}${item.error_code ? ` · ${escapeHtml(item.error_code)}` : ""}</td>
                        <td>${escapeHtml(String(item.duration_ms ?? 0))} ms</td>
                    </tr>`,
                )
                .join("");
        }

        // -- actions ---------------------------------------------------------
        async function loadAgentTokens() {
            statusBox("正在加载 Agent 令牌...");
            try {
                const data = await api("/api/admin/agent/tokens", { allowFailure: true });
                if (data.success === false) {
                    statusBox(agentDisabledHint(data.message), "error");
                    renderTokens([]);
                    return data;
                }
                renderProfiles(data);
                renderTokens(data.items || []);
                statusBox(`共 ${(data.items || []).length} 个令牌。令牌明文只在创建时显示一次。`, "ok");
                return data;
            } catch (error) {
                statusBox(agentDisabledHint(error.message), "error");
                throw error;
            }
        }

        async function createAgentToken() {
            const name = String($("agentCreateName")?.value || "").trim();
            if (!name) {
                toast("请填写令牌名称", "error");
                return;
            }
            const profile = String($("agentCreateProfile")?.value || "read");
            const body = { name, profile };
            const expires = expiryValue();
            if (expires) body.expires_at = expires;
            const data = await api("/api/admin/agent/tokens", {
                method: "POST",
                body: JSON.stringify(body),
            });
            const reveal = $("agentTokenReveal");
            if (reveal) {
                reveal.hidden = false;
                reveal.innerHTML = `
                    <strong>令牌已创建，请立刻复制保存（只显示这一次）</strong>
                    <p><code id="agentTokenPlain">${escapeHtml(data.token)}</code></p>`;
            }
            if ($("agentCreateName")) $("agentCreateName").value = "";
            await loadAgentTokens();
            toast("令牌已创建", "ok");
        }

        async function revokeAgentToken(tokenId) {
            if (!window.showConfirmDialog) {
                toast("确认组件未加载，已取消", "error");
                return;
            }
            const ok = await window.showConfirmDialog({
                title: "撤销 Agent 令牌",
                message: `撤销后使用该令牌的 Agent 会立即失去访问权限（#${tokenId}）。`,
                confirmText: "撤销",
                tone: "danger",
            });
            if (!ok) return;
            await api(`/api/admin/agent/tokens/${encodeURIComponent(tokenId)}`, { method: "DELETE" });
            await loadAgentTokens();
            toast("令牌已撤销", "ok");
        }

        async function loadAgentAudit() {
            const data = await api("/api/admin/agent/audit?limit=30", { allowFailure: true });
            renderAudit(data.success === false ? [] : data.items || []);
            if (data.success === false) statusBox(data.message || "读取审计失败", "error");
            return data;
        }

        function bindEvents() {
            if (bound) return;
            bound = true;
            $("agentCreateBtn")?.addEventListener("click", () => createAgentToken().catch((error) => toast(error.message, "error")));
            $("agentAuditLoadBtn")?.addEventListener("click", () => loadAgentAudit().catch((error) => toast(error.message, "error")));
            $("agentCreateProfile")?.addEventListener("change", () => loadAgentTokens().catch(() => {}));
            $("agentCreateExpiryMode")?.addEventListener("change", () => toggleExpiryPicker());
            $("agentCreateExpiryYear")?.addEventListener("change", () => syncExpiryDays());
            $("agentCreateExpiryMonth")?.addEventListener("change", () => syncExpiryDays());
            toggleExpiryPicker();
        }

        bindEvents();

        return Object.freeze({
            loadAgentTokens,
            createAgentToken,
            revokeAgentToken,
            loadAgentAudit,
        });
    }
    window.FnosAdminAgent = Object.freeze({ create });
})();
