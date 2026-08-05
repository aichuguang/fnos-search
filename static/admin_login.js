const $ = (id) => document.getElementById(id);

function toast(message, type = "") {
    window.FnosUI.showToast("toast", message, type);
}

async function api(path, options = {}) {
    const { allowFailure = false, headers = {}, ...fetchOptions } = options;
    return window.FnosUI.requestJson(path, {
        allowFailure,
        headers: { "Content-Type": "application/json", ...headers },
        ...fetchOptions,
    });
}

async function checkSession() {
    const data = await api("/api/admin/session", { allowFailure: true });
    if (data.logged_in) {
        window.location.href = "/admin";
    }
}

async function login() {
    const username = $("adminUsername").value.trim();
    const password = $("adminPassword").value;
    const status = $("adminLoginStatus");
    if (!username || !password) {
        toast("请输入用户名和密码", "error");
        return;
    }
    status.textContent = "登录中...";
    try {
        await api("/api/admin/login", {
            method: "POST",
            body: JSON.stringify({ username, password }),
        });
        status.textContent = "登录成功，正在进入后台...";
        window.location.href = "/admin";
    } catch (error) {
        status.textContent = error.message;
        toast(error.message, "error");
    }
}

document.addEventListener("DOMContentLoaded", () => {
    $("adminLoginForm")?.addEventListener("submit", (event) => {
        event.preventDefault();
        login();
    });
    checkSession().catch(() => {});
});
