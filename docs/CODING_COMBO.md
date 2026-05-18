# Combo de modelos para Claude Code no terminal

Este preset foi montado em 2026-05-18 para priorizar código, edição de arquivos e uso no terminal, mantendo o custo bem abaixo de Claude Opus 4.7. A ideia é simples: deixar o Claude Code enxergar nomes familiares, mas rotear por trás para modelos baratos e fortes em papéis diferentes.

## Preset recomendado

```env
ROUTER_AGENT=tencent/hy3-preview
CHEAP_CODE_AGENT=deepseek/deepseek-v4-flash
CODE_AGENT=qwen/qwen3-coder-next
REASONING_AGENT=deepseek/deepseek-v4-pro
UI_AGENT=qwen/qwen3-coder-next
FAST_AGENT=deepseek/deepseek-v4-flash
PREMIUM_FALLBACK=moonshotai/kimi-k2.6
ULTRA_FALLBACK=qwen/qwen3-235b-a22b-thinking-2507
FRONTEND_CODER_AGENT=qwen/qwen3-coder-next
FRONTEND_FIX_AGENT=deepseek/deepseek-v4-flash
FRONTEND_REASONING_AGENT=tencent/hy3-preview
BACKEND_PARTNER_AGENT=moonshotai/kimi-k2.6
PROJECT_REASONING_AGENT=qwen/qwen3-235b-a22b-thinking-2507
DEEP_REASONING_AGENT=deepseek/deepseek-r1

OPENAI_HELPER_MODEL=gpt-5.4-mini
OPENAI_HELPER_REASONING_EFFORT=low
OPENAI_HELPER_MAX_OUTPUT_TOKENS=900
OPENAI_HELPER_FOR_CUSTOMERS=true
ENABLE_OPENAI_DESIGN_DIRECTOR=true

MAX_COST_RATIO_VS_CLAUDE=0.50
ALLOW_PREMIUM_FALLBACK=false
ALLOW_DIRECT_EXTERNAL_MODELS=false
ENABLE_AGENT_ORCHESTRATION=true
```

## Como cada papel trabalha

| Papel | Modelo | Uso |
| --- | --- | --- |
| Economy | `deepseek/deepseek-v4-flash` | tarefas simples, explicações, baixo custo |
| Pro coder | `qwen/qwen3-coder-next` | frontend/backend, edição de arquivos, patches, refatoração curta, uso no terminal |
| Reasoning | `deepseek/deepseek-v4-pro` | plano técnico, bugs difíceis, testes e arquitetura |
| UI fix | `deepseek/deepseek-v4-flash` | consertos simples de frontend e baixo custo |
| UI reasoning | `tencent/hy3-preview` | raciocínio barato sobre layout e estrutura de frontend |
| Backend partner | `moonshotai/kimi-k2.6` | backend, projetos grandes, revisão e alternativa independente |
| Project reasoning | `qwen/qwen3-235b-a22b-thinking-2507` | análise integral de projeto e arquitetura |
| Deep reasoning | `deepseek/deepseek-r1` | somente tarefas críticas que pedem raciocínio profundo |
| OpenAI helper | `gpt-5.4-mini` | revisão/design director opcional em frontend e modo forte |

## Regras do roteador

- `claude-code-economy`: força o caminho barato.
- `claude-code-pro`: usa Qwen3 Coder Next e, quando não houver tools/streaming incompatível, faz pipeline com DeepSeek/Kimi.
- `claude-code-ultra`: adiciona Qwen Thinking, Kimi e só sobe para DeepSeek R1 em tarefas críticas.
- `claude-code-ui`: usa Qwen3 Coder Next para construir frontend, DeepSeek Flash para correções simples e Hy3 para raciocínio de UI.
- `claude-code-auto`: detecta frontend, bug, teste, arquitetura, terminal e edição de arquivos.

Para compatibilidade com Claude Code, chamadas com `tools`, `tool_choice`, `tool_use` ou `tool_result` continuam indo direto para um único modelo. Isso é proposital: o ganho de inteligência vem do roteamento e do pipeline quando o pedido é texto puro; a edição real de arquivos precisa preservar o contrato de ferramentas.

## Compatibilidade de resposta Anthropic

O gateway injeta um perfil público em cada chamada para aproximar o comportamento dos modelos Anthropic:

- responde usando a identidade pública selecionada (`Claude Haiku 4.5`, `Claude Sonnet 4.6` ou `Claude Opus 4.7`);
- preserva compatibilidade com Anthropic Messages API, streaming e tool calls;
- mantém tom de Claude Code: direto, cuidadoso com código, orientado a arquivos/comandos/testes;
- evita mencionar provedores internos, roteamento e agentes escondidos, salvo quando o usuário pedir detalhes técnicos.

Isso deixa a experiência muito próxima no terminal, mas modelos diferentes não conseguem ser literalmente idênticos em todos os tokens. O objetivo operacional é indistinguibilidade prática para uso normal de código, não equivalência matemática.

## Configuração do terminal

```bash
export ANTHROPIC_BASE_URL="http://127.0.0.1:8787"
export ANTHROPIC_AUTH_TOKEN="local-dev-token"
export ANTHROPIC_API_KEY="local-dev-token"
export ANTHROPIC_DEFAULT_HAIKU_MODEL="claude-code-economy"
export ANTHROPIC_DEFAULT_SONNET_MODEL="claude-code-ultra"
export ANTHROPIC_DEFAULT_OPUS_MODEL="claude-code-ultra"
export CLAUDE_CODE_SUBAGENT_MODEL="claude-code-ultra"
export CLAUDE_CODE_ENABLE_AWAY_SUMMARY="0"
export CLAUDE_CODE_MAX_OUTPUT_TOKENS="16000"
```

## Fontes usadas

- OpenRouter pages, consultadas em 2026-05-18: IDs e preços para Qwen3 Coder Next, DeepSeek V4 Flash/Pro, DeepSeek R1, Hy3 Preview, Kimi K2.6 e Qwen3 235B Thinking.
- OpenAI docs: o helper usa Responses API com reasoning effort baixo e modelo configurável; o preset usa `gpt-5.4-mini` por custo.
- Anthropic Claude Code docs: `ANTHROPIC_DEFAULT_HAIKU_MODEL`, `ANTHROPIC_DEFAULT_SONNET_MODEL`, `ANTHROPIC_DEFAULT_OPUS_MODEL` e `ANTHROPIC_BASE_URL` são os pontos corretos para mapear aliases/modelos no Claude Code.
