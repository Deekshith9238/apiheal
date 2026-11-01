"""Resolve SDK calls without importing repository code.

Automatic transformations deliberately cover a small, testable subset. Findings
outside that subset remain visible with an explicit unsupported reason.
"""

import ast
import hashlib
import io
import tokenize
from dataclasses import dataclass, field
from pathlib import Path

import libcst as cst
from libcst.metadata import (
    MetadataWrapper,
    PositionProvider,
    QualifiedNameProvider,
    QualifiedNameSource,
)

from apiheal.core.models import Finding, Span

OPENAI_DOC = "https://developers.openai.com/api/reference/python"
GEMINI_DOC = "https://ai.google.dev/gemini-api/docs/libraries"
LANGCHAIN_DOC = "https://github.com/langchain-ai/langchain-google/discussions/1422"
LANGCHAIN_RULE = "langchain-vertexai-deprecated"
LANGCHAIN_MODULE = "langchain_google_vertexai"
# Deprecated with the langchain-google-genai 4.0.0 release (announced 2025-12-08).
LANGCHAIN_REPLACEMENTS = {
    "ChatVertexAI": "ChatGoogleGenerativeAI",
    "VertexAI": "GoogleGenerativeAI",
    "VertexAIEmbeddings": "GoogleGenerativeAIEmbeddings",
}
RULE = "openai-legacy-completions"
TARGETS = {
    "openai.ChatCompletion.create": RULE,
    "openai.Completion.create": RULE,
    "openai.Embedding.create": "openai-legacy-embeddings",
}
ALLOWED_ARGUMENTS = {
    "model",
    "messages",
    "temperature",
    "max_tokens",
    "top_p",
    "frequency_penalty",
    "presence_penalty",
    "stop",
    "n",
    "seed",
    "timeout",
    "user",
}


def read_source(path: Path) -> tuple[str, str]:
    data = path.read_bytes()
    encoding, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
    return data.decode(encoding), encoding


def finding_id(rule: str, file: str, line: int) -> str:
    return hashlib.sha256(f"{rule}:{file}:{line}".encode()).hexdigest()[:20]


@dataclass
class CallSite:
    node: cst.Call
    name: str
    span: Span
    ambiguous: bool = False


@dataclass
class SourceAnalysis:
    findings: list[Finding] = field(default_factory=list)
    migrated_source: str | None = None


class SDKVisitor(cst.CSTVisitor):
    METADATA_DEPENDENCIES = (QualifiedNameProvider, PositionProvider)

    def __init__(self):
        self.calls: list[CallSite] = []
        self.gemini: list[Span] = []
        self.langchain: list[tuple[str, Span]] = []

    def visit_Call(self, node: cst.Call) -> None:
        names = self.get_metadata(QualifiedNameProvider, node.func, set())
        # A local binding may replace the SDK entirely. Do not report that
        # object's methods as a confirmed imported SDK call.
        if any(name.source != QualifiedNameSource.IMPORT for name in names):
            return
        imported = sorted(
            (
                name
                for name in names
                if name.source == QualifiedNameSource.IMPORT and name.name in TARGETS
            ),
            key=lambda name: name.name,
        )
        if not imported:
            return
        name = imported[0]
        pos = self.get_metadata(PositionProvider, node)
        self.calls.append(
            CallSite(
                node,
                name.name,
                Span(
                    line=pos.start.line,
                    column=pos.start.column,
                    end_line=pos.end.line,
                    end_column=pos.end.column,
                ),
                ambiguous=len(names) != 1,
            )
        )

    def visit_Import(self, node: cst.Import) -> None:
        if any(
            cst.helpers.get_full_name_for_node(a.name) == "google.generativeai" for a in node.names
        ):
            self.gemini.append(self._span(node))

    def visit_ImportFrom(self, node: cst.ImportFrom) -> None:
        # Relative imports have no module and never name an installed SDK.
        module = cst.helpers.get_full_name_for_node(node.module) if node.module else None
        if module == "google.generativeai" or (
            module == "google"
            and not isinstance(node.names, cst.ImportStar)
            and any(
                cst.helpers.get_full_name_for_node(a.name) == "generativeai" for a in node.names
            )
        ):
            self.gemini.append(self._span(node))
        if _langchain_module(module) and not isinstance(node.names, cst.ImportStar):
            for alias in node.names:
                name = cst.helpers.get_full_name_for_node(alias.name)
                if name in LANGCHAIN_REPLACEMENTS:
                    self.langchain.append((name, self._span(node)))

    def visit_Attribute(self, node: cst.Attribute) -> None:
        # Module-style use, e.g. `import langchain_google_vertexai as lgv; lgv.ChatVertexAI`.
        for qualified in self.get_metadata(QualifiedNameProvider, node, set()):
            module, _, name = qualified.name.rpartition(".")
            if (
                qualified.source == QualifiedNameSource.IMPORT
                and name in LANGCHAIN_REPLACEMENTS
                and _langchain_module(module)
            ):
                self.langchain.append((name, self._span(node)))
                return

    def _span(self, node: cst.CSTNode) -> Span:
        pos = self.get_metadata(PositionProvider, node)
        return Span(
            line=pos.start.line,
            column=pos.start.column,
            end_line=pos.end.line,
            end_column=pos.end.column,
        )


def _langchain_module(module: str | None) -> bool:
    return module is not None and (
        module == LANGCHAIN_MODULE or module.startswith(f"{LANGCHAIN_MODULE}.")
    )


def _unique_name(base: str, names: set[str]) -> str:
    name = base
    suffix = 2
    while name in names:
        name = f"{base}_{suffix}"
        suffix += 1
    names.add(name)
    return name


def _function_for_call(tree: ast.Module, site: CallSite, source: str) -> tuple | str:
    for function in tree.body:
        if not isinstance(function, ast.FunctionDef):
            continue
        body = function.body
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            if isinstance(body[0].value.value, str):
                body = body[1:]
        if len(body) != 2 or function.decorator_list:
            continue
        assignment, returned = body
        if not (
            isinstance(assignment, ast.Assign)
            and len(assignment.targets) == 1
            and isinstance(assignment.targets[0], ast.Name)
            and isinstance(assignment.value, ast.Call)
            and isinstance(returned, ast.Return)
        ):
            continue
        call = assignment.value
        if (call.lineno, call.col_offset) != (site.span.line, site.span.column):
            continue
        if assignment.lineno == function.lineno or len({s.lineno for s in function.body}) != len(
            function.body
        ):
            return "Inline/semicolon function bodies require review to preserve statement scope."
        if call.args or any(k.arg not in ALLOWED_ARGUMENTS for k in call.keywords):
            return "Only supported, explicit keyword arguments can be migrated."
        if not {"model", "messages"}.issubset({k.arg for k in call.keywords}):
            return "The first rule requires explicit model and messages arguments."
        variable = assignment.targets[0].id
        expected = ast.parse(f'{variable}["choices"][0]["message"]["content"]', mode="eval").body
        if ast.dump(returned.value) != ast.dump(expected):
            return "Response handling is outside the supported content extraction pattern."
        segment = ast.get_source_segment(source, returned.value) or ""
        if "\n" in segment or "\r" in segment or "#" in segment:
            return "Multiline response access needs review to preserve comments."
        if any(isinstance(n, (ast.Global, ast.Nonlocal)) for n in ast.walk(function)):
            return "Global/nonlocal response handling requires review."
        return function, variable, returned
    return "Only simple top-level synchronous functions returning response text are supported."


def _migration(
    wrapper: MetadataWrapper, visitor: SDKVisitor, source: str
) -> tuple[str | None, str]:
    if not visitor.calls:
        return None, ""
    if any(site.ambiguous for site in visitor.calls):
        return None, "Ambiguous import bindings require manual review."
    if any(site.name != "openai.ChatCompletion.create" for site in visitor.calls):
        return None, "This release only repairs synchronous legacy ChatCompletion calls."
    tree = ast.parse(source)
    if any(
        isinstance(n, ast.ImportFrom) and any(a.name == "*" for a in n.names)
        for n in ast.walk(tree)
    ):
        return None, "Star imports make migration scope ambiguous."
    if any(
        isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id in {"exec", "eval", "globals", "locals", "setattr", "getattr", "__import__"}
        for n in ast.walk(tree)
    ):
        return None, "Dynamic namespace or attribute access requires review."
    # Support a single top-level SDK binding. Any SDK configuration/mutation or
    # other use of that binding makes the migration ineligible.
    imports = []
    for statement in tree.body:
        if isinstance(statement, ast.Import) and len(statement.names) == 1:
            alias = statement.names[0]
            if alias.name == "openai":
                imports.append((statement, alias.asname or "openai"))
        if isinstance(statement, ast.ImportFrom) and statement.module == "openai":
            if len(statement.names) == 1 and statement.names[0].name == "ChatCompletion":
                alias = statement.names[0]
                imports.append((statement, alias.asname or "ChatCompletion"))
    if len(imports) != 1:
        return None, "A single top-level OpenAI import is required by the first codemod."
    statement, binding = imports[0]
    uses = [n for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id == binding]
    if len(uses) != len(visitor.calls):
        return None, "Additional SDK uses, shadowing, or global configuration require review."
    functions = {}
    for site in visitor.calls:
        match = _function_for_call(tree, site, source)
        if isinstance(match, str):
            return None, match
        function, variable, returned = match
        functions[function.lineno] = (variable, returned.lineno, returned.col_offset)
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names.update(
        a.asname or a.name
        for n in ast.walk(tree)
        if isinstance(n, (ast.Import, ast.ImportFrom))
        for a in n.names
    )
    names.update(n.arg for n in ast.walk(tree) if isinstance(n, ast.arg))
    names.update(
        n.name
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    )
    client_import = _unique_name("_apiheal_OpenAI", names)
    client_variable = _unique_name("_apiheal_client", names)
    transformer = MigrationTransformer(
        {(s.span.line, s.span.column) for s in visitor.calls},
        functions,
        (statement.lineno, statement.col_offset),
        client_import,
        client_variable,
    )
    return wrapper.visit(transformer).code, ""


class MigrationTransformer(cst.CSTTransformer):
    METADATA_DEPENDENCIES = (PositionProvider,)

    def __init__(self, calls, functions, import_line, client_import, client_variable):
        self.calls = calls
        self.functions = functions
        self.import_line = import_line
        self.client_import = client_import
        self.client_variable = client_variable

    def leave_Import(self, original_node: cst.Import, updated_node: cst.Import):
        return self._import(original_node, updated_node)

    def leave_ImportFrom(self, original_node: cst.ImportFrom, updated_node: cst.ImportFrom):
        return self._import(original_node, updated_node)

    def _import(self, original, updated):
        pos = self.get_metadata(PositionProvider, original).start
        if (pos.line, pos.column) == self.import_line:
            return cst.ImportFrom(
                module=cst.Name("openai"),
                names=[
                    cst.ImportAlias(
                        name=cst.Name("OpenAI"),
                        asname=cst.AsName(cst.Name(self.client_import)),
                    )
                ],
                semicolon=updated.semicolon,
            )
        return updated

    def leave_Call(self, original_node: cst.Call, updated_node: cst.Call):
        pos = self.get_metadata(PositionProvider, original_node).start
        if (pos.line, pos.column) in self.calls:
            return updated_node.with_changes(
                func=cst.parse_expression(f"{self.client_variable}.chat.completions.create")
            )
        return updated_node

    def leave_Return(self, original_node: cst.Return, updated_node: cst.Return):
        pos = self.get_metadata(PositionProvider, original_node).start
        for variable, line, column in self.functions.values():
            if (line, column) == (pos.line, pos.column):
                return updated_node.with_changes(
                    value=cst.parse_expression(f"{variable}.choices[0].message.content")
                )
        return updated_node

    def leave_FunctionDef(self, original_node: cst.FunctionDef, updated_node: cst.FunctionDef):
        if self.get_metadata(PositionProvider, original_node).start.line not in self.functions:
            return updated_node
        if not isinstance(updated_node.body, cst.IndentedBlock):
            return updated_node
        body = list(updated_node.body.body)
        prefix = []
        if len(body) == 3:
            prefix = body[:1]  # Preserve the function docstring outside the context manager.
            body = body[1:]
        context = cst.With(
            items=[
                cst.WithItem(
                    item=cst.Call(cst.Name(self.client_import)),
                    asname=cst.AsName(cst.Name(self.client_variable)),
                )
            ],
            body=cst.IndentedBlock(body=body),
        )
        return updated_node.with_changes(
            body=updated_node.body.with_changes(body=[*prefix, context])
        )


def analyze_source(source: str, file: str) -> SourceAnalysis:
    result = SourceAnalysis()
    try:
        wrapper = MetadataWrapper(cst.parse_module(source))
        visitor = SDKVisitor()
        wrapper.visit(visitor)
        migrated, reason = _migration(wrapper, visitor, source)
    except (cst.ParserSyntaxError, SyntaxError, ValueError, RecursionError) as exc:
        result.findings.append(
            Finding(
                id=finding_id("python-analysis", file, 1),
                rule_id="python-analysis",
                provider="unknown",
                category="unsupported",
                file=file,
                confidence="low",
                message=f"Source cannot be safely analyzed ({type(exc).__name__}).",
            )
        )
        return result
    result.migrated_source = migrated
    for site in visitor.calls:
        rule = TARGETS[site.name]
        result.findings.append(
            Finding(
                id=finding_id(rule, file, site.span.line),
                rule_id=rule,
                provider="openai",
                category="sdk_migration",
                file=file,
                span=site.span,
                confidence="low" if site.ambiguous else "high",
                repairable=migrated is not None,
                message=("Supported legacy chat call can be migrated." if migrated else reason),
                evidence=[
                    f"{'Possible' if site.ambiguous else 'Resolved'} imported call: {site.name}"
                ],
                documentation=OPENAI_DOC,
            )
        )
    for span in visitor.gemini:
        result.findings.append(
            Finding(
                id=finding_id("gemini-old-sdk", file, span.line),
                rule_id="gemini-old-sdk",
                provider="gemini",
                category="sdk_migration",
                file=file,
                span=span,
                message="Legacy Gemini SDK import found; automatic migration is not yet supported.",
                documentation=GEMINI_DOC,
            )
        )
    for name, span in visitor.langchain:
        replacement = LANGCHAIN_REPLACEMENTS[name]
        result.findings.append(
            Finding(
                # Several deprecated classes can share one import line.
                id=finding_id(f"{LANGCHAIN_RULE}.{name}", file, span.line),
                rule_id=LANGCHAIN_RULE,
                provider="langchain",
                category="deprecation",
                file=file,
                span=span,
                message=(
                    f"{name} (langchain-google-vertexai) is deprecated and scheduled for "
                    f"removal; use {replacement} from langchain-google-genai>=4.0.0, which "
                    "reaches Vertex AI with vertexai=True, project, and location."
                ),
                evidence=[f"Resolved imported class: {LANGCHAIN_MODULE}.{name}"],
                documentation=LANGCHAIN_DOC,
            )
        )
    return result
