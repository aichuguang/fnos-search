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
})();
