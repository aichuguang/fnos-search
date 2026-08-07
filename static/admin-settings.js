(function () {
    function toBoolean(value, fallback = false) {
        if (value === undefined || value === null || value === "") return fallback;
        if (typeof value === "boolean") return value;
        return ["1", "true", "yes", "on", "y", "是", "启用"].includes(String(value).trim().toLowerCase());
    }

    function listToText(value) {
        return Array.isArray(value) ? value.join("\n") : String(value || "");
    }

    function textToList(value) {
        return String(value || "").split(/[\n,，]+/).map((item) => item.trim()).filter(Boolean);
    }

    function parseNumberList(value) {
        return textToList(value).map((item) => Number(item)).filter((item) => item > 0);
    }

    function normalizeTemplateRoot(value, options = {}) {
        const { leadingSlash = false } = options;
        const text = String(value || "").trim().replaceAll("\\", "/").replace(/\/+/g, "/");
        const stripped = text.replace(/^\/+|\/+$/g, "");
        if (!stripped) return leadingSlash ? "/" : "";
        return leadingSlash ? `/${stripped}` : stripped;
    }

    function joinTemplatePath(root, label, options = {}) {
        const { leadingSlash = false } = options;
        const cleanLabel = String(label || "").trim().replaceAll("\\", "/").replace(/^\/+|\/+$/g, "");
        const cleanRoot = normalizeTemplateRoot(root);
        const joined = cleanRoot ? `${cleanRoot}/${cleanLabel}` : cleanLabel;
        return leadingSlash ? `/${joined.replace(/^\/+/, "")}` : joined;
    }

    function inferCategoryDir(item = {}, fallbackLabel = "") {
        const candidates = [
            item.cloud139_target_path,
            item.cloud139_fnos_target_path,
            item.openlist_root_path,
            item.quark_save_path,
            item.sixpan_save_path,
            item.sixpan_fnos_target_path,
            item.label,
            fallbackLabel,
        ];
        for (const value of candidates) {
            const text = String(value || "").trim().replaceAll("\\", "/").replace(/\/+/g, "/").replace(/\/+$/g, "");
            if (!text) continue;
            const part = text.split("/").filter(Boolean).pop();
            if (part) return part;
        }
        return fallbackLabel;
    }

    function inferCategoryRoot(categories, field, fallback = "", options = {}) {
        const { leadingSlash = false, order = [] } = options;
        for (const [key, fallbackLabel] of order) {
            const item = categories?.[key] || {};
            const label = String(inferCategoryDir(item, item.label || fallbackLabel) || fallbackLabel).trim();
            const value = String(item?.[field] || "").trim().replaceAll("\\", "/").replace(/\/+/g, "/");
            if (!value || !label) continue;
            const normalizedValue = value.replace(/\/+$/g, "");
            const normalizedLabel = label.replace(/^\/+|\/+$/g, "");
            if (normalizedValue === normalizedLabel || normalizedValue === `/${normalizedLabel}`) return leadingSlash ? "/" : "";
            const suffix = `/${normalizedLabel}`;
            if (normalizedValue.endsWith(suffix)) {
                const root = normalizedValue.slice(0, -suffix.length) || (value.startsWith("/") ? "/" : "");
                return normalizeTemplateRoot(root, { leadingSlash });
            }
        }
        return fallback;
    }

    function setValue(id, value, root = document) {
        const element = root.getElementById(id);
        if (element) element.value = value ?? "";
    }

    function setChecked(id, value, fallback = false, root = document) {
        const element = root.getElementById(id);
        if (element) element.checked = toBoolean(value, fallback);
    }

    function setSecret(id, configuredValue, root = document) {
        const element = root.getElementById(id);
        if (!element) return;
        element.value = "";
        element.dataset.secretConfigured = configuredValue ? "1" : "0";
        element.placeholder = configuredValue ? "已配置，留空不修改" : "未配置";
    }

    function changedConfigPatch(candidate, baseline) {
        if (!candidate || typeof candidate !== "object" || Array.isArray(candidate)) return {};
        const previous = baseline && typeof baseline === "object" && !Array.isArray(baseline) ? baseline : {};
        const patch = {};
        Object.entries(candidate).forEach(([key, value]) => {
            const oldValue = previous[key];
            if (value && typeof value === "object" && !Array.isArray(value)) {
                const nested = changedConfigPatch(value, oldValue);
                if (Object.keys(nested).length) patch[key] = nested;
                return;
            }
            if (Array.isArray(value)) {
                const oldArray = Array.isArray(oldValue) ? oldValue : [];
                if (JSON.stringify(value) !== JSON.stringify(oldArray)) patch[key] = value;
                return;
            }
            if (value === "" && ["***", "******", "已配置，留空不修改"].includes(String(oldValue || ""))) return;
            if (value !== oldValue) patch[key] = value;
        });
        return patch;
    }

    function createFormReader(root = document) {
        const element = (id) => root.getElementById(id);
        const value = (id) => element(id)?.value?.trim() || "";
        return {
            value,
            number(id, fallback = 0) {
                const number = Number(value(id));
                return Number.isFinite(number) ? number : fallback;
            },
            checked(id) {
                return Boolean(element(id)?.checked);
            },
        };
    }

    function collectSearchProviders(items, root = document) {
        const rows = Array.from(root.querySelectorAll("[data-search-provider]"));
        return (items || []).map((item) => {
            const row = rows.find((candidate) => candidate.dataset.searchProvider === String(item.key));
            return {
                key: item.key,
                enabled: Boolean(row?.querySelector("[data-provider-enabled]")?.checked),
                priority: Number(row?.querySelector("[data-provider-priority]")?.value || item.priority || 100),
            };
        });
    }

    function buildCategoryPatch(rows) {
        const categories = {};
        (rows || []).forEach((row) => {
            categories[row.key] = {
                label: row.label,
                quark_save_path: row.quark_save_path,
                sixpan_save_path: row.sixpan_save_path,
                openlist_root_path: row.openlist_root_path,
                cloud139_target_path: row.cloud139_target_path,
                cloud139_fnos_target_path: row.cloud139_fnos_target_path,
                fnos_lib: row.label,
            };
        });
        return categories;
    }

    window.FnosAdminSettings = Object.freeze({
        toBoolean,
        listToText,
        textToList,
        parseNumberList,
        normalizeTemplateRoot,
        joinTemplatePath,
        inferCategoryDir,
        inferCategoryRoot,
        setValue,
        setChecked,
        setSecret,
        changedConfigPatch,
        createFormReader,
        collectSearchProviders,
        buildCategoryPatch,
    });

    // --- 存储目标管理（阶段 5）：自包含渲染与测试，复用 window.FnosUI.requestJson。 ---
    const STORAGE_CAPABILITY_LABELS = {
        supports_native_save: "原生转存",
        supports_rclone_dest: "rclone 目标",
        supports_relay_upload: "自定义上传",
        supports_quota: "配额",
        supports_organizer_access: "Organizer 可见",
        supports_atomic_rename: "原子重命名",
        supports_server_side_copy: "服务端复制",
        supports_checksum: "校验和",
    };

    async function requestJson(path, options = {}) {
        return window.FnosUI.requestJson(path, options);
    }

    function storageStatusBox(message, className = "") {
        const box = document.getElementById("storageTargetsStatus");
        if (!box) return;
        box.className = `notice-box compact ${className}`.trim();
        box.textContent = message;
    }

    function storageEscape(value) {
        return window.FnosUI.escapeHtml(value ?? "");
    }

    function renderStorageTargets(data = {}) {
        const body = document.getElementById("storageTargetsBody");
        if (!body) return;
        const targets = Array.isArray(data.targets) ? data.targets : [];
        if (data.enabled === false) {
            body.innerHTML = `<div class="empty-cell">存储目标功能未启用（storage.enabled=false）。</div>`;
            return;
        }
        if (!targets.length) {
            body.innerHTML = `<div class="empty-cell">尚未配置存储目标。</div>`;
            return;
        }
        body.innerHTML = `
            <div class="table-wrap">
                <table class="data-table table-storage-targets">
                    <thead>
                        <tr>
                            <th>目标</th>
                            <th>驱动</th>
                            <th>能力</th>
                            <th>状态</th>
                            <th>操作</th>
                        </tr>
                    </thead>
                    <tbody>
                        ${targets.map((target) => renderStorageTargetRow(target)).join("")}
                    </tbody>
                </table>
            </div>
        `;
    }

    function renderStorageTargetRow(target = {}) {
        const badges = [];
        if (target.default) badges.push(`<span class="pill ok">默认</span>`);
        if (target.guest_selectable) badges.push(`<span class="pill">访客可选</span>`);
        if (!target.enabled) badges.push(`<span class="pill">已停用</span>`);
        const statusControls = `
            <label class="settings-row"><span>启用</span><input type="checkbox" data-storage-enabled ${target.enabled ? "checked" : ""}></label>
            <label class="settings-row"><span>访客可选</span><input type="checkbox" data-storage-guest ${target.guest_selectable ? "checked" : ""}></label>
        `;
        const capabilities = Object.entries(STORAGE_CAPABILITY_LABELS)
            .filter(([key]) => Boolean(target.capabilities?.[key]))
            .map(([, label]) => `<span class="storage-capability">${storageEscape(label)}</span>`)
            .join("");
        const driverCell = target.driver_error
            ? `<code>${storageEscape(target.driver || "-")}</code><span class="storage-driver-error">${storageEscape(target.driver_error)}</span>`
            : `<code>${storageEscape(target.driver || "-")}</code>`;
        return `
            <tr data-destination-id="${storageEscape(target.destination_id || "")}">
                <td>
                    <strong>${storageEscape(target.name || target.destination_id || "-")}</strong>
                    <small><code>${storageEscape(target.destination_id || "")}</code> ${badges.join(" ")}</small>
                </td>
                <td>${driverCell}</td>
                <td>${capabilities || `<span class="muted">-</span>`}</td>
                <td>${statusControls}</td>
                <td>
                    <button class="secondary mini" type="button" data-storage-save="${storageEscape(target.destination_id || "")}">保存</button>
                    <button class="secondary mini" type="button" data-storage-test="${storageEscape(target.destination_id || "")}">连接检测</button>
                    <div class="storage-test-result" hidden></div>
                </td>
            </tr>
        `;
    }

    async function loadStorageTargets() {
        const body = document.getElementById("storageTargetsBody");
        if (!body) return;
        storageStatusBox("正在读取存储目标。");
        try {
            const data = await requestJson("/api/admin/settings/storage/status", { allowFailure: true });
            if (data.success === false) throw new Error(data.message || "读取存储目标失败");
            renderStorageTargets(data);
            const count = Array.isArray(data.targets) ? data.targets.length : 0;
            storageStatusBox(data.enabled === false ? "存储目标功能未启用。" : `共 ${count} 个存储目标。`);
            bindStorageTestButtons();
            bindStorageSaveButtons();
        } catch (error) {
            storageStatusBox(error?.message || "读取存储目标失败", "status-error");
            body.innerHTML = "";
        }
    }

    function bindStorageTestButtons() {
        document.querySelectorAll("[data-storage-test]").forEach((button) => {
            button.replaceWith(button.cloneNode(true));
        });
        document.querySelectorAll("[data-storage-test]").forEach((button) => {
            button.addEventListener("click", () => testStorageTarget(button.dataset.storageTest, button).catch((error) => {
                renderStorageTestResult(button, { ok: false, errors: [error?.message || "测试失败"] });
            }));
        });
    }

    function bindStorageSaveButtons() {
        document.querySelectorAll("[data-storage-save]").forEach((button) => {
            button.addEventListener("click", () => saveStorageTarget(button).catch((error) => {
                storageStatusBox(error?.message || "保存存储目标失败", "status-error");
            }));
        });
    }

    async function saveStorageTarget(button) {
        const row = button.closest("tr");
        const destinationId = button.dataset.storageSave || row?.dataset.destinationId || "";
        if (!destinationId || !row) return;
        button.disabled = true;
        try {
            const data = await requestJson("/api/admin/settings/storage", {
                method: "PATCH",
                body: JSON.stringify({
                    destination_id: destinationId,
                    enabled: Boolean(row.querySelector("[data-storage-enabled]")?.checked),
                    guest_selectable: Boolean(row.querySelector("[data-storage-guest]")?.checked),
                }),
                allowFailure: true,
            });
            if (data.success === false) throw new Error(data.message || "保存存储目标失败");
            storageStatusBox(data.message || `存储目标 ${destinationId} 已保存`);
            await loadStorageTargets();
        } finally {
            button.disabled = false;
        }
    }

    async function testStorageTarget(destinationId, button) {
        if (!destinationId) return;
        button.disabled = true;
        const originalText = button.textContent;
        button.textContent = "测试中...";
        renderStorageTestResult(button, null);
        try {
            const data = await requestJson("/api/admin/settings/storage/test", {
                method: "POST",
                body: JSON.stringify({ destination_id: destinationId }),
                allowFailure: true,
            });
            renderStorageTestResult(button, data);
        } finally {
            button.disabled = false;
            button.textContent = originalText;
        }
    }

    function renderStorageTestResult(button, data) {
        const row = button.closest("tr");
        const box = row?.querySelector(".storage-test-result");
        if (!box) return;
        if (data === null) {
            box.hidden = true;
            box.innerHTML = "";
            return;
        }
        const errors = Array.isArray(data.errors) ? data.errors : [];
        const checks = Array.isArray(data.checks) ? data.checks : [];
        const ok = data.ok === true && data.success !== false && !errors.length;
        box.hidden = false;
        box.className = `storage-test-result ${ok ? "ok" : "status-error"}`;
        box.innerHTML = ok
            ? `<span class="pill ok">通过</span><span>${storageEscape(data.message || "目标连接检测通过")}</span>${
                checks.length ? `<ul>${checks.map((item) => `<li>${storageEscape(item.name || "检查项")}：${storageEscape(item.message || (item.ok ? "通过" : "未通过"))}</li>`).join("")}</ul>` : ""
              }`
            : `<span class="pill">未通过</span><span>${storageEscape(data.message || "目标配置检查未通过")}</span>${
                errors.length ? `<ul>${errors.map((err) => `<li>${storageEscape(err)}</li>`).join("")}</ul>` : ""
              }`;
    }

    function bindStorageTargetsSection() {
        document.getElementById("storageTargetsRefreshBtn")?.addEventListener("click", () => loadStorageTargets().catch((error) => storageStatusBox(error?.message || "读取存储目标失败", "status-error")));
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", () => {
            if (document.getElementById("storageTargetsBody")) {
                bindStorageTargetsSection();
                loadStorageTargets().catch(() => {});
            }
        });
    } else if (document.getElementById("storageTargetsBody")) {
        bindStorageTargetsSection();
        loadStorageTargets().catch(() => {});
    }
})();

/* ============ V1.0 云盘与入库工作区（账号 / OpenList / 存储目标 / 入库规则） ============ */
(function () {
  "use strict";
  if (!window.FnosUI || !window.FnosUI.requestJson) return;
  const api = (path, opts) => window.FnosUI.requestJson(path, Object.assign({ allowFailure: true }, opts || {}));
  const esc = (v) => window.FnosUI.escapeHtml(v ?? "");

  function mount() {
    const anchor = document.getElementById("storageTargetsBody")?.closest(".settings-card")
      || document.querySelector("#storageTargetsRefreshBtn")?.closest(".settings-card")
      || document.querySelector(".settings-section-block[data-settings-section~='clouds']");
    const host = anchor ? anchor.parentElement : document.body;
    const panel = document.createElement("section");
    panel.className = "settings-card";
    panel.id = "v1Workspace";
    panel.innerHTML = `
      <h3>云盘与入库（V1.0）</h3>
      <div class="v1-grid" style="display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));">
        ${card("账号", "v1Accounts")}
        ${card("OpenList 挂载", "v1Openlist")}
        ${card("存储目标", "v1Targets")}
        ${card("入库规则", "v1Rules")}
      </div>`;
    host.insertBefore(panel, anchor ? anchor.nextSibling : null);
    loadAccounts(); loadOpenlist(); loadTargets(); loadRules();
  }
  function card(title, id) {
    return `<section class="v1-card" style="border:1px solid #333;border-radius:8px;padding:10px;">
      <h4 style="margin:0 0 8px;">${esc(title)}</h4><div id="${id}" class="v1-body" style="font-size:13px;"></div></section>`;
  }

  async function loadAccounts() {
    const box = document.getElementById("v1Accounts"); if (!box) return;
    const data = await api("/api/admin/provider-accounts");
    const rows = (data.accounts || []).map((a) => `<div>${esc(a.name || a.account_id)} · ${esc(a.provider_kind)} · ${esc(a.identity_hint || "")} · ${esc(a.last_test_status || "untested")} ${a.enabled ? "" : "(停用)"}</div>`).join("");
    box.innerHTML = `<div style="margin-bottom:6px;"><input placeholder="账号名" id="v1acctName" style="width:110px;"><select id="v1acctKind"><option>mobile</option><option>quark</option><option>tianyi</option><option>sixpan</option></select> <button class="secondary mini" id="v1acctAdd">新增</button></div>${rows || '<span class="muted">无账号</span>'}`;
    document.getElementById("v1acctAdd")?.addEventListener("click", async () => {
      const name = document.getElementById("v1acctName").value.trim() || "未命名";
      const kind = document.getElementById("v1acctKind").value;
      const r = await api("/api/admin/provider-accounts", { method: "POST", body: JSON.stringify({ name, provider_kind: kind }) });
      loadAccounts();
    });
  }

  async function loadTargets() {
    const box = document.getElementById("v1Targets"); if (!box) return;
    const data = await api("/api/admin/storage-targets");
    const rows = (data.targets || []).map((t) => `<div>${esc(t.name || t.destination_id)} · ${esc(t.provider_kind || "")} · ${esc(t.target_kind || "")} ${t.enabled ? "" : "(停用)"}</div>`).join("");
    box.innerHTML = `<div style="margin-bottom:6px;"><input placeholder="目标ID" id="v1tgtId" style="width:110px;"><input placeholder="名称" id="v1tgtName" style="width:110px;"> <button class="secondary mini" id="v1tgtAdd">新增</button></div>${rows || '<span class="muted">无目标</span>'}`;
    document.getElementById("v1tgtAdd")?.addEventListener("click", async () => {
      const destination_id = (document.getElementById("v1tgtId").value || "").trim();
      const name = (document.getElementById("v1tgtName").value || "").trim() || destination_id;
      if (!destination_id) return;
      await api("/api/admin/storage-targets", { method: "POST", body: JSON.stringify({ destination_id, name, provider_kind: "generic", target_kind: "openlist_generic" }) });
      loadTargets();
    });
  }

  async function loadOpenlist() {
    const box = document.getElementById("v1Openlist"); if (!box) return;
    const data = await api("/api/admin/openlist/mounts");
    const rows = (data.items || []).map((m) => `<div>${esc(m.name || m.mount_id)} · ${esc(m.namespace_id || "")} ${m.readable ? "R" : ""}${m.writable ? "W" : ""}</div>`).join("");
    box.innerHTML = rows || '<span class="muted">无挂载（先到高级配置配好 OpenList）</span>';
  }

  async function loadRules() {
    const box = document.getElementById("v1Rules"); if (!box) return;
    const data = await api("/api/admin/destination-rules");
    const rows = (data.rules || []).map((r) => `<div>${esc(r.source_provider_kind)}/${esc(r.source_form)} → ${esc(r.destination_id)} ${r.is_default ? "(默认)" : ""}</div>`).join("");
    box.innerHTML = rows || '<span class="muted">无规则（为每个搜索源配置默认保存目标）</span>';
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", mount);
  else mount();
})();
