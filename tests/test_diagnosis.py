import pytest

from apiheal.diagnosis.gemini_auth import diagnose_gemini


@pytest.mark.parametrize(
    "log,category",
    [
        ("401 UNAUTHENTICATED: missing credentials", "authentication"),
        ("400 API_KEY_INVALID: API key not valid", "authentication"),
        ("403 Your API key was reported as leaked", "authentication"),
        ("403 SERVICE_DISABLED: API is disabled", "api_enablement"),
        ("403 billing account is suspended", "billing"),
        ("PROJECT_MISMATCH wrong project", "project_mismatch"),
        ("429 RESOURCE_EXHAUSTED: per minute request quota", "quota_or_rate_limit"),
        ("404 model_not_found", "model_availability"),
        ("TypeError: unexpected keyword argument 'model'", "sdk_compatibility"),
        ("403 PERMISSION_DENIED", "permissions_or_configuration"),
        ("503 service unavailable", "network_or_service"),
        ("TimeoutError: timed out", "network_or_service"),
        ("some unknown failure", "unknown"),
    ],
)
def test_diagnostic_categories_and_no_automatic_fix(log, category):
    result = diagnose_gemini(log)
    assert result.category == category
    assert result.outcome == "manual_action_required"
    assert result.manual_steps and result.evidence


def test_generic_permission_error_retains_uncertainty():
    result = diagnose_gemini("403 PERMISSION_DENIED")
    assert result.confidence == "low" and result.missing_evidence
    assert result.endpoint == "unknown"
    assert "status alone" in " ".join(result.likely_causes)


def test_key_with_bound_identity_and_developer_endpoint():
    result = diagnose_gemini(
        "https://generativelanguage.googleapis.com 403 Authorization key bound to service account"
    )
    assert result.endpoint == "developer_api"
    assert result.auth_context == "authorization_key_with_bound_identity"


def test_project_presence_does_not_imply_vertex():
    result = diagnose_gemini("GCP project example, 403 permission denied")
    assert result.endpoint == "unknown"
    assert diagnose_gemini("403", endpoint="vertex_ai").endpoint == "vertex_ai"


def test_diagnosis_redacts_before_evidence_storage():
    result = diagnose_gemini("403 API_KEY=synthetic-short-secret")
    assert "synthetic-short-secret" not in result.model_dump_json()
