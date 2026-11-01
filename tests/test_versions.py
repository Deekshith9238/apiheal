from pathlib import Path

import pytest

from apiheal.core.models import RepositorySnapshot
from apiheal.detection.python_cst import analyze_source
from apiheal.detection.versions import apply_versions, declared_requirements

CHAT = "from langchain_google_vertexai import ChatVertexAI\n"


def scan(tmp_path: Path, source: str, manifests: dict[str, str]):
    for name, text in manifests.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    snapshot = RepositorySnapshot(root=str(tmp_path), manifests=dict.fromkeys(manifests, ""))
    findings = analyze_source(source, "app.py").findings
    apply_versions(findings, declared_requirements(tmp_path, snapshot))
    return findings


@pytest.mark.parametrize(
    "requirement,severity,phrase",
    [
        ("langchain-google-vertexai==3.1.0", "info", "predates the 3.2.0 deprecation"),
        ("langchain-google-vertexai~=3.1.0", "info", "predates the 3.2.0 deprecation"),
        ("langchain-google-vertexai==3.2.5", "warning", None),
        ("langchain-google-vertexai>=3.2,<4", "warning", None),
        ("langchain-google-vertexai", "warning", "allows 4.0.0 or later"),
        ("langchain_google_vertexai>=3.0", "warning", "allows 4.0.0 or later"),
        ("langchain-google-vertexai==4.0.1", "error", "where it is removed"),
    ],
)
def test_langchain_severity_follows_declared_version(tmp_path, requirement, severity, phrase):
    (finding,) = scan(tmp_path, CHAT, {"requirements.txt": f"fastapi\n{requirement}\n"})
    assert finding.severity == severity
    assert finding.confidence == "high"
    assert any("Declared langchain-google-vertexai" in item for item in finding.evidence)
    if phrase:
        assert phrase in finding.message
    else:
        assert finding.message.endswith("project, and location.")


def test_langchain_findings_carry_rule_version_two(tmp_path):
    (finding,) = scan(tmp_path, CHAT, {})
    assert finding.rule_version == "2"


def test_undeclared_package_lowers_confidence(tmp_path):
    (finding,) = scan(tmp_path, CHAT, {"requirements.txt": "langchain-google-genai==4.2.2\n"})
    assert finding.confidence == "medium"
    assert "installed version is unknown" in finding.evidence[-1]


def test_pyproject_dependencies_and_extras_are_read(tmp_path):
    pyproject = (
        '[project]\nname = "x"\ndependencies = ["fastapi"]\n'
        '[project.optional-dependencies]\nai = ["langchain-google-vertexai<3.2"]\n'
    )
    (finding,) = scan(tmp_path, CHAT, {"pyproject.toml": pyproject})
    assert finding.severity == "info"
    assert "<3.2 in pyproject.toml" in " ".join(finding.evidence)


def test_any_manifest_allowing_deprecation_keeps_the_warning(tmp_path):
    manifests = {
        "requirements.txt": "langchain-google-vertexai==3.1.0\n",
        "requirements-dev.txt": "langchain-google-vertexai>=3.2.0,<4  # dev\n",
    }
    (finding,) = scan(tmp_path, CHAT, manifests)
    assert finding.severity == "warning"


def test_options_comments_and_urls_are_ignored(tmp_path):
    text = (
        "-r base.txt\n--index-url https://example.invalid/simple\n# langchain-google-vertexai\n"
        "langchain-google-vertexai @ https://example.invalid/pkg.whl\n"
    )
    (finding,) = scan(tmp_path, CHAT, {"requirements.txt": text})
    assert finding.confidence == "medium"


def test_gemini_finding_gains_declared_version_evidence(tmp_path):
    source = "import google.generativeai as genai\n"
    (finding,) = scan(tmp_path, source, {"requirements.txt": "google-generativeai==0.8.6\n"})
    assert finding.severity == "warning"
    assert "Declared google-generativeai: ==0.8.6 in requirements.txt" in finding.evidence
