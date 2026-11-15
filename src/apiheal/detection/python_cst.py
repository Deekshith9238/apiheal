"""Resolve SDK calls without importing repository code.

Automatic transformations deliberately cover a small, testable subset. Findings
outside that subset remain visible with an explicit unsupported reason.
"""

import ast
import hashlib
import io
import json
import tokenize
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from functools import cache
from importlib.resources import files
from pathlib import Path

import libcst as cst
from libcst.metadata import (
    MetadataWrapper,
    PositionProvider,
    QualifiedNameProvider,
    QualifiedNameSource,
)

from apiheal.core.models import Finding, Span
from apiheal.detection.catalog import SDKRule, Selector, builtin_rules

MODEL_RULE = "model-retired"
MODEL_KEYWORDS = {"model", "model_name", "model_id"}
# Shutdowns further away than this are reported as info rather than warnings.
MODEL_WARNING_DAYS = 180
PROVIDER_NOTES = {
    "google": "Gemini API; earliest announced date, Vertex AI schedules may differ",
    "google_vertex": "Vertex AI",
    "openai": "OpenAI API",
    "anthropic": "Claude API; Bedrock and Google-managed Vertex set their own dates",
}
RULE = "openai-legacy-completions"
OPENAI_MIGRATION_RULES = {RULE, "openai-legacy-embeddings"}


@cache
def retired_models() -> tuple[dict[str, dict], dict[str, str]]:
    data = json.loads(
        files("apiheal.detection").joinpath("data/model_retirements.json").read_text("utf-8")
    )
    return {row["id"]: row for row in data["models"]}, data["sources"]


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
class SDKMatch:
    rule: SDKRule
    selector: Selector
    name: str
    span: Span
    ambiguous: bool


@dataclass
class SourceAnalysis:
    findings: list[Finding] = field(default_factory=list)
    migrated_source: str | None = None


class SDKVisitor(cst.CSTVisitor):
    METADATA_DEPENDENCIES = (QualifiedNameProvider, PositionProvider)

    def __init__(self, rules: tuple[SDKRule, ...]):
        self.matches: list[SDKMatch] = []
        self.selectors: dict[tuple[str, str], list[tuple[SDKRule, Selector]]] = {}
        for rule in rules:
            for selector in rule.selectors:
                root = selector.module.split(".", 1)[0]
                self.selectors.setdefault((selector.kind, root), []).append((rule, selector))
        self.models: list[tuple[str, Span, bool]] = []
        self._model_arguments: set[int] = set()

    def visit_Arg(self, node: cst.Arg) -> None:
        if node.keyword and node.keyword.value in MODEL_KEYWORDS:
            self._model_arguments.add(id(node.value))

    def visit_SimpleString(self, node: cst.SimpleString) -> None:
        value = node.evaluated_value
        if not isinstance(value, str):
            return
        model = value.removeprefix("models/")  # Gemini resource-name form.
        if model in retired_models()[0]:
            self.models.append((model, self._span(node), id(node) in self._model_arguments))

    def _match(self, kind: str, names: list[str], node: cst.CSTNode, ambiguous=False) -> None:
        matched = set()
        for name in names:
            root = name.split(".", 1)[0]
            for rule, selector in self.selectors.get((kind, root), ()):
                key = (rule.id, selector.module, selector.name)
                if key not in matched and selector.matches(name):
                    matched.add(key)
                    self.matches.append(SDKMatch(rule, selector, name, self._span(node), ambiguous))

    def _match_usage(self, kind: str, expression: cst.BaseExpression, node: cst.CSTNode) -> None:
        names = self.get_metadata(QualifiedNameProvider, expression, set())
        if any(name.source != QualifiedNameSource.IMPORT for name in names):
            return
        self._match(kind, sorted(name.name for name in names), node, ambiguous=len(names) > 1)

    def visit_Call(self, node: cst.Call) -> None:
        self._match_usage("call", node.func, node)

    def visit_Import(self, node: cst.Import) -> None:
        names = [cst.helpers.get_full_name_for_node(alias.name) for alias in node.names]
        self._match("module", [name for name in names if name], node)

    def visit_ImportFrom(self, node: cst.ImportFrom) -> None:
        if node.relative or node.module is None:
            return
        module = cst.helpers.get_full_name_for_node(node.module)
        if module is None:
            return
        names = []
        if not isinstance(node.names, cst.ImportStar):
            names = [
                f"{module}.{cst.helpers.get_full_name_for_node(alias.name)}" for alias in node.names
            ]
        self._match("module", [module, *names], node)
        self._match("symbol", names, node)

    def visit_Attribute(self, node: cst.Attribute) -> None:
        self._match_usage("symbol", node, node)

    def _span(self, node: cst.CSTNode) -> Span:
        pos = self.get_metadata(PositionProvider, node)
        return Span(
            line=pos.start.line,
            column=pos.start.column,
            end_line=pos.end.line,
            end_column=pos.end.column,
        )


def _unique_name(base: str, names: set[str]) -> str:
    name = base
    suffix = 2
    while name in names:
        name = f"{base}_{suffix}"
        suffix += 1
    names.add(name)
    return name


def _function_for_call(tree: ast.Module, site: SDKMatch, source: str) -> tuple | str:
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
    wrapper: MetadataWrapper, calls: list[SDKMatch], source: str
) -> tuple[str | None, str]:
    if not calls:
        return None, ""
    if any(site.ambiguous for site in calls):
        return None, "Ambiguous import bindings require manual review."
    if any(site.name != "openai.ChatCompletion.create" for site in calls):
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
    if len(uses) != len(calls):
        return None, "Additional SDK uses, shadowing, or global configuration require review."
    functions = {}
    for site in calls:
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
        {(s.span.line, s.span.column) for s in calls},
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


def _model_finding(model: str, span: Span, argument: bool, file: str, today: date) -> Finding:
    table, sources = retired_models()
    row = table[model]
    shutdown = date.fromisoformat(row["shutdown"])
    note = PROVIDER_NOTES[row["provider"]]
    replacement = f"use {row['replacement']}" if row["replacement"] else "choose a current model"
    if shutdown <= today and row["provider"] == "google":
        # The Gemini API publishes earliest dates and Vertex AI runs its own schedule.
        severity = "error"
        message = (
            f"Model '{model}' passed its announced shutdown date, {shutdown} ({note}); "
            f"requests to it may fail. To fix, {replacement}."
        )
    elif shutdown <= today:
        severity = "error"
        message = (
            f"Model '{model}' was shut down on {shutdown} ({note}); requests to it fail. "
            f"To fix, {replacement}."
        )
    else:
        days = (shutdown - today).days
        severity = "warning" if days <= MODEL_WARNING_DAYS else "info"
        message = (
            f"Model '{model}' is scheduled to shut down on {shutdown}, in {days} days ({note}); "
            f"{replacement} before then."
        )
    return Finding(
        id=finding_id(f"{MODEL_RULE}.{model}", file, span.line),
        rule_id=MODEL_RULE,
        provider=row["provider"].removesuffix("_vertex"),
        category="model_retirement",
        file=file,
        span=span,
        severity=severity,
        confidence="high" if argument else "medium",
        message=message,
        evidence=[
            "Passed as a model argument"
            if argument
            else "String literal matches a model ID outside a model argument",
            f"Provider schedule: {sources[row['provider']]}",
        ],
        documentation=sources[row["provider"]],
    )


def analyze_source(
    source: str,
    file: str,
    today: date | None = None,
    rules: tuple[SDKRule, ...] | None = None,
) -> SourceAnalysis:
    today = today or datetime.now(UTC).date()
    result = SourceAnalysis()
    try:
        wrapper = MetadataWrapper(cst.parse_module(source))
        visitor = SDKVisitor(rules if rules is not None else builtin_rules())
        wrapper.visit(visitor)
        calls = [match for match in visitor.matches if match.rule.id in OPENAI_MIGRATION_RULES]
        migrated, reason = _migration(wrapper, calls, source)
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
    for match in visitor.matches:
        rule, selector = match.rule, match.selector
        migration_rule = rule.id in OPENAI_MIGRATION_RULES
        repairable = migration_rule and migrated is not None
        message = selector.message
        if repairable:
            message = "Supported legacy chat call can be migrated."
        elif migration_rule:
            message = reason or message
        evidence = selector.evidence
        if evidence is None and selector.kind != "module":
            prefix = "Possible" if match.ambiguous else "Resolved"
            evidence = f"{prefix} imported {selector.kind}: {match.name}"
        key = f"{rule.id}.{match.name}.{match.span.column}"
        result.findings.append(
            Finding(
                id=finding_id(key, file, match.span.line),
                rule_id=rule.id,
                rule_version=rule.version,
                provider=rule.provider,
                category=rule.category,
                file=file,
                span=match.span,
                severity=rule.severity,
                confidence="low" if match.ambiguous else "high",
                repairable=repairable,
                message=message,
                evidence=[evidence] if evidence else [],
                documentation=rule.documentation,
            )
        )
    for model, span, argument in visitor.models:
        result.findings.append(_model_finding(model, span, argument, file, today))
    return result
