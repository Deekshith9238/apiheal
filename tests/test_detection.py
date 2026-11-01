import ast
from pathlib import Path

import pytest

from apiheal.detection.python_cst import analyze_source

FIXTURE = (Path(__file__).parent.parent / "examples/legacy-openai/app.py").read_text()


@pytest.mark.parametrize(
    "import_line,call",
    [
        ("import openai", "openai.ChatCompletion.create"),
        ("import openai as oa", "oa.ChatCompletion.create"),
        ("from openai import ChatCompletion as Chat", "Chat.create"),
        ("from openai import ChatCompletion", "ChatCompletion.create"),
    ],
)
def test_alias_resolution_and_minimal_migration(import_line, call):
    source = FIXTURE.replace("import openai", import_line).replace(
        "openai.ChatCompletion.create",
        call,
    )
    analysis = analyze_source(source, "app.py")
    assert len(analysis.findings) == 1
    finding = analysis.findings[0]
    assert finding.repairable and finding.span.line == 8
    migrated = analysis.migrated_source
    assert "with _apiheal_OpenAI() as _apiheal_client:" in migrated
    assert "return response.choices[0].message.content" in migrated
    assert "model=model" in migrated and "messages=messages" in migrated
    assert '"""Extract the first chat response\'s text."""' in migrated
    # Parsing/compilation never executes this repository code.
    tree = ast.parse(migrated)
    assert ast.get_docstring(tree.body[2]) == "Extract the first chat response's text."
    compile(tree, "app.py", "exec")
    assert analyze_source(migrated, "app.py").findings == []


def test_preserves_comments_and_crlf():
    source = FIXTURE.replace("import openai", "import openai  # SDK import")
    source = source.replace("        model=model,", "        model=model,  # chosen by caller")
    source = source.replace("\n", "\r\n")
    migrated = analyze_source(source, "app.py").migrated_source
    assert "# SDK import" in migrated and "# chosen by caller" in migrated
    assert "\r\n" in migrated and "\n" not in migrated.replace("\r\n", "")


@pytest.mark.parametrize(
    "source",
    [
        'text = "openai.ChatCompletion.create(model=...)"\n',
        "# openai.ChatCompletion.create(model=...)\n",
        "import unrelated as openai\nopenai.ChatCompletion.create()\n",
        "import openai\ndef f(openai):\n    return openai.ChatCompletion.create()\n",
        "import openai\nopenai = object()\nopenai.ChatCompletion.create()\n",
    ],
)
def test_no_false_positives(source):
    analysis = analyze_source(source, "app.py")
    assert analysis.findings == []
    assert analysis.migrated_source is None


@pytest.mark.parametrize(
    "source,reason",
    [
        (FIXTURE.replace("def answer", "async def answer"), "synchronous"),
        (FIXTURE.replace("def answer", "@decorator\ndef answer"), "synchronous"),
        (FIXTURE.replace("temperature=0", "stream=True"), "keyword"),
        (FIXTURE.replace("temperature=0", "**options"), "keyword"),
        (FIXTURE.replace('["content"]', '["role"]'), "Response"),
        (
            FIXTURE.replace("import openai", "import openai\nopenai.api_key = 'not-a-real-secret'"),
            "configuration",
        ),
        (
            FIXTURE.replace("import openai", "import openai\nprint(openai.__version__)"),
            "Additional",
        ),
        (FIXTURE.replace("import openai", "import openai\nfrom anything import *"), "Star"),
        (
            FIXTURE.replace("import openai", "import openai\ngetattr(openai, 'ChatCompletion')"),
            "Dynamic",
        ),
        (
            'import openai\ndef f(): r = openai.ChatCompletion.create(model="x", messages=[]); '
            'return r["choices"][0]["message"]["content"]\n',
            "Inline",
        ),
    ],
)
def test_unsupported_patterns_are_visible_and_not_rewritten(source, reason):
    analysis = analyze_source(source, "app.py")
    assert analysis.findings
    assert analysis.migrated_source is None
    assert not analysis.findings[0].repairable
    assert reason in analysis.findings[0].message


def test_generated_names_do_not_shadow_module_functions_or_parameters():
    source = FIXTURE.replace(
        "def answer(model, messages)", "def answer(model, messages, _apiheal_client)"
    )
    source += "\ndef _apiheal_OpenAI():\n    pass\n"
    migrated = analyze_source(source, "app.py").migrated_source
    assert "as _apiheal_OpenAI_2" in migrated
    assert "as _apiheal_client_2:" in migrated


def test_another_import_on_same_line_is_preserved():
    source = FIXTURE.replace("import openai", "import openai; import os")
    migrated = analyze_source(source, "app.py").migrated_source
    assert "; import os" in migrated


@pytest.mark.parametrize(
    "source",
    [
        "import google.generativeai as genai\n",
        "from google import generativeai as genai\n",
        "from google.generativeai import GenerativeModel\n",
    ],
)
def test_gemini_legacy_import_is_reported(source):
    finding = analyze_source(source, "app.py").findings[0]
    assert finding.rule_id == "gemini-old-sdk"
    assert not finding.repairable


@pytest.mark.parametrize(
    "source,name",
    [
        ("from langchain_google_vertexai import ChatVertexAI\n", "ChatVertexAI"),
        ("from langchain_google_vertexai import VertexAIEmbeddings as E\n", "VertexAIEmbeddings"),
        ("from langchain_google_vertexai.llms import VertexAI\n", "VertexAI"),
        ("def build():\n    from langchain_google_vertexai import ChatVertexAI\n", "ChatVertexAI"),
        ("import langchain_google_vertexai as lgv\nmodel = lgv.ChatVertexAI()\n", "ChatVertexAI"),
    ],
)
def test_langchain_vertexai_deprecation_is_reported(source, name):
    (finding,) = analyze_source(source, "app.py").findings
    assert finding.rule_id == "langchain-vertexai-deprecated"
    assert finding.provider == "langchain"
    assert not finding.repairable
    assert finding.evidence == [f"Resolved imported class: langchain_google_vertexai.{name}"]


def test_langchain_deprecations_on_one_line_have_distinct_ids():
    source = "from langchain_google_vertexai import ChatVertexAI, VertexAIEmbeddings\n"
    findings = analyze_source(source, "app.py").findings
    assert len({f.id for f in findings}) == 2


@pytest.mark.parametrize(
    "source",
    [
        "from langchain_google_genai import ChatGoogleGenerativeAI\n",
        "from langchain_google_vertexai import VectorSearchVectorStore\n",
        "from langchain_google_vertexai import *\n",
        "from . import ChatVertexAI\n",
        "class ChatVertexAI:\n    pass\n",
    ],
)
def test_langchain_rule_has_no_false_positives(source):
    assert not analyze_source(source, "app.py").findings


def test_legacy_embeddings_reported_without_repair():
    source = "import openai as oa\nresult = oa.Embedding.create(input=['test'])\n"
    analysis = analyze_source(source, "app.py")
    assert analysis.findings[0].rule_id == "openai-legacy-embeddings"
    assert not analysis.findings[0].repairable


def test_invalid_source_is_a_finding_not_silent_success():
    finding = analyze_source("def broken(\n", "broken.py").findings[0]
    assert finding.rule_id == "python-analysis"
    assert not finding.repairable


def test_ambiguous_import_stays_visible_but_cannot_be_repaired():
    source = (
        "if flag:\n    import openai as sdk\nelse:\n    import other as sdk\n"
        "result = sdk.ChatCompletion.create(model='x', messages=[])\n"
    )
    analysis = analyze_source(source, "app.py")
    assert analysis.findings
    assert not analysis.findings[0].repairable
    assert "Ambiguous" in analysis.findings[0].message
