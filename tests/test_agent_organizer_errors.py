from __future__ import annotations

import pytest

from fnos_media_import.agent.errors import organizer_error, translate
from fnos_media_import.organizer.identity import IdentityError
from fnos_media_import.organizer.identity_pipeline import IdentityReviewRequired
from fnos_media_import.organizer.library_scan import LibraryScanError
from fnos_media_import.organizer.openlist_client import OpenListTransientError
from fnos_media_import.organizer.tmdb import TmdbLookupError


@pytest.mark.parametrize("error,code", [
    (IdentityError("invalid identity"), "REVIEW_REQUIRED"),
    (IdentityReviewRequired("missing directory"), "REVIEW_REQUIRED"),
    (LibraryScanError("duplicate directory"), "REVIEW_REQUIRED"),
    (TmdbLookupError("upstream failure"), "METADATA_UNAVAILABLE"),
    (RuntimeError("unexpected"), "INTERNAL_ERROR"),
])
def test_domain_refusals_and_unknown_errors_do_not_trigger_blind_retries(error, code):
    result = translate(error).to_payload()
    assert result["code"] == code
    assert result["retryable"] is False
    assert result["hint"]


def test_explicit_transient_failure_provides_a_bounded_backoff_hint():
    result = translate(OpenListTransientError("busy")).to_payload()
    assert result["retryable"] is True
    assert result["details"]["retry_after_seconds"] == 30


def test_backend_plan_changed_code_survives_bridge_without_raw_data_leak():
    result = organizer_error({
        "success": False, "code": "PLAN_CHANGED", "message": "changed", "retryable": True,
        "details": {"current_plan_hash": "new-hash", "next_action": "preview", "raw_data": {"token": "secret"}},
    }, 409, "failed").to_payload()
    assert result["code"] == "PLAN_CHANGED" and result["retryable"] is False
    assert result["details"] == {"current_plan_hash": "new-hash", "next_action": "preview"}
    assert "secret" not in str(result)


def test_rollback_or_partial_write_refusal_keeps_actionable_guidance():
    result = organizer_error({"code": "EXECUTION_CONFIRMATION_REQUIRED", "message": "partial run"}, 409, "failed").to_payload()
    assert result["code"] == "EXECUTION_CONFIRMATION_REQUIRED"
    assert result["retryable"] is False
    assert "apply" in result["hint"] and "rollback" in result["hint"]
