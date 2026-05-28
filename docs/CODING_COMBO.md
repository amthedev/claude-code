# Combo de modelos para Claude Code no terminal

Este preset agora prioriza a IA rodando na sua VPS. A ideia é simples: deixar Claude Code, Cursor, Windsurf e clientes OpenAI-compatible enxergarem nomes familiares, mas rotear por trás para um único provedor principal Anthropic-compatible.

## Provedor principal

```env
VPS_MODEL_BASE_URL=https://sua-vps.example.com
VPS_MODEL_ID=local-model
VPS_MODEL_API_FORMAT=anthropic
VPS_MODEL_API_KEY=
VPS_MODEL_TIMEOUT_SECONDS=55
VPS_MODEL_SLOW_FALLBACK_SECONDS=6
VPS_DISABLE_QWEN_THINKING=true
SIMPLE_REQUEST_MAX_OUTPUT_TOKENS=768
```

## OpenRouter desativado

```env
OPENROUTER_EMERGENCY_FALLBACK=false
OPENROUTER_API_KEY=
```

O gateway nao usa OpenRouter em producao. Se a VPS falhar, a resposta deve falhar claramente em vez de consumir creditos OpenRouter.

## Modelos publicos

```text
claude-code-pro
```

A VPS recebe sempre `VPS_MODEL_ID`. Use `VPS_MODEL_API_FORMAT=openai-chat` para RunPod/vLLM/OpenAI-compatible e `anthropic` para `/v1/messages`.

## Variaveis ainda configuraveis

```env
OPENAI_API_KEY=sk-proj-...
GATEWAY_API_KEYS=strong-admin-token

OPENAI_HELPER_MODEL=gpt-5.4-mini
OPENAI_HELPER_REASONING_EFFORT=low
OPENAI_HELPER_MAX_OUTPUT_TOKENS=900
ENABLE_OPENAI_DESIGN_DIRECTOR=true
ENABLE_OPENAI_DECISION_DIRECTOR=true
ENABLE_WEB_SEARCH=true
WEB_SEARCH_MODEL=gpt-5.5
WEB_SEARCH_CONTEXT_SIZE=low
WEB_SEARCH_FOR_CUSTOMERS=true
WEB_SEARCH_MAX_OUTPUT_TOKENS=900
WEB_SEARCH_TIMEOUT_SECONDS=8

MAX_COST_RATIO_VS_CLAUDE=0.50
ALLOW_PREMIUM_FALLBACK=false
ALLOW_DIRECT_EXTERNAL_MODELS=false
ENABLE_AGENT_ORCHESTRATION=true
```

## Como cada papel trabalha

| Papel | Modelo | Uso |
| --- | --- | --- |
| VPS model | `VPS_MODEL_ID` | resposta principal para todos os modos publicos |
| OpenAI helper | `gpt-5.4-mini` | escolhe defaults, reduz perguntas desnecessárias e revisa/design director em frontend e modo forte |

## Regras do roteador

- `claude-code-pro`: mantém a identidade pública Claude Sonnet 4.5 e chama a VPS.
- Aliases antigos de modelo continuam aceitos por compatibilidade, mas são normalizados para `claude-code-pro`.
- Pesquisa web: por padrão fica em `auto`; usa OpenAI Responses API `web_search` somente quando o pedido exige informação atual ou quando `gateway_web_search="required"`.

Para compatibilidade com Claude Code, chamadas com `tools`, `tool_choice`, `tool_use` ou `tool_result` continuam indo direto para um único modelo. Isso é proposital: o ganho de inteligência vem do roteamento e do pipeline quando o pedido é texto puro; a edição real de arquivos precisa preservar o contrato de ferramentas.

## Compatibilidade de resposta Anthropic

O gateway injeta um perfil público do Claude em cada chamada, mantendo compatibilidade Anthropic:

- responde usando a identidade pública `Claude Sonnet 4.5`;
- preserva compatibilidade com Anthropic Messages API, streaming e tool calls;
- mantém tom de Claude Code: direto, cuidadoso com código, orientado a arquivos/comandos/testes;
- evita mencionar provedores internos, roteamento e agentes escondidos, salvo quando o usuário pedir detalhes técnicos.

Isso deixa a experiência muito próxima no terminal, mas modelos diferentes não conseguem ser literalmente idênticos em todos os tokens. O objetivo operacional é indistinguibilidade prática para uso normal de código, não equivalência matemática.

## Configuração do terminal

```bash
export ANTHROPIC_BASE_URL="http://127.0.0.1:8787"
export ANTHROPIC_AUTH_TOKEN="local-dev-token"
export ANTHROPIC_API_KEY="local-dev-token"
export ANTHROPIC_DEFAULT_HAIKU_MODEL="claude-code-pro"
export ANTHROPIC_DEFAULT_SONNET_MODEL="claude-code-pro"
export ANTHROPIC_DEFAULT_OPUS_MODEL="claude-code-pro"
export CLAUDE_CODE_SUBAGENT_MODEL="claude-code-pro"
export CLAUDE_CODE_ENABLE_AWAY_SUMMARY="0"
export CLAUDE_CODE_MAX_OUTPUT_TOKENS="16000"
```

## Fontes usadas

- Configuracao da VPS: API Anthropic-compatible em `/v1/messages` ou OpenAI-compatible em `/v1/chat/completions`.
- OpenRouter: desativado em producao; mantenha `OPENROUTER_EMERGENCY_FALLBACK=false`.
- Anthropic Claude Code docs: `ANTHROPIC_DEFAULT_HAIKU_MODEL`, `ANTHROPIC_DEFAULT_SONNET_MODEL`, `ANTHROPIC_DEFAULT_OPUS_MODEL` e `ANTHROPIC_BASE_URL` são os pontos corretos para mapear aliases/modelos no Claude Code.
