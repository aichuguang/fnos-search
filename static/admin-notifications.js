(function () {
    const CHANNELS = [
        ["email", "管理员邮件"],
        ["webhook", "Webhook"],
        ["guest_email", "访客邮件"],
    ];
    const CHANNEL_LABELS = Object.freeze({
        email: "管理员邮件",
        webhook: "Webhook",
        guest_email: "访客邮件",
    });
    const STATUS_LABELS = Object.freeze({
        success: "已送达",
        retryable: "等待重试",
        failed: "发送失败",
        skipped: "已跳过",
    });

    function create(context) {
        const { state, getElement, api, toast, escapeHtml, formatDate } = context;
        const $ = getElement;
        let settingsRevision = 0;
        let toggleSaveQueue = Promise.resolve();

        function setValue(id, value) {
            const element = $(id);
            if (element) element.value = value ?? "";
        }

        function setChecked(id, value) {
            const element = $(id);
            if (element) element.checked = Boolean(value);
        }

        function setSecret(id, value) {
            const element = $(id);
            if (!element) return;
            element.value = "";
            element.placeholder = value ? "已配置，留空不修改" : "未配置";
        }

        async function loadNotificationSettings() {
            const revisionAtStart = settingsRevision;
            const data = await api("/api/admin/notifications", {
                skipButtonLoading: true,
                loadingMessage: "正在加载通知设置...",
            });
            if (revisionAtStart === settingsRevision) {
                state.notifications = data.config || {};
                state.notificationEvents = data.events || {};
                state.notificationSummary = data.summary || {};
                renderNotificationSettings();
            }
            await loadNotificationDeliveries();
        }

        function renderNotificationSettings() {
            const config = state.notifications || {};
            const smtp = config.smtp || {};
            const webhook = config.webhook || {};
            setChecked("notificationEnabled", config.enabled);
            setValue("notificationDigestHour", config.digest_hour ?? 9);
            window.syncCustomSelect?.($("notificationDigestHour"));
            setValue("notificationDigestTimezone", config.digest_timezone || "Asia/Shanghai");
            setValue("notificationRetentionDays", config.delivery_retention_days ?? 90);
            setChecked("notificationGuestEnabled", config.guest?.enabled !== false);
            setChecked("notificationSmtpEnabled", smtp.enabled);
            setValue("notificationSmtpHost", smtp.host);
            setValue("notificationSmtpPort", smtp.port ?? 465);
            setValue("notificationSmtpSecurity", smtp.security || "ssl");
            window.syncCustomSelect?.($("notificationSmtpSecurity"));
            setValue("notificationSmtpUsername", smtp.username);
            setSecret("notificationSmtpPassword", smtp.password);
            setValue("notificationSmtpFromName", smtp.from_name);
            setValue("notificationSmtpFromEmail", smtp.from_email);
            setValue("notificationAdminRecipients", (smtp.admin_recipients || []).join("\n"));
            setChecked("notificationWebhookEnabled", webhook.enabled);
            setSecret("notificationWebhookUrl", webhook.url);
            setSecret("notificationWebhookSecret", webhook.secret);
            setChecked("notificationWebhookAllowPrivate", webhook.allow_private);
            renderNotificationStatuses(config);
            renderNotificationRules(config.rules || {});
            renderNotificationSummary(state.notificationSummary || {});
        }

        function renderNotificationStatuses(config) {
            const enabled = Boolean(config.enabled);
            const master = $("notificationMasterStatus");
            const smtpStatus = $("notificationSmtpStatus");
            const webhookStatus = $("notificationWebhookStatus");
            if (master) {
                master.textContent = enabled ? "运行中" : "已停用";
                master.classList.toggle("is-on", enabled);
                master.classList.toggle("is-off", !enabled);
            }
            setChannelStatus(smtpStatus, enabled && Boolean(config.smtp?.enabled));
            setChannelStatus(webhookStatus, enabled && Boolean(config.webhook?.enabled));
        }

        function setChannelStatus(element, active) {
            if (!element) return;
            element.textContent = active ? "已启用" : "未启用";
            element.classList.toggle("is-on", active);
            element.classList.toggle("is-off", !active);
        }

        function renderNotificationRules(rules) {
            const box = $("notificationRules");
            if (!box) return;
            box.innerHTML = Object.entries(state.notificationEvents || {}).map(([eventKey, label]) => {
                const selected = Array.isArray(rules[eventKey]) ? rules[eventKey] : [];
                return `<div class="notification-rule-row" role="group" aria-label="${escapeHtml(label)}" data-notification-event="${escapeHtml(eventKey)}">
                    <span class="notification-rule-name">${escapeHtml(label)}</span>
                    ${CHANNELS.map(([channel, channelLabel]) => `<label class="notification-rule-check" title="${escapeHtml(channelLabel)}"><input type="checkbox" value="${channel}" aria-label="${escapeHtml(`${label}：${channelLabel}`)}" ${selected.includes(channel) ? "checked" : ""}><span>${escapeHtml(channelLabel)}</span></label>`).join("")}
                </div>`;
            }).join("");
        }

        function collectNotificationSettings() {
            const rules = {};
            document.querySelectorAll("[data-notification-event]").forEach((row) => {
                rules[row.dataset.notificationEvent] = Array.from(row.querySelectorAll("input:checked")).map((input) => input.value);
            });
            const lines = String($("notificationAdminRecipients")?.value || "").split(/[\n,，;]+/).map((item) => item.trim()).filter(Boolean);
            const config = {
                enabled: Boolean($("notificationEnabled")?.checked),
                digest_hour: Number($("notificationDigestHour")?.value || 9),
                digest_timezone: $("notificationDigestTimezone")?.value.trim() || "Asia/Shanghai",
                delivery_retention_days: Number($("notificationRetentionDays")?.value || 90),
                guest: { enabled: Boolean($("notificationGuestEnabled")?.checked) },
                smtp: {
                    enabled: Boolean($("notificationSmtpEnabled")?.checked),
                    host: $("notificationSmtpHost")?.value.trim() || "",
                    port: Number($("notificationSmtpPort")?.value || 465),
                    security: $("notificationSmtpSecurity")?.value || "ssl",
                    username: $("notificationSmtpUsername")?.value.trim() || "",
                    from_name: $("notificationSmtpFromName")?.value.trim() || "",
                    from_email: $("notificationSmtpFromEmail")?.value.trim() || "",
                    admin_recipients: lines,
                },
                webhook: {
                    enabled: Boolean($("notificationWebhookEnabled")?.checked),
                    allow_private: Boolean($("notificationWebhookAllowPrivate")?.checked),
                },
                rules,
            };
            const smtpPassword = $("notificationSmtpPassword")?.value.trim() || "";
            const webhookUrl = $("notificationWebhookUrl")?.value.trim() || "";
            const webhookSecret = $("notificationWebhookSecret")?.value.trim() || "";
            if (smtpPassword) config.smtp.password = smtpPassword;
            if (webhookUrl) config.webhook.url = webhookUrl;
            if (webhookSecret) config.webhook.secret = webhookSecret;
            return config;
        }

        async function saveNotificationSettings() {
            await toggleSaveQueue.catch(() => {});
            settingsRevision += 1;
            const button = $("notificationSaveBtn");
            const data = await api("/api/admin/notifications", {
                method: "POST",
                body: JSON.stringify({ config: collectNotificationSettings() }),
                button,
                buttonLabel: "保存中...",
            });
            state.notifications = data.config || {};
            renderNotificationSettings();
            toast(data.message || "通知设置已保存", "success");
        }

        function configWithToggle(id, checked) {
            const config = state.notifications || {};
            if (id === "notificationEnabled") return { ...config, enabled: checked };
            if (id === "notificationGuestEnabled") {
                return { ...config, guest: { ...(config.guest || {}), enabled: checked } };
            }
            if (id === "notificationSmtpEnabled") {
                return { ...config, smtp: { ...(config.smtp || {}), enabled: checked } };
            }
            return { ...config, webhook: { ...(config.webhook || {}), enabled: checked } };
        }

        function togglePatch(id, checked) {
            if (id === "notificationEnabled") return { enabled: checked };
            if (id === "notificationGuestEnabled") return { guest: { enabled: checked } };
            if (id === "notificationSmtpEnabled") {
                return { smtp: checked ? collectNotificationSettings().smtp : { enabled: false } };
            }
            return { webhook: checked ? collectNotificationSettings().webhook : { enabled: false } };
        }

        function persistedToggleValue(id) {
            const config = state.notifications || {};
            if (id === "notificationEnabled") return Boolean(config.enabled);
            if (id === "notificationGuestEnabled") return Boolean(config.guest?.enabled);
            if (id === "notificationSmtpEnabled") return Boolean(config.smtp?.enabled);
            return Boolean(config.webhook?.enabled);
        }

        function saveNotificationToggle(id, label) {
            const element = $(id);
            if (!element) return Promise.resolve();
            const checked = Boolean(element.checked);
            const previous = persistedToggleValue(id);
            const patch = togglePatch(id, checked);
            settingsRevision += 1;
            renderNotificationStatuses(configWithToggle(id, checked));
            element.disabled = true;

            const operation = toggleSaveQueue.catch(() => {}).then(async () => {
                try {
                    const data = await api("/api/admin/notifications", {
                        method: "POST",
                        body: JSON.stringify({ config: patch }),
                        silentLoading: true,
                        skipButtonLoading: true,
                    });
                    state.notifications = data.config || configWithToggle(id, checked);
                    element.checked = checked;
                    renderNotificationStatuses(state.notifications);
                    toast(`${label}已${checked ? "启用" : "停用"}`, "success");
                } catch (error) {
                    element.checked = previous;
                    renderNotificationStatuses(state.notifications || {});
                    toast(`${label}保存失败：${error.message}`, "error");
                    throw error;
                } finally {
                    element.disabled = false;
                }
            });
            toggleSaveQueue = operation;
            return operation;
        }

        async function testNotificationChannel(channel) {
            const button = channel === "email" ? $("notificationTestEmailBtn") : $("notificationTestWebhookBtn");
            const data = await api("/api/admin/notifications/test", {
                method: "POST",
                body: JSON.stringify({ channel, config: collectNotificationSettings() }),
                allowFailure: true,
                button,
                buttonLabel: "测试中...",
            });
            toast(data.message || "测试完成", data.success ? "success" : "error");
        }

        async function loadNotificationDeliveries(button = null) {
            const data = await api("/api/admin/notifications/deliveries?limit=20", {
                silentLoading: !button,
                skipButtonLoading: !button,
                button,
                buttonLabel: "刷新中...",
            });
            state.notificationSummary = data.summary || {};
            state.notificationDeliveries = data.deliveries || [];
            renderNotificationSummary(state.notificationSummary);
            renderNotificationDeliveries(state.notificationDeliveries);
        }

        async function retryNotificationTask(taskId, button) {
            const data = await api(`/api/admin/notifications/tasks/${encodeURIComponent(taskId)}/retry`, {
                method: "POST",
                body: JSON.stringify({}),
                button,
                buttonLabel: "重试中...",
            });
            toast(data.message || "通知任务已重新入队", "success");
            await loadNotificationDeliveries();
        }

        function renderNotificationSummary(summary) {
            const box = $("notificationDeliverySummary");
            if (!box) return;
            const items = [
                ["total", "全部", ""],
                ["success", "已送达", "ok"],
                ["retryable", "等待重试", "warn"],
                ["failed", "发送失败", "error"],
            ];
            box.innerHTML = items.map(([key, label, tone]) => `<div class="notification-summary-item ${tone}"><span>${label}</span><strong>${Number(summary[key] || 0)}</strong></div>`).join("");
        }

        function renderNotificationDeliveries(items) {
            const box = $("notificationDeliveryList");
            if (!box) return;
            if (!items.length) {
                box.innerHTML = '<div class="notification-delivery-empty"><strong>暂无投递记录</strong><span>渠道开始发送后，结果会显示在这里。</span></div>';
                return;
            }
            box.innerHTML = items.map((item) => `<div class="notification-delivery-item">
                <div class="notification-delivery-main"><strong>${escapeHtml(state.notificationEvents?.[item.event_type] || item.event_type)}</strong><span>${escapeHtml(CHANNEL_LABELS[item.channel] || item.channel)}</span></div>
                <div class="notification-delivery-target"><span>${escapeHtml(item.recipient || "-")}</span><time>${escapeHtml(formatDate(item.updated_at))}</time></div>
                <div class="notification-delivery-result"><span class="pill ${item.status === "success" ? "ok" : item.status === "retryable" ? "warn" : "error"}">${escapeHtml(STATUS_LABELS[item.status] || item.status)}</span>${item.task_id && item.status === "failed" ? `<button class="secondary mini" type="button" data-notification-retry="${escapeHtml(item.task_id)}">重试</button>` : ""}</div>
            </div>`).join("");
            box.querySelectorAll("[data-notification-retry]").forEach((button) => {
                button.addEventListener("click", () => retryNotificationTask(button.dataset.notificationRetry, button).catch((error) => toast(error.message, "error")));
            });
        }

        function activateNotificationTab(name) {
            document.querySelectorAll("[data-notification-tab-target]").forEach((button) => {
                const active = button.dataset.notificationTabTarget === name;
                button.classList.toggle("active", active);
                button.setAttribute("aria-selected", String(active));
            });
            document.querySelectorAll("[data-notification-tab]").forEach((panel) => {
                const active = panel.dataset.notificationTab === name;
                panel.classList.toggle("active", active);
                panel.hidden = !active;
            });
        }

        function stepNotificationNumber(button) {
            const input = $(button.dataset.notificationStepTarget);
            if (!input) return;
            const step = Number(button.dataset.notificationStep || 0);
            const minimum = Number(input.min);
            const maximum = Number(input.max);
            const fallback = Number.isFinite(minimum) ? minimum : 0;
            const current = Number.isFinite(Number(input.value)) ? Number(input.value) : fallback;
            let next = current + step;
            if (button.dataset.notificationStepWrap === "true") {
                if (Number.isFinite(maximum) && next > maximum) next = minimum;
                if (Number.isFinite(minimum) && next < minimum) next = maximum;
            } else {
                if (Number.isFinite(minimum)) next = Math.max(minimum, next);
                if (Number.isFinite(maximum)) next = Math.min(maximum, next);
            }
            input.value = String(next);
            input.dispatchEvent(new Event("change", { bubbles: true }));
        }

        function bindNotificationSettings() {
            document.querySelectorAll("[data-notification-tab-target]").forEach((button) => {
                button.addEventListener("click", () => activateNotificationTab(button.dataset.notificationTabTarget));
            });
            document.querySelectorAll("[data-notification-step-target]").forEach((button) => {
                button.addEventListener("click", () => stepNotificationNumber(button));
            });
            $("notificationEnabled")?.addEventListener("change", () => saveNotificationToggle("notificationEnabled", "通知").catch(() => {}));
            $("notificationGuestEnabled")?.addEventListener("change", () => saveNotificationToggle("notificationGuestEnabled", "访客邮件").catch(() => {}));
            $("notificationSmtpEnabled")?.addEventListener("change", () => saveNotificationToggle("notificationSmtpEnabled", "SMTP").catch(() => {}));
            $("notificationWebhookEnabled")?.addEventListener("change", () => saveNotificationToggle("notificationWebhookEnabled", "Webhook").catch(() => {}));
            $("notificationSaveBtn")?.addEventListener("click", () => saveNotificationSettings().catch((error) => toast(error.message, "error")));
            $("notificationTestEmailBtn")?.addEventListener("click", () => testNotificationChannel("email").catch((error) => toast(error.message, "error")));
            $("notificationTestWebhookBtn")?.addEventListener("click", () => testNotificationChannel("webhook").catch((error) => toast(error.message, "error")));
            $("notificationRefreshDeliveriesBtn")?.addEventListener("click", (event) => loadNotificationDeliveries(event.currentTarget).catch((error) => toast(error.message, "error")));
        }

        return Object.freeze({
            loadNotificationSettings,
            renderNotificationSettings,
            collectNotificationSettings,
            saveNotificationSettings,
            testNotificationChannel,
            loadNotificationDeliveries,
            bindNotificationSettings,
        });
    }

    window.FnosAdminNotifications = Object.freeze({ create });
})();
