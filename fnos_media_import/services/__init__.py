"""Service package with lazy public exports.

Importing one narrow service (for example the V1 execution-plan guard) must
not eagerly import every optional Web/rclone/media integration.  Lazy exports
keep the historical ``from fnos_media_import.services import ImportService``
API while isolating worker/domain tests from unrelated composition failures.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any


_EXPORTS = {
    "ImportService": (".import_service", "ImportService"),
    "JobService": (".job_service", "JobService"),
    "RcloneService": (".rclone_service", "RcloneService"),
    "SearchService": (".search_service", "SearchService"),
    "PublicSearchService": (".public_search_service", "PublicSearchService"),
    "PublicSubmissionService": (".public_submission_service", "PublicSubmissionService"),
    "GuestRequestAdminService": (".guest_request_admin_service", "GuestRequestAdminService"),
    "JobAdminQueryService": (".job_admin_query_service", "JobAdminQueryService"),
    "JobAdminCommandService": (".job_admin_command_service", "JobAdminCommandService"),
    "JobCancellationService": (".job_cancellation_service", "JobCancellationService"),
    "RequestReviewCommandService": (".request_review_command_service", "RequestReviewCommandService"),
    "RequestApprovalService": (".request_approval_service", "RequestApprovalService"),
    "AdminDashboardService": (".admin_dashboard_service", "AdminDashboardService"),
    "MediaAdminCommandService": (".media_admin_service", "MediaAdminCommandService"),
    "MediaAdminQueryService": (".media_admin_service", "MediaAdminQueryService"),
    "RcloneAdminQueryService": (".rclone_admin_service", "RcloneAdminQueryService"),
    "RcloneAdminCommandService": (".rclone_admin_service", "RcloneAdminCommandService"),
    "RcloneFileRetryService": (".rclone_file_retry_service", "RcloneFileRetryService"),
    "OrganizerAdminCommandService": (".organizer_admin_service", "OrganizerAdminCommandService"),
    "OrganizerAdminQueryService": (".organizer_admin_service", "OrganizerAdminQueryService"),
    "SystemDiagnosticsService": (".system_diagnostics_service", "SystemDiagnosticsService"),
    "ExternalDiagnosticsService": (".external_diagnostics_service", "ExternalDiagnosticsService"),
    "PublicResourceService": (".public_resource_service", "PublicResourceService"),
}


def __getattr__(name: str) -> Any:
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(name)
    module_name, attribute = target
    value = getattr(import_module(module_name, __name__), attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *_EXPORTS})


__all__ = list(_EXPORTS)
