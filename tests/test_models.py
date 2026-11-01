from datetime import date

import pytest

from apiheal.detection.python_cst import analyze_source, retired_models

TODAY = date(2026, 11, 1)


def findings(source: str):
    return [
        f
        for f in analyze_source(source, "app.py", today=TODAY).findings
        if f.rule_id == "model-retired"
    ]


def test_retired_gemini_alias_in_model_argument_is_an_error():
    source = 'llm = ChatGoogleGenerativeAI(model="gemini-1.5-pro", temperature=0.7)\n'
    (finding,) = findings(source)
    assert finding.severity == "error" and finding.confidence == "high"
    assert finding.provider == "google"
    assert "was shut down on 2025-09-24" in finding.message
    assert "choose a current model" in finding.message
    assert finding.span.line == 1


@pytest.mark.parametrize(
    "source,severity,phrase",
    [
        ('client.messages.create(model="claude-sonnet-4-5-20250929")\n', "warning", "in 29 days"),
        ('client.chat.completions.create(model="gpt-4-turbo")\n', "error", "use gpt-5.6-sol"),
        ('embed(model_name="models/text-embedding-004")\n', "error", "gemini-embedding-2"),
        ('embed(model="gemini-embedding-001")\n', "info", "scheduled to shut down on 2028-05-14"),
    ],
)
def test_severity_follows_shutdown_date(source, severity, phrase):
    (finding,) = findings(source)
    assert finding.severity == severity
    assert phrase in finding.message


def test_model_ids_outside_model_arguments_have_medium_confidence():
    source = 'AVAILABLE_MODELS = ["gemini-2.5-flash", "gemini-1.5-flash-002"]\n'
    (finding,) = findings(source)
    assert finding.confidence == "medium"
    assert "gemini-1.5-flash-002" in finding.message


@pytest.mark.parametrize(
    "source",
    [
        'llm = ChatOpenAI(model="gpt-4o")\n',
        'client.messages.create(model="claude-haiku-4-5-20251001")\n',
        'model = f"gemini-1.5-{size}"\n',
        'note = "migrate off gemini-1.5-pro soon"\n',
        "data = b'gpt-4-turbo'\n",
    ],
)
def test_current_or_partial_model_strings_are_not_reported(source):
    assert not findings(source)


def test_table_rows_are_complete_and_dated():
    table, sources = retired_models()
    assert len(table) >= 100
    for row in table.values():
        assert row["provider"] in sources
        date.fromisoformat(row["shutdown"])
