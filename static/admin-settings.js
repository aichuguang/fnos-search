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
        return String(value || "").split(/[\n,，]/).map((item) => item.trim()).filter(Boolean);
    }

    function parseNumberList(value) {
        return textToList(value).map((item) => Number(item)).filter((item) => item > 0);
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
        element.placeholder = configuredValue ? "已配置，留空保持不变" : "请输入";
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
            if (value === "" && ["***", "******", "已配置，留空保持不变"].includes(String(oldValue || ""))) return;
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

    window.FnosAdminSettings = Object.freeze({
        toBoolean,
        listToText,
        textToList,
        parseNumberList,
        setValue,
        setChecked,
        setSecret,
        changedConfigPatch,
        createFormReader,
        collectSearchProviders,
    });
})();
