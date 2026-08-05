(function () {
    const DIALOG_ID = "uiDialogRoot";
    const FOCUSABLE_SELECTOR = [
        "button:not([disabled])",
        "input:not([disabled])",
        "textarea:not([disabled])",
        "select:not([disabled])",
        "a[href]",
        "[tabindex]:not([tabindex='-1'])",
    ].join(",");

    function escapeHtml(value) {
        return String(value ?? "")
            .replaceAll("&", "&amp;")
            .replaceAll("<", "&lt;")
            .replaceAll(">", "&gt;")
            .replaceAll('"', "&quot;")
            .replaceAll("'", "&#039;");
    }

    function ensureDialogRoot() {
        let root = document.getElementById(DIALOG_ID);
        if (root) return root;
        root = document.createElement("div");
        root.id = DIALOG_ID;
        root.className = "ui-dialog-overlay hidden";
        root.innerHTML = `
            <section class="ui-dialog" role="dialog" aria-modal="true" aria-labelledby="uiDialogTitle" aria-describedby="uiDialogMessage">
                <header class="ui-dialog-head">
                    <div>
                        <span class="ui-dialog-eyebrow" id="uiDialogEyebrow">操作确认</span>
                        <h2 id="uiDialogTitle">确认操作</h2>
                    </div>
                    <button class="ui-dialog-close secondary mini icon-only" type="button" data-ui-dialog-cancel aria-label="关闭">×</button>
                </header>
                <div class="ui-dialog-body">
                    <p id="uiDialogMessage"></p>
                    <label class="ui-dialog-prompt hidden" id="uiDialogPromptWrap">
                        <span id="uiDialogPromptLabel">请输入</span>
                        <input id="uiDialogPromptInput" type="text" autocomplete="off">
                    </label>
                    <div class="ui-dialog-error hidden" id="uiDialogError" role="alert"></div>
                </div>
                <footer class="ui-dialog-actions">
                    <button class="secondary" type="button" data-ui-dialog-cancel>取消</button>
                    <button type="button" data-ui-dialog-confirm>确认</button>
                </footer>
            </section>
        `;
        document.body.appendChild(root);
        return root;
    }

    function focusableWithin(root) {
        return Array.from(root.querySelectorAll(FOCUSABLE_SELECTOR)).filter((item) => {
            const style = window.getComputedStyle(item);
            return style.display !== "none" && style.visibility !== "hidden";
        });
    }

    function toneText(tone) {
        if (tone === "danger") return "危险操作";
        if (tone === "warning") return "需要确认";
        if (tone === "success") return "操作完成";
        return "提示";
    }

    function showDialog(options = {}) {
        const type = options.type || "confirm";
        const tone = options.tone || (type === "alert" ? "info" : "warning");
        const root = ensureDialogRoot();
        const dialog = root.querySelector(".ui-dialog");
        const title = root.querySelector("#uiDialogTitle");
        const eyebrow = root.querySelector("#uiDialogEyebrow");
        const message = root.querySelector("#uiDialogMessage");
        const promptWrap = root.querySelector("#uiDialogPromptWrap");
        const promptLabel = root.querySelector("#uiDialogPromptLabel");
        const promptInput = root.querySelector("#uiDialogPromptInput");
        const errorBox = root.querySelector("#uiDialogError");
        const confirmBtn = root.querySelector("[data-ui-dialog-confirm]");
        const cancelBtns = root.querySelectorAll("[data-ui-dialog-cancel]");
        const previousFocus = document.activeElement;

        root.className = `ui-dialog-overlay tone-${tone}`;
        dialog.className = `ui-dialog tone-${tone}`;
        eyebrow.textContent = options.eyebrow || toneText(tone);
        title.textContent = options.title || (type === "prompt" ? "请输入" : type === "alert" ? "提示" : "确认操作");
        message.innerHTML = escapeHtml(options.message || "").replaceAll("\n", "<br>");
        errorBox.textContent = "";
        errorBox.classList.add("hidden");
        confirmBtn.textContent = options.confirmText || (type === "alert" ? "知道了" : "确认");
        confirmBtn.classList.toggle("danger", tone === "danger");
        confirmBtn.classList.toggle("secondary", tone !== "danger" && type === "alert");
        root.querySelector(".ui-dialog-actions [data-ui-dialog-cancel]").classList.toggle("hidden", type === "alert");

        if (type === "prompt") {
            promptWrap.classList.remove("hidden");
            promptLabel.textContent = options.promptLabel || "输入内容";
            promptInput.value = options.defaultValue ?? "";
            promptInput.placeholder = options.placeholder || "";
        } else {
            promptWrap.classList.add("hidden");
            promptInput.value = "";
        }

        return new Promise((resolve) => {
            let settled = false;

            function cleanup(result) {
                if (settled) return;
                settled = true;
                root.classList.add("hidden");
                root.removeEventListener("click", onMaskClick);
                document.removeEventListener("keydown", onKeydown, true);
                confirmBtn.removeEventListener("click", onConfirm);
                cancelBtns.forEach((button) => button.removeEventListener("click", onCancel));
                if (previousFocus && typeof previousFocus.focus === "function") {
                    window.setTimeout(() => previousFocus.focus({ preventScroll: true }), 0);
                }
                resolve(result);
            }

            function onCancel() {
                cleanup(type === "confirm" ? false : null);
            }

            function onConfirm() {
                if (type === "prompt") {
                    const value = promptInput.value.trim();
                    if (options.required && !value) {
                        errorBox.textContent = options.requiredMessage || "该项不能为空";
                        errorBox.classList.remove("hidden");
                        promptInput.focus();
                        return;
                    }
                    cleanup(value);
                    return;
                }
                cleanup(true);
            }

            function onMaskClick(event) {
                if (event.target === root) onCancel();
            }

            function onKeydown(event) {
                if (root.classList.contains("hidden")) return;
                if (event.key === "Escape") {
                    event.preventDefault();
                    onCancel();
                    return;
                }
                if (event.key === "Enter" && !event.shiftKey) {
                    event.preventDefault();
                    onConfirm();
                    return;
                }
                if (event.key !== "Tab") return;
                const focusables = focusableWithin(dialog);
                if (!focusables.length) return;
                const first = focusables[0];
                const last = focusables[focusables.length - 1];
                if (event.shiftKey && document.activeElement === first) {
                    event.preventDefault();
                    last.focus();
                } else if (!event.shiftKey && document.activeElement === last) {
                    event.preventDefault();
                    first.focus();
                }
            }

            root.addEventListener("click", onMaskClick);
            document.addEventListener("keydown", onKeydown, true);
            confirmBtn.addEventListener("click", onConfirm);
            cancelBtns.forEach((button) => button.addEventListener("click", onCancel));
            root.classList.remove("hidden");
            window.setTimeout(() => {
                if (type === "prompt") {
                    promptInput.focus();
                    promptInput.select();
                } else {
                    confirmBtn.focus();
                }
            }, 0);
        });
    }

    window.showConfirmDialog = function showConfirmDialog(options = {}) {
        return showDialog({ ...options, type: "confirm" });
    };

    window.showPromptDialog = function showPromptDialog(options = {}) {
        return showDialog({ ...options, type: "prompt" });
    };

    window.showAlertDialog = function showAlertDialog(options = {}) {
        return showDialog({ ...options, type: "alert" });
    };
})();
