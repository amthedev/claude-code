# Benchmark economico do gateway Claude Code

Este benchmark existe para conferir se a IA esta roteando certo sem gastar creditos a toa.

## Sem gastar creditos

Com o servidor local rodando, execute:

```bash
python3 scripts/benchmark_gateway.py
```

O modo padrao chama apenas `/v1/router/debug`. Ele valida:

- perguntas simples ficam sem pipeline multiagente;
- pedidos atuais ativam pesquisa web em `Auto`;
- tool calls continuam sem orquestracao;
- frontend vai para `claude-code-ui`;
- bugs, arquitetura e tarefas profundas entram no pipeline quando faz sentido;
- a rota efetiva continua dentro do alvo de custo.

## Smoke real com gasto baixo

Para medir latencia real, use poucos prompts curtos:

```bash
python3 scripts/benchmark_gateway.py --live
```

Esse modo chama `/v1/messages` apenas nos casos marcados como seguros: identidade do modelo,
explicacao simples e tool contract curto. Ele usa `max_tokens` pequeno para gastar pouco.

Para testar pipeline profundo real, rode conscientemente:

```bash
python3 scripts/benchmark_gateway.py --live --live-deep
```

Esse modo pode gerar varias chamadas internas, entao nao use em loop.

## Resultado esperado

- `simple_pro`: `use_orchestration=false`.
- `tool_contract`: `use_orchestration=false`.
- `current_web_auto`: `web_search_should_search=true`.
- `frontend_auto`: `mode=ui`.
- `bugfix_deep` e `architecture_ultra`: `use_orchestration=true`.

Se algo falhar, revise `claude_gateway/routing.py` antes de trocar modelo.

## Precos

Os precos do roteador ficam em `claude_gateway/budget.py`. Em 2026-05-22, a checagem
contra a API publica de modelos da OpenRouter confirmou os modelos padrao e corrigiu
`qwen/qwen3-235b-a22b-thinking-2507` para o preco atual publicado.

Fonte primaria: https://openrouter.ai/api/v1/models
