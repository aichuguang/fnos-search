(function () {
    "use strict";

    const ui = window.FnosUI;
    const host = document.getElementById("v1StorageWorkspace");
    if (!ui || !host) return;

    const state = {
        data: null,
        activeView: "accounts",
        editor: null,
        busy: false,
        notice: { message: "正在读取配置…", kind: "" },
        directoryOptions: [],
        directoryError: "",
        preflight: null,
        authSession: null,
        authSessionHidden: false,
        authSessionRestored: false,
        authSessionRestoreWarning: "",
        loginPrompt: null,
        authError: "",
    };
    const AUTH_SESSION_STORAGE_KEY = "fnos.providerAuthSessionId";
    let authPollTimer = null;
    let authPollInFlight = false;

    const esc = (value) => ui.escapeHtml(value ?? "");

    async function request(path, options) {
        const requestOptions = Object.assign({ allowFailure: true }, options || {});
        const requester = ui.requestAdminJson || ui.requestJson;
        let result;
        try {
            result = await requester(path, requestOptions);
        } catch (cause) {
            throw apiError(cause?.data || {}, cause);
        }
        if (!result || result.success === false) throw apiError(result || {});
        return result;
    }

    function apiError(data, cause) {
        const parts = [];
        if (data?.stage) parts.push(`阶段：${data.stage}`);
        if (data?.trace_id) parts.push(`追踪号：${data.trace_id}`);
        const fieldErrors = Object.entries(data?.field_errors || {})
            .map(([field, message]) => `${field}：${message}`);
        if (fieldErrors.length) parts.push(fieldErrors.join("；"));
        if (data?.retryable) parts.push("可以稍后重试");
        const error = new Error(
            `${data?.message || cause?.message || "请求失败"}${parts.length ? `（${parts.join("，")}）` : ""}`,
            cause ? { cause } : undefined,
        );
        error.data = data || {};
        error.code = data?.code || "REQUEST_FAILED";
        error.status = cause?.status || 0;
        return error;
    }

    function setNotice(message, kind) {
        state.notice = { message: String(message || ""), kind: kind || "" };
        const box = document.getElementById("storageSetupNotice");
        if (box) {
            box.className = `storage-setup-notice ${state.notice.kind}`;
            box.textContent = state.notice.message;
        }
    }

    function setAuthError(message) {
        state.authError = String(message || "");
        const box = document.getElementById("storageAuthError");
        if (box) {
            box.hidden = !state.authError;
            box.textContent = state.authError;
        }
    }

    function reportAuthError(error, fallback = "账号授权失败") {
        const message = error?.message || fallback;
        const inlineErrorPresent = Boolean(document.getElementById("storageAuthError"));
        setAuthError(message);
        setNotice(message, "is-error");
        // Password, QR and device-code failures can occur before an inline
        // authorization panel exists.  Keep the persistent page notice, also
        // publish an assertive toast, and rebuild an existing auth dialog only
        // when its error region was missing.  Avoid unconditional rendering so
        // a failed password submission does not erase the user's input.
        ui.showToast?.("toast", message, "error", 6000);
        if (
            !inlineErrorPresent
            && (state.loginPrompt || (state.authSession && !state.authSessionHidden))
        ) {
            render();
        }
        const inlineError = document.getElementById("storageAuthError");
        inlineError?.scrollIntoView?.({ block: "nearest" });
    }

    function renderAuthError() {
        return `<div id="storageAuthError" class="storage-auth-error" role="alert" aria-live="assertive" ${state.authError ? "" : "hidden"}>${esc(state.authError)}</div>`;
    }

    function setBusy(value) {
        state.busy = Boolean(value);
        host.setAttribute("aria-busy", state.busy ? "true" : "false");
        host.querySelectorAll("button").forEach((element) => {
            if (element.dataset.keepEnabled === "true") return;
            if (state.busy) {
                element.dataset.busyWasDisabled = element.disabled ? "true" : "false";
                element.disabled = true;
                return;
            }
            if (Object.prototype.hasOwnProperty.call(element.dataset, "busyWasDisabled")) {
                element.disabled = element.dataset.busyWasDisabled === "true";
                delete element.dataset.busyWasDisabled;
            }
        });
    }

    async function load(message) {
        if (message) setNotice(message);
        try {
            state.data = await request("/api/admin/storage-setup");
            await restoreAuthSession();
            render();
            const progress = state.data.progress || {};
            if (state.authSessionRestoreWarning) {
                setNotice(state.authSessionRestoreWarning, "is-error");
                state.authSessionRestoreWarning = "";
            } else {
                setNotice(
                    progress.ready ? "多链路入库配置完整，可以接收任务。" : `配置进度 ${progress.completed || 0}/${progress.total || 4}`,
                    progress.ready ? "is-success" : ""
                );
            }
        } catch (error) {
            render();
            setNotice(error.message || "配置读取失败", "is-error");
        }
    }

    function persistAuthSession(session) {
        try {
            if (session?.status === "waiting" && session.session_id) {
                window.sessionStorage.setItem(AUTH_SESSION_STORAGE_KEY, session.session_id);
            } else {
                window.sessionStorage.removeItem(AUTH_SESSION_STORAGE_KEY);
            }
        } catch (_error) {
            // Session persistence is an enhancement; authorization still works
            // when storage is unavailable in a restricted browser context.
        }
    }

    async function restoreAuthSession() {
        if (state.authSessionRestored) return;
        state.authSessionRestored = true;
        let sessionId = "";
        try { sessionId = window.sessionStorage.getItem(AUTH_SESSION_STORAGE_KEY) || ""; }
        catch (_error) { return; }
        if (!sessionId) return;
        try {
            const result = await request(`/api/admin/provider-auth-sessions/${encodeURIComponent(sessionId)}`);
            state.authSession = result.session || null;
            state.authSessionHidden = false;
            setAuthError("");
            persistAuthSession(state.authSession);
        } catch (error) {
            persistAuthSession(null);
            state.authSessionRestoreWarning = error.status === 404
                ? "上次账号授权会话已结束，请重新发起登录。"
                : (error.message || "账号授权会话恢复失败，请重新登录。");
        }
    }

    function render() {
        if (!state.data) {
            host.innerHTML = `<div class="storage-setup-loading"><span class="storage-spinner" aria-hidden="true"></span><span>正在读取多链路配置</span></div>`;
            return;
        }
        const progress = state.data.progress || { completed: 0, total: 4, steps: [] };
        host.innerHTML = `
            <header class="storage-setup-header">
                <div class="storage-setup-title">
                    <span class="storage-setup-kicker">网盘入库</span>
                    <h3>多链路入库</h3>
                    <p>保存位置由管理员统一设置，系统自动选择同源直转、rclone 或移动云中继。</p>
                </div>
                <div class="storage-setup-header-actions">
                    <span class="storage-progress-label ${progress.ready ? "is-ready" : ""}">${progress.ready ? "已就绪" : `${progress.completed}/${progress.total} 已完成`}</span>
                    <button type="button" class="secondary mini" data-setup-action="preflight">完整预检</button>
                    <button type="button" class="secondary mini storage-icon-button" data-setup-action="refresh" title="刷新配置" aria-label="刷新配置"><span class="icon-slot icon-refresh" aria-hidden="true"></span></button>
                </div>
            </header>
            <div class="storage-progress" aria-label="配置进度">${(progress.steps || []).map(progressStep).join("")}</div>
            <div id="storageSetupNotice" class="storage-setup-notice ${esc(state.notice.kind)}" role="status" aria-live="polite">${esc(state.notice.message)}</div>
            <nav class="storage-setup-nav" aria-label="多链路配置" role="tablist">
                ${viewButton("accounts", "云盘账号", "统一维护各网盘账号")}
                ${viewButton("locations", "保存位置", "OpenList 与入库目录")}
                ${viewButton("routes", "入库规则", "每个来源保存到哪里")}
            </nav>
            <section class="storage-setup-content" role="tabpanel">
                ${state.activeView === "accounts" ? renderAccountsV1() : state.activeView === "locations" ? renderLocations() : renderRoutes()}
            </section>
            ${state.preflight ? renderPreflight() : ""}
            ${state.loginPrompt ? renderProviderLoginPrompt() : state.authSession && !state.authSessionHidden ? renderAuthSession() : ""}`;
        // The workspace is rendered after DOMContentLoaded, so let the shared
        // select enhancer upgrade its dynamically-created native selects too.
        window.refreshCustomSelects?.();
        scheduleAuthPolling();
    }

    function progressStep(step, index) {
        return `<div class="storage-progress-step ${step.complete ? "is-complete" : ""}"><span>${step.complete ? "✓" : index + 1}</span><strong>${esc(step.label)}</strong></div>`;
    }

    function viewButton(key, label, note) {
        return `<button type="button" role="tab" aria-selected="${state.activeView === key}" class="${state.activeView === key ? "active" : ""}" data-setup-view="${key}"><strong>${label}</strong><small>${note}</small></button>`;
    }

    function schemaFields(schema) {
        return Array.isArray(schema?.fields) ? schema.fields.filter((field) => field && field.name) : [];
    }

    function authInputSchema(provider, authDefinition) {
        if (
            authDefinition
            && Object.prototype.hasOwnProperty.call(authDefinition, "input_schema")
        ) {
            return authDefinition.input_schema || { fields: [] };
        }
        // QR authorization intentionally has no local credential form. Never
        // expose a manual Cookie form when an older catalog is cached.
        if (String(authDefinition?.kind || "").trim().toLowerCase() === "qrcode") {
            return { fields: [] };
        }
        // During a rolling restart the browser may briefly receive a catalog
        // without auth_methods.input_schema. Non-QR login can safely fall back
        // to the canonical account schema instead of submitting an empty body.
        return schemaFields(provider?.account_schema).length
            ? provider.account_schema
            : { fields: [] };
    }

    function renderSchemaFields(schema, values = {}, options = {}) {
        const editing = Boolean(options.editing);
        const namePrefix = String(options.namePrefix || "");
        return schemaFields(schema).map((field) => {
            const name = `${namePrefix}${field.name}`;
            const type = field.type || "text";
            const configured = Boolean(field.configured_flag && values[field.configured_flag]);
            const value = values[field.name] ?? field.default ?? "";
            const secretMayRemain = field.secret && editing && (configured || options.allowSecretOmission);
            const required = field.required && !secretMayRemain ? "required" : "";
            const placeholder = secretMayRemain
                ? "已保存，留空保持不变"
                : (field.placeholder || "");
            const autocomplete = field.autocomplete || (field.secret ? "new-password" : "off");
            const labelClass = field.span === 2 || ["textarea", "url"].includes(type) ? "span-2" : "";
            if (type === "textarea") {
                return `<label class="${labelClass}"><span>${esc(field.label || field.name)}</span><textarea name="${esc(name)}" ${required} placeholder="${esc(placeholder)}" autocomplete="${esc(autocomplete)}">${esc(value)}</textarea></label>`;
            }
            if (type === "select") {
                const choices = Array.isArray(field.options) ? field.options : [];
                return `<label class="${labelClass}"><span>${esc(field.label || field.name)}</span><select name="${esc(name)}" ${required}>${choices.map((item) => {
                    const option = typeof item === "object" ? item : { value: item, label: item };
                    return `<option value="${esc(option.value)}" ${String(option.value) === String(value) ? "selected" : ""}>${esc(option.label ?? option.value)}</option>`;
                }).join("")}</select></label>`;
            }
            return `<label class="${labelClass}"><span>${esc(field.label || field.name)}</span><input name="${esc(name)}" type="${esc(type)}" ${required} value="${field.secret ? "" : esc(value)}" placeholder="${esc(placeholder)}" autocomplete="${esc(autocomplete)}"></label>`;
        }).join("");
    }

    function renderAccountEditorV1() {
        const provider = providerFor(state.editor.provider_kind) || {};
        const values = state.editor.values || {};
        const editing = Boolean(state.editor.account_id);
        const authManaged = Boolean((provider.auth_methods || []).length);
        const fieldValues = { ...(values.config || {}), ...values };
        const fieldMarkup = editing && authManaged
            ? ""
            : renderSchemaFields(provider.account_schema, fieldValues, { editing, allowSecretOmission: true });
        return `<form id="storageAccountForm" class="storage-editor-form">
            ${editorHeader(`${state.editor.account_id ? "编辑" : "添加"}${esc(provider.display_name || "")}账号`, "accounts")}
            <div class="storage-form-grid">
                <label><span>账号名称</span><input name="name" required maxlength="80" value="${esc(values.name || "")}" placeholder="例如：主账号"></label>
                ${fieldMarkup}
            </div>
            ${editing && authManaged ? '<div class="storage-path-note">登录凭证由授权流程维护；这里仅修改账号在后台显示的名称。</div>' : ""}
            <div class="storage-form-actions"><button type="submit">${editing && authManaged ? "保存名称" : "保存账号"}</button><button type="button" class="secondary" data-setup-action="cancel-editor">取消</button></div>
        </form>`;
    }

    function renderLocations() {
        if (state.editor?.type === "openlist") return renderOpenListEditor();
        if (state.editor?.type === "location") return renderLocationEditor();
        const connections = state.data.connections || [];
        const readyConnections = connections.filter((item) => item.enabled && item.last_test_status === "ok");
        const locations = state.data.locations || [];
        return `
            <div class="storage-section-heading">
                <div><h4>保存位置</h4><p>先维护 OpenList 连接，再把账号或通用网盘映射为保存位置。</p></div>
                <div class="storage-heading-actions"><button type="button" class="secondary" data-openlist-action="add">添加 OpenList</button><button type="button" data-location-action="add">添加保存位置</button></div>
            </div>
            <div class="storage-openlist-list">
                ${connections.length ? connections.map(renderOpenListConnectionRow).join("") : `<div class="storage-empty-state"><strong>尚未连接 OpenList</strong><p>本地位置可以直接创建；云盘位置需要先添加 WebDAV 连接。</p></div>`}
            </div>
            ${readyConnections.length ? "" : `<div class="storage-blocker">没有检测通过的 OpenList 连接，目前只能创建本地保存位置。</div>`}
            <div class="storage-location-list">
                ${locations.length ? locations.map(renderLocationRow).join("") : `<div class="storage-empty-state"><strong>还没有保存位置</strong><p>连接 OpenList 后添加第一个保存位置。</p></div>`}
            </div>`;
    }

    function renderOpenListConnectionRow(connection) {
        const ready = connection.enabled && connection.last_test_status === "ok";
        return `<section class="storage-openlist-band ${ready ? "is-ready" : ""}">
            <span class="storage-openlist-icon"><span class="icon-slot icon-openlist" aria-hidden="true"></span></span>
            <div><strong>${esc(connection.name)}</strong><p>${esc(connection.base_url)} · ${esc(connection.username || "未设置用户")}</p></div>
            <span class="storage-status ${ready ? "is-success" : "is-warning"}">${ready ? "连接正常" : "需要检测"}</span>
            <div class="storage-row-actions"><button type="button" class="secondary mini" data-openlist-action="edit" data-id="${esc(connection.connection_id)}">编辑</button><button type="button" class="danger mini" data-openlist-action="delete" data-id="${esc(connection.connection_id)}">删除</button></div>
        </section>`;
    }

    function renderLocationRow(location) {
        const provider = providerFor(location.provider_kind);
        const typeLabel = location.kind === "local" ? "本地目录" : location.kind === "generic" ? "OpenList 通用盘" : provider?.display_name || location.provider_kind;
        return `<article class="storage-location-row">
            <span class="storage-provider-mark kind-${esc(location.provider_kind)}">${esc(provider?.icon_text || (location.kind === "local" ? "本" : "盘"))}</span>
            <div class="storage-record-main">
                <div class="storage-record-title"><strong>${esc(location.name)}</strong><span class="storage-status ${location.ready ? "is-success" : "is-warning"}">${location.ready ? "可用" : "待检查"}</span></div>
                <p>${esc(typeLabel)}${location.account_name ? ` · ${esc(location.account_name)}` : ""}${location.webdav_root ? ` · ${esc(location.webdav_root)}` : ""}</p>
                <small>${esc(location.status_message)}</small>
            </div>
            <div class="storage-row-actions"><button type="button" class="secondary mini" data-location-action="edit" data-id="${esc(location.destination_id)}">编辑</button><button type="button" class="danger mini" data-location-action="delete" data-id="${esc(location.destination_id)}">删除</button></div>
        </article>`;
    }

    function renderOpenListEditor() {
        const values = state.editor.values || {};
        return `<form id="storageOpenListForm" class="storage-editor-form">
            ${editorHeader(values.connection_id ? "编辑 OpenList WebDAV" : "连接 OpenList WebDAV", "locations")}
            <div class="storage-form-grid">
                <label class="span-2"><span>连接名称</span><input name="name" required maxlength="80" value="${esc(values.name || "")}" placeholder="例如：家庭 OpenList"></label>
                <label class="span-2"><span>WebDAV 地址</span><input name="base_url" type="url" required value="${esc(values.base_url || "")}" placeholder="http://openlist:5244/dav"></label>
                <label><span>用户名</span><input name="username" required value="${esc(values.username || "")}" autocomplete="username"></label>
                <label><span>密码</span><input name="password" type="password" ${values.connection_id ? "" : "required"} placeholder="${values.connection_id ? "已保存，留空保持不变" : "WebDAV 密码"}" autocomplete="new-password"></label>
            </div>
            <div class="storage-form-actions"><button type="submit">保存并检测</button><button type="button" class="secondary" data-setup-action="cancel-editor">取消</button></div>
        </form>`;
    }

    function renderLocationEditor() {
        const values = state.editor.values || {};
        const kind = values.kind || "provider";
        const allAccounts = (state.data.accounts || []).filter((item) => item.enabled);
        const providerKinds = [...new Set(allAccounts.map((item) => item.provider_kind))];
        const selectedAccount = allAccounts.find((item) => item.account_id === values.account_id);
        const selectedProviderKind = values.provider_kind || selectedAccount?.provider_kind || providerKinds[0] || "";
        const accounts = allAccounts.filter((item) => item.provider_kind === selectedProviderKind && (item.last_test_status === "ok" || item.account_id === values.account_id));
        const provider = providerFor(selectedProviderKind);
        const locatorField = schemaFields(provider?.native_locator_schema)[0];
        const connections = (state.data.connections || []).filter((item) => item.enabled);
        return `<form id="storageLocationForm" class="storage-editor-form">
            ${editorHeader(values.destination_id ? "编辑保存位置" : "添加保存位置", "locations")}
            <div class="storage-form-grid">
                <label><span>名称</span><input name="name" required maxlength="80" value="${esc(values.name || "")}" placeholder="例如：夸克主盘"></label>
                <label><span>位置类型</span><select name="kind" ${values.destination_id ? "disabled" : ""}><option value="provider" ${kind === "provider" ? "selected" : ""}>已接入的云盘</option><option value="generic" ${kind === "generic" ? "selected" : ""}>OpenList 其他网盘</option><option value="local" ${kind === "local" ? "selected" : ""}>本地目录</option></select></label>
                ${kind === "provider" ? `<label><span>网盘类型</span><select name="provider_kind" required ${values.destination_id ? "disabled" : ""}><option value="">请选择网盘</option>${providerKinds.map((providerKind) => `<option value="${esc(providerKind)}" ${providerKind === selectedProviderKind ? "selected" : ""}>${esc(providerFor(providerKind)?.display_name || providerKind)}</option>`).join("")}</select></label><label><span>云盘账号</span><select name="account_id" required><option value="">请选择账号</option>${accounts.map((item) => `<option value="${esc(item.account_id)}" ${item.account_id === values.account_id ? "selected" : ""}>${esc(item.name)}${item.last_test_status === "ok" ? "" : "（待检测）"}</option>`).join("")}</select></label>` : ""}
                ${kind !== "local" ? `<label><span>OpenList 连接</span><select name="connection_id" required><option value="">请选择连接</option>${connections.map((item) => `<option value="${esc(item.connection_id)}" ${item.connection_id === values.connection_id ? "selected" : ""}>${esc(item.name)}${item.last_test_status === "ok" ? "" : "（待检测）"}</option>`).join("")}</select></label>
                <label class="span-2"><span>OpenList 中的目录</span><input name="webdav_root" list="storageDirectoryOptions" required value="${esc(values.webdav_root || "")}" placeholder="例如：/夸克/媒体库"><datalist id="storageDirectoryOptions">${state.directoryOptions.map((item) => `<option value="${esc(item.path)}">${esc(item.name || item.path)}</option>`).join("")}</datalist></label>` : ""}
                ${kind === "provider" ? `<label class="span-2"><span>${esc(locatorField?.label || "云盘保存根目录")}</span><input name="native_root" required value="${esc(values.native_root || "")}" placeholder="同源直转和跨盘落地使用"></label>` : ""}
                ${kind !== "local" ? `<label class="span-2 storage-identity-confirm"><input name="confirm_identity" type="checkbox" value="true" required><span>我确认该 OpenList 目录确实对应上面选择的账号或存储位置。修改目录、账号或挂载关系时会重新确认。</span></label>` : ""}
            </div>
            ${state.directoryError ? `<div class="storage-inline-error" role="alert">${esc(state.directoryError)}</div>` : ""}
            <div class="storage-path-note">电影、电视剧、动漫等目录由系统自动生成，不再单独配置分类路径。</div>
            <div class="storage-form-actions"><button type="submit">保存并检测目录</button><button type="button" class="secondary" data-setup-action="cancel-editor">取消</button></div>
        </form>`;
    }

    function renderRoutes() {
        const routes = state.data.routes || [];
        const locations = (state.data.locations || []).filter((item) => item.enabled);
        return `<form id="storageRoutesForm">
            <div class="storage-section-heading"><div><h4>入库规则</h4><p>访客无需选择保存位置，提交后按搜索来源自动执行。</p></div><button type="submit" ${locations.length ? "" : "disabled"}>保存规则</button></div>
            <div class="storage-route-list">
                ${routes.map((route) => renderRouteRow(route, locations)).join("")}
            </div>
        </form>`;
    }

    function renderRouteRow(route, locations) {
        const eligible = locations.filter((item) => item.ready && (item.eligible_source_ids || []).includes(route.search_source_id));
        return `<article class="storage-route-row" data-source="${esc(route.search_source_id)}">
            <div class="storage-route-source"><span class="storage-provider-mark kind-${esc(route.provider_kind)}">${esc(route.icon_text || providerFor(route.provider_kind)?.icon_text || "源")}</span><div><strong>${esc(route.label)}</strong><small>搜索来源</small></div></div>
            <label><span>默认保存到</span><select name="route_${esc(route.search_source_id)}" required><option value="">请选择保存位置</option>${locationOptions(eligible, route.default_destination_id)}</select></label>
            <div class="storage-route-summary"><span>执行链路</span><strong data-route-summary>${esc(route.chain_summary)}</strong></div>
            <details class="storage-route-overrides"><summary>按分类单独设置</summary><div>${(state.data.categories || []).map((category) => `<label><span>${esc(category.label)}</span><select name="override_${esc(route.search_source_id)}_${esc(category.key)}"><option value="">跟随默认</option>${locationOptions(eligible, route.overrides?.[category.key])}</select></label>`).join("")}</div></details>
        </article>`;
    }

    function locationOptions(locations, selectedId) {
        return locations.map((item) => `<option value="${esc(item.destination_id)}" ${item.destination_id === selectedId ? "selected" : ""}>${esc(item.name)}</option>`).join("");
    }

    function routeChainSummary(sourceId, destinationId) {
        const route = (state.data.routes || []).find((item) => item.search_source_id === sourceId);
        const location = (state.data.locations || []).find((item) => item.destination_id === destinationId);
        if (!location) return "尚未设置保存位置";
        if (sourceId === "sixpan_magnet") return "六盘离线后直接入库";
        if (route?.provider_kind === location.provider_kind) return "同源直转，不经过本地";
        if (location.provider_kind === "mobile") return "来源落地后下载到本地，再由移动云 API 上传";
        if (location.kind === "local") return "来源落地后通过 rclone 下载到本地";
        return "来源落地后通过 rclone 搬运到目标网盘";
    }

    function renderPreflight() {
        return `<div class="storage-preflight-mask" role="dialog" aria-modal="true" aria-label="多链路入库预检"><section class="storage-preflight-dialog"><header><div><h4>完整预检</h4><p>${esc(state.preflight.message)}</p></div><button type="button" class="secondary mini" data-setup-action="close-preflight">关闭</button></header><div class="storage-preflight-list">${(state.preflight.checks || []).map((item) => `<div class="${item.ok ? "is-success" : "is-error"}"><span>${item.ok ? "✓" : "!"}</span><div><strong>${esc(item.name)}</strong><p>${esc(item.message || (item.ok ? "通过" : "未通过"))}</p></div></div>`).join("")}</div></section></div>`;
    }

    function renderAuthSessionQr() {
        const session = state.authSession;
        const provider = providerFor(session.provider_kind);
        const info = session.public || {};
        const waiting = session.status === "waiting";
        return `<div class="storage-preflight-mask" role="dialog" aria-modal="true" aria-label="${esc(provider?.display_name || "网盘")}登录">
            <section class="storage-auth-dialog storage-auth-qr-dialog">
                <header><span class="storage-provider-mark kind-${esc(session.provider_kind)}">${esc(provider?.icon_text || "盘")}</span><div><h4>${esc(provider?.display_name || session.provider_kind)}账号登录</h4><p>${esc(session.message || "正在等待授权")}</p></div></header>
                ${renderAuthError()}
                ${info.qr_image ? `<div class="storage-auth-qr-wrap"><img class="storage-auth-qr" src="${esc(info.qr_image)}" alt="${esc(provider?.display_name || "网盘")}登录二维码"><small>请使用对应网盘 APP 扫描此二维码</small></div>` : ""}
                ${info.qr_url ? `<a class="storage-auth-link" href="${esc(info.qr_url)}" target="_blank" rel="noreferrer">在新窗口打开扫码页</a>` : ""}
                ${info.user_code ? `<div class="storage-auth-code"><span>授权码</span><strong>${esc(info.user_code)}</strong></div>` : ""}
                ${info.verification_uri ? `<a class="storage-auth-link" href="${esc(info.verification_uri)}" target="_blank" rel="noreferrer">打开授权页面</a>` : ""}
                <div class="storage-form-actions">${waiting ? `<button type="button" class="secondary" data-auth-action="verify">立即检查状态</button><button type="button" class="secondary" data-auth-action="hide">关闭窗口，后台继续</button><button type="button" class="danger" data-auth-action="cancel">取消授权</button>` : `<button type="button" class="secondary" data-auth-action="close">关闭</button>`}</div>
            </section>
        </div>`;
    }

    function renderAuthSession() {
        const info = state.authSession?.public || {};
        if (info.challenge === "captcha" || info.captcha_image) return renderAuthChallengeSession();
        return renderAuthSessionQr();
    }

    function renderProviderLoginPrompt() {
        const prompt = state.loginPrompt || {};
        const account = prompt.account;
        const provider = providerFor(prompt.providerKind) || {};
        const authMethod = (provider.auth_methods || []).find((item) => item.kind === prompt.authMethod) || {};
        return `<div class="storage-preflight-mask" role="dialog" aria-modal="true" aria-label="${esc(provider.display_name || "网盘")}账号登录">
            <section class="storage-auth-dialog storage-auth-password-dialog">
                <header><span class="storage-provider-mark kind-${esc(provider.provider_kind)}">${esc(provider.icon_text || "盘")}</span><div><h4>${account ? "重新登录" : "登录"}${esc(provider.display_name || "网盘")}账号</h4><p>${esc(authMethod.description || "登录信息只会发送到本服务并加密保存。")}</p></div></header>
                ${renderAuthError()}
                <form id="providerLoginForm" class="storage-form-grid storage-auth-form">
                    <label class="span-2"><span>账号名称</span><input name="name" required maxlength="80" value="${esc(account?.name || `${provider.display_name || "网盘"}主账号`)}" autocomplete="off"></label>
                    ${renderSchemaFields(authInputSchema(provider, authMethod), {}, { namePrefix: "auth_" })}
                    <div class="storage-form-actions span-2"><button type="submit">登录</button><button type="button" class="secondary" data-auth-action="cancel-local">取消</button></div>
                </form>
            </section>
        </div>`;
    }

    function renderAuthChallengeSession() {
        const session = state.authSession;
        const provider = providerFor(session.provider_kind) || {};
        const info = session.public || {};
        const waiting = session.status === "waiting";
        return `<div class="storage-preflight-mask" role="dialog" aria-modal="true" aria-label="${esc(provider.display_name || "网盘")}安全验证">
            <section class="storage-auth-dialog storage-auth-password-dialog">
                <header><span class="storage-provider-mark kind-${esc(provider.provider_kind)}">${esc(provider.icon_text || "盘")}</span><div><h4>${esc(provider.display_name || "网盘")}安全验证</h4><p>${esc(session.message || "请输入图片验证码")}</p></div></header>
                ${renderAuthError()}
                ${info.captcha_image ? `<div class="storage-auth-captcha"><img src="${esc(info.captcha_image)}" alt="${esc(provider.display_name || "网盘")}图片验证码"></div>` : ""}
                ${waiting ? `<form id="providerChallengeForm" class="storage-form-grid storage-auth-form"><label class="span-2"><span>图片验证码</span><input name="validate_code" required autocomplete="off" inputmode="text" autofocus placeholder="请输入图片中的字符"></label><div class="storage-form-actions span-2"><button type="submit">验证并登录</button><button type="button" class="secondary" data-auth-action="refresh-captcha">换一张</button><button type="button" class="secondary" data-auth-action="cancel">取消登录</button></div></form>` : `<div class="storage-form-actions"><button type="button" class="secondary" data-auth-action="close">关闭</button></div>`}
            </section>
        </div>`;
    }

    // Provider manifests define account/login forms; the workspace
    // contains no provider-kind switch for future adapters such as 115.
    function renderAccountsV1() {
        if (state.editor?.type === "account") return renderAccountEditorV1();
        const providers = state.data.providers || [];
        return `<div class="storage-section-heading"><div><h4>云盘账号</h4><p>每种云盘可以维护多个账号，入库只使用当前激活账号。</p></div></div><div class="storage-provider-grid">${providers.map((provider) => {
            const accounts = (state.data.accounts || []).filter((item) => item.provider_kind === provider.provider_kind);
            const active = accounts.find((item) => item.active);
            const authMethod = (provider.auth_methods || [])[0];
            const authReady = Boolean(
                authMethod
                && !["planned", "unavailable", "disabled"].includes(authMethod.availability),
            );
            const authMessage = authMethod?.description || "";
            const loginAction = authMethod
                ? (authReady
                    ? `<button type="button" class="secondary mini" data-account-action="login" data-provider="${esc(provider.provider_kind)}" data-auth-method="${esc(authMethod.kind)}">${esc(authMethod.label || "登录账号")}</button>`
                    : `<button type="button" class="secondary mini" disabled title="${esc(authMessage || "当前登录方式不可用")}">${esc(authMethod.label || "登录未开放")}</button>`)
                : (schemaFields(provider.account_schema).length
                    ? `<button type="button" class="secondary mini" data-account-action="edit-new" data-provider="${esc(provider.provider_kind)}">添加账号</button>`
                    : `<button type="button" class="secondary mini" disabled>登录未开放</button>`);
            const actions = `<div class="storage-provider-actions">${loginAction}</div>`;
            return `<section class="storage-provider-group" data-provider-kind="${esc(provider.provider_kind)}"><header><span class="storage-provider-mark kind-${esc(provider.provider_kind)}">${esc(provider.icon_text || provider.display_name?.slice(0, 1) || "盘")}</span><div><h5>${esc(provider.display_name)}</h5><p>${active ? `当前使用：${esc(active.name)}` : "尚未设置当前账号"}</p></div>${actions}</header><small class="storage-provider-auth-status ${authReady ? "is-ready" : "is-warning"}">${esc(authMessage || (authReady ? "登录入口可用" : "登录入口未开放"))}</small><div class="storage-account-list">${accounts.length ? accounts.map(renderAccountRowQr).join("") : `<div class="storage-empty-inline">还没有账号</div>`}</div></section>`;
        }).join("")}</div>`;
    }

    function renderAccountRowQr(account) {
        const provider = providerFor(account.provider_kind);
        const authMethod = (provider?.auth_methods || [])[0];
        const canRelogin = Boolean(
            authMethod
            && !["planned", "unavailable", "disabled"].includes(authMethod.availability),
        );
        const status = account.last_test_status === "ok" ? "已验证" : account.last_test_status === "failed" ? "验证失败" : "待验证";
        return `<article class="storage-account-row ${account.active ? "is-active" : ""}"><div class="storage-record-main"><div class="storage-record-title"><strong>${esc(account.name)}</strong>${account.active ? '<span class="storage-status is-success">当前使用</span>' : ""}</div><p>${esc(account.identity_hint || "未返回账号身份")} · <span class="${account.last_test_status === "ok" ? "text-success" : account.last_test_status === "failed" ? "text-error" : ""}">${status}</span></p></div><div class="storage-row-actions">${account.active ? "" : `<button type="button" class="secondary mini" data-account-action="activate" data-id="${esc(account.account_id)}">设为当前</button>`}<button type="button" class="secondary mini" data-account-action="test" data-id="${esc(account.account_id)}">检测</button>${canRelogin ? `<button type="button" class="secondary mini" data-account-action="relogin" data-id="${esc(account.account_id)}" data-auth-method="${esc(authMethod.kind)}">重新登录</button>` : ""}<button type="button" class="secondary mini" data-account-action="edit" data-id="${esc(account.account_id)}">编辑</button>${account.active ? "" : `<button type="button" class="danger mini" data-account-action="delete" data-id="${esc(account.account_id)}">删除</button>`}</div></article>`;
    }

    function editorHeader(title, backView) {
        return `<header class="storage-editor-header"><div><button type="button" class="storage-back-button" data-setup-action="cancel-editor" aria-label="返回">←</button><h4>${esc(title)}</h4></div><span>${backView === "accounts" ? "账号凭证会加密保存" : "技术 ID 和绑定关系由系统生成"}</span></header>`;
    }

    function providerFor(kind) {
        return (state.data?.providers || []).find((item) => item.provider_kind === kind);
    }

    function formValues(form) {
        return Object.fromEntries(new FormData(form).entries());
    }

    async function submitAccountV1(form) {
        const raw = formValues(form);
        const provider = providerFor(state.editor.provider_kind) || {};
        const authManaged = Boolean((provider.auth_methods || []).length);
        if (authManaged && !state.editor.account_id) {
            throw new Error("该网盘必须通过登录授权添加账号");
        }
        if (authManaged) {
            await request(`/api/admin/provider-accounts/${encodeURIComponent(state.editor.account_id)}`, {
                method: "PATCH",
                body: JSON.stringify({ name: raw.name }),
            });
            state.editor = null;
            await load();
            setNotice("账号名称已保存。", "is-success");
            return;
        }
        const config = {};
        const credentials = {};
        schemaFields(provider.account_schema).forEach((field) => {
            const value = String(raw[field.name] || "");
            if (!value && state.editor.account_id && field.secret) return;
            if (field.secret) credentials[field.name] = value;
            else if (value) config[field.name] = value;
        });
        const payload = { name: raw.name, config, credentials };
        const path = state.editor.account_id
            ? `/api/admin/provider-accounts/${encodeURIComponent(state.editor.account_id)}`
            : "/api/admin/provider-accounts";
        if (!state.editor.account_id) payload.provider_kind = state.editor.provider_kind;
        await request(path, { method: state.editor.account_id ? "PATCH" : "POST", body: JSON.stringify(payload) });
        state.editor = null;
        await load();
        setNotice("账号已保存，请执行检测后再设为当前账号。", "is-success");
    }

    async function submitOpenList(form) {
        const raw = formValues(form);
        const payload = {
            connection_id: state.editor.values?.connection_id,
            name: raw.name,
            base_url: raw.base_url,
            username: raw.username,
            expected_revision: state.editor.values?.revision,
        };
        if (raw.password) payload.password = raw.password;
        try {
            await request("/api/admin/storage-setup/openlist", { method: "PUT", body: JSON.stringify(payload) });
        } catch (error) {
            if (error.code === "OPENLIST_CONNECTION_CONFLICT") {
                state.editor = null;
                await load();
                const conflict = new Error("OpenList 连接已被其他管理员修改，页面已刷新，请重新编辑后保存。", { cause: error });
                conflict.code = error.code;
                throw conflict;
            }
            throw error;
        }
        state.editor = null;
        await load();
        setNotice("OpenList WebDAV 已保存并连接成功。", "is-success");
    }

    async function submitLocation(form) {
        const raw = formValues(form);
        const values = state.editor.values || {};
        const payload = {
            name: raw.name,
            kind: raw.kind || values.kind,
            provider_kind: raw.provider_kind || values.provider_kind || null,
            account_id: raw.account_id || null,
            connection_id: raw.connection_id || null,
            webdav_root: raw.webdav_root || null,
            native_root: raw.native_root || null,
            confirm_identity: raw.confirm_identity === "true",
            expected_revision: values.current_revision,
        };
        const path = values.destination_id ? `/api/admin/storage-setup/locations/${values.destination_id}` : "/api/admin/storage-setup/locations";
        try {
            await request(path, { method: values.destination_id ? "PATCH" : "POST", body: JSON.stringify(payload) });
        } catch (error) {
            if (error.code === "TARGET_CONFLICT" || error.code === "OPENLIST_CONNECTION_CONFLICT") {
                await load();
                const conflict = new Error("保存位置已被其他管理员修改，页面已刷新，请重新确认后保存。", { cause: error });
                conflict.code = error.code;
                throw conflict;
            }
            throw error;
        }
        state.editor = null;
        await load();
        setNotice("保存位置已保存并通过目录检测。", "is-success");
    }

    async function submitRoutes(form) {
        const raw = formValues(form);
        const routes = (state.data.routes || []).map((route) => {
            const overrides = {};
            (state.data.categories || []).forEach((category) => {
                const value = raw[`override_${route.search_source_id}_${category.key}`];
                if (value) overrides[category.key] = value;
            });
            return {
                search_source_id: route.search_source_id,
                default_destination_id: raw[`route_${route.search_source_id}`],
                overrides,
                assignment_revisions: { ...(route.assignment_revisions || {}) },
            };
        });
        try {
            await request("/api/admin/storage-setup/routes", { method: "PUT", body: JSON.stringify({ routes }) });
        } catch (error) {
            if (error.code === "ASSIGNMENT_CONFLICT") {
                await load();
                const conflict = new Error("入库规则已被其他管理员修改，页面已刷新，请重新确认后保存。", { cause: error });
                conflict.code = error.code;
                throw conflict;
            }
            throw error;
        }
        await load();
        setNotice("入库规则已保存，访客提交时会自动选择保存位置。", "is-success");
    }

    async function openAccountEditor(providerKind, accountId) {
        let values = {};
        if (accountId) {
            const detail = await request(`/api/admin/provider-accounts/${encodeURIComponent(accountId)}`);
            values = { ...detail.account, config: detail.config || {} };
        }
        state.editor = { type: "account", provider_kind: providerKind, account_id: accountId || "", values };
        render();
    }

    async function startProviderLogin(providerKind, authMethod, account) {
        setAuthError("");
        const normalizedProvider = String(providerKind || account?.provider_kind || "").trim();
        const provider = providerFor(normalizedProvider);
        const normalizedMethod = String(
            authMethod || (provider?.auth_methods || [])[0]?.kind || ""
        ).trim();
        if (!normalizedProvider || !normalizedMethod) {
            throw new Error("登录配置未加载完整，请刷新页面后重试");
        }
        if (
            state.authSession
            && state.authSession.status === "waiting"
            && state.authSession.provider_kind === normalizedProvider
            && state.authSession.auth_method === normalizedMethod
        ) {
            state.authSessionHidden = false;
            render();
            return;
        }
        const authDefinition = (provider?.auth_methods || []).find((item) => item.kind === normalizedMethod);
        if (schemaFields(authInputSchema(provider, authDefinition)).length) {
            state.loginPrompt = {
                providerKind: normalizedProvider,
                authMethod: normalizedMethod,
                account: account || null,
            };
            render();
            return;
        }
        const result = await request("/api/admin/provider-auth-sessions", {
            method: "POST",
            body: JSON.stringify({
                provider_kind: normalizedProvider,
                auth_method: normalizedMethod,
                payload: account ? { account_id: account.account_id, name: account.name } : {},
            }),
        });
        state.authSession = result.session;
        state.authSessionHidden = false;
        setAuthError("");
        persistAuthSession(state.authSession);
        if (state.authSession?.status === "completed") {
            state.authSession = null;
            setAuthError("");
            persistAuthSession(null);
            await load();
            setNotice("账号登录成功并已设为当前使用。", "is-success");
            return;
        }
        render();
    }

    async function submitProviderLogin(form) {
        setAuthError("");
        const raw = formValues(form);
        const prompt = state.loginPrompt;
        const account = prompt?.account;
        const provider = providerFor(prompt?.providerKind) || {};
        const authDefinition = (provider.auth_methods || []).find((item) => item.kind === prompt?.authMethod) || {};
        const authPayload = {
            account_id: account?.account_id || "",
            name: raw.name,
        };
        schemaFields(authInputSchema(provider, authDefinition)).forEach((field) => {
            authPayload[field.name] = raw[`auth_${field.name}`] || "";
        });
        const result = await request("/api/admin/provider-auth-sessions", {
            method: "POST",
            body: JSON.stringify({
                provider_kind: prompt?.providerKind,
                auth_method: prompt?.authMethod,
                payload: authPayload,
            }),
        });
        state.loginPrompt = null;
        state.authSession = result.session;
        state.authSessionHidden = false;
        setAuthError("");
        persistAuthSession(state.authSession);
        if (state.authSession?.status === "completed") {
            state.authSession = null;
            setAuthError("");
            persistAuthSession(null);
            await load();
            setNotice(`${provider.display_name || "网盘"}账号登录成功，凭证已加密保存。`, "is-success");
            return;
        }
        render();
    }

    function scheduleAuthPolling() {
        if (authPollTimer) window.clearTimeout(authPollTimer);
        authPollTimer = null;
        const session = state.authSession;
        if (
            !session
            || session.status !== "waiting"
            || !["qrcode", "device_code"].includes(session.auth_method)
        ) return;
        const interval = Math.max(2, Number(session.public?.interval || 2));
        authPollTimer = window.setTimeout(async () => {
            if (authPollInFlight) return scheduleAuthPolling();
            authPollInFlight = true;
            try { await verifyProviderLogin(true); }
            catch (error) { reportAuthError(error, "账号授权状态查询失败"); scheduleAuthPolling(); }
            finally { authPollInFlight = false; }
        }, interval * 1000);
    }

    async function verifyProviderLogin(silent = false, payload = {}) {
        const sessionId = state.authSession?.session_id;
        if (!sessionId) return;
        setAuthError("");
        const result = await request(`/api/admin/provider-auth-sessions/${encodeURIComponent(sessionId)}/verify`, {
            method: "POST",
            body: JSON.stringify(payload || {}),
        });
        if (!state.authSession || state.authSession.session_id !== sessionId) return;
        state.authSession = result.session;
        persistAuthSession(state.authSession);
        if (state.authSession.status === "completed") {
            state.authSession = null;
            setAuthError("");
            persistAuthSession(null);
            state.authSessionHidden = false;
            await load();
            setNotice("账号授权成功并已设为当前使用。", "is-success");
            return;
        }
        if (state.authSession.status === "failed" || state.authSession.status === "cancelled") {
            state.authSessionHidden = false;
            setAuthError(state.authSession.message || "账号授权失败");
            render();
            setNotice(state.authSession.message || "账号授权失败", "is-error");
            return;
        }
        setAuthError(state.authSession.public?.error || "");
        render();
        if (!silent) setNotice(state.authSession.message || "请继续完成账号授权", "");
    }

    async function cancelProviderLogin() {
        if (authPollTimer) window.clearTimeout(authPollTimer);
        authPollTimer = null;
        if (state.authSession?.session_id) {
            await request(`/api/admin/provider-auth-sessions/${encodeURIComponent(state.authSession.session_id)}`, { method: "DELETE" });
        }
        state.authSession = null;
        state.authSessionHidden = false;
        setAuthError("");
        persistAuthSession(null);
        render();
        setNotice("账号授权已取消。", "");
    }

    async function openLocationEditor(location) {
        const connections = state.data.connections || [];
        const readyConnection = connections.find((item) => item.enabled && item.last_test_status === "ok");
        const readyAccount = (state.data.accounts || []).find((item) => item.enabled && item.last_test_status === "ok");
        const values = location ? { ...location } : {
            kind: readyConnection && readyAccount ? "provider" : "local",
            connection_id: readyConnection?.connection_id || "",
            provider_kind: readyAccount?.provider_kind || "",
            account_id: readyAccount?.account_id || "",
        };
        state.editor = { type: "location", values };
        await loadDirectoryOptions(values.connection_id);
        render();
    }

    async function loadDirectoryOptions(connectionId) {
        state.directoryOptions = [];
        state.directoryError = "";
        if (!connectionId) return;
        try {
            const result = await request(`/api/admin/openlist/mounts/dirs?connection_id=${encodeURIComponent(connectionId)}`);
            state.directoryOptions = result.items || [];
        } catch (error) {
            state.directoryOptions = [];
            state.directoryError = error.message || "OpenList 目录读取失败";
        }
    }

    host.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (state.busy) return;
        const authSubmission = ["providerLoginForm", "providerChallengeForm"].includes(event.target.id);
        setBusy(true);
        try {
            if (event.target.id === "storageAccountForm") await submitAccountV1(event.target);
            if (event.target.id === "providerLoginForm") await submitProviderLogin(event.target);
            if (event.target.id === "providerChallengeForm") {
                const raw = formValues(event.target);
                await verifyProviderLogin(false, { validate_code: raw.validate_code });
            }
            if (event.target.id === "storageOpenListForm") await submitOpenList(event.target);
            if (event.target.id === "storageLocationForm") await submitLocation(event.target);
            if (event.target.id === "storageRoutesForm") await submitRoutes(event.target);
        } catch (error) {
            if (authSubmission) reportAuthError(error);
            else setNotice(error.message || "保存失败", "is-error");
        } finally {
            setBusy(false);
        }
    });

    host.addEventListener("change", async (event) => {
        const form = event.target.closest("#storageLocationForm");
        if (form && ["kind", "provider_kind", "account_id", "connection_id"].includes(event.target.name)) {
            const raw = formValues(form);
            state.editor.values = { ...state.editor.values, ...raw, kind: raw.kind || state.editor.values.kind };
            if (event.target.name === "provider_kind") state.editor.values.account_id = "";
            if (event.target.name === "connection_id") await loadDirectoryOptions(raw.connection_id);
            render();
            return;
        }
        if (event.target.name?.startsWith("route_")) {
            const sourceId = event.target.name.slice("route_".length);
            const row = event.target.closest(".storage-route-row");
            const summary = row?.querySelector("[data-route-summary]");
            if (summary) summary.textContent = routeChainSummary(sourceId, event.target.value);
        }
    });

    host.addEventListener("click", async (event) => {
        const button = event.target.closest("button");
        if (!button || state.busy) return;
        const view = button.dataset.setupView;
        if (view) {
            state.activeView = view;
            state.editor = null;
            render();
            return;
        }
        const action = button.dataset.setupAction;
        if (action === "cancel-editor") { state.editor = null; render(); return; }
        if (action === "close-preflight") { state.preflight = null; render(); return; }
        if (action === "refresh") { await load("正在刷新配置…"); return; }
        if (action === "preflight") {
            setBusy(true);
            setNotice("正在执行账号、OpenList、目录和规则预检…");
            try {
                state.preflight = await request("/api/admin/storage-setup/preflight", { method: "POST" });
                render();
                setNotice(state.preflight.message, state.preflight.ok ? "is-success" : "is-error");
            } catch (error) { setNotice(error.message, "is-error"); }
            finally { setBusy(false); }
            return;
        }

        const accountAction = button.dataset.accountAction;
        if (accountAction) {
            const account = (state.data.accounts || []).find((item) => item.account_id === button.dataset.id);
            if (accountAction === "edit-new") {
                const provider = providerFor(button.dataset.provider);
                const authMethod = (provider?.auth_methods || [])[0];
                if (authMethod) await startProviderLogin(button.dataset.provider, authMethod.kind, null);
                else await openAccountEditor(button.dataset.provider, "");
                return;
            }
            if (accountAction === "login") {
                const providerKind = button.dataset.provider
                    || button.closest("[data-provider-kind]")?.dataset.providerKind
                    || "";
                setBusy(true);
                try { await startProviderLogin(providerKind, button.dataset.authMethod, null); }
                catch (error) { reportAuthError(error); }
                finally { setBusy(false); }
                return;
            }
            if (accountAction === "edit") { await openAccountEditor(account.provider_kind, account.account_id); return; }
            if (accountAction === "relogin") {
                setBusy(true);
                try { await startProviderLogin(account.provider_kind, button.dataset.authMethod, account); }
                catch (error) { reportAuthError(error); }
                finally { setBusy(false); }
                return;
            }
            if (accountAction === "test") {
                setBusy(true);
                try {
                    const response = await request(`/api/admin/provider-accounts/${encodeURIComponent(account.account_id)}/test`, { method: "POST" });
                    await load();
                    const result = response.result || {};
                    setNotice(result.message || (result.ok ? "账号检测通过。" : "账号检测失败。"), result.ok ? "is-success" : "is-error");
                } catch (error) { setNotice(error.message, "is-error"); }
                finally { setBusy(false); }
                return;
            }
            if (accountAction === "activate") {
                setBusy(true);
                try { await request("/api/admin/storage-setup/accounts", { method: "POST", body: JSON.stringify({ account_id: account.account_id, name: account.name, activate: true }) }); await load(); setNotice("当前账号已切换。", "is-success"); }
                catch (error) { setNotice(error.message, "is-error"); }
                finally { setBusy(false); }
                return;
            }
            if (accountAction === "delete" && window.confirm("确认删除这个账号？")) {
                setBusy(true);
                try {
                    await request(`/api/admin/provider-accounts/${encodeURIComponent(account.account_id)}`, { method: "DELETE" });
                    await load();
                    setNotice("账号已删除。", "is-success");
                }
                catch (error) { setNotice(error.message, "is-error"); }
                finally { setBusy(false); }
            }
            return;
        }

        const authAction = button.dataset.authAction;
        if (authAction) {
            setBusy(true);
            try {
                if (authAction === "verify") await verifyProviderLogin();
                if (authAction === "refresh-captcha") await verifyProviderLogin(false, { refresh_captcha: true });
                if (authAction === "cancel") await cancelProviderLogin();
                if (authAction === "cancel-local") { state.loginPrompt = null; setAuthError(""); render(); }
                if (authAction === "hide") { state.authSessionHidden = true; render(); setNotice("授权窗口已关闭，系统仍在后台等待授权结果。", ""); }
                if (authAction === "close") { state.authSession = null; state.authSessionHidden = false; setAuthError(""); render(); }
            } catch (error) { reportAuthError(error); }
            finally { setBusy(false); }
            return;
        }

        if (button.dataset.openlistAction === "add") {
            state.editor = { type: "openlist", values: {} };
            render();
            return;
        }
        if (button.dataset.openlistAction === "edit") {
            const connection = (state.data.connections || []).find((item) => item.connection_id === button.dataset.id);
            state.editor = { type: "openlist", values: connection || {} };
            render();
            return;
        }
        if (button.dataset.openlistAction === "delete" && window.confirm("确认删除这个 OpenList 连接？连接下存在保存位置时不能删除。")) {
            setBusy(true);
            try {
                await request(`/api/admin/storage-setup/openlist/${encodeURIComponent(button.dataset.id)}`, { method: "DELETE" });
                await load();
                setNotice("OpenList 连接已删除。", "is-success");
            } catch (error) { setNotice(error.message, "is-error"); }
            finally { setBusy(false); }
            return;
        }
        const locationAction = button.dataset.locationAction;
        if (locationAction === "add") { await openLocationEditor(null); return; }
        if (locationAction === "edit") {
            const location = (state.data.locations || []).find((item) => item.destination_id === button.dataset.id);
            await openLocationEditor(location);
            return;
        }
        if (locationAction === "delete" && window.confirm("确认删除这个保存位置？已被入库规则使用的位置不能删除。")) {
            setBusy(true);
            try {
                await request(`/api/admin/storage-setup/locations/${button.dataset.id}`, { method: "DELETE" });
                await load();
                setNotice("保存位置已删除。", "is-success");
            }
            catch (error) { setNotice(error.message, "is-error"); }
            finally { setBusy(false); }
            return;
        }
    });

    load();
})();
