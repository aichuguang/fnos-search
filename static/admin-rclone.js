(function () {
    function create(context) {
        const {
            state,
            getElement,
            api,
            toast,
            escapeHtml,
            statusPill,
            statusText,
            formatDate,
            normalizePagination,
            renderPager,
            renderTimelineRawData,
            openRawLogModal,
            confirmDialog,
            loadOverview,
            showJobDetail,
        } = context;

        let taskLogDatePickersInitialized = false;
        let openDatePicker = null;
        const taskLogDatePickers = new Map();

        function padDatePart(value) {
            return String(value).padStart(2, "0");
        }

        function formatDateValue(date) {
            return `${date.getFullYear()}-${padDatePart(date.getMonth() + 1)}-${padDatePart(date.getDate())}`;
        }

        function parseDateValue(value) {
            const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(value || ""));
            if (!match) return null;
            const date = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]), 12);
            if (
                date.getFullYear() !== Number(match[1])
                || date.getMonth() !== Number(match[2]) - 1
                || date.getDate() !== Number(match[3])
            ) return null;
            return date;
        }

        function closeTaskLogDatePicker(picker, options = {}) {
            if (!picker) return;
            picker.popover.hidden = true;
            picker.root.classList.remove("open");
            picker.trigger.setAttribute("aria-expanded", "false");
            if (openDatePicker === picker) openDatePicker = null;
            if (options.restoreFocus) picker.trigger.focus();
        }

        function renderTaskLogDatePicker(picker) {
            const year = picker.viewDate.getFullYear();
            const month = picker.viewDate.getMonth();
            const selectedValue = picker.input.value;
            const todayValue = formatDateValue(new Date());
            const firstDay = new Date(year, month, 1, 12);
            const mondayOffset = (firstDay.getDay() + 6) % 7;
            const firstVisibleDate = new Date(year, month, 1 - mondayOffset, 12);

            picker.monthLabel.textContent = `${year} 年 ${month + 1} 月`;
            picker.days.innerHTML = Array.from({ length: 42 }, (_, index) => {
                const date = new Date(firstVisibleDate);
                date.setDate(firstVisibleDate.getDate() + index);
                const value = formatDateValue(date);
                const classes = ["date-picker-day"];
                if (date.getMonth() !== month) classes.push("is-outside");
                if (value === todayValue) classes.push("is-today");
                if (value === selectedValue) classes.push("is-selected");
                return `<button class="${classes.join(" ")}" type="button" data-date-day="${value}" aria-label="${value}" aria-pressed="${value === selectedValue}">${date.getDate()}</button>`;
            }).join("");
        }

        function applyTaskLogDatePickerValue(picker, value) {
            picker.input.value = value;
            picker.valueLabel.textContent = value || "不限";
            picker.trigger.classList.toggle("has-value", Boolean(value));
            picker.input.dispatchEvent(new Event("change", { bubbles: true }));
            if (value) {
                const selectedDate = parseDateValue(value);
                if (selectedDate) picker.viewDate = new Date(selectedDate.getFullYear(), selectedDate.getMonth(), 1, 12);
            }
        }

        function setTaskLogDatePickerValue(picker, value) {
            applyTaskLogDatePickerValue(picker, value);
            const startPicker = taskLogDatePickers.get("start");
            const endPicker = taskLogDatePickers.get("end");
            if (value && picker.role === "start" && endPicker?.input.value && value > endPicker.input.value) {
                applyTaskLogDatePickerValue(endPicker, value);
            }
            if (value && picker.role === "end" && startPicker?.input.value && value < startPicker.input.value) {
                applyTaskLogDatePickerValue(startPicker, value);
            }
            taskLogDatePickers.forEach(renderTaskLogDatePicker);
        }

        function openTaskLogDatePicker(picker) {
            if (openDatePicker && openDatePicker !== picker) closeTaskLogDatePicker(openDatePicker);
            const selectedDate = parseDateValue(picker.input.value) || new Date();
            picker.viewDate = new Date(selectedDate.getFullYear(), selectedDate.getMonth(), 1, 12);
            renderTaskLogDatePicker(picker);
            picker.popover.hidden = false;
            picker.root.classList.add("open");
            picker.trigger.setAttribute("aria-expanded", "true");
            openDatePicker = picker;
        }

        function initTaskLogDatePickers() {
            if (taskLogDatePickersInitialized) return;
            const roots = Array.from(document.querySelectorAll("[data-date-picker]"));
            if (!roots.length) return;
            roots.forEach((root, index) => {
                const input = root.querySelector("input[type='hidden']");
                const trigger = root.querySelector(".date-picker-trigger");
                const popover = root.querySelector(".date-picker-popover");
                const monthLabel = root.querySelector("[data-date-month]");
                const days = root.querySelector("[data-date-days]");
                const valueLabel = root.querySelector("[data-date-value]");
                if (!input || !trigger || !popover || !monthLabel || !days || !valueLabel) return;

                const pickerId = popover.id || `task-log-date-picker-${index + 1}`;
                popover.id = pickerId;
                trigger.setAttribute("aria-controls", pickerId);
                const initialDate = parseDateValue(input.value) || new Date();
                const picker = {
                    role: root.dataset.dateRole || (input.id === "taskLogDateFrom" ? "start" : "end"),
                    root,
                    input,
                    trigger,
                    popover,
                    monthLabel,
                    days,
                    valueLabel,
                    viewDate: new Date(initialDate.getFullYear(), initialDate.getMonth(), 1, 12),
                };

                taskLogDatePickers.set(picker.role, picker);
                setTaskLogDatePickerValue(picker, input.value);
                trigger.addEventListener("click", () => {
                    if (openDatePicker === picker) closeTaskLogDatePicker(picker);
                    else openTaskLogDatePicker(picker);
                });
                root.querySelector("[data-date-prev]")?.addEventListener("click", () => {
                    picker.viewDate = new Date(picker.viewDate.getFullYear(), picker.viewDate.getMonth() - 1, 1, 12);
                    renderTaskLogDatePicker(picker);
                });
                root.querySelector("[data-date-next]")?.addEventListener("click", () => {
                    picker.viewDate = new Date(picker.viewDate.getFullYear(), picker.viewDate.getMonth() + 1, 1, 12);
                    renderTaskLogDatePicker(picker);
                });
                days.addEventListener("click", (event) => {
                    const button = event.target.closest("[data-date-day]");
                    if (!button) return;
                    setTaskLogDatePickerValue(picker, button.dataset.dateDay || "");
                    closeTaskLogDatePicker(picker, { restoreFocus: true });
                });
                root.querySelector("[data-date-today]")?.addEventListener("click", () => {
                    setTaskLogDatePickerValue(picker, formatDateValue(new Date()));
                    closeTaskLogDatePicker(picker, { restoreFocus: true });
                });
                root.querySelector("[data-date-clear]")?.addEventListener("click", () => {
                    setTaskLogDatePickerValue(picker, "");
                    closeTaskLogDatePicker(picker, { restoreFocus: true });
                });
            });

            document.addEventListener("pointerdown", (event) => {
                if (openDatePicker && !openDatePicker.root.contains(event.target)) closeTaskLogDatePicker(openDatePicker);
            });
            document.addEventListener("keydown", (event) => {
                if (event.key !== "Escape" || !openDatePicker) return;
                event.preventDefault();
                closeTaskLogDatePicker(openDatePicker, { restoreFocus: true });
            });
            taskLogDatePickersInitialized = true;
        }

        function renderRunItem(item) {
            return `
                <div class="list-item rclone-run-item">
                    <div>
                        <strong>rclone 批次 #${escapeHtml(item.id)}</strong>
                        <p>${statusPill(item.status)}</p>
                        <p>${escapeHtml(formatDate(item.started_at))} → ${escapeHtml(formatDate(item.finished_at))}</p>
                    </div>
                    <button class="secondary mini" type="button" data-rclone-run="${escapeHtml(item.id)}">查看日志</button>
                </div>
            `;
        }

        function renderRcloneEvent(item) {
            return `
                <div class="timeline-item ${escapeHtml(item.level || "")}">
                    <div class="timeline-time">${escapeHtml(formatDate(item.created_at))}</div>
                    <div class="timeline-message">${escapeHtml(item.message || "-")}</div>
                    ${renderTimelineRawData(item)}
                </div>
            `;
        }

        function renderRcloneStatus(status) {
            const box = getElement("adminRcloneStatus");
            if (!box) return;
            box.innerHTML = `
                <strong>${status.running ? "运行中" : "空闲"}</strong>
                <p>状态：${statusPill(status.status || "idle")}　队列：${escapeHtml(status.queue_count || 0)} 个待执行${status.current_run_id ? `　当前任务 #${escapeHtml(status.current_run_id)}` : ""}</p>
                ${status.last_error ? `<p class="status-error">${escapeHtml(status.last_error)}</p>` : ""}
            `;
        }

        function renderLogBox(id, items, emptyText) {
            const box = getElement(id);
            if (!box) return;
            const atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 24;
            box.textContent = items.join("\n") || emptyText;
            if (atBottom) box.scrollTop = box.scrollHeight;
        }

        function renderRcloneLiveLogs(items = []) {
            renderLogBox("adminRcloneLogs", items, "暂无日志");
        }

        function renderSystemLiveLogs(items = []) {
            renderLogBox("adminSystemLogs", items, "暂无系统日志");
        }

        function renderTaskLogs(items = []) {
            const box = getElement("adminTaskLogs");
            if (!box) return;
            box.innerHTML = items.length
                ? items.map((item) => `
                    <div class="list-item task-log-item ${Number(item.error_count || 0) > 0 ? "has-error" : ""}">
                        <div class="task-log-id">#${escapeHtml(item.id)}</div>
                        <div class="task-log-main">
                            <strong>${escapeHtml(item.title || "未命名任务")}</strong>
                            <p>${statusPill(item.status)} · ${escapeHtml(item.category_label || item.category || "-")} · ${escapeHtml(String(item.source_type || "-").toUpperCase())}</p>
                            <p>创建：${escapeHtml(formatDate(item.created_at))} · ${item.finished_at ? `结束：${escapeHtml(formatDate(item.finished_at))}` : "尚未结束"}</p>
                        </div>
                        <div class="task-log-stats">
                            <span><strong>${escapeHtml(item.log_count || 0)}</strong> 条记录</span>
                            <span class="${Number(item.error_count || 0) > 0 ? "danger-text" : ""}"><strong>${escapeHtml(item.error_count || 0)}</strong> 条异常</span>
                            <small>最后记录 ${escapeHtml(formatDate(item.latest_log_at || item.updated_at))}</small>
                        </div>
                        <button class="secondary" type="button" data-task-log-id="${escapeHtml(item.id)}">查看完整日志</button>
                    </div>
                `).join("")
                : `<div class="empty">没有符合条件的任务日志</div>`;
            box.querySelectorAll("[data-task-log-id]").forEach((button) => {
                button.addEventListener("click", () => showJobDetail(button.dataset.taskLogId, { openTechnical: true }));
            });
        }

        function taskLogsUrl() {
            const page = state.pagination.taskLogs.page || 1;
            const perPage = state.pagination.taskLogs.per_page || 20;
            const params = new URLSearchParams({ page: String(page), per_page: String(perPage) });
            const keyword = String(getElement("taskLogKeyword")?.value || "").trim();
            const status = String(getElement("taskLogStatus")?.value || "").trim();
            const dateFrom = String(getElement("taskLogDateFrom")?.value || "").trim();
            const dateTo = String(getElement("taskLogDateTo")?.value || "").trim();
            if (dateFrom && dateTo && dateFrom > dateTo) {
                throw new Error("开始日期不能晚于结束日期");
            }
            if (keyword) params.set("keyword", keyword);
            if (status) params.set("status", status);
            if (dateFrom) params.set("date_from", dateFrom);
            if (dateTo) params.set("date_to", dateTo);
            return `/api/admin/system/task-logs?${params.toString()}`;
        }

        async function loadTaskLogs(options = {}) {
            const page = state.pagination.taskLogs.page || 1;
            const perPage = state.pagination.taskLogs.per_page || 20;
            const data = await api(taskLogsUrl(), options);
            state.pagination.taskLogs = normalizePagination(data.pagination, { page, per_page: perPage });
            renderTaskLogs(data.items || []);
            renderPager("taskLogsPager", state.pagination.taskLogs, (nextPage) => {
                state.pagination.taskLogs.page = nextPage;
                loadTaskLogs().catch((error) => toast(error.message, "error"));
            });
        }

        async function showRcloneRunDetail(runId) {
            const [eventsData, filesData] = await Promise.all([
                api(`/api/admin/rclone/events?run_id=${encodeURIComponent(runId)}&limit=500`),
                api(`/api/admin/rclone/files?run_id=${encodeURIComponent(runId)}&limit=500`),
            ]);
            const events = eventsData.items || [];
            const files = filesData.items || [];
            const failedFiles = files.filter((item) => ["failed", "error"].includes(String(item.status || "").toLowerCase()));
            const doneFiles = files.filter((item) => ["done", "success", "skipped_existing"].includes(String(item.status || "").toLowerCase()));
            const header = [
                `rclone run #${runId}`,
                `完成文件：${doneFiles.length}；失败文件：${failedFiles.length}；事件数：${events.length}；文件记录：${files.length}`,
                "----------------------------------------",
            ];
            const eventLines = events.slice().reverse().map((item) => `[${formatDate(item.created_at)}] ${item.message || "-"}`);
            const fileLines = files.length
                ? [
                    "",
                    "-------- 文件记录 --------",
                    ...files.slice().reverse().map((item) => `[${formatDate(item.created_at)}] ${statusText(item.status || "")} ${item.filename || "-"} ${item.message || ""}`.trim()),
                ]
                : [];
            openRawLogModal(`rclone run #${runId} 原始日志`, [...header, ...eventLines, ...fileLines]);
        }

        async function loadRclone() {
            initTaskLogDatePickers();
            const runsPage = state.pagination.rcloneRuns.page || 1;
            const runsPerPage = state.pagination.rcloneRuns.per_page || 20;
            const [statusData, runsData, logsData, systemLogsData] = await Promise.all([
                api("/api/admin/rclone/status"),
                api(`/api/admin/rclone/runs?page=${encodeURIComponent(runsPage)}&per_page=${encodeURIComponent(runsPerPage)}`),
                api("/api/admin/rclone/logs?limit=300"),
                api("/api/admin/system/logs?limit=300"),
                loadTaskLogs(),
            ]);
            renderRcloneStatus(statusData.status || {});
            state.pagination.rcloneRuns = normalizePagination(runsData.pagination, { page: runsPage, per_page: runsPerPage });
            const runsBox = getElement("adminRcloneRuns");
            runsBox.innerHTML = (runsData.items || []).map(renderRunItem).join("") || `<div class="empty">暂无 rclone 运行批次</div>`;
            runsBox.querySelectorAll("[data-rclone-run]").forEach((button) => button.addEventListener("click", () => showRcloneRunDetail(button.dataset.rcloneRun)));
            renderPager("rcloneRunsPager", state.pagination.rcloneRuns, (page) => {
                state.pagination.rcloneRuns.page = page;
                loadRclone().catch((error) => toast(error.message, "error"));
            });
            renderRcloneLiveLogs(logsData.items || []);
            renderSystemLiveLogs(systemLogsData.lines || []);
        }

        async function loadRcloneLive() {
            if (state.rcloneLiveLoading) return;
            state.rcloneLiveLoading = true;
            try {
                const [statusData, logsData, systemLogsData] = await Promise.all([
                    api("/api/admin/rclone/status", { silentLoading: true, skipButtonLoading: true }),
                    api("/api/admin/rclone/logs?limit=500", { silentLoading: true, skipButtonLoading: true }),
                    api("/api/admin/system/logs?limit=500", { silentLoading: true, skipButtonLoading: true }),
                ]);
                renderRcloneStatus(statusData.status || {});
                renderRcloneLiveLogs(logsData.items || []);
                renderSystemLiveLogs(systemLogsData.lines || []);
            } finally {
                state.rcloneLiveLoading = false;
            }
        }

        function startRcloneLivePolling() {
            if (state.rcloneLiveTimer) return;
            state.rcloneLiveTimer = window.setInterval(() => {
                if (state.activeTab !== "rclone") return;
                loadRcloneLive().catch((error) => {
                    const logBox = getElement("adminRcloneLogs");
                    if (logBox) logBox.textContent = `实时日志刷新失败：${error.message}`;
                });
            }, 3000);
        }

        async function retryFileEvent(id) {
            const data = await api(`/api/admin/rclone/files/${id}/retry`, {
                method: "POST",
                body: JSON.stringify({}),
                allowFailure: true,
            });
            toast(data.success ? "已启动单文件重试" : data.message || "单文件重试未启动", data.success ? "success" : "error");
            await Promise.all([loadRclone(), loadOverview()]);
        }

        async function startRclone() {
            const data = await api("/api/admin/rclone/start", {
                method: "POST",
                body: JSON.stringify({ reason: "admin_manual" }),
                allowFailure: true,
            });
            toast(data.message || "已提交启动请求", data.success ? "success" : "error");
            await loadRclone();
        }

        async function stopRclone() {
            const ok = await confirmDialog({
                title: "停止 rclone 搬运",
                message: "确认停止当前 rclone 搬运？如果文件已传到 100%，系统会在停止后按云端大小校验结果兜底。",
                confirmText: "停止搬运",
                tone: "danger",
            });
            if (!ok) return;
            const data = await api("/api/admin/rclone/stop", {
                method: "POST",
                body: JSON.stringify({}),
                allowFailure: true,
            });
            toast(data.message || "已发送停止请求", data.success ? "success" : "error");
            await loadRclone();
        }

        async function checkRclone() {
            const data = await api("/api/admin/rclone/check", { allowFailure: true });
            getElement("adminRcloneStatus").innerHTML = `
                <strong>${escapeHtml(data.message || "环境检查完成")}</strong>
                <div class="check-list">
                    ${(data.items || []).map((item) => `<div class="${item.ok ? "ok-text" : "danger-text"}">${item.ok ? "通过" : "失败"}：${escapeHtml(item.name)} - ${escapeHtml(item.message || "")}</div>`).join("")}
                </div>
            `;
        }

        return Object.freeze({
            loadRclone,
            loadTaskLogs,
            loadRcloneLive,
            startRcloneLivePolling,
            retryFileEvent,
            startRclone,
            stopRclone,
            checkRclone,
            showRcloneRunDetail,
            renderRcloneEvent,
        });
    }

    window.FnosAdminRclone = Object.freeze({ create });
})();
