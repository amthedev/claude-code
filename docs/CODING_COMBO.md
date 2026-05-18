# Combo de modelos para Claude Code no terminal

Este preset foi montado em 2026-05-18 para priorizar código, edição de arquivos e uso no terminal, mantendo o custo bem abaixo de Claude Opus 4.7. A ideia é simples: deixar o Claude Code enxergar nomes familiares, mas rotear por trás para modelos baratos e fortes em papéis diferentes.

## Preset recomendado

```env
ROUTER_AGENT=tencent/hy3-preview
CHEAP_CODE_AGENT=deepseek/deepseek-v4-flash
CODE_AGENT=qwen/qwen3-coder-flash
REASONING_AGENT=deepseek/deepseek-v4-pro
UI_AGENT=moonshotai/kimi-k2.6
FAST_AGENT=deepseek/deepseek-v4-flash
PREMIUM_FALLBACK=moonshotai/kimi-k2.6
ULTRA_FALLBACK=qwen/qwen3.6-flash

OPENAI_HELPER_MODEL=gpt-5.5
OPENAI_HELPER_REASONING_EFFORT=low
OPENAI_HELPER_MAX_OUTPUT_TOKENS=900
OPENAI_HELPER_FOR_CUSTOMERS=false

MAX_COST_RATIO_VS_CLAUDE=0.50
ALLOW_PREMIUM_FALLBACK=false
ALLOW_DIRECT_EXTERNAL_MODELS=false
ENABLE_AGENT_ORCHESTRATION=true
```

## Como cada papel trabalha

| Papel | Modelo | Uso |
| --- | --- | --- |
| Economy | `deepseek/deepseek-v4-flash` | tarefas simples, explicações, baixo custo |
| Pro coder | `qwen/qwen3-coder-flash` | edição de arquivos, patches, refatoração curta, uso no terminal |
| Reasoning | `deepseek/deepseek-v4-pro` | plano técnico, bugs difíceis, testes e arquitetura |
| UI | `moonshotai/kimi-k2.6` | frontend, layout, UX e telas |
| Ultra fallback | `qwen/qwen3.6-flash` | candidato extra barato para tarefas críticas |
| OpenAI helper | `gpt-5.5` | revisão opcional em chamadas admin sem streaming |

## Regras do roteador

- `claude-code-economy`: força o caminho barato.
- `claude-code-pro`: usa o coder principal e, quando não houver tools/streaming incompatível, faz pipeline de plano, teste, resposta e revisão.
- `claude-code-ultra`: adiciona candidato extra e revisão, ainda dentro do budget.
- `claude-code-ui`: manda frontend direto para Kimi.
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
export ANTHROPIC_DEFAULT_SONNET_MODEL="claude-code-pro"
export ANTHROPIC_DEFAULT_OPUS_MODEL="claude-code-ultra"
export CLAUDE_CODE_SUBAGENT_MODEL="claude-code-pro"
```

## Fontes usadas

- OpenRouter models API, consultada em 2026-05-18: preços e suporte a tool calling para Claude Opus 4.7, Sonnet 4.6, DeepSeek V4, Qwen, Kimi e StepFun.
- OpenAI docs: GPT-5.5 é indicado para coding, tool-heavy agents e workflows complexos; a própria orientação recomenda Responses API, reasoning effort ajustável, prompt caching, tool design, compaction e Agents SDK para sistemas agenticos.
- Anthropic Claude Code docs: `ANTHROPIC_DEFAULT_HAIKU_MODEL`, `ANTHROPIC_DEFAULT_SONNET_MODEL`, `ANTHROPIC_DEFAULT_OPUS_MODEL` e `ANTHROPIC_BASE_URL` são os pontos corretos para mapear aliases/modelos no Claude Code.
