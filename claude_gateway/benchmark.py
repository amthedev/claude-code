from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class BenchmarkCase:
    id: str
    label: str
    model: str
    prompt: str
    max_tokens: int = 96
    gateway_web_search: str = "auto"
    tools: tuple[dict[str, Any], ...] = ()
    live_default: bool = False
    expect_mode: str | None = None
    expect_task_type: str | None = None
    expect_orchestration: bool | None = None
    expect_web_search: bool | None = None


BENCHMARK_CASES: tuple[BenchmarkCase, ...] = (
    BenchmarkCase(
        id="simple_pro",
        label="Pergunta simples Pro",
        model="claude-code-pro",
        prompt="Explique em 3 bullets o que e uma funcao Python simples.",
        live_default=True,
        expect_task_type="explanation",
        expect_orchestration=False,
        expect_web_search=False,
    ),
    BenchmarkCase(
        id="identity_no_upstream",
        label="Identidade sem upstream",
        model="claude-code-ultra",
        prompt="qual modelo voce e?",
        live_default=True,
        expect_mode="ultra",
        expect_web_search=False,
    ),
    BenchmarkCase(
        id="current_web_auto",
        label="Pesquisa web em Auto",
        model="claude-code-pro",
        prompt="Pesquise noticias atuais sobre IA e resuma em 2 frases.",
        gateway_web_search="auto",
        expect_web_search=True,
    ),
    BenchmarkCase(
        id="tool_contract",
        label="Tool call Claude Code",
        model="claude-code-pro",
        prompt="Leia um arquivo local e diga o proximo passo.",
        tools=({"name": "read_file", "input_schema": {"type": "object"}},),
        live_default=True,
        expect_mode="pro",
        expect_orchestration=False,
        expect_web_search=False,
    ),
    BenchmarkCase(
        id="frontend_auto",
        label="Frontend automatico",
        model="claude-code-auto",
        prompt="Crie um dashboard React bonito para metricas financeiras.",
        expect_mode="ui",
        expect_task_type="frontend",
        expect_orchestration=True,
        expect_web_search=False,
    ),
    BenchmarkCase(
        id="bugfix_deep",
        label="Bug profundo",
        model="claude-code-pro",
        prompt="Corrija um bug dificil de autenticacao em producao e liste os testes necessarios.",
        expect_mode="pro",
        expect_orchestration=True,
        expect_web_search=False,
    ),
    BenchmarkCase(
        id="architecture_ultra",
        label="Arquitetura Ultra",
        model="claude-code-ultra",
        prompt="Analise a arquitetura integral de todo o projeto e encontre riscos.",
        expect_mode="ultra",
        expect_task_type="architecture",
        expect_orchestration=True,
        expect_web_search=False,
    ),
)


def benchmark_payload(case: BenchmarkCase) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": case.model,
        "max_tokens": case.max_tokens,
        "gateway_web_search": case.gateway_web_search,
        "messages": [{"role": "user", "content": case.prompt}],
    }
    if case.tools:
        payload["tools"] = list(case.tools)
    return payload


def benchmark_failures(case: BenchmarkCase, data: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    if case.expect_mode is not None and data.get("mode") != case.expect_mode:
        failures.append(f"mode expected {case.expect_mode}, got {data.get('mode')}")
    if case.expect_task_type is not None and data.get("task_type") != case.expect_task_type:
        failures.append(f"task_type expected {case.expect_task_type}, got {data.get('task_type')}")
    if case.expect_orchestration is not None and data.get("use_orchestration") != case.expect_orchestration:
        failures.append(
            f"use_orchestration expected {case.expect_orchestration}, got {data.get('use_orchestration')}"
        )
    if case.expect_web_search is not None and data.get("web_search_should_search") != case.expect_web_search:
        failures.append(
            f"web_search_should_search expected {case.expect_web_search}, "
            f"got {data.get('web_search_should_search')}"
        )
    effective_path = (data.get("cost_estimate") or {}).get("effective_path") or {}
    if effective_path and not effective_path.get("within_budget", False):
        failures.append("effective path is over budget")
    return failures
