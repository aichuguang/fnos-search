(function () {
    const SELECTOR = "select:not(.sr-only-select)";

    function setOpen(root, open) {
        if (!root) return;
        root.classList.toggle("open", open);
        const trigger = root.querySelector(".fn-select-trigger");
        if (trigger) trigger.setAttribute("aria-expanded", String(open));
    }

    function closeAll(except) {
        document.querySelectorAll(".fn-select.open").forEach((node) => {
            if (node !== except) setOpen(node, false);
        });
    }

    function optionText(option) {
        return option ? option.textContent.trim() : "";
    }

    function rebuildOptions(select, root) {
        const list = root.querySelector(".fn-select-options");
        if (!list) return;
        list.innerHTML = "";
        Array.from(select.options).forEach((option) => {
            const item = document.createElement("button");
            item.type = "button";
            item.className = "fn-select-option";
            item.dataset.value = option.value;
            item.setAttribute("role", "option");
            item.setAttribute("aria-selected", String(option.selected));
            item.disabled = option.disabled;
            item.textContent = optionText(option);
            if (option.selected) item.classList.add("selected");
            item.addEventListener("click", () => {
                if (option.disabled) return;
                select.value = option.value;
                select.dispatchEvent(new Event("change", { bubbles: true }));
                syncSelect(select);
                setOpen(root, false);
                root.querySelector(".fn-select-trigger")?.focus();
            });
            item.addEventListener("keydown", (event) => {
                const options = Array.from(list.querySelectorAll(".fn-select-option:not([disabled])"));
                const currentIndex = options.indexOf(item);
                if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
                    event.preventDefault();
                    const nextIndex = event.key === "Home"
                        ? 0
                        : event.key === "End"
                            ? options.length - 1
                            : (currentIndex + (event.key === "ArrowDown" ? 1 : -1) + options.length) % options.length;
                    options[nextIndex]?.focus();
                } else if (event.key === "Escape") {
                    event.preventDefault();
                    setOpen(root, false);
                    root.querySelector(".fn-select-trigger")?.focus();
                }
            });
            list.appendChild(item);
        });
    }

    function syncSelect(select) {
        const root = select.__fnSelectRoot;
        if (!root) return;
        const trigger = root.querySelector(".fn-select-trigger");
        const current = select.options[select.selectedIndex];
        if (trigger) {
            trigger.textContent = optionText(current) || select.getAttribute("placeholder") || "请选择";
            trigger.disabled = select.disabled;
        }
        root.classList.toggle("disabled", select.disabled);
        root.querySelectorAll(".fn-select-option").forEach((item) => {
            const selected = item.dataset.value === select.value;
            item.classList.toggle("selected", selected);
            item.setAttribute("aria-selected", String(selected));
        });
    }

    function enhance(select) {
        if (!select || select.__fnSelectRoot || select.classList.contains("native-select-hidden")) return;
        const root = document.createElement("div");
        root.className = "fn-select";
        if (select.id) root.dataset.for = select.id;

        const trigger = document.createElement("button");
        trigger.type = "button";
        trigger.className = "fn-select-trigger";
        trigger.setAttribute("aria-haspopup", "listbox");
        trigger.setAttribute("aria-expanded", "false");

        const panel = document.createElement("div");
        panel.className = "fn-select-options";
        panel.setAttribute("role", "listbox");
        panel.id = `fnSelectOptions${Math.random().toString(36).slice(2, 10)}`;
        trigger.setAttribute("aria-controls", panel.id);
        const associatedLabel = select.id ? Array.from(document.querySelectorAll("label[for]")).find((label) => label.htmlFor === select.id) : null;
        const accessibleName = select.getAttribute("aria-label") || associatedLabel?.textContent?.trim();
        if (accessibleName) trigger.setAttribute("aria-label", accessibleName);

        root.appendChild(trigger);
        root.appendChild(panel);
        select.insertAdjacentElement("afterend", root);
        select.classList.add("native-select-hidden");
        select.tabIndex = -1;
        select.setAttribute("aria-hidden", "true");
        select.__fnSelectRoot = root;

        trigger.addEventListener("click", () => {
            if (select.disabled) return;
            const nextOpen = !root.classList.contains("open");
            closeAll(root);
            rebuildOptions(select, root);
            syncSelect(select);
            setOpen(root, nextOpen);
            if (nextOpen) {
                window.setTimeout(() => root.querySelector(".fn-select-option.selected:not([disabled]), .fn-select-option:not([disabled])")?.focus(), 0);
            }
        });
        trigger.addEventListener("keydown", (event) => {
            if (event.key === "Escape") {
                setOpen(root, false);
            }
            if (event.key === "ArrowDown" || event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                if (!root.classList.contains("open")) trigger.click();
            }
        });
        select.addEventListener("change", () => syncSelect(select));
        const observer = new MutationObserver(() => {
            rebuildOptions(select, root);
            syncSelect(select);
        });
        observer.observe(select, {
            attributes: true,
            attributeFilter: ["disabled", "label", "selected", "value"],
            childList: true,
            characterData: true,
            subtree: true,
        });
        select.__fnSelectObserver = observer;
        rebuildOptions(select, root);
        syncSelect(select);
    }

    function enhanceAll() {
        document.querySelectorAll(SELECTOR).forEach((select) => {
            if (select.__fnSelectRoot) {
                rebuildOptions(select, select.__fnSelectRoot);
                syncSelect(select);
            } else {
                enhance(select);
            }
        });
    }

    function syncOne(target) {
        const select = typeof target === "string" ? document.getElementById(target) : target;
        if (!select || !select.matches || !select.matches(SELECTOR)) return;
        if (!select.__fnSelectRoot) enhance(select);
        if (select.__fnSelectRoot) {
            rebuildOptions(select, select.__fnSelectRoot);
            syncSelect(select);
        }
    }

    document.addEventListener("click", (event) => {
        if (!event.target.closest(".fn-select")) closeAll();
    });
    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape") closeAll();
    });
    document.addEventListener("DOMContentLoaded", enhanceAll);
    window.refreshCustomSelects = enhanceAll;
    window.syncCustomSelect = syncOne;
})();
