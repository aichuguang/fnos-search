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
        const RCLONE_COPY = Object.freeze({
            taskNavigation: "搬运由具体入库任务自动调度。请打开对应任务详情，使用确认继续或重试；这里不会启动无任务的全局扫描。",
            missingTaskEntry: "入库任务入口暂不可用，请刷新页面后进入“正式任务”。",
        });
        const PROGRESS_COPY = Object.freeze({
            active: "入库任务处理中",
            attention: "入库任务待处理",
            processRunning: "下载进程运行中",
            processIdle: "下载进程空闲",
            processState: "下载批次状态",
            queue: "下载队列",
            queued: "个待执行",
            currentRun: "当前下载批次",
            job: "任务",
            run: "下载批次",
            details: "任务详情",
            complete: "入库完成",
            download: "下载",
            upload: "上传",
            hash: "秒传校验 / 上传准备",
            fileScope: "单文件进度",
            stageScope: "本阶段指标，非任务总进度",
            file: "文件",
            currentFile: "当前文件",
            transferred: "已传输",
            hashed: "已校验",
            waiting: "等待真实进度反馈",
            confirmedNoMetrics: "已确认完成，未提供字节统计",
            unknown: "—",
            speed: "速度",
            remaining: "预计剩余",
            seconds: "秒",
            minutes: "分",
            hours: "小时",
            lastFeedback: "最新反馈",
            noFeedbackTime: "尚未收到反馈时间",
            age: "距上次进度变化",
            stale: "反馈较旧，等待下一次实际回报",
            stageOnly: "下载或上传达到 100% 不等于入库完成；仍需确认和后续入库处理。",
            hashOnly: "此项仅表示校验和上传准备，不是已上传字节。",
            statusFailure: "状态刷新失败，已保留上次状态",
            logFailure: "搬运日志刷新失败，已保留当前日志",
            systemFailure: "系统日志刷新失败，已保留当前日志",
            invalidResponse: "服务返回的实时数据格式无效",
            refreshFailure: "实时数据刷新失败",
            retry: "重新加载",
            noLogs: "暂无日志",
            noSystemLogs: "暂无系统日志",
            recentOnly: "仅展示最近传输，完整记录请到正式任务查看。",
            searchEmpty: "没有匹配的传输任务",
            searchEmptyHint: "试试任务编号或文件名，也可以清空搜索。",
            clearSearch: "清空搜索",
            currentEmpty: "当前没有正在传输的任务",
            currentEmptyHint: "新任务开始后会自动出现在这里，已结束任务不会继续占屏。",
            attentionEmpty: "暂时没有需要处理的任务",
            attentionEmptyHint: "传输失败或需要人工确认的任务会集中显示在这里。",
            historyEmpty: "暂无最近结束的任务",
            historyNoMatches: "没有匹配的已结束记录。",
            historyLimit: "仅展示最近 5 条匹配记录。",
            shortDetails: "详情",
            openJobs: "查看入库任务",
            phaseRecord: "查看阶段记录",
            lastObservation: "最后观测",
            noObservation: "未提供该阶段的统计",
            notLive: "以下为最后一次回报，不代表当前仍在传输。",
            phaseEnded: "此阶段已结束，等待后续入库确认。",
            bytesAwaiting: "进度已到 100%，等待阶段确认",
            awaitingResult: "等待阶段结果确认",
            needsAction: "任务需要处理，请打开任务详情查看原因并继续或重试。",
            cancelled: "已取消",
            organizingHint: "传输阶段已结束，正在完成校验、整理或入库确认。",
            waitingHint: "任务由系统自动推进，有实际进度后会在这里更新。",
            nativeHint: "正在由网盘处理，暂未提供可展示的字节进度。",
            stepDownload: "下载",
            stepUpload: "上传",
            stepOrganize: "整理入库",
            stepComplete: "完成",
            logStatusFailure: "服务状态刷新失败，已保留上次状态",
            environmentDone: "环境检查完成",
            checkPass: "通过",
            checkFail: "失败",
            parallelUpload: "下载 / 上传并行",
            parallelHash: "下载 / 校验并行",
            relayBuffer: "预下载缓存",
            relayWaiting: "等待缓存释放",
        });
        const TRANSFER_STAGES = Object.freeze({
            planning: "准备传输",
            source_materializing: "准备源文件",
            source_waiting_visible: "等待源文件就绪",
            native_submitting: "提交网盘任务",
            native_waiting: "等待网盘处理",
            transferring: "传输处理中",
            waiting_download: "等待下载",
            download: "下载中",
            downloading: "下载中",
            waiting_upload: "下载已结束，等待上传",
            hash: "秒传校验 / 上传准备",
            hashing: "秒传校验 / 上传准备",
            preparing_upload: "上传准备中",
            upload: "上传中",
            uploading: "上传中",
            verifying: "核对入库文件",
            target_verifying: "核对目标暂存文件",
            organizing: "整理媒体文件",
            finalizing: "归入最终目录",
            final_verifying: "核对最终文件",
            ack_pending: "确认入库结果",
            cleanup: "清理临时文件",
            cleanup_pending: "清理临时文件",
            completed: "等待入库终态确认",
            done: "等待入库终态确认",
            failed: "传输失败，请查看任务详情",
            cancelled: "任务已取消",
            review: "需要人工处理",
            manual_review: "需要人工处理",
            retry_wait: "等待继续执行",
            cancel_pending: "正在取消任务",
        });
        const METRIC_STATES = Object.freeze({
            idle: "等待开始",
            waiting: "等待开始",
            pending: "等待开始",
            running: "进行中",
            completed: "已完成",
            done: "已完成",
            success: "已完成",
            failed: "失败",
            cancelled: "已取消",
            unknown: "结果待确认",
            skipped: "无需执行",
            not_required: "无需执行",
        });
        const liveRequests = new Map();
        const liveErrors = new Map();
        const ATTENTION_STAGES = new Set(["failed", "review", "manual_review"]);
        const POST_TRANSFER_STAGES = new Set([
            "verifying", "target_verifying", "organizing", "finalizing", "final_verifying",
            "ack_pending", "cleanup", "cleanup_pending", "completed", "done",
        ]);
        const CONFIRMED_METRIC_STATES = new Set(["completed", "done", "success"]);
        const TRANSFER_HISTORY_LIMIT = 5;
        let latestTransferStatus = null;
        let transferFilter = "current";
        let transferSearch = "";
        let transferHistoryOpen = false;
        let transferControlsInitialized = false;
        const expandedTransferIds = new Set();

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

        function metricNumber(value) {
            return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
        }

        function metricBytes(value) {
            const amount = metricNumber(value);
            if (amount === null) return PROGRESS_COPY.unknown;
            const units = ["B", "KiB", "MiB", "GiB", "TiB"];
            const unit = amount > 0 ? Math.min(Math.floor(Math.log(amount) / Math.log(1024)), units.length - 1) : 0;
            return `${Number((amount / (1024 ** Math.max(0, unit))).toFixed(2))} ${units[Math.max(0, unit)]}`;
        }

        function metricDuration(value) {
            const seconds = metricNumber(value);
            if (seconds === null) return PROGRESS_COPY.unknown;
            const rounded = Math.ceil(seconds);
            if (rounded >= 3600) return `${Math.floor(rounded / 3600)} ${PROGRESS_COPY.hours} ${Math.floor((rounded % 3600) / 60)} ${PROGRESS_COPY.minutes}`;
            if (rounded >= 60) return `${Math.floor(rounded / 60)} ${PROGRESS_COPY.minutes} ${rounded % 60} ${PROGRESS_COPY.seconds}`;
            return `${rounded} ${PROGRESS_COPY.seconds}`;
        }

        function renderTransferMetric(kind, value, showFilename = false) {
            const metric = value && typeof value === "object" ? value : {};
            const rawPercent = metricNumber(metric.percent);
            const percent = rawPercent !== null && rawPercent <= 100 ? rawPercent : null;
            const confirmed = CONFIRMED_METRIC_STATES.has(String(metric.state || "").toLowerCase());
            if (confirmed) {
                return `<div class="transfer-phase-summary">${PROGRESS_COPY[kind]} · ${percent === null ? PROGRESS_COPY.confirmedNoMetrics : METRIC_STATES.completed}<small>${PROGRESS_COPY.phaseEnded}</small></div>`;
            }
            const percentText = percent === null ? PROGRESS_COPY.waiting : `${Number(percent.toFixed(1))}%`;
            const stale = metric.stale === true;
            const speed = stale ? null : metricNumber(metric.speed_bps);
            const speedText = speed === null ? PROGRESS_COPY.unknown : `${metricBytes(speed)}/s`;
            const remainingText = metricDuration(stale ? null : metric.eta_seconds);
            const scope = metric.scope === "file" ? PROGRESS_COPY.fileScope : PROGRESS_COPY.stageScope;
            const stageState = percent === 100 ? PROGRESS_COPY.awaitingResult : METRIC_STATES[String(metric.state || "").toLowerCase()] || PROGRESS_COPY.waiting;
            const age = metricNumber(metric.age_seconds);
            return `<section class="rclone-transfer-metric" data-transfer-metric="${kind}" data-metric-stale="${stale}">
                <div class="transfer-metric-heading">
                    <div class="transfer-metric-label"><strong>${PROGRESS_COPY[kind]}</strong><small>${scope}</small></div>
                    <div class="transfer-metric-value${percent === null ? " is-unknown" : ""}"><strong>${percentText}</strong><small>${stageState}</small></div>
                </div>
                ${showFilename && metric.filename ? `<div class="transfer-current-file transfer-channel-file"><small>${PROGRESS_COPY.file}</small><span title="${escapeHtml(metric.filename)}">${escapeHtml(metric.filename)}</span></div>` : ""}
                <div class="rclone-progress-track" role="progressbar" aria-label="${PROGRESS_COPY[kind]} · ${scope}"
                    data-measured="${percent !== null}" aria-valuemin="0" aria-valuemax="100" ${percent === null ? `aria-valuetext="${percentText}"` : `aria-valuenow="${percent}" aria-valuetext="${percentText}"`}>
                    ${percent === null ? "" : `<div class="rclone-progress-fill" style="width:${percent}%;"></div>`}
                </div>
                <div class="transfer-metric-stats">
                    <div><small>${kind === "hash" ? PROGRESS_COPY.hashed : PROGRESS_COPY.transferred}</small><strong>${metricBytes(metric.bytes_done)} / ${metricBytes(metric.bytes_total)}</strong></div>
                    <div aria-label="${PROGRESS_COPY.speed} ${speedText}"><small>${PROGRESS_COPY.speed}</small><strong>${speedText}</strong></div>
                    <div aria-label="${PROGRESS_COPY.remaining} ${remainingText}"><small>${PROGRESS_COPY.remaining}</small><strong>${remainingText}</strong></div>
                </div>
                ${percent === 100 ? `<p class="transfer-metric-note">${PROGRESS_COPY.bytesAwaiting}</p>` : ""}
                ${kind === "hash" ? `<p class="transfer-metric-note">${PROGRESS_COPY.hashOnly}</p>` : ""}
                ${metric.updated_at ? `<small class="transfer-channel-feedback">${PROGRESS_COPY.lastFeedback}：${escapeHtml(formatDate(metric.updated_at))}${age === null ? "" : ` · ${PROGRESS_COPY.age} ${metricDuration(age)}`}</small>` : ""}
                ${stale ? `<small class="transfer-stale">${PROGRESS_COPY.stale}</small>` : ""}
            </section>`;
        }

        function transferStage(transfer) {
            return String(transfer.stage || "").toLowerCase();
        }

        function transferEnded(transfer) {
            return transfer.completed === true || transferStage(transfer) === "cancelled";
        }

        function transferNeedsAttention(transfer) {
            return !transferEnded(transfer) && ATTENTION_STAGES.has(transferStage(transfer));
        }

        function transferChannel(transfer) {
            const stage = transferStage(transfer);
            if (["download", "downloading"].includes(stage)) return "download";
            if (["hash", "hashing", "preparing_upload"].includes(stage)) return "hash";
            if (["upload", "uploading"].includes(stage)) return "upload";
            if (stage === "transferring") {
                return ["upload", "hash", "download"].find((kind) => String(transfer[kind]?.state).toLowerCase() === "running") || null;
            }
            return null;
        }

        function transferChannels(transfer) {
            const stage = transferStage(transfer);
            if (transferEnded(transfer) || transferNeedsAttention(transfer) || POST_TRANSFER_STAGES.has(stage)
                || !["download", "downloading", "waiting_download", "waiting_upload", "hash", "hashing",
                    "preparing_upload", "upload", "uploading", "transferring"].includes(stage)) return [];
            const pipeline = transfer.relay_buffer?.enabled === true;
            const active = ["download", "hash", "upload"].filter((kind) => {
                const metric = transfer[kind] || {};
                return ["running", "waiting"].includes(String(metric.state || "").toLowerCase())
                    && (pipeline ? metric.active !== false : metric.active === true && metric.stale !== true);
            });
            // Hash and upload are two phases of one outbound file, not two
            // transfer lanes. Old snapshots without explicit activity retain
            // their single-stage rendering instead of reviving stale download.
            const outbound = active.filter((kind) => kind !== "download").sort((left, right) =>
                String(transfer[right]?.updated_at || "").localeCompare(String(transfer[left]?.updated_at || ""))
                || Number(right === transferChannel(transfer)) - Number(left === transferChannel(transfer)))[0];
            const lanes = [...(active.includes("download") ? ["download"] : []), ...(outbound ? [outbound] : [])];
            if (pipeline && transfer.relay_buffer.depth === 0) {
                const current = transferChannel(transfer);
                return lanes.includes(current) ? [current] : lanes.slice(0, 1);
            }
            if (pipeline || lanes.length > 1) return lanes;
            const channel = transferChannel(transfer);
            return channel ? [channel] : [];
        }

        function renderRelayBuffer(transfer) {
            const buffer = transfer.relay_buffer;
            if (!buffer || buffer.enabled !== true || transferEnded(transfer) || transferNeedsAttention(transfer)
                || POST_TRANSFER_STAGES.has(transferStage(transfer))) return "";
            if (![1, 2].includes(buffer.depth) || !Number.isInteger(buffer.ready)
                || buffer.ready < 0 || buffer.ready > buffer.depth) return "";
            return `<div class="transfer-relay-buffer" role="status"><span>${PROGRESS_COPY.relayBuffer} ${buffer.ready} / ${buffer.depth}</span>${buffer.waiting === true ? `<span class="transfer-relay-waiting">${PROGRESS_COPY.relayWaiting}</span>` : ""}</div>`;
        }

        function transferFilename(transfer) {
            const channel = transferChannel(transfer);
            return transfer[channel]?.filename || transfer.upload?.filename || transfer.hash?.filename || transfer.download?.filename || "";
        }

        function renderTransferSteps(transfer) {
            const stage = transferStage(transfer);
            const channel = transferChannel(transfer);
            const parallelPhase = transfer.relay_buffer?.enabled === true && !POST_TRANSFER_STAGES.has(stage)
                && transfer.completed !== true;
            const lanes = parallelPhase ? transferChannels(transfer) : [];
            const current = transfer.completed === true ? 3 : POST_TRANSFER_STAGES.has(stage) ? 2
                : ["hash", "upload"].includes(channel) || stage === "waiting_upload" ? 1 : 0;
            const labels = [PROGRESS_COPY.stepDownload, PROGRESS_COPY.stepUpload, PROGRESS_COPY.stepOrganize, PROGRESS_COPY.stepComplete];
            return `<ol class="transfer-steps" aria-label="${PROGRESS_COPY.stageOnly}">${labels.map((label, index) => {
                const done = !parallelPhase && (index < current || transfer.completed === true);
                const active = parallelPhase ? (index === 0 && lanes.includes("download"))
                    || (index === 1 && lanes.some((kind) => kind !== "download")) : index === current && !done;
                return `<li class="${done ? "is-done" : active ? "is-current" : ""}"${active ? ' aria-current="step"' : ""}><span class="transfer-step-dot" aria-hidden="true">${done ? "✓" : index + 1}</span><span>${label}${done ? `<span class="sr-only"> · ${METRIC_STATES.completed}</span>` : ""}</span></li>`;
            }).join("")}</ol>`;
        }

        function renderTransferRecord(transfer) {
            const key = escapeHtml(String(transfer.job_id || ""));
            const observations = ["download", "hash", "upload"].map((kind) => {
                const metric = transfer[kind] || {};
                const percent = metricNumber(metric.percent);
                const validPercent = percent !== null && percent <= 100;
                const bytes = metricNumber(metric.bytes_done);
                const confirmed = CONFIRMED_METRIC_STATES.has(String(metric.state || "").toLowerCase());
                if (!validPercent && bytes === null && !confirmed) return "";
                const summary = confirmed ? (!validPercent && bytes === null ? PROGRESS_COPY.confirmedNoMetrics : METRIC_STATES.completed) : PROGRESS_COPY.lastObservation;
                return `<p><strong>${PROGRESS_COPY[kind]}</strong> · ${summary}${validPercent ? ` · ${Number(percent.toFixed(1))}%` : ""}${bytes === null ? "" : ` · ${metricBytes(bytes)} / ${metricBytes(metric.bytes_total)}`}</p>`;
            }).filter(Boolean).join("");
            return `<details class="transfer-technical" data-transfer-details="${key}"${expandedTransferIds.has(String(transfer.job_id || "")) ? " open" : ""}>
                <summary data-transfer-detail-toggle="${key}">${PROGRESS_COPY.phaseRecord}</summary>
                <div class="transfer-technical-body">${transferNeedsAttention(transfer) ? "" : renderTransferSteps(transfer)}${transfer.message ? `<p>${escapeHtml(transfer.message)}</p>` : ""}${observations || `<p>${PROGRESS_COPY.noObservation}</p>`}<small>${PROGRESS_COPY.notLive}</small></div>
            </details>`;
        }

        function renderLiveTransfer(transfer) {
            const stage = transferStage(transfer);
            const attention = transferNeedsAttention(transfer);
            const channels = transferChannels(transfer);
            const dual = channels.length > 1;
            const pipeline = transfer.relay_buffer?.enabled === true;
            const label = dual ? PROGRESS_COPY[channels.includes("hash") ? "parallelHash" : "parallelUpload"]
                : TRANSFER_STAGES[stage] || PROGRESS_COPY.waiting;
            const channel = channels[0] || null;
            const filename = transferFilename(transfer);
            const title = transfer.title || transfer.job_title || filename || `${PROGRESS_COPY.job} #${transfer.job_id || PROGRESS_COPY.unknown}`;
            const age = metricNumber(transfer.age_seconds);
            const feedbackTime = transfer.updated_at ? formatDate(transfer.updated_at) : PROGRESS_COPY.noFeedbackTime;
            const stale = !attention && transfer.stale === true && !dual;
            const phaseHint = attention ? PROGRESS_COPY.needsAction : POST_TRANSFER_STAGES.has(stage) ? PROGRESS_COPY.organizingHint
                : stage.startsWith("native_") ? PROGRESS_COPY.nativeHint : PROGRESS_COPY.waitingHint;
            const tone = attention ? "is-attention" : "is-active";
            return `<article class="rclone-live-transfer transfer-card${attention ? " is-attention" : ""}" data-transfer-job-id="${escapeHtml(transfer.job_id)}" data-transfer-completed="false" data-transfer-channel="${channels.join(",") || "none"}" data-transfer-stale="${stale}" data-transfer-pipeline="${pipeline}">
                <header class="transfer-card-header">
                    <span class="transfer-file-icon" aria-hidden="true"><svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="3" y="3" width="18" height="18" rx="4"/><path d="m10 8 6 4-6 4V8Z"/></svg></span>
                    <div class="transfer-card-heading"><h3 title="${escapeHtml(title)}">${escapeHtml(title)}</h3><div class="transfer-card-meta">${PROGRESS_COPY.job} #${escapeHtml(transfer.job_id || PROGRESS_COPY.unknown)}${transfer.run_id ? ` <span>·</span> ${PROGRESS_COPY.run} #${escapeHtml(transfer.run_id)}` : ""}</div></div>
                    <span class="transfer-stage-badge ${tone}">${escapeHtml(label)}</span>
                    ${transfer.job_id ? `<button type="button" class="secondary mini transfer-detail-button" data-transfer-job="${escapeHtml(transfer.job_id)}">${PROGRESS_COPY.details}</button>` : ""}
                </header>
                ${channel && !dual && !pipeline && filename && filename !== title ? `<div class="transfer-current-file"><small>${PROGRESS_COPY.currentFile}</small><span title="${escapeHtml(filename)}">${escapeHtml(filename)}</span></div>` : ""}
                ${channels.length ? `<div class="transfer-metrics${dual ? " is-parallel" : ""}">${channels.map((kind) => renderTransferMetric(kind, transfer[kind], dual || pipeline)).join("")}</div>` : `<div class="transfer-phase-summary">${escapeHtml(label)}<small>${phaseHint}</small></div>`}
                ${renderRelayBuffer(transfer)}
                <footer class="transfer-card-footer"><small class="transfer-feedback">${PROGRESS_COPY.lastFeedback}：${escapeHtml(feedbackTime)}${age === null ? "" : ` · ${PROGRESS_COPY.age} ${metricDuration(age)}`}</small>${stale ? `<small class="transfer-stale">${PROGRESS_COPY.stale}</small>` : ""}</footer>
                ${renderTransferRecord(transfer)}
            </article>`;
        }

        function transferMatchesSearch(transfer) {
            return !transferSearch || [transfer.job_id, transfer.run_id, transfer.title, transfer.job_title,
                transfer.download?.filename, transfer.hash?.filename, transfer.upload?.filename,
            ].some((value) => String(value ?? "").toLocaleLowerCase().includes(transferSearch));
        }

        function renderTransferHistoryEntry(transfer) {
            const name = transfer.title || transfer.job_title || transferFilename(transfer);
            const job = `${PROGRESS_COPY.job} #${transfer.job_id || PROGRESS_COPY.unknown}`;
            const completed = transfer.completed === true;
            return `<div class="transfer-history-entry" role="listitem" data-transfer-job-id="${escapeHtml(transfer.job_id)}" data-transfer-completed="${transfer.completed === true}">
                <span class="transfer-history-result${completed ? " is-completed" : " is-cancelled"}" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">${completed ? '<path d="m6 12 4 4 8-8"/>' : '<path d="m8 8 8 8m0-8-8 8"/>'}</svg></span>
                <div class="transfer-history-main"><span class="transfer-history-title" title="${escapeHtml(name || job)}">${escapeHtml(name || job)}</span><div class="transfer-history-meta">${name ? `<span>${escapeHtml(job)}</span>` : ""}<time class="transfer-history-time">${escapeHtml(formatDate(transfer.updated_at) || PROGRESS_COPY.unknown)}</time></div></div>
                <span class="transfer-history-status${completed ? " is-completed" : ""}">${completed ? PROGRESS_COPY.complete : PROGRESS_COPY.cancelled}</span>
                ${transfer.job_id ? `<button type="button" class="secondary mini transfer-history-open" data-transfer-job="${escapeHtml(transfer.job_id)}" aria-label="${PROGRESS_COPY.details} #${escapeHtml(transfer.job_id)}">${PROGRESS_COPY.shortDetails}</button>` : ""}
            </div>`;
        }

        function bindTransferJobButtons(box, focusKey) {
            box.querySelectorAll("[data-transfer-job]").forEach((button) => {
                button.addEventListener("click", () => Promise.resolve().then(() => showJobDetail(button.dataset.transferJob)).catch((error) => toast(error.message, "error")));
                if (focusKey !== undefined && button.dataset.transferJob === focusKey) button.focus({ preventScroll: true });
            });
        }

        function renderTransferHistory() {
            const details = getElement("transferHistory");
            const box = getElement("adminTransferHistory");
            const transfers = Array.isArray(latestTransferStatus?.transfers) ? latestTransferStatus.transfers : [];
            const ended = transfers.filter((item) => item && typeof item === "object" && transferEnded(item));
            if (details) {
                transferHistoryOpen = details.open;
                details.hidden = !ended.length && !transferHistoryOpen;
            }
            setTransferText("transferHistoryCount", ended.length);
            if (!box) return;
            // Closed history has no task rows, regardless of how many past
            // transfers the service returns. Polling must never open it.
            if (!transferHistoryOpen) {
                box.innerHTML = "";
                return;
            }
            const matches = ended.filter(transferMatchesSearch).sort((left, right) => String(right.updated_at || "").localeCompare(String(left.updated_at || "")));
            const visible = matches.slice(0, TRANSFER_HISTORY_LIMIT);
            const focused = document.activeElement;
            const focusKey = box.contains?.(focused) ? focused?.dataset?.transferJob : undefined;
            box.innerHTML = visible.length ? visible.map(renderTransferHistoryEntry).join("")
                : `<p class="transfer-history-empty" role="status">${transferSearch ? PROGRESS_COPY.historyNoMatches : PROGRESS_COPY.historyEmpty}</p>`;
            setTransferText("transferVisibilityHint", `${matches.length > TRANSFER_HISTORY_LIMIT ? `${PROGRESS_COPY.historyLimit} ` : ""}${PROGRESS_COPY.recentOnly}`);
            bindTransferJobButtons(box, focusKey);
        }

        function setTransferHistoryOpen(open) {
            transferHistoryOpen = open === true;
            const details = getElement("transferHistory");
            if (details) details.open = transferHistoryOpen;
            renderTransferHistory();
        }

        function setTransferText(id, text) {
            const element = getElement(id);
            if (element) element.textContent = String(text);
        }

        function setTransferFilter(value) {
            transferFilter = ["current", "attention"].includes(value) ? value : "current";
            if (latestTransferStatus) renderRcloneStatus(latestTransferStatus);
        }

        function setTransferSearch(value) {
            transferSearch = String(value || "").trim().toLocaleLowerCase();
            if (latestTransferStatus) renderRcloneStatus(latestTransferStatus);
        }

        function initTransferControls() {
            if (transferControlsInitialized || !getElement("transferSearch")) return;
            getElement("transferSearch").addEventListener("input", (event) => setTransferSearch(event.target.value));
            document.querySelectorAll("[data-transfer-filter]").forEach((button) => {
                button.addEventListener("click", () => setTransferFilter(button.dataset.transferFilter));
            });
            getElement("transferHistory")?.addEventListener("toggle", () => renderTransferHistory());
            getElement("adminTransferHistoryJobsBtn")?.addEventListener("click", () => getElement("adminTabJobs")?.click());
            transferControlsInitialized = true;
        }

        function renderTransferEmpty() {
            const title = transferSearch ? PROGRESS_COPY.searchEmpty : transferFilter === "attention" ? PROGRESS_COPY.attentionEmpty : PROGRESS_COPY.currentEmpty;
            const hint = transferSearch ? PROGRESS_COPY.searchEmptyHint : transferFilter === "attention" ? PROGRESS_COPY.attentionEmptyHint : PROGRESS_COPY.currentEmptyHint;
            return `<div class="transfer-empty"><div class="transfer-empty-copy"><h3>${title}</h3><p>${hint}</p></div><button type="button" class="secondary mini" ${transferSearch ? "data-transfer-clear-search" : "data-transfer-open-jobs"}>${transferSearch ? PROGRESS_COPY.clearSearch : PROGRESS_COPY.openJobs}</button></div>`;
        }

        function renderRcloneStatus(status = {}) {
            latestTransferStatus = status;
            initTransferControls();
            const box = getElement("adminRcloneStatus");
            const transfers = Array.isArray(status.transfers) ? status.transfers.filter((item) => item && typeof item === "object") : [];
            const current = transfers.filter((item) => !transferEnded(item));
            const attention = current.filter(transferNeedsAttention);
            const activeCount = current.length - attention.length;
            const headline = activeCount ? PROGRESS_COPY.active : attention.length ? PROGRESS_COPY.attention
                : status.running ? PROGRESS_COPY.processRunning : PROGRESS_COPY.processIdle;
            const queue = metricNumber(status.queue_count) ?? 0;
            const summary = `<strong>${headline}</strong><span>${status.running ? PROGRESS_COPY.processRunning : PROGRESS_COPY.processIdle} · ${PROGRESS_COPY.processState}：${statusPill(status.status || "idle")} · ${PROGRESS_COPY.queue}：${queue} ${PROGRESS_COPY.queued}${status.current_run_id ? ` · ${PROGRESS_COPY.currentRun} #${escapeHtml(status.current_run_id)}` : ""}</span>${status.last_error ? `<span class="status-error">${escapeHtml(status.last_error)}</span>` : ""}`;
            ["adminTransferService", "adminRcloneServiceStatus"].forEach((id) => {
                const service = getElement(id);
                if (service) service.innerHTML = summary;
            });
            setTransferText("transferQueueCount", queue);
            const queueSummary = getElement("transferQueueSummary");
            if (queueSummary) queueSummary.hidden = queue === 0;
            setTransferText("transferCurrentTabCount", current.length);
            setTransferText("transferAttentionTabCount", attention.length);
            renderTransferHistory();
            document.querySelectorAll("[data-transfer-filter]").forEach((button) => {
                const selected = button.dataset.transferFilter === transferFilter;
                button.classList.toggle("active", selected);
                button.setAttribute("aria-pressed", String(selected));
            });
            if (!box) return;
            box.setAttribute("aria-busy", "false");
            const activeElement = document.activeElement;
            const focusKey = box.contains?.(activeElement) ? activeElement?.dataset?.transferJob : undefined;
            const detailKey = box.contains?.(activeElement) ? activeElement?.dataset?.transferDetailToggle : undefined;
            const pool = transferFilter === "attention" ? attention : current;
            const visible = pool.filter(transferMatchesSearch);
            visible.sort((left, right) => Number(transferNeedsAttention(right)) - Number(transferNeedsAttention(left))
                || String(right.updated_at || "").localeCompare(String(left.updated_at || "")));
            box.innerHTML = visible.length ? visible.map(renderLiveTransfer).join("") : renderTransferEmpty();
            bindTransferJobButtons(box, focusKey);
            box.querySelectorAll("[data-transfer-details]").forEach((details) => {
                details.addEventListener("toggle", () => {
                    if (details.open) expandedTransferIds.add(details.dataset.transferDetails);
                    else expandedTransferIds.delete(details.dataset.transferDetails);
                });
                if (detailKey !== undefined && details.dataset.transferDetails === detailKey) details.querySelector("summary")?.focus({ preventScroll: true });
            });
            box.querySelectorAll("[data-transfer-clear-search]").forEach((button) => button.addEventListener("click", () => {
                const input = getElement("transferSearch");
                if (input) input.value = "";
                setTransferSearch("");
                input?.focus();
            }));
            box.querySelectorAll("[data-transfer-open-jobs]").forEach((button) => button.addEventListener("click", () => getElement("adminTabJobs")?.click()));
        }

        function renderLogBox(id, items, emptyText) {
            const box = getElement(id);
            if (!box) return;
            const atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 24;
            box.textContent = items.join("\n") || emptyText;
            if (atBottom) box.scrollTop = box.scrollHeight;
        }

        function renderRcloneLiveLogs(items = []) {
            renderLogBox("adminRcloneLogs", items, PROGRESS_COPY.noLogs);
        }

        function renderSystemLiveLogs(items = []) {
            renderLogBox("adminSystemLogs", items, PROGRESS_COPY.noSystemLogs);
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
                            <p>${statusPill(item.status)} · ${escapeHtml(item.category_label || item.category || "-")} · ${escapeHtml(String(item.source_provider_kind || "-").toUpperCase())}</p>
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
            const runs = api(`/api/admin/rclone/runs?page=${encodeURIComponent(runsPage)}&per_page=${encodeURIComponent(runsPerPage)}`).then((runsData) => {
                state.pagination.rcloneRuns = normalizePagination(runsData.pagination, { page: runsPage, per_page: runsPerPage });
                const runsBox = getElement("adminRcloneRuns");
                runsBox.innerHTML = (runsData.items || []).map(renderRunItem).join("") || `<div class="empty">暂无 rclone 运行批次</div>`;
                runsBox.querySelectorAll("[data-rclone-run]").forEach((button) => button.addEventListener("click", () => showRcloneRunDetail(button.dataset.rcloneRun)));
                renderPager("rcloneRunsPager", state.pagination.rcloneRuns, (page) => {
                    state.pagination.rcloneRuns.page = page;
                    loadRclone().catch((error) => toast(error.message, "error"));
                });
            });
            await Promise.all([runs, loadTaskLogs(), loadRcloneLive({ silentLoading: false, limit: 300 })]);
        }

        function showLiveChannelError(channel, error = null) {
            const targets = { status: "adminRcloneStatus", logs: "adminRcloneLogs", system: "adminSystemLogs" };
            const labels = { status: PROGRESS_COPY.statusFailure, logs: PROGRESS_COPY.logFailure, system: PROGRESS_COPY.systemFailure };
            const targetId = targets[channel];
            const errorId = `${targetId}RefreshError`;
            const text = error ? `${labels[channel]}：${error.message || PROGRESS_COPY.refreshFailure}` : "";
            const target = getElement(targetId);
            let notice = getElement(errorId);
            if (text && !notice && target?.insertAdjacentElement && typeof document !== "undefined") {
                notice = document.createElement("p");
                notice.id = errorId;
                notice.className = "status-error rclone-live-refresh-error";
                notice.setAttribute("role", "status");
                notice.setAttribute("aria-live", "polite");
                target.insertAdjacentElement("beforebegin", notice);
            }
            if (notice) {
                notice.textContent = text;
                notice.hidden = !text;
            } else if (text && liveErrors.get(channel) !== text) {
                toast(text, "error");
            }
            liveErrors.set(channel, text);
            if (channel === "status") {
                const serviceNotice = getElement("adminRcloneServiceStatusRefreshError");
                if (serviceNotice) {
                    serviceNotice.textContent = error ? `${PROGRESS_COPY.logStatusFailure}：${error.message || PROGRESS_COPY.refreshFailure}` : "";
                    serviceNotice.hidden = !error;
                }
                if (error && !latestTransferStatus) {
                    setTransferText("adminTransferService", PROGRESS_COPY.statusFailure);
                    setTransferText("adminRcloneServiceStatus", PROGRESS_COPY.statusFailure);
                    const retryBox = getElement("adminRcloneStatus");
                    if (retryBox) {
                        retryBox.setAttribute("aria-busy", "false");
                        retryBox.innerHTML = `<div class="transfer-empty"><h3>${PROGRESS_COPY.refreshFailure}</h3><p>${escapeHtml(error.message || PROGRESS_COPY.invalidResponse)}</p><button type="button" class="secondary" data-transfer-retry>${PROGRESS_COPY.retry}</button></div>`;
                    }
                    retryBox?.querySelectorAll("[data-transfer-retry]").forEach((button) => button.addEventListener("click", () => loadTransfers()));
                }
            }
        }

        function loadLiveChannel(channel, options = {}) {
            if (liveRequests.has(channel)) return liveRequests.get(channel);
            const limit = options.limit || 500;
            const urls = {
                status: "/api/admin/rclone/status",
                logs: `/api/admin/rclone/logs?limit=${limit}`,
                system: `/api/admin/system/logs?limit=${limit}`,
            };
            const request = Promise.resolve().then(async () => {
                try {
                    const data = await api(urls[channel], { silentLoading: options.silentLoading !== false, skipButtonLoading: true });
                    if (!data || data.success === false) throw new Error(data?.message || PROGRESS_COPY.invalidResponse);
                    if (channel === "status") {
                        if (!data.status || typeof data.status !== "object" || Array.isArray(data.status)
                            || (data.status.transfers !== undefined && !Array.isArray(data.status.transfers))) throw new Error(PROGRESS_COPY.invalidResponse);
                        renderRcloneStatus(data.status);
                    } else if (channel === "logs") {
                        if (!Array.isArray(data.items)) throw new Error(PROGRESS_COPY.invalidResponse);
                        renderRcloneLiveLogs(data.items);
                    } else {
                        if (!Array.isArray(data.lines)) throw new Error(PROGRESS_COPY.invalidResponse);
                        renderSystemLiveLogs(data.lines);
                    }
                    showLiveChannelError(channel);
                    return data;
                } catch (error) {
                    showLiveChannelError(channel, error);
                    return null;
                } finally {
                    liveRequests.delete(channel);
                }
            });
            liveRequests.set(channel, request);
            return request;
        }

        function loadRcloneLive(options = {}) {
            // Each channel renders on arrival and owns only its own in-flight
            // guard. A slow status request cannot pause later log refreshes.
            return Promise.allSettled(["status", "logs", "system"].map((channel) => loadLiveChannel(channel, options)));
        }

        function loadTransfers(options = {}) {
            initTransferControls();
            return loadLiveChannel("status", options);
        }

        function startRcloneLivePolling() {
            if (state.rcloneLiveTimer) return;
            state.rcloneLiveTimer = window.setInterval(() => {
                if (typeof document !== "undefined" && document.hidden) return;
                if (state.activeTab === "transfers") {
                    loadTransfers();
                    return;
                }
                if (state.activeTab !== "rclone") return;
                loadRcloneLive().catch((error) => {
                    showLiveChannelError("logs", error);
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
            const jobsTab = getElement("adminTabJobs");
            if (!jobsTab) {
                toast(RCLONE_COPY.missingTaskEntry, "error");
                return;
            }
            jobsTab.click();
            jobsTab.focus();
            toast(RCLONE_COPY.taskNavigation, "info");
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
            const box = getElement("adminRcloneCheckResult");
            if (!box) return;
            box.hidden = false;
            box.innerHTML = `
                <strong>${escapeHtml(data.message || PROGRESS_COPY.environmentDone)}</strong>
                <div class="check-list">
                    ${(data.items || []).map((item) => `<div class="${item.ok ? "ok-text" : "danger-text"}">${item.ok ? PROGRESS_COPY.checkPass : PROGRESS_COPY.checkFail}：${escapeHtml(item.name)} - ${escapeHtml(item.message || "")}</div>`).join("")}
                </div>
            `;
        }

        return Object.freeze({
            renderRcloneStatus,
            setTransferFilter,
            setTransferSearch,
            setTransferHistoryOpen,
            renderRcloneLiveLogs,
            loadTransfers,
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
