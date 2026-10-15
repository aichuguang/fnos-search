(function () {
    function create(context) {
        const {
            state,
            getElement,
            api,
            toast,
            escapeHtml,
            icon,
            statusPill,
            statusText,
            formatDate,
            normalizePagination,
            renderPager,
            openDrawer,
            closeDrawer,
            promptDialog,
            confirmDialog,
            renderTimelineRawData,
            loadRclone,
        } = context;
        const adminState = state;
        const $ = getElement;
        const pendingJobActions = new Set();
        const REVIEW_COPY = Object.freeze({
            action: "处理确认",
            viewJob: "查看关联任务",
            risk: "需人工复核",
            suggestion: "请处理确认，核实原任务的执行结果",
            noJobSuggestion: "需人工审核，请先查看详情和审核原因",
            submittingRisk: "结果待确认",
            submittingSuggestion: "网盘提交处理中，请勿重复提交",
            checkPending: "执行结果待核实",
            title: "人工复核：请先核对执行结果",
            reason: "复核原因",
            reasonFallback: "原任务已暂停，需要核实执行结果后继续。",
            phase: "当前阶段",
            resumePhase: "继续阶段",
            savedPath: "网盘保存目录（原任务记录）",
            targetPath: "目标目录",
            checkPath: "核对目录",
            missingPath: "未记录，请在对应网盘中核对",
            guidance: "操作指引",
            preserveEvidence: "若已有本次文件、存在进行中的网盘任务，或无法确认结果，请保留原任务和相关证据，不要重新提交；此处不能直接标记入库成功。",
            checklist: "请核对源端暂存目录、目标目录，以及对应网盘的任务列表（含进行中和历史任务）。误判可能导致重复转存或产生重复文件。",
            resume: "确认并继续",
            resubmit: "确认未执行，重新提交",
            reload: "刷新任务状态",
            processing: "处理中...",
            unavailable: "该操作当前不可用，请刷新任务详情后确认。",
            versionMissing: "任务状态版本缺失，请刷新任务详情后重试。",
            noteLabel: "核对说明（目录、任务列表和核对时间，最多 1000 字）",
            noteRequired: "请填写实际核对说明，不能留空。",
            noteTooLong: "核对说明不能超过 1000 个字符，请精简后重试。",
            recorded: "确认已记录，请刷新状态",
            unknownPhase: "未知阶段，请刷新任务状态",
            next: "继续确认",
            attest: "已核对不存在本次文件或进行中的任务，允许重新提交",
            resubmitRisk: "仅在确认外部操作未执行时继续。判断错误会造成重复转存；系统不会替你确认网盘中的实际结果。",
            resumeMessage: "请确认导致暂停的问题已处理。系统将保留已有回执继续原任务，不会另建入库任务。",
            retryFailure: "任务重试失败，请保留当前任务并检查原因。",
            reviewFailure: "复核操作未成功，请保留当前任务并检查原因。",
            retrySuccess: "已提交原任务继续执行",
            reviewSuccess: "已记录核对说明，原任务已重新排队",
            refreshFailure: "操作已受理，但部分状态刷新失败，请使用“刷新任务状态”核对进度。",
        });
        const REVIEW_PHASES = Object.freeze({
            planning: "检查入库计划",
            source_materializing: "来源网盘转存",
            source_waiting_visible: "等待来源文件可见",
            native_submitting: "提交原生网盘任务",
            native_waiting: "等待网盘任务完成",
            transferring: "跨盘搬运",
            target_verifying: "核对目标暂存文件",
            organizing: "整理媒体文件",
            finalizing: "归入最终目录",
            final_verifying: "核对最终文件",
            ack_pending: "确认入库结果",
            cleanup_pending: "清理临时文件",
            completed: "入库完成",
            retry_wait: "等待继续执行",
            manual_review: "等待人工复核",
            cancel_pending: "等待取消结果",
            failed: "执行失败",
            cancelled: "已取消",
        });

        async function loadOverview() {
            const data = await api("/api/admin/dashboard");
            const summary = data.summary || {};
            adminState.dashboardSummary = summary;
            adminState.overviewJobs = summary.recent_jobs || [];
            const statusCounts = summary.status_counts || {};
            const requestCounts = summary.guest_request_status_counts || {};
            const pending = requestCounts.pending_review || 0;
            const processingStatuses = ["created", "provider_submitting", "submitted", "waiting_transfer", "transferring", "waiting_openlist", "waiting_organizer", "organizing", "confirming", "review", "refreshing", "retry_requested"];
            const processing = processingStatuses.reduce((total, status) => total + (statusCounts[status] || 0), 0);
            const failed = (statusCounts.failed || 0) + (statusCounts.error || 0);
            const completed = (statusCounts.done || 0) + (statusCounts.success || 0) + (statusCounts.completed || 0);
            if ($("pendingRequestBadge")) $("pendingRequestBadge").textContent = pending;
            $("adminOverview").innerHTML = `
                ${renderMetricCard("待审核", pending, "需要管理员处理", "clock", "orange")}
                ${renderMetricCard("正在入库", processing, (summary.rclone || {}).running ? "搬运服务运行中" : "任务队列状态", "motrix", "cyan")}
                ${renderMetricCard("异常任务", failed, failed ? "等待人工处理" : "暂无异常", "failed", "red")}
                ${renderMetricCard("已完成", completed, "最近任务统计", "success", "green")}
            `;
            renderOverviewHeader({ pending, failed });
            renderOverviewJobs();
            renderOverviewServices({ pending, processing, failed, completed });
        }

        function renderOverviewHeader({ pending, failed }) {
            const hour = new Date().getHours();
            const greeting = hour < 6 ? "夜深了" : hour < 12 ? "上午好" : hour < 18 ? "下午好" : "晚上好";
            const attention = pending + failed;
            if ($("adminOverviewGreeting")) {
                $("adminOverviewGreeting").textContent = attention
                    ? `${greeting}，当前有 ${attention} 项需要关注`
                    : `${greeting}，任务运行平稳`;
            }
            if ($("adminOverviewMessage")) {
                $("adminOverviewMessage").textContent = attention
                    ? `待审核 ${pending} 项，异常任务 ${failed} 项。`
                    : "没有需要立即处理的审核或异常任务。";
            }
        }

        function renderMetricCard(title, value, subText, iconName, tone) {
            return `
                <div class="metric-card tone-${tone}">
                    <div class="metric-icon">${icon(iconName)}</div>
                    <div>
                        <span>${escapeHtml(title)}</span>
                        <strong>${escapeHtml(value)}</strong>
                        <small>${escapeHtml(subText)}</small>
                    </div>
                </div>
            `;
        }

        async function loadRequests() {
            const status = $("requestStatusFilter").value;
            const page = adminState.pagination.requests.page || 1;
            const perPage = adminState.pagination.requests.per_page || 20;
            const query = new URLSearchParams({ page: String(page), per_page: String(perPage) });
            if (status) query.set("status", status);
            const data = await api(`/api/admin/requests?${query.toString()}`);
            adminState.requests = data.items || [];
            adminState.pagination.requests = normalizePagination(data.pagination, { page, per_page: perPage });
            renderRequests();
        }

        function renderRequests() {
            const body = $("adminRequestsBody");
            const goRequestsPage = (page) => {
                adminState.pagination.requests.page = page;
                loadRequests().catch((error) => toast(error.message, "error"));
            };
            if (!adminState.requests.length) {
                body.innerHTML = `<tr><td colspan="9" class="empty-cell">暂无访客提交</td></tr>`;
                renderListPagers(["requestsPagerTop", "requestsPager"], adminState.pagination.requests, goRequestsPage);
                return;
            }
            body.innerHTML = adminState.requests
                .map(
                    (item) => `
                    <tr>
                        <td><code>${escapeHtml(item.request_token)}</code></td>
                        <td>${escapeHtml(item.title)}</td>
                        <td>${escapeHtml(item.category_label || item.category)}</td>
                        <td>${escapeHtml(sourceText(item.source_provider_kind || "网盘分享"))}</td>
                        <td>${escapeHtml(formatDate(item.created_at))}</td>
                        <td>${statusPill(item.status)}</td>
                        <td>${riskPill(item)}</td>
                        <td>${escapeHtml(suggestionText(item))}</td>
                        <td class="table-actions">${requestActionButtons(item)}</td>
                    </tr>
                `
                )
                .join("");
            body.querySelectorAll("[data-request-detail]").forEach((button) => button.addEventListener("click", () => showRequestDetail(button.dataset.requestDetail)));
            body.querySelectorAll("[data-request-job]").forEach((button) => button.addEventListener("click", () => showJobDetail(button.dataset.requestJob)));
            body.querySelectorAll("[data-request-approve]").forEach((button) => button.addEventListener("click", () => approveRequest(button.dataset.requestApprove)));
            body.querySelectorAll("[data-request-retry]").forEach((button) => button.addEventListener("click", () => approveRequest(button.dataset.requestRetry, { retry: true })));
            body.querySelectorAll("[data-request-reject]").forEach((button) => button.addEventListener("click", () => rejectRequest(button.dataset.requestReject)));
            body.querySelectorAll("[data-request-cancel]").forEach((button) => button.addEventListener("click", () => cancelRequest(button.dataset.requestCancel)));
            renderListPagers(["requestsPagerTop", "requestsPager"], adminState.pagination.requests, goRequestsPage);
        }

        function requestActionButtons(item) {
            const status = String(item.status || "").toLowerCase();
            const terminal = ["done", "success", "cancelled"].includes(status);
            const buttons = [`<button class="secondary mini" data-request-detail="${item.id}">详情</button>`];
            if (item.job_id) {
                buttons.push(`<button class="mini" data-request-job="${escapeHtml(item.job_id)}">${requestNeedsReview(item) ? REVIEW_COPY.action : REVIEW_COPY.viewJob}</button>`);
            }
            if (status === "pending_review" && !item.job_id) {
                buttons.push(`<button class="mini" data-request-approve="${item.id}">批准</button>`);
                buttons.push(`<button class="secondary mini danger" data-request-reject="${item.id}">拒绝</button>`);
            } else if (status === "failed" && !item.job_id) {
                buttons.push(`<button class="mini" data-request-retry="${item.id}">重试提交</button>`);
            } else if (!["rejected", "unsupported", "failed"].includes(status) && !terminal) {
                buttons.push(`<button class="secondary mini danger" data-request-reject="${item.id}">拒绝</button>`);
            }
            if (!terminal && status !== "rejected") {
                buttons.push(`<button class="secondary mini danger" data-request-cancel="${item.id}">取消</button>`);
            }
            if (status === "rejected" && item.job_id) {
                buttons.push(`<button class="secondary mini danger" data-request-cancel="${item.id}">取消任务</button>`);
            }
            return buttons.join("");
        }

        function requestNeedsReview(item) {
            return ["review", "manual_review"].includes(String(item.status || "").toLowerCase());
        }

        function sourceText(value) {
            const map = {
                quark: "网盘分享",
                uc: "网盘分享",
                cloud139: "移动云",
                cloud189: "天翼云",
                magnet: "种子/磁链",
                torrent: "种子",
            };
            return map[value] || value || "-";
        }

        function providerText(value) {
            const map = {
                quark: "夸克网盘",
                mobile: "移动云",
                tianyi: "天翼云盘",
                sixpan: "六盘",
            };
            const key = String(value || "").toLowerCase();
            return map[key] || value || "-";
        }

        function sourceFormText(value) {
            const map = { share: "分享链接", magnet: "磁链/种子", local: "本地文件", http: "HTTP 资源" };
            const key = String(value || "").toLowerCase();
            return map[key] || value || "-";
        }

        function jobSourceText(item = {}) {
            const provider = providerText(item.source_provider_kind);
            const form = sourceFormText(item.source_form);
            return provider === "-" ? form : form === "-" ? provider : `${provider} · ${form}`;
        }

        function transferStrategyText(value) {
            const map = {
                native_save: "同源原生转存",
                native_offline: "原生离线任务",
                rclone_copy: "OpenList / rclone 跨盘搬运",
                relay_upload_provider: "Provider 原生 API 直传（本地中转）",
            };
            const key = String(value || "").toLowerCase();
            return map[key] || value || "-";
        }

        function riskPill(item) {
            const status = String(item.status || "");
            if (requestNeedsReview(item)) return `<span class="pill warn">${REVIEW_COPY.risk}</span>`;
            if (status === "provider_submitting") return `<span class="pill warn">${REVIEW_COPY.submittingRisk}</span>`;
            const guard = contentGuardInfo(item);
            if (guard) return `<span class="pill warn">${escapeHtml(guard.label || "内容风控")}</span>`;
            if (["failed", "rejected", "unsupported"].includes(status)) return `<span class="pill error">高风险</span>`;
            if (status === "cancelled") return `<span class="pill info">已终止</span>`;
            if (status === "pending_review") return `<span class="pill warn">中风险</span>`;
            return `<span class="pill ok">低风险</span>`;
        }

        function suggestionText(item) {
            const status = String(item.status || "");
            if (requestNeedsReview(item)) return item.job_id ? REVIEW_COPY.suggestion : REVIEW_COPY.noJobSuggestion;
            if (status === "provider_submitting") return REVIEW_COPY.submittingSuggestion;
            const guard = contentGuardInfo(item);
            if (guard) return guard.reason || "疑似敏感内容，人工审核";
            if (status === "pending_review") return "人工审核";
            if (status === "cancelled") return "已取消";
            if (["failed", "rejected", "unsupported"].includes(status)) return "需复核";
            return "可自动通过";
        }

        function contentGuardInfo(item) {
            const raw = item?.raw_data && typeof item.raw_data === "object" ? item.raw_data : {};
            const guard = raw.content_guard && typeof raw.content_guard === "object" ? raw.content_guard : null;
            if (!guard || guard.review_required !== true) return null;
            return {
                label: guard.label || "内容风控",
                reason: guard.public_message || guard.reason || guard.admin_message || "疑似敏感内容，等待人工审核",
                score: guard.score,
                safety_score: guard.safety_score,
                evidence: Array.isArray(guard.evidence) ? guard.evidence : [],
            };
        }

        function mediaIdentityInfo(item) {
            const raw = item?.raw_data && typeof item.raw_data === "object" ? item.raw_data : {};
            const request = raw.request && typeof raw.request === "object" ? raw.request : {};
            const media = raw.media_identity && typeof raw.media_identity === "object"
                ? raw.media_identity
                : request.media_identity && typeof request.media_identity === "object" ? request.media_identity : null;
            return media && media.tmdb_id ? media : null;
        }

        function jobPosterHtml(item) {
            const media = mediaIdentityInfo(item);
            if (!media || !media.poster_url) return "";
            return `<img class="job-poster" src="${escapeHtml(media.poster_url)}" alt="${escapeHtml(media.title || item.title || "影视海报")}" loading="lazy" referrerpolicy="no-referrer" onerror="this.style.display='none'">`;
        }

        function jobPosterSubText(item) {
            const media = mediaIdentityInfo(item);
            if (!media) return "";
            const type = media.media_type === "tv" ? "电视剧" : "电影";
            const text = [media.year, type].filter(Boolean).join(" · ");
            return text ? `<p class="muted">${escapeHtml(text)}</p>` : "";
        }

        function renderMediaIdentity(item) {
            const media = mediaIdentityInfo(item);
            if (!media) return "";
            const type = media.media_type === "tv" ? "电视剧" : "电影";
            const subtitle = [media.year, type, `TMDB #${media.tmdb_id}`].filter(Boolean).join(" · ");
            return `<section class="admin-media-identity">
                ${media.poster_url ? `<img src="${escapeHtml(media.poster_url)}" alt="${escapeHtml(media.title || "影视海报")}" loading="lazy">` : ""}
                <div><span>访客确认的影视</span><strong>${escapeHtml(media.title || "-")}</strong><small>${escapeHtml(subtitle)}</small>${media.original_title ? `<small>原名：${escapeHtml(media.original_title)}</small>` : ""}${media.original_query ? `<small>原始搜索：${escapeHtml(media.original_query)}</small>` : ""}</div>
            </section>`;
        }

        async function showRequestDetail(id) {
            const data = await api(`/api/admin/requests/${id}`);
            const job = data.job || null;
            const item = { ...(data.request || {}), job_id: data.request?.job_id || job?.id };
            const events = data.events || [];
            const guard = contentGuardInfo(item);
            const needsReview = requestNeedsReview(item);
            openDrawer(
                `请求详情`,
                `
                <div class="request-detail">
                    ${renderMediaIdentity(item)}
                    <div class="detail-block">
                        <span>资源名称</span>
                        <strong>${escapeHtml(item.title || "-")}</strong>
                    </div>
                    <div class="detail-block">
                        <span>提交编号</span>
                        <code>${escapeHtml(item.request_token || "-")}</code>
                    </div>
                    <div class="detail-block">
                        <span>资源分类</span>
                        ${statusPill(item.category_label || item.category || "-")}
                    </div>
                    <div class="detail-block">
                        <span>来源类型</span>
                        <strong>${escapeHtml(sourceText(item.source_provider_kind || "网盘分享"))}</strong>
                    </div>
                    <div class="detail-block">
                        <span>当前状态</span>
                        ${statusPill(item.status)}
                    </div>
                    <div class="detail-block">
                        <span>用户备注</span>
                        <p>${escapeHtml(item.note || "无")}</p>
                    </div>
                    <div class="detail-block ${needsReview ? "warn-block" : "ok-block"}">
                        <span>检测结果</span>
                        <strong>${needsReview ? REVIEW_COPY.checkPending : "检测完成"}</strong>
                        <small>状态：${escapeHtml(statusText(item.status || ""))}</small>
                    </div>
                    <div class="detail-block warn-block">
                        <span>风险提示</span>
                        <strong>${escapeHtml(suggestionText(item))}</strong>
                        <small>${guard ? `风险分：${escapeHtml(guard.score ?? "-")}；安全分：${escapeHtml(guard.safety_score ?? "-")}；请重点核对资源名和解析文件名。` : needsReview && item.job_id ? REVIEW_COPY.preserveEvidence : "请根据资源名称、分类和备注确认是否入库。"}</small>
                    </div>
                    ${guard ? `
                    <div class="detail-block warn-block">
                        <span>内容风控</span>
                        <strong>${escapeHtml(guard.reason)}</strong>
                        <small>${guard.evidence.length ? escapeHtml(guard.evidence.map((row) => `${row.where || ""}:${row.label || ""}(${row.match || ""})`).join("；")) : "未记录具体命中项"}</small>
                    </div>` : ""}
                </div>
                <h4>审核事件</h4>
                <div class="timeline">${events.length ? events.map(renderTimelineItem).join("") : `<div class="empty">暂无事件</div>`}</div>
                <h4>关联任务</h4>
                <div class="detail-block">
                    <span>关联任务</span>
                    <strong>${job ? `#${job.id} ${escapeHtml(statusText(job.status || ""))}` : "未关联"}</strong>
                </div>
                <div class="drawer-action-stack">${requestDrawerActions(item)}</div>
                `
            );
            $("adminDrawerBody").querySelector("[data-drawer-job]")?.addEventListener("click", (event) => showJobDetail(event.currentTarget.dataset.drawerJob));
            $("adminDrawerBody").querySelector("[data-drawer-approve]")?.addEventListener("click", (event) => approveRequest(event.currentTarget.dataset.drawerApprove));
            $("adminDrawerBody").querySelector("[data-drawer-retry]")?.addEventListener("click", (event) => approveRequest(event.currentTarget.dataset.drawerRetry, { retry: true }));
            $("adminDrawerBody").querySelector("[data-drawer-reject]")?.addEventListener("click", (event) => rejectRequest(event.currentTarget.dataset.drawerReject));
            $("adminDrawerBody").querySelector("[data-drawer-cancel]")?.addEventListener("click", (event) => cancelRequest(event.currentTarget.dataset.drawerCancel));
        }

        function requestDrawerActions(item) {
            const status = String(item.status || "").toLowerCase();
            const terminal = ["done", "success", "cancelled"].includes(status);
            const buttons = [];
            if (item.job_id) {
                buttons.push(`<button data-drawer-job="${escapeHtml(item.job_id)}">${requestNeedsReview(item) ? REVIEW_COPY.action : REVIEW_COPY.viewJob}</button>`);
            }
            if (status === "pending_review" && !item.job_id) {
                buttons.push(`<button data-drawer-approve="${item.id}">批准入库</button>`);
                buttons.push(`<button class="secondary danger" data-drawer-reject="${item.id}">拒绝请求</button>`);
            } else if (status === "failed" && !item.job_id) {
                buttons.push(`<button data-drawer-retry="${item.id}">重试提交</button>`);
            } else if (!["rejected", "unsupported", "failed"].includes(status) && !terminal) {
                buttons.push(`<button class="secondary danger" data-drawer-reject="${item.id}">拒绝请求</button>`);
            }
            if (!terminal && status !== "rejected") {
                buttons.push(`<button class="secondary danger" data-drawer-cancel="${item.id}">取消并清理</button>`);
            }
            if (status === "rejected" && item.job_id) {
                buttons.push(`<button class="secondary danger" data-drawer-cancel="${item.id}">取消关联任务</button>`);
            }
            return buttons.join("") || `<button class="secondary" type="button" disabled>无可用操作</button>`;
        }

        async function requestJobAction(path, options) {
            try {
                return await api(path, options);
            } catch (error) {
                toast(error?.message || "操作请求失败，请刷新状态后核对结果", "error");
                return null;
            }
        }

        async function approveRequest(id, { retry = false } = {}) {
            const data = await requestJobAction(`/api/admin/requests/${id}/approve`, { method: "POST", body: JSON.stringify(retry ? { retry: true } : {}) });
            if (!data) return;
            toast(data.message || (retry ? "已重新提交后台处理" : "已批准提交"), "success");
            closeDrawer();
            await Promise.all([loadRequests(), loadJobs(), loadOverview()]);
        }

        async function rejectRequest(id) {
            const reason = await promptDialog({
                title: "拒绝访客提交",
                message: "请输入拒绝原因。确认后会记录审核事件，并尝试清理关联任务。",
                defaultValue: "资源不符合入库要求",
                required: true,
                confirmText: "拒绝并清理",
                tone: "danger",
            });
            if (reason === null) return;
            const data = await requestJobAction(`/api/admin/requests/${id}/reject`, {
                method: "POST",
                body: JSON.stringify({ reason, cleanup: true, delete_source: true, delete_temp: true, delete_target_partial: true, stop_running: true }),
            });
            if (!data) return;
            const cleanupMessage = data.job_cancel?.cleanup?.message;
            toast(cleanupMessage || "已拒绝提交", "success");
            closeDrawer();
            await Promise.all([loadRequests(), loadJobs(), loadOverview(), loadRclone()]);
        }

        async function cancelRequest(id) {
            const reason = await promptDialog({
                title: "取消提交并清理",
                message: "请输入取消原因。确认后会尝试删除源端待搬运文件和 rclone 本地 temp 缓存。",
                defaultValue: "不再入库，取消并清理",
                required: true,
                confirmText: "取消并清理",
                tone: "danger",
            });
            if (reason === null) return;
            const data = await requestJobAction(`/api/admin/requests/${id}/cancel`, {
                method: "POST",
                body: JSON.stringify({
                    reason,
                    cleanup: true,
                    delete_source: true,
                    delete_temp: true,
                    delete_target_partial: true,
                    stop_running: true,
                }),
            });
            if (!data) return;
            toast(data.job_cancel?.cleanup?.message || data.message || "已取消提交", "success");
            closeDrawer();
            await Promise.all([loadRequests(), loadJobs(), loadOverview(), loadRclone()]);
        }

        async function loadJobs() {
            const page = adminState.pagination.jobs.page || 1;
            const perPage = adminState.pagination.jobs.per_page || 20;
            const query = new URLSearchParams({ page: String(page), per_page: String(perPage) });
            const status = $("jobStatusFilter").value;
            const category = $("jobCategoryFilter").value;
            const keyword = $("jobKeywordFilter").value.trim();
            if (status) query.set("status", status);
            if (category) query.set("category", category);
            if (keyword) query.set("keyword", keyword);
            const data = await api(`/api/admin/jobs?${query.toString()}`);
            adminState.jobs = data.items || [];
            adminState.pagination.jobs = normalizePagination(data.pagination, { page, per_page: perPage });
            renderJobs();
            renderOverviewJobs();
        }

        function isMediaRefreshOnlyJob(item = {}) {
            const rawData = item.raw_data && typeof item.raw_data === "object" ? item.raw_data : {};
            const completion = rawData.completion && typeof rawData.completion === "object" ? rawData.completion : {};
            const refreshOnly = Boolean(completion.provider_completed)
                && String(completion.retry_action || "").toLowerCase() === "media_refresh_only";
            return String(item.source_provider_kind || "").toLowerCase() === "sixpan"
                && String(item.source_form || "").toLowerCase() === "magnet"
                && ["review", "refreshing"].includes(String(item.status || "").toLowerCase())
                && refreshOnly;
        }

        function renderJobs() {
            const body = $("adminJobsBody");
            const goJobsPage = (page) => {
                adminState.pagination.jobs.page = page;
                loadJobs().catch((error) => toast(error.message, "error"));
            };
            if (!adminState.jobs.length) {
                body.innerHTML = `<tr><td colspan="7" class="empty-cell">暂无任务</td></tr>`;
                renderListPagers(["jobsPagerTop", "jobsPager"], adminState.pagination.jobs, goJobsPage);
                return;
            }
            body.innerHTML = adminState.jobs
                .map(
                    (item) => `
                    <tr>
                        <td>#${item.id}</td>
                        <td><div class="job-title-cell">${jobPosterHtml(item)}<div class="job-title-main"><strong>${escapeHtml(item.title)}</strong>${jobPosterSubText(item)}</div></div></td>
                        <td>${escapeHtml(item.category_label || item.category)}</td>
                        <td>${statusPill(item.status)}</td>
                        <td>${escapeHtml(jobSourceText(item))}</td>
                        <td>${escapeHtml(formatDate(item.updated_at))}</td>
                        <td class="table-actions">${jobActionButtons(item)}</td>
                    </tr>
                `
                )
                .join("");
            body.querySelectorAll("[data-job-detail]").forEach((button) => button.addEventListener("click", () => showJobDetail(button.dataset.jobDetail)));
            body.querySelectorAll("[data-job-retry]").forEach((button) => button.addEventListener("click", () => retryJob(button.dataset.jobRetry)));
            body.querySelectorAll("[data-job-media-refresh]").forEach((button) => button.addEventListener("click", () => retryMediaRefreshOnly(button.dataset.jobMediaRefresh, button)));
            body.querySelectorAll("[data-job-cancel]").forEach((button) => button.addEventListener("click", () => cancelJob(button.dataset.jobCancel)));
            body.querySelectorAll("[data-job-delete]").forEach((button) => button.addEventListener("click", () => deleteJob(button.dataset.jobDelete, button)));
            renderListPagers(["jobsPagerTop", "jobsPager"], adminState.pagination.jobs, goJobsPage);
        }

        function jobActionButtons(item) {
            const id = escapeHtml(item.id);
            const refreshOnly = isMediaRefreshOnlyJob(item)
                ? `<button class="mini" data-job-media-refresh="${id}" title="六盘文件已完成，只重新刷新飞牛媒体库，不会再次提交网盘离线任务">仅重试媒体库刷新</button>`
                : "";
            const deleteButton = jobRecordDeletable(item)
                ? `<button class="secondary mini danger" data-job-delete="${id}" title="删除记录">删除</button>`
                : "";
            return `${refreshOnly}<button class="secondary mini" data-job-detail="${id}" title="查看任务详情和可用操作">详情</button>${deleteButton}`;
        }

        function jobRecordDeletable(item) {
            return ["done", "success", "completed", "cancelled"].includes(String(item?.status || "").toLowerCase());
        }

        function renderOverviewJobs() {
            const list = $("adminOverviewJobsList");
            if (!list) return;
            const items = adminState.overviewJobs || [];
            const total = adminState.dashboardSummary?.total_recent_jobs ?? items.length;
            if ($("adminOverviewJobsCount")) $("adminOverviewJobsCount").textContent = `共 ${total} 项`;
            if (!items.length) {
                list.innerHTML = `<div class="dashboard-empty">暂无入库任务。</div>`;
                renderOverviewPipeline(null);
                renderOverviewActivity([]);
                return;
            }
            list.innerHTML = items
                .map(
                    (item) => `
                    <article class="dashboard-task-row">
                        <div class="dashboard-task-cover">${overviewPosterHtml(item) || icon(categoryIconName(item.category))}</div>
                        <div class="dashboard-task-main">
                            <strong title="${escapeHtml(item.title || "未命名任务")}">${escapeHtml(item.title || "未命名任务")}</strong>
                            <small>${escapeHtml(jobSourceText(item))} · ${escapeHtml(item.category_label || item.category || "未分类")}</small>
                        </div>
                        <div class="dashboard-task-stage">
                            <span>${escapeHtml(dashboardStageLabel(item.status))}</span>
                            <small>${escapeHtml(formatDate(item.updated_at))}</small>
                        </div>
                        <div class="dashboard-task-status">${statusPill(item.status)}</div>
                        <button class="secondary mini dashboard-task-action" type="button" data-job-detail="${escapeHtml(item.id)}">详情</button>
                    </article>
                `
                )
                .join("");
            list.querySelectorAll("[data-job-detail]").forEach((button) => button.addEventListener("click", () => showJobDetail(button.dataset.jobDetail)));
            const selected = items.find((item) => !dashboardTerminalStatuses().includes(String(item.status || "").toLowerCase())) || items[0];
            renderOverviewPipeline(selected);
            renderOverviewActivity(items.slice(0, 3));
        }

        function categoryIconName(category) {
            return { movie: "movie", tv: "tv", anime: "anime", variety: "variety" }[String(category || "").toLowerCase()] || "other";
        }

        function overviewPosterHtml(item) {
            const media = mediaIdentityInfo(item);
            const poster = media?.poster_url || item?.poster_url || "";
            if (!poster) return "";
            return `<img src="${escapeHtml(poster)}" alt="${escapeHtml(media?.title || item.title || "影视海报")}" loading="lazy" referrerpolicy="no-referrer" onerror="this.remove()">`;
        }

        function dashboardTerminalStatuses() {
            return ["done", "success", "completed", "failed", "error", "cancelled", "rejected"];
        }

        function dashboardStageIndex(status) {
            const normalized = String(status || "").toLowerCase();
            if (["done", "success", "completed"].includes(normalized)) return 5;
            if (normalized === "refreshing") return 4;
            if (["waiting_openlist", "waiting_organizer", "organizing", "confirming", "review"].includes(normalized)) return 3;
            if (["waiting_transfer", "transferring"].includes(normalized)) return 2;
            if (["provider_submitting", "submitted"].includes(normalized)) return 1;
            return 0;
        }

        function dashboardStageLabel(status) {
            const labels = ["资源审核", "网盘转存", "文件搬运", "整理与命名", "刷新媒体库", "入库完成"];
            return labels[dashboardStageIndex(status)] || labels[0];
        }

        function renderOverviewPipeline(item) {
            const target = $("adminOverviewPipeline");
            if (!target) return;
            if (!item) {
                target.innerHTML = `<div class="dashboard-empty">暂无进行中的任务。</div>`;
                return;
            }
            const currentIndex = dashboardStageIndex(item.status);
            const normalized = String(item.status || "").toLowerCase();
            const failed = ["failed", "error", "cancelled", "rejected"].includes(normalized);
            const steps = [
                ["资源审核", "确认提交内容与目标分类"],
                ["网盘转存", "提交至目标网盘"],
                ["文件搬运", "将资源搬运到暂存目录"],
                ["整理与命名", "识别媒体信息并规范目录"],
                ["刷新媒体库", "同步飞牛媒体库索引"],
            ];
            target.innerHTML = `
                <div class="dashboard-selected-job">
                    <span class="dashboard-selected-icon">${overviewPosterHtml(item) || icon(categoryIconName(item.category))}</span>
                    <div>
                        <strong>${escapeHtml(item.title || "未命名任务")}</strong>
                        <small>#${escapeHtml(item.id)} · ${escapeHtml(jobSourceText(item))}</small>
                    </div>
                    ${statusPill(item.status)}
                </div>
                <div class="dashboard-pipeline-list">
                    ${steps.map(([name, description], index) => {
                        const stateClass = failed && index === currentIndex ? "error" : currentIndex > index ? "done" : currentIndex === index ? "current" : "";
                        const mark = currentIndex > index ? "✓" : failed && index === currentIndex ? "!" : String(index + 1);
                        return `<div class="dashboard-pipeline-step ${stateClass}"><span class="dashboard-step-dot">${mark}</span><div><strong>${name}</strong><small>${description}</small></div></div>`;
                    }).join("")}
                </div>
                <button class="secondary mini dashboard-pipeline-detail" type="button" data-job-detail="${escapeHtml(item.id)}">查看任务详情</button>
            `;
            target.querySelector("[data-job-detail]")?.addEventListener("click", () => showJobDetail(item.id));
        }

        function renderOverviewServices() {
            const target = $("adminOverviewServices");
            if (!target) return;
            const summary = adminState.dashboardSummary || {};
            const health = summary.health || {};
            const services = Array.isArray(health.items) ? health.items : [];
            const healthSummary = $("adminOverviewHealthSummary");
            if (healthSummary) {
                healthSummary.textContent = health.issue_count
                    ? `${health.issue_count} 项需要关注`
                    : services.length ? "核心链路运行正常" : "健康状态暂不可用";
            }
            if (!services.length) {
                target.innerHTML = `<div class="dashboard-empty">健康状态暂不可用。</div>`;
                return;
            }
            const targets = {
                worker: "rclone",
                queue: "jobs",
                rclone: "rclone",
                organizer: "organizer",
                scheduler: "updates",
                data: "rclone",
            };
            const icons = {
                worker: "shell",
                queue: "task",
                rclone: "rclone",
                organizer: "openlist",
                scheduler: "refresh",
                data: "shield",
            };
            target.innerHTML = services.map((item) => `
                <button class="dashboard-service-item state-${escapeHtml(item.state || "idle")}" type="button"
                    data-health-target="${escapeHtml(targets[item.id] || "")}" title="${escapeHtml(item.detail || item.summary || "")}">
                    <span class="dashboard-service-icon">${icon(icons[item.id] || "shield")}</span>
                    <span class="dashboard-service-copy"><strong>${escapeHtml(item.label || "系统状态")}</strong><small>${escapeHtml(item.summary || "状态未知")}</small></span>
                </button>
            `).join("");
            target.querySelectorAll("[data-health-target]").forEach((button) => {
                button.addEventListener("click", () => {
                    const tab = button.dataset.healthTarget;
                    if (tab) document.querySelector(`.tab[data-tab="${tab}"]`)?.click();
                });
            });
        }

        function renderOverviewActivity(items) {
            const target = $("adminOverviewActivity");
            if (!target) return;
            if (!items.length) {
                target.innerHTML = `<div class="dashboard-empty">暂无系统动态。</div>`;
                return;
            }
            target.innerHTML = items.map((item) => `
                <div class="dashboard-activity-item">
                    <span class="dashboard-activity-dot"></span>
                    <div>
                        <strong>${escapeHtml(item.title || "未命名任务")}</strong>
                        <p>已进入${escapeHtml(statusText(item.status))}</p>
                        <small>${escapeHtml(formatDate(item.updated_at))}</small>
                    </div>
                </div>
            `).join("");
        }

        async function withJobActionLock(id, button, action) {
            const key = String(id);
            if (pendingJobActions.has(key)) return null;
            pendingJobActions.add(key);
            const previous = button ? {
                disabled: button.disabled,
                html: button.innerHTML,
                busy: button.getAttribute("aria-busy"),
            } : null;
            if (button) {
                button.disabled = true;
                button.textContent = REVIEW_COPY.processing;
                button.setAttribute("aria-busy", "true");
            }
            try {
                return await action();
            } finally {
                pendingJobActions.delete(key);
                if (button) {
                    const consumed = button.dataset.reviewConsumed === "true";
                    button.disabled = previous.disabled || consumed;
                    button.innerHTML = consumed ? REVIEW_COPY.recorded : previous.html;
                    if (previous.busy === null) button.removeAttribute("aria-busy");
                    else button.setAttribute("aria-busy", previous.busy);
                }
            }
        }

        async function refreshAfterJobAction(id, reopenDetail) {
            const reloads = [loadRequests(), loadJobs(), loadOverview()];
            if (reopenDetail) reloads.push(showJobDetail(id));
            const results = await Promise.allSettled(reloads);
            if (results.some((result) => result.status === "rejected")) toast(REVIEW_COPY.refreshFailure, "warning");
        }

        async function submitJobRetry(id, reopenDetail) {
            const data = await api(`/api/admin/jobs/${encodeURIComponent(id)}/retry`, {
                method: "POST",
                body: JSON.stringify({}),
                allowFailure: true,
                skipButtonLoading: true,
            });
            if (data.success !== true) {
                toast(data.message || REVIEW_COPY.retryFailure, "error");
                return data;
            }
            toast(data.message || REVIEW_COPY.retrySuccess, "success");
            await refreshAfterJobAction(id, reopenDetail);
            return data;
        }

        async function retryJob(id, button = null, reopenDetail = false) {
            return withJobActionLock(id, button, async () => {
                try {
                    return await submitJobRetry(id, reopenDetail);
                } catch (error) {
                    toast(error?.message || REVIEW_COPY.retryFailure, "error");
                    return null;
                }
            });
        }

        function transferReviewInfo(job) {
            const execution = job?.transfer_execution;
            if (!execution || typeof execution !== "object") return null;
            const review = execution.review;
            return review && review.required === true ? { execution, review } : null;
        }

        function reviewPaths(job) {
            const paths = job.transfer_execution?.review?.paths;
            const frozenPaths = Array.isArray(paths) ? paths.filter((row) => row && typeof row.path === "string" && row.path.trim()) : [];
            if (frozenPaths.length) {
                return frozenPaths.map((row) => ({
                    label: String(row.label || REVIEW_COPY.checkPath),
                    namespace: String(row.namespace || ""),
                    path: row.path,
                }));
            }
            return [
                { label: REVIEW_COPY.savedPath, namespace: "", path: job.official_save_path || REVIEW_COPY.missingPath },
                { label: REVIEW_COPY.targetPath, namespace: "", path: job.rclone_target_path || job.target_path || job.openlist_visible_path || REVIEW_COPY.missingPath },
            ];
        }

        function reviewPathLabel(row) {
            return `${row.label}${row.namespace ? `（${row.namespace}）` : ""}`;
        }

        function reviewPhaseText(phase) {
            const key = String(phase || "").toLowerCase();
            return key === "" || key === "-" ? "-" : REVIEW_PHASES[key] || REVIEW_COPY.unknownPhase;
        }

        function invalidateReviewSnapshot(job, button) {
            const info = transferReviewInfo(job);
            if (info) {
                info.review.can_resume = false;
                info.review.can_confirm_not_submitted = false;
            }
            if (button) {
                button.dataset.reviewConsumed = "true";
                button.disabled = true;
            }
        }

        function renderTransferReview(job) {
            const info = transferReviewInfo(job);
            if (!info) return "";
            const { execution, review } = info;
            const paths = reviewPaths(job);
            const guidance = Array.isArray(review.guidance) ? review.guidance.join("\n") : review.guidance;
            return `<section class="notice-box warning job-manual-review">
                <strong>${REVIEW_COPY.title}</strong>
                <p><strong>${REVIEW_COPY.reason}：</strong>${escapeHtml(review.reason || REVIEW_COPY.reasonFallback)}</p>
                <div class="detail-grid">
                    <div><span>${REVIEW_COPY.phase}</span>${escapeHtml(reviewPhaseText(execution.phase || "manual_review"))}</div>
                    <div><span>${REVIEW_COPY.resumePhase}</span>${escapeHtml(reviewPhaseText(review.resume_phase))}</div>
                    ${paths.map((row) => `<div><span>${escapeHtml(reviewPathLabel(row))}</span>${escapeHtml(row.path)}</div>`).join("")}
                </div>
                <p><strong>${REVIEW_COPY.guidance}：</strong>${escapeHtml(guidance || REVIEW_COPY.reasonFallback)}</p>
                ${review.can_confirm_not_submitted === true ? `<p>${REVIEW_COPY.checklist}</p>` : ""}
                <p>${REVIEW_COPY.preserveEvidence}</p>
                <div class="filter-row">
                    ${review.can_resume === true ? `<button type="button" data-job-review-action="resume">${REVIEW_COPY.resume}</button>` : ""}
                    ${review.can_confirm_not_submitted === true ? `<button type="button" class="secondary danger" data-job-review-action="confirm_not_submitted">${REVIEW_COPY.resubmit}</button>` : ""}
                    <button type="button" class="secondary" data-job-review-reload="${escapeHtml(job.id)}">${REVIEW_COPY.reload}</button>
                </div>
            </section>`;
        }

        async function reviewJob(job, action, button = null) {
            const info = transferReviewInfo(job);
            if (!info || (action === "resume" ? info.review.can_resume !== true
                : action !== "confirm_not_submitted" || info.review.can_confirm_not_submitted !== true)) {
                toast(REVIEW_COPY.unavailable, "error");
                return null;
            }
            return withJobActionLock(job.id, button, async () => {
                try {
                    if (action === "resume") {
                        const confirmed = await confirmDialog({
                            title: `${REVIEW_COPY.resume} #${job.id}`,
                            message: `${REVIEW_COPY.resumeMessage}\n${REVIEW_COPY.reason}：${info.review.reason || REVIEW_COPY.reasonFallback}\n${REVIEW_COPY.resumePhase}：${reviewPhaseText(info.review.resume_phase)}`,
                            confirmText: REVIEW_COPY.resume,
                            tone: "warning",
                        });
                        if (!confirmed) return null;
                        const data = await submitJobRetry(job.id, true);
                        if (data.success === true || data.recovered === true) invalidateReviewSnapshot(job, button);
                        if (data.success !== true && data.recovered === true) await refreshAfterJobAction(job.id, true);
                        return data;
                    }
                    if (!Number.isInteger(info.execution.row_version) || info.execution.row_version <= 0) {
                        toast(REVIEW_COPY.versionMissing, "error");
                        return null;
                    }
                    const paths = reviewPaths(job);
                    const note = await promptDialog({
                        title: `${REVIEW_COPY.resubmit} #${job.id}`,
                        message: `${REVIEW_COPY.checklist}\n${paths.map((row) => `${reviewPathLabel(row)}：${row.path}`).join("\n")}\n${REVIEW_COPY.preserveEvidence}`,
                        promptLabel: REVIEW_COPY.noteLabel,
                        defaultValue: "",
                        required: true,
                        requiredMessage: REVIEW_COPY.noteRequired,
                        confirmText: REVIEW_COPY.next,
                        tone: "danger",
                    });
                    if (note === null || note === undefined) return null;
                    const normalizedNote = String(note).trim();
                    if (!normalizedNote) {
                        toast(REVIEW_COPY.noteRequired, "error");
                        return null;
                    }
                    if (Array.from(normalizedNote).length > 1000) {
                        toast(REVIEW_COPY.noteTooLong, "error");
                        return null;
                    }
                    const confirmed = await confirmDialog({
                        title: REVIEW_COPY.resubmit,
                        message: `${REVIEW_COPY.attest}。\n${REVIEW_COPY.resubmitRisk}\n${REVIEW_COPY.noteLabel}：${normalizedNote}`,
                        confirmText: REVIEW_COPY.resubmit,
                        tone: "danger",
                    });
                    if (!confirmed) return null;
                    const data = await api(`/api/admin/jobs/${encodeURIComponent(job.id)}/review`, {
                        method: "POST",
                        body: JSON.stringify({
                            action: "confirm_not_submitted",
                            confirmed: true,
                            expected_row_version: info.execution.row_version,
                            note: normalizedNote,
                        }),
                        allowFailure: true,
                        skipButtonLoading: true,
                    });
                    if (data.success !== true) {
                        toast(data.message || REVIEW_COPY.reviewFailure, "error");
                        if (data.recovered === true) {
                            invalidateReviewSnapshot(job, button);
                            await refreshAfterJobAction(job.id, true);
                        }
                        return data;
                    }
                    invalidateReviewSnapshot(job, button);
                    toast(data.message || REVIEW_COPY.reviewSuccess, "success");
                    await refreshAfterJobAction(job.id, true);
                    return data;
                } catch (error) {
                    toast(error?.message || REVIEW_COPY.reviewFailure, "error");
                    if (error?.data?.recovered === true) {
                        invalidateReviewSnapshot(job, button);
                        await refreshAfterJobAction(job.id, true);
                    }
                    return null;
                }
            });
        }

        async function retryMediaRefreshOnly(id, button = null, reopenDetail = false) {
            try {
                const data = await api(`/api/admin/sixpan/jobs/${encodeURIComponent(id)}/retry-media-refresh`, {
                    method: "POST",
                    body: JSON.stringify({}),
                    allowFailure: true,
                    button,
                    buttonLabel: "刷新中...",
                    loadingMessage: "正在重试飞牛媒体库刷新，不会重新提交六盘离线任务...",
                });
                if (!data.success) {
                    await Promise.all([loadJobs(), loadOverview()]);
                    if (reopenDetail) await showJobDetail(id);
                    toast(data.message || "飞牛媒体库刷新失败，请稍后再试", "error");
                    return data;
                }
                toast(data.message || "飞牛媒体库刷新成功", "success");
                await Promise.all([loadJobs(), loadOverview()]);
                if (reopenDetail) await showJobDetail(id);
                return data;
            } catch (error) {
                toast(error?.message || "飞牛媒体库刷新请求失败", "error");
                return null;
            }
        }

        async function cancelJob(id) {
            const reason = await promptDialog({
                title: `取消任务 #${id}`,
                message: "请输入取消原因。确认后会尝试停止当前搬运，并删除源端待搬运文件和 rclone 本地 temp 缓存。",
                defaultValue: "不再入库，取消并清理",
                required: true,
                confirmText: "取消并清理",
                tone: "danger",
            });
            if (reason === null) return;
            const data = await requestJobAction(`/api/admin/jobs/${id}/cancel`, {
                method: "POST",
                body: JSON.stringify({
                    reason,
                    cleanup: true,
                    delete_source: true,
                    delete_temp: true,
                    delete_target_partial: true,
                    stop_running: true,
                }),
            });
            if (!data) return;
            toast(data.cleanup?.message || data.message || "已取消任务", "success");
            closeDrawer();
            await Promise.all([loadRequests(), loadJobs(), loadOverview(), loadRclone()]);
        }

        async function deleteJob(id, button = null) {
            const confirmed = await confirmDialog({
                title: `删除任务 #${id}`,
                message: "确认删除这条记录？此操作不可撤销。",
                confirmText: "删除记录",
                tone: "danger",
            });
            if (!confirmed) return;
            const data = await requestJobAction(`/api/admin/jobs/${encodeURIComponent(id)}`, {
                method: "DELETE",
                allowFailure: true,
                button,
                buttonLabel: "删除中...",
            });
            if (!data) return;
            if (!data.success) {
                toast(data.message || "任务记录删除失败", "error");
                return;
            }
            toast("记录已删除", "success");
            closeDrawer();
            if (adminState.jobs.length === 1 && Number(adminState.pagination.jobs.page || 1) > 1) {
                adminState.pagination.jobs.page -= 1;
            }
            await Promise.all([loadRequests(), loadJobs(), loadOverview()]);
        }

        async function showJobDetail(id, options = {}) {
            const data = await api(`/api/admin/jobs/${id}`);
            const job = data.job || {};
            const timeline = job.timeline || [];
            const technicalEvents = Array.isArray(job.technical_events) ? job.technical_events : [];
            const timelineSummary = job.timeline_summary || {};
            const checks = Array.isArray(job.completion_checks) ? job.completion_checks : [];
            const strategy = String(job.transfer_strategy || "").toLowerCase();
            const rcloneStaging = ["rclone_copy", "relay_upload_provider"].includes(strategy);
            const stagingPath = job.official_save_path || job.target_path || "-";
            const rcloneTargetPath = job.rclone_target_path || "未配置";
            const openlistVisiblePath = job.openlist_visible_path || (rcloneStaging ? "等待 rclone 搬运完成后生成" : "-");
            const organizerScanPath = job.organizer_scan_path || (rcloneStaging ? "等待 rclone 回调后生成" : "-");
            const finalPath = job.organized_target_path || job.openlist_visible_path || job.target_path || job.official_save_path || "-";
            const failedChecks = checks.filter((item) => item && item.success === false);
            const mediaRefreshOnly = isMediaRefreshOnlyJob(job);
            const deletable = jobRecordDeletable(job);
            const review = transferReviewInfo(job);
            openDrawer(
                `任务 #${job.id}`,
                `
                ${renderMediaIdentity(job)}
                <div class="detail-grid">
                    <div><span>标题</span>${escapeHtml(job.title || "-")}</div>
                    <div><span>状态</span>${statusPill(job.status)}</div>
                    <div><span>分类</span>${escapeHtml(job.category_label || job.category || "-")}</div>
                    <div><span>来源</span>${escapeHtml(jobSourceText(job))}</div>
                    <div><span>保存位置</span>${escapeHtml(finalPath)}</div>
                    <div><span>更新时间</span>${escapeHtml(formatDate(job.updated_at || job.created_at))}</div>
                </div>
                ${renderTransferReview(job)}
                ${mediaRefreshOnly ? `<div class="notice-box warning"><strong>六盘文件已经完成</strong><p>当前只剩飞牛媒体库刷新失败。请使用“仅重试媒体库刷新”，系统不会再次提交六盘离线任务。</p></div>` : ""}
                ${failedChecks.length ? `<div class="notice-box warning"><strong>路径检查异常</strong>${renderCompletionChecks(failedChecks)}</div>` : ""}
                <details class="advanced-details">
                    <summary>查看处理路径</summary>
                    <div class="detail-grid">
                        <div><span>完成阶段</span>${escapeHtml(statusText(job.completion_stage || job.status || "-"))}</div>
                        <div><span>传输策略</span>${escapeHtml(transferStrategyText(job.transfer_strategy))}</div>
                        <div><span>来源标识</span>${escapeHtml(job.search_source_id || "-")}</div>
                        <div><span>保存目标</span>${escapeHtml(job.destination_id || "-")}</div>
                        <div><span>目标 Provider</span>${escapeHtml(providerText(job.destination_provider_kind))}</div>
                        ${
                            rcloneStaging
                                ? `
                        <div><span>中转保存</span>${escapeHtml(stagingPath)}</div>
                        <div><span>搬运目标</span>${escapeHtml(rcloneTargetPath)}</div>
                        `
                                : `
                        <div><span>目标路径</span>${escapeHtml(job.target_path || "-")}</div>
                        <div><span>网盘保存</span>${escapeHtml(job.official_save_path || "-")}</div>
                        `
                        }
                        <div><span>OpenList 路径</span>${escapeHtml(openlistVisiblePath)}</div>
                        <div><span>整理扫描目录</span>${escapeHtml(organizerScanPath)}</div>
                        <div><span>最终整理目录</span>${escapeHtml(job.organized_target_path || "-")}</div>
                    </div>
                    ${checks.length ? `<h4>路径检查</h4>${renderCompletionChecks(checks)}` : ""}
                </details>
                <div class="job-timeline-head">
                    <div>
                        <h4>任务流程</h4>
                        <p>按业务阶段汇总，重复的小记录已合并。</p>
                    </div>
                    <div class="job-timeline-counts">
                        <span>${escapeHtml(timelineSummary.phase_count || 0)} 个阶段</span>
                        <span>${escapeHtml(timelineSummary.milestone_count || timeline.length)} 个节点</span>
                    </div>
                </div>
                <div class="timeline job-business-timeline">${timeline.length ? timeline.map(renderTimelineItem).join("") : `<div class="empty">暂无流程记录</div>`}</div>
                <details class="advanced-details job-technical-details" ${options.openTechnical ? "open" : ""}>
                    <summary>完整原始日志 <span>${escapeHtml(timelineSummary.technical_event_count || technicalEvents.length)} 条</span></summary>
                    <p class="muted">包含后台执行、单文件搬运、整理操作、接口参数和原始响应。</p>
                    <div id="jobTechnicalEventsPaged" class="organizer-paged-list"></div>
                </details>
                <div class="filter-row" style="margin-top:16px;">
                    ${review ? "" : mediaRefreshOnly
                        ? `<button data-job-media-refresh-now="${escapeHtml(job.id)}" title="只刷新飞牛媒体库，不会再次提交六盘离线任务">仅重试媒体库刷新</button>`
                        : (deletable ? "" : `<button data-job-retry-now="${job.id}">重试整个任务</button><button class="secondary danger" data-job-cancel-now="${job.id}">取消并清理</button>`)}
                    ${deletable ? `<button class="secondary danger" data-job-delete-now="${escapeHtml(job.id)}">删除记录</button>` : ""}
                </div>
                `,
                { mode: "modal", className: "job-detail-modal" }
            );
            const body = $("adminDrawerBody");
            mountOrganizerPagedList(
                "jobTechnicalEventsPaged",
                `job:${job.id}:technical-events`,
                technicalEvents,
                renderTechnicalEventItem,
                { perPage: 30, empty: "暂无原始日志" },
            );
            body.querySelector("[data-job-retry-now]")?.addEventListener("click", (event) => retryJob(event.currentTarget.dataset.jobRetryNow, event.currentTarget, true));
            body.querySelectorAll("[data-job-review-action]").forEach((button) => button.addEventListener("click", () => reviewJob(job, button.dataset.jobReviewAction, button)));
            body.querySelector("[data-job-review-reload]")?.addEventListener("click", () => showJobDetail(job.id).catch((error) => toast(error.message, "error")));
            body.querySelector("[data-job-media-refresh-now]")?.addEventListener("click", (event) => retryMediaRefreshOnly(event.currentTarget.dataset.jobMediaRefreshNow, event.currentTarget, true));
            body.querySelector("[data-job-cancel-now]")?.addEventListener("click", (event) => cancelJob(event.currentTarget.dataset.jobCancelNow));
            body.querySelector("[data-job-delete-now]")?.addEventListener("click", (event) => deleteJob(event.currentTarget.dataset.jobDeleteNow, event.currentTarget));
            body.querySelector("#jobTechnicalEventsPaged")?.addEventListener("click", (event) => {
                const button = event.target.closest("[data-file-retry]");
                if (button) retryFileEvent(button.dataset.fileRetry);
            });
        }

        function renderCompletionChecks(checks = []) {
            if (!checks.length) return `<div class="empty">暂无体检结果</div>`;
            return `
                <div class="list compact-list">
                    ${checks
                        .map((item) => {
                            const ok = item.success === true;
                            return `
                                <div class="list-item">
                                    <div>
                                        <strong>${ok ? "✅" : "⚠️"} ${escapeHtml(item.label || item.name || "-")}</strong>
                                        <p>${escapeHtml(item.path || item.message || "-")}</p>
                                        ${item.path && item.message ? `<p class="muted">${escapeHtml(item.message)}</p>` : ""}
                                    </div>
                                </div>
                            `;
                        })
                        .join("")}
                </div>
            `;
        }

        function renderTimelineItem(item) {
            const occurrence = Number(item.occurrence_count || 1);
            return `
                <div class="timeline-item ${escapeHtml(item.level || "")}">
                    <div class="timeline-time">${escapeHtml(formatDate(item.created_at))} · ${escapeHtml(item.phase_label || "系统处理")}</div>
                    <div class="timeline-message">${escapeHtml(item.message || item.type || "")}</div>
                    <div class="timeline-meta">${escapeHtml(item.source_label || item.type || "")}${item.status ? ` · ${escapeHtml(statusText(item.status))}` : ""}${occurrence > 1 ? ` · 合并 ${escapeHtml(occurrence)} 条重复记录` : ""}</div>
                    ${renderTimelineRawData(item)}
                </div>
            `;
        }

        function renderTechnicalEventItem(item) {
            const canRetry = item.source === "rclone_file_event" && ["failed", "error", "upload_error", "upload_exception"].includes(String(item.status || "").toLowerCase());
            return `
                <div class="list-item compact job-technical-event ${escapeHtml(item.level || "")}">
                    <div>
                        <strong>${escapeHtml(item.message || item.source_label || "原始日志")}</strong>
                        <p>${escapeHtml(formatDate(item.created_at))} · ${escapeHtml(item.source_label || item.source || "系统")}${item.status ? ` · ${statusPill(item.status)}` : ""}</p>
                        ${item.filename ? `<p>${escapeHtml(item.filename)}</p>` : ""}
                        ${item.source_path || item.target_path ? `<details class="advanced-details compact"><summary>查看路径</summary><p><code>${escapeHtml(item.source_path || "-")}</code> → <code>${escapeHtml(item.target_path || "-")}</code></p></details>` : ""}
                        ${item.error_message ? `<p class="danger-text">${escapeHtml(item.error_message)}</p>` : ""}
                        ${renderOriginalLogData(item)}
                    </div>
                    ${canRetry ? `<button class="secondary mini" type="button" data-file-retry="${escapeHtml(item.event_id)}">重试</button>` : ""}
                </div>
            `;
        }

        function renderOriginalLogData(item) {
            if (!item || item.raw_data === undefined || item.raw_data === null || item.raw_data === "") return "";
            let text = "";
            try {
                text = JSON.stringify(item.raw_data, null, 2);
            } catch {
                text = String(item.raw_data);
            }
            if (!text || text === "{}") return "";
            return `
                <details class="timeline-raw original-log-data">
                    <summary>原始参数 / 响应</summary>
                    <pre>${escapeHtml(text)}</pre>
                </details>
            `;
        }

        return Object.freeze({
            loadOverview,
            renderMetricCard,
            loadRequests,
            renderRequests,
            requestActionButtons,
            sourceText,
            riskPill,
            suggestionText,
            contentGuardInfo,
            showRequestDetail,
            requestDrawerActions,
            approveRequest,
            rejectRequest,
            cancelRequest,
            loadJobs,
            renderJobs,
            jobActionButtons,
            renderOverviewJobs,
            isMediaRefreshOnlyJob,
            retryJob,
            reviewJob,
            retryMediaRefreshOnly,
            cancelJob,
            deleteJob,
            showJobDetail,
            renderCompletionChecks,
            renderTimelineItem,
            renderTechnicalEventItem,
            renderOriginalLogData,
        });
    }

    window.FnosAdminJobs = Object.freeze({ create });
})();
