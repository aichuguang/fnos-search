from __future__ import annotations

import hashlib
import json
import re
import secrets
from collections.abc import Callable, Mapping
from typing import Any

from ..constants import JOB_CANCELLED, JOB_DONE, JOB_FAILED, JOB_UNSUPPORTED


class TrendingInitialImportError(ValueError):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


class TrendingInitialImportService:
    """Connects a reviewed hot-list candidate to the normal search/import path.

    This service deliberately creates only an initial import job. It never
    creates an update subscription or invokes the episodic update workflow.
    """

    _TERMINAL_FAILURES = {JOB_FAILED, JOB_UNSUPPORTED, JOB_CANCELLED}
    _IMPORTABLE_STATUSES = {"discovered", "import_failed"}

    def __init__(
        self,
        *,
        repository: Any,
        search_service: Callable[[], Any],
        import_service: Callable[[], Any],
        get_resource: Callable[[int], dict[str, Any] | None],
        get_cached_resource: Callable[[str], dict[str, Any] | None] | None = None,
        find_resource_by_url: Callable[..., dict[str, Any] | None] | None = None,
        search_resources: Callable[..., dict[str, Any]] | None = None,
        get_job: Callable[[int], dict[str, Any] | None],
        categories: Callable[[], Mapping[str, Any]],
        runtime_revision: Callable[[], int] | None = None,
        executor_id: Callable[[], str] | None = None,
        start_import: Callable[[dict[str, Any], str], Any] | None = None,
        sanitize_string_list: Callable[[Any], list[str]] | None = None,
        sanitize_quark_selection: Callable[[Any], dict[str, Any]] | None = None,
        sanitize_cloud139_selection: Callable[[Any], dict[str, Any]] | None = None,
    ) -> None:
        self.repository = repository
        self.search_service = search_service
        self.import_service = import_service
        self.get_resource = get_resource
        self.get_cached_resource = get_cached_resource
        self.find_resource_by_url = find_resource_by_url
        self.search_resources = search_resources
        self.get_job = get_job
        self.categories = categories
        self.runtime_revision = runtime_revision or (lambda: 1)
        self.executor_id = executor_id or (lambda: "web")
        self.start_import = start_import
        self.sanitize_string_list = sanitize_string_list or self._safe_string_list
        self.sanitize_quark_selection = sanitize_quark_selection or (lambda _value: {})
        self.sanitize_cloud139_selection = sanitize_cloud139_selection or (lambda _value: {})

    def search(
        self,
        candidate_id: int,
        *,
        sources: list[str] | None = None,
        token: str = "",
        refresh: bool = False,
        keyword: str | None = None,
    ) -> dict[str, Any]:
        candidate = self._candidate(candidate_id)
        if str(candidate.get("status") or "") not in self._IMPORTABLE_STATUSES:
            raise TrendingInitialImportError(self._not_importable_message(candidate), 409)
        search_keyword = str(keyword or "").strip()[:300]
        if not search_keyword:
            search_keyword = self.default_search_keyword(candidate.get("title"))
        if not search_keyword:
            raise TrendingInitialImportError("\u70ed\u699c\u5019\u9009\u7f3a\u5c11\u6807\u9898")
        trace_id = f"trending-{int(candidate_id)}-{secrets.token_hex(3)}"
        cache_keyword = f"trending:{int(candidate_id)}:{search_keyword}"
        options = {
            "async_poll": False,
            "trace_id": trace_id,
            "save_resources": True,
            "refresh": bool(refresh),
        }
        if self.search_resources:
            result = self.search_resources(
                keyword=search_keyword,
                cache_keyword=cache_keyword,
                sources=sources or [],
                token=str(token or ""),
                options=options,
                trace_id=trace_id,
            )
        else:
            result = self.search_service().search(
                search_keyword,
                sources=sources,
                token=str(token or ""),
                options=options,
            )
        return {
            "success": True,
            "candidate": candidate,
            "keyword": search_keyword,
            "items": result.get("items") or [],
            "raw": result.get("raw") or {},
            "trace_id": trace_id,
        }

    def create_initial_import(self, candidate_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        candidate = self._candidate(candidate_id)
        resource = self._resolve_resource(candidate, payload)
        source_url = str(resource.get("url") or resource.get("source_url") or "").strip()
        if not source_url:
            raise TrendingInitialImportError("\u6240\u9009\u8d44\u6e90\u7f3a\u5c11\u53ef\u5165\u5e93\u94fe\u63a5")
        category = self._category(candidate, payload)
        submit_payload = self._import_payload(
            candidate=candidate, resource=resource, category=category, request_payload=payload
        )
        existing = self._existing_import_result(candidate, submit_payload["idempotency_key"])
        if existing is not None:
            return existing
        claimed = self.repository.claim_trending_candidate_for_initial_import(
            int(candidate_id), allowed_statuses=tuple(sorted(self._IMPORTABLE_STATUSES))
        )
        if not claimed:
            latest = self.repository.get_trending_candidate(int(candidate_id))
            if not latest:
                raise TrendingInitialImportError("\u70ed\u699c\u5019\u9009\u4e0d\u5b58\u5728", 404)
            raise TrendingInitialImportError(self._not_importable_message(latest), 409)
        try:
            result = self.import_service().create_import_job(submit_payload)
            job = result.get("job") if isinstance(result, dict) else None
            if not isinstance(job, dict) or not job.get("id"):
                raise RuntimeError("\u5165\u5e93\u670d\u52a1\u672a\u8fd4\u56de\u4efb\u52a1")
            candidate_status = self._candidate_status_for_job(job)
            if not self.repository.bind_trending_candidate_initial_import_job(
                int(candidate_id), int(job["id"]), status=candidate_status
            ):
                raise RuntimeError("\u70ed\u699c\u5019\u9009\u4e0e\u5165\u5e93\u4efb\u52a1\u7ed1\u5b9a\u5931\u8d25")
            if self.start_import:
                try:
                    result["rclone_start"] = self.start_import(
                        result, f"trending_initial_import:{int(candidate_id)}"
                    )
                except Exception as exc:  # noqa: BLE001
                    result["rclone_start"] = {"success": False, "message": str(exc)}
            latest_job = self.get_job(int(job["id"])) or job
            latest_status = self._candidate_status_for_job(latest_job)
            if latest_status != candidate_status:
                self.repository.bind_trending_candidate_initial_import_job(
                    int(candidate_id), int(job["id"]), status=latest_status
                )
            succeeded = bool(result.get("success", True)) and latest_status != "import_failed"
            return {
                "success": succeeded,
                "created": bool(result.get("created", True)),
                "message": result.get("message") or "\u5df2\u521b\u5efa\u70ed\u699c\u9996\u6b21\u5165\u5e93\u4efb\u52a1",
                "candidate": self.repository.get_trending_candidate(int(candidate_id)),
                "job": latest_job,
            }
        except Exception:
            self.repository.release_trending_candidate_initial_import(
                int(candidate_id), status=str(candidate.get("status") or "discovered")
            )
            raise

    def _existing_import_result(
        self, candidate: Mapping[str, Any], expected_idempotency_key: str
    ) -> dict[str, Any] | None:
        status = str(candidate.get("status") or "")
        job_id = candidate.get("initial_import_job_id")
        if status not in {"importing", "imported"} or not job_id:
            return None
        job = self.get_job(int(job_id))
        if not isinstance(job, Mapping):
            return None
        raw_data = job.get("raw_data") if isinstance(job.get("raw_data"), Mapping) else {}
        request = raw_data.get("request") if isinstance(raw_data.get("request"), Mapping) else raw_data
        if str(request.get("idempotency_key") or "") != str(expected_idempotency_key or ""):
            raise TrendingInitialImportError("\u8be5\u70ed\u699c\u5019\u9009\u5df2\u7ed1\u5b9a\u5176\u4ed6\u5165\u5e93\u8d44\u6e90", 409)
        current_status = self._candidate_status_for_job(job)
        if current_status != status:
            self.repository.bind_trending_candidate_initial_import_job(
                int(candidate["id"]), int(job_id), status=current_status
            )
            candidate = self.repository.get_trending_candidate(int(candidate["id"])) or candidate
        return {
            "success": current_status != "import_failed",
            "created": False,
            "message": "\u8be5\u70ed\u699c\u5019\u9009\u5df2\u5b58\u5728\u76f8\u540c\u7684\u9996\u5165\u5e93\u4efb\u52a1",
            "candidate": candidate,
            "job": dict(job),
        }

    def reconcile_candidate(self, candidate: dict[str, Any]) -> dict[str, Any]:
        job_id = candidate.get("initial_import_job_id")
        if not job_id:
            return candidate
        job = self.get_job(int(job_id))
        if not job:
            return candidate
        expected = self._candidate_status_for_job(job)
        if str(candidate.get("status") or "") != expected:
            self.repository.bind_trending_candidate_initial_import_job(
                int(candidate["id"]), int(job_id), status=expected
            )
            return self.repository.get_trending_candidate(int(candidate["id"])) or candidate
        return candidate

    def reconcile_candidates(self, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [self.reconcile_candidate(item) for item in candidates]

    def cached_resource_for_candidate(self, candidate_id: int, public_id: str) -> dict[str, Any]:
        candidate = self._candidate(candidate_id)
        return self._cached_resource(candidate, public_id)

    def _candidate(self, candidate_id: int) -> dict[str, Any]:
        candidate = self.repository.get_trending_candidate(int(candidate_id))
        if not candidate:
            raise TrendingInitialImportError("\u70ed\u699c\u5019\u9009\u4e0d\u5b58\u5728", 404)
        return self.reconcile_candidate(candidate)

    def _resolve_resource(self, candidate: dict[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
        public_id = str(payload.get("public_id") or "").strip()
        if public_id:
            cached = self._cached_resource(candidate, public_id)
            source_url = str(cached.get("source_url") or cached.get("url") or "").strip()
            raw = cached.get("raw_data") if isinstance(cached.get("raw_data"), Mapping) else {}
            source = str(raw.get("source") or raw.get("provider") or cached.get("source_type") or "").strip()
            if not source_url or not self.find_resource_by_url:
                raise TrendingInitialImportError("\u8d44\u6e90\u6620\u5c04\u4e0d\u5b58\u5728\uff0c\u8bf7\u91cd\u65b0\u641c\u7d22", 404)
            resource = self.find_resource_by_url(source_url, source=source)
            if not resource:
                raise TrendingInitialImportError("\u8d44\u6e90\u6620\u5c04\u4e0d\u5b58\u5728\uff0c\u8bf7\u91cd\u65b0\u641c\u7d22", 404)
            return {**resource, "_trending_public_id": public_id}
        resource_id = self._resource_id(payload.get("resource_id"))
        resource = self.get_resource(resource_id)
        if not resource:
            raise TrendingInitialImportError("\u641c\u7d22\u8d44\u6e90\u4e0d\u5b58\u5728\uff0c\u8bf7\u91cd\u65b0\u641c\u7d22", 404)
        return resource

    def _cached_resource(self, candidate: Mapping[str, Any], public_id: str) -> dict[str, Any]:
        public_id = str(public_id or "").strip()
        if not public_id or len(public_id) > 80:
            raise TrendingInitialImportError("\u8d44\u6e90\u6807\u8bc6\u65e0\u6548")
        if not self.get_cached_resource:
            raise TrendingInitialImportError("\u8d44\u6e90\u7f13\u5b58\u670d\u52a1\u4e0d\u53ef\u7528", 503)
        resource = self.get_cached_resource(public_id)
        if not resource:
            raise TrendingInitialImportError("\u8d44\u6e90\u8be6\u60c5\u4e0d\u5b58\u5728\u6216\u5df2\u8fc7\u671f\uff0c\u8bf7\u91cd\u65b0\u641c\u7d22", 404)
        stored_keyword = str(resource.get("keyword") or "")
        scoped_prefix = f"trending:{int(candidate['id'])}:"
        if stored_keyword.startswith("trending:"):
            belongs_to_candidate = stored_keyword.startswith(scoped_prefix)
        else:
            # Compatibility for caches created before candidate-scoped keys.
            keyword_key = self._match_text(stored_keyword)
            allowed = {
                self._match_text(candidate.get("title")),
                self._match_text(self.default_search_keyword(candidate.get("title"))),
            }
            belongs_to_candidate = bool(keyword_key and keyword_key in allowed)
        if not belongs_to_candidate:
            raise TrendingInitialImportError("\u8d44\u6e90\u4e0d\u5c5e\u4e8e\u5f53\u524d\u70ed\u699c\u5019\u9009", 404)
        return resource

    def _category(self, candidate: dict[str, Any], payload: dict[str, Any]) -> str:
        categories = self.categories()
        explicit = str(payload.get("category") or "").strip().lower()
        if explicit:
            if explicit not in categories:
                raise TrendingInitialImportError("\u5165\u5e93\u5206\u7c7b\u4e0d\u5b58\u5728")
            return explicit
        requested = str(candidate.get("media_type") or "").strip().lower()
        if requested in categories:
            return requested
        if "other" in categories:
            return "other"
        if "movie" in categories:
            return "movie"
        if categories:
            return str(next(iter(categories)))
        raise TrendingInitialImportError("\u7cfb\u7edf\u672a\u914d\u7f6e\u5165\u5e93\u5206\u7c7b", 503)

    def _import_payload(
        self,
        *,
        candidate: dict[str, Any],
        resource: dict[str, Any],
        category: str,
        request_payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        raw = resource.get("raw_data") if isinstance(resource.get("raw_data"), dict) else {}
        url = str(resource.get("url") or resource.get("source_url") or "").strip()
        public_id = str(resource.get("_trending_public_id") or "").strip()
        resource_id = int(resource["id"])
        title = str(candidate.get("title") or resource.get("title") or "").strip()
        selections = self._selection_payload(request_payload)
        selection_text = json.dumps(selections, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(f"{url}\n{category}\n{selection_text}".encode("utf-8")).hexdigest()[:24]
        result: dict[str, Any] = {
            "title": title,
            "url": url,
            "password": str(resource.get("password") or raw.get("password") or ""),
            "category": category,
            "idempotency_key": f"trending:{int(candidate['id'])}:resource:{resource_id}:{digest}",
            "config_revision": int(self.runtime_revision() or 1),
            "executor_id": str(self.executor_id() or "web"),
            "trending_candidate_id": int(candidate["id"]),
            "trending_resource_id": resource_id,
            **selections,
        }
        if public_id:
            result["trending_public_id"] = public_id
        return result

    def _selection_payload(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        quark = self.sanitize_quark_selection(payload.get("quark_selection"))
        cloud139 = self.sanitize_cloud139_selection(payload.get("cloud139_selection"))
        sixpan = payload.get("sixpan_selection") if isinstance(payload.get("sixpan_selection"), Mapping) else {}
        ignore_source = payload.get("ignore_files")
        if ignore_source is None:
            ignore_source = sixpan.get("ignore_files")
        ignore_files = self.sanitize_string_list(ignore_source)
        result: dict[str, Any] = {}
        if quark:
            result["quark_selection"] = quark
        if cloud139:
            result["cloud139_selection"] = cloud139
        if ignore_files:
            result["ignore_files"] = ignore_files
            result["sixpan_selection"] = {"ignore_files": ignore_files}
        return result

    @staticmethod
    def _safe_string_list(value: Any) -> list[str]:
        rows = value if isinstance(value, list) else ([] if value is None else [value])
        result: list[str] = []
        for item in rows[:2000]:
            text = str(item or "").strip()
            if text and len(text) <= 512 and text not in result:
                result.append(text)
        return result

    @staticmethod
    def _match_text(value: Any) -> str:
        return "".join(char.lower() for char in str(value or "") if char.isalnum())

    @staticmethod
    def default_search_keyword(value: Any) -> str:
        title = str(value or "").strip()
        without_season = re.sub(
            r"\s*\u7b2c\s*(?:\d+|[\u4e00\u4e8c\u4e24\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341\u767e\u5343]+)\s*\u5b63\s*",
            " ",
            title,
            flags=re.I,
        )
        normalized = re.sub(r"[\s\-\u2014_:\uFF1A\u00B7]+", " ", without_season).strip()
        return normalized or title

    @classmethod
    def _candidate_status_for_job(cls, job: Mapping[str, Any]) -> str:
        status = str(job.get("status") or "").strip().lower()
        if status == JOB_DONE:
            return "imported"
        if status in cls._TERMINAL_FAILURES:
            return "import_failed"
        return "importing"

    @staticmethod
    def _resource_id(value: Any) -> int:
        try:
            resource_id = int(value)
        except (TypeError, ValueError) as exc:
            raise TrendingInitialImportError("\u8bf7\u9009\u62e9\u8981\u5165\u5e93\u7684\u641c\u7d22\u8d44\u6e90") from exc
        if resource_id <= 0:
            raise TrendingInitialImportError("\u8bf7\u9009\u62e9\u8981\u5165\u5e93\u7684\u641c\u7d22\u8d44\u6e90")
        return resource_id

    @staticmethod
    def _not_importable_message(candidate: Mapping[str, Any]) -> str:
        status = str(candidate.get("status") or "")
        messages = {
            "already_exists": "\u5a92\u4f53\u5e93\u5df2\u6709\u8be5\u5185\u5bb9\uff0c\u65e0\u9700\u91cd\u590d\u5165\u5e93",
            "task_exists": "\u8be5\u5185\u5bb9\u5df2\u6709\u5165\u5e93\u6216\u8ffd\u66f4\u4efb\u52a1",
            "ignored": "\u70ed\u699c\u5019\u9009\u5df2\u5ffd\u7565\uff0c\u8bf7\u5148\u6062\u590d",
            "importing": "\u70ed\u699c\u5019\u9009\u5df2\u5728\u5165\u5e93",
            "imported": "\u70ed\u699c\u5019\u9009\u5df2\u5b8c\u6210\u9996\u6b21\u5165\u5e93",
        }
        return messages.get(status, "\u70ed\u699c\u5019\u9009\u5f53\u524d\u72b6\u6001\u4e0d\u5141\u8bb8\u5165\u5e93")


__all__ = ["TrendingInitialImportError", "TrendingInitialImportService"]
