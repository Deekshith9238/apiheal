"""Conservative Gemini failure classification; status alone is not a root cause."""

import re
from typing import Literal

from apiheal.core.models import Diagnosis
from apiheal.telemetry.redaction import redact_text

TROUBLESHOOTING = "https://ai.google.dev/gemini-api/docs/troubleshooting"
API_KEYS = "https://ai.google.dev/gemini-api/docs/api-key"


def diagnose_gemini(
    log: str,
    endpoint: Literal["developer_api", "vertex_ai", "unknown"] = "unknown",
) -> Diagnosis:
    sanitized = redact_text(log)
    observed = sanitized.lower()
    if endpoint == "unknown":
        if "aiplatform.googleapis.com" in observed or "vertex ai" in observed:
            endpoint = "vertex_ai"
        elif "generativelanguage.googleapis.com" in observed or "ai studio" in observed:
            endpoint = "developer_api"
    auth = "unknown"
    if "authorization key" in observed or "auth key" in observed:
        auth = "authorization_key_with_bound_identity"
    elif "standard api key" in observed:
        auth = "standard_api_key"
    elif any(s in observed for s in ("api key", "api_key", "x-goog-api-key")):
        auth = "api_key_type_unknown"
    elif any(
        s in observed
        for s in (
            "application default",
            "google_application_credentials",
            "service account",
            "oauth",
        )
    ):
        auth = "google_cloud_credentials"

    category = "unknown"
    confidence: Literal["low", "medium", "high"] = "low"
    causes = ["The supplied evidence does not establish a specific root cause."]
    steps = [
        "Collect the exact SDK/version, endpoint family, and sanitized provider error details."
    ]

    if any(
        s in observed for s in ("key was reported as leaked", "api key is blocked", "key blocked")
    ):
        category, confidence = "authentication", "high"
        causes = ["The provider explicitly reports a blocked or exposed key."]
        steps = [
            "Have the credential owner replace the affected key and update secret storage.",
            "Investigate the exposure; do not place replacement credentials in this report.",
        ]
    elif any(
        s in observed
        for s in (
            "api_key_invalid",
            "api key not valid",
            "invalid api key",
            "missing api key",
            "unauthenticated",
            "defaultcredentialserror",
        )
    ) or re.search(r"\b401\b", observed):
        category, confidence = "authentication", "medium"
        causes = ["Authentication is missing, rejected, expired, or unavailable in this context."]
        steps = [
            "Verify which credential source the selected endpoint expects.",
            "Have the owner verify the configured credential without sharing its value.",
        ]
    elif any(
        s in observed
        for s in (
            "service_disabled",
            "api has not been used",
            "api is disabled",
            "api not enabled",
        )
    ):
        category, confidence = "api_enablement", "high"
        causes = ["The provider identifies a disabled or unavailable API in the selected project."]
        steps = [
            "Verify the project identifier and required API with the project administrator.",
            "An authorized administrator must review any API enablement change.",
        ]
    elif any(
        s in observed
        for s in (
            "billing",
            "payment required",
            "prepay credit",
            "insufficient credit",
        )
    ):
        category, confidence = "billing", "medium"
        causes = ["The error explicitly references billing, credits, or account eligibility."]
        steps = [
            "Ask the billing owner to review the specific reported restriction.",
            "Do not change billing settings or assume that a new key resolves the restriction.",
        ]
    elif any(s in observed for s in ("wrong project", "project mismatch", "project_mismatch")):
        category, confidence = "project_mismatch", "medium"
        causes = ["The error reports inconsistent project selection or credential ownership."]
        steps = ["Compare the configured project with the credential's nonsecret project metadata."]
    elif re.search(r"\b429\b", observed) or any(
        s in observed
        for s in (
            "resource_exhausted",
            "quota_exceeded",
            "rate_limit_exceeded",
        )
    ):
        category, confidence = "quota_or_rate_limit", "medium"
        causes = ["A request, token, daily, or spend limit may have been reached."]
        steps = [
            "Read the provider's limit details and retry timing; identify the affected quota.",
            "Use bounded backoff only for retryable limits; quota changes require the owner.",
        ]
    elif any(s in observed for s in ("model_not_found", "model is not found", "model unavailable")):
        category, confidence = "model_availability", "medium"
        causes = ["The named model may be unavailable for this endpoint, region, or API version."]
        steps = [
            "Verify the exact model, endpoint, region, and documented capabilities.",
            "Choose a replacement only after compatibility is independently validated.",
        ]
    elif any(
        s in observed
        for s in (
            "apiremovedinv1",
            "unexpected keyword argument",
            "cannot import name",
            "modulenotfounderror",
            "attributeerror",
        )
    ):
        category, confidence = "sdk_compatibility", "low"
        causes = [
            "A dependency or call-signature mismatch is possible; application bugs remain possible."
        ]
        steps = [
            "Compare the installed SDK version and failing call with its versioned documentation."
        ]
    elif re.search(r"\b403\b", observed) or "permission_denied" in observed:
        category, confidence = "permissions_or_configuration", "low"
        causes = [
            "Credential restrictions, project access, IAM, or account policy may deny the request.",
            "The status alone does not identify which configuration needs changing.",
        ]
        steps = [
            "Inspect the sanitized error reason and endpoint-specific access requirements.",
            "Ask the appropriate administrator to verify the identified restriction.",
        ]
    elif re.search(r"\b(?:408|500|502|503|504)\b", observed) or any(
        s in observed
        for s in (
            "timeout",
            "timed out",
            "connectionerror",
            "connection refused",
            "dns",
        )
    ):
        category, confidence = "network_or_service", "medium"
        causes = ["The evidence indicates a network failure, timeout, or transient service error."]
        steps = [
            "Check endpoint connectivity and provider status through approved channels.",
            "Use bounded retries where documented; this does not justify an SDK migration.",
        ]

    missing = []
    if endpoint == "unknown":
        missing.append("Endpoint family: Gemini Developer API or Vertex AI.")
    if auth == "unknown":
        missing.append("Credential mechanism and nonsecret identity/project context.")
    if confidence == "low":
        missing.append("Specific provider reason/details beyond the HTTP status.")
    return Diagnosis(
        endpoint=endpoint,
        auth_context=auth,
        category=category,
        confidence=confidence,
        evidence=[sanitized[:1500]] if sanitized.strip() else ["No error content supplied."],
        likely_causes=causes,
        missing_evidence=missing,
        manual_steps=steps,
        documentation=[TROUBLESHOOTING, API_KEYS],
    )
