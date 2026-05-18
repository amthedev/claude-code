# Analise do projeto e deploy Square Cloud

Data da revisao: 2026-05-17.

## Estado atual

O projeto e um gateway FastAPI compativel com o formato da Anthropic Messages API. Ele deixa Claude Code e o chat web chamarem nomes publicos como `claude-code-pro`, enquanto o backend roteia para modelos OpenRouter mais baratos.

O `frontier` e uma interface HTML/CSS/JS estatica com app de cliente e Admin. O Admin gera gift cards para venda; o cliente cria a propria conta com nome, e-mail, senha e gift card. O backend tambem salva gift cards e contas em `ACCOUNT_DATA_FILE`, entao o resgate funciona entre navegadores diferentes. A protecao real fica no backend: tokens de cliente e reserva diaria de custo por token.

## Modelos usados

Precos confirmados na API publica do OpenRouter em 2026-05-17. Os valores abaixo sao USD por token.

| Papel | Modelo atual | Entrada | Saida | Uso |
| --- | --- | ---: | ---: | --- |
| Baseline de comparacao | `anthropic/claude-opus-4.7` | 0.000005 | 0.000025 | So para calcular economia |
| Roteador | `tencent/hy3-preview` | 0.000000066 | 0.00000026 | Classificar/planejar barato |
| Economico | `deepseek/deepseek-v4-flash` | 0.000000112 | 0.000000224 | Tarefas simples e clientes baratos |
| Codigo/raciocinio | `deepseek/deepseek-v4-pro` | 0.000000435 | 0.00000087 | Caminho principal de codigo |
| UI/front | `moonshotai/kimi-k2.6` | 0.00000073 | 0.00000349 | Frontend, layout e escrita visual |
| Rapido/testes | `stepfun/step-3.5-flash` | 0.0000001 | 0.0000003 | Agente auxiliar barato |

Comparado ao Claude Opus 4.7 por custo misto entrada+saida:

- DeepSeek V4 Pro fica em torno de 4.35% do custo.
- DeepSeek V4 Flash fica em torno de 1.12% do custo.
- Kimi K2.6 fica em torno de 14.07% do custo.
- Mesmo com pipeline multiagente, o projeto continua abaixo da meta de 50% quando os modelos padrao sao mantidos.

## O que foi fechado para evitar prejuizo

1. `ALLOW_DIRECT_EXTERNAL_MODELS=false` por padrao.
   Antes, uma chamada direta para `anthropic/claude-opus-4.7` podia virar modo `direct`. Agora nomes externos com `/` entram no roteador seguro, a menos que voce habilite explicitamente para uso admin.

2. `MAX_REQUEST_OUTPUT_TOKENS=4096`.
   O backend limita `max_tokens` antes de enviar ao OpenRouter.

3. `MAX_REQUEST_INPUT_CHARS=120000`.
   Prompts gigantes sao recusados antes de gerar custo.

4. `CUSTOMER_ACCOUNTS`.
   Cliente pode ter token proprio, preco mensal, limite diario, modelo permitido e status ativo/pausado.

5. Reserva diaria por custo.
   O backend calcula um custo estimado conservador por request, aplica `COST_RESERVE_MULTIPLIER=2.0` e bloqueia quando bater o orcamento diario do cliente.

## Como vender uma conta API sem perder dinheiro

Voce pode criar clientes fixos em `CUSTOMER_ACCOUNTS` ou deixar o Admin gerar gift cards. Quando o cliente resgata um gift card, o backend gera um token `cus_...` automaticamente.

```env
CUSTOMER_ACCOUNTS=cus_live_abc|Cliente|149.90|60000|claude-code-pro|true;cus_live_xyz|Maria|299.90|120000|claude-code-ultra|true
```

Formato:

```text
token|nome|preco_mensal_brl|limite_diario_tokens|modelo_publico_permitido|ativo
```

Modelos publicos recomendados:

- `claude-code-economy`: barato, tarefas simples.
- `claude-code-pro`: melhor equilibrio para Claude Code.
- `claude-code-ultra`: raciocinio extra, ainda sem chamar Claude direto.
- `claude-code-ui`: tarefas de frontend.
- `claude-code-auto`: roteador automatico.

Para o cliente usar no Claude Code:

```bash
export ANTHROPIC_BASE_URL="https://SEU-SUBDOMINIO.squareweb.app"
export ANTHROPIC_AUTH_TOKEN="TOKEN_DO_CLIENTE"
export ANTHROPIC_API_KEY=""
export ANTHROPIC_DEFAULT_HAIKU_MODEL="claude-code-economy"
export ANTHROPIC_DEFAULT_SONNET_MODEL="claude-code-pro"
export ANTHROPIC_DEFAULT_OPUS_MODEL="claude-code-ultra"
export CLAUDE_CODE_SUBAGENT_MODEL="claude-code-pro"
```

## Square Cloud

Arquivos adicionados:

- `requirements.txt`: dependencias que a Square Cloud instala com `pip install`.
- `squarecloud.app`: configuracao de deploy com `START=uvicorn claude_gateway.main:app --host 0.0.0.0 --port 80`.

Variaveis obrigatorias no painel da Square Cloud:

```env
OPENROUTER_API_KEY=sk-or-v1-...
GATEWAY_API_KEYS=um-token-admin-forte
OPENROUTER_SITE_URL=https://SEU-SUBDOMINIO.squareweb.app
OPENROUTER_APP_NAME=Claude Code
MAX_COST_RATIO_VS_CLAUDE=0.50
ALLOW_PREMIUM_FALLBACK=false
ALLOW_DIRECT_EXTERNAL_MODELS=false
ACCOUNT_DATA_FILE=data/accounts.json
CUSTOMER_ACCOUNTS=...
```

Depois do deploy:

```bash
curl https://SEU-SUBDOMINIO.squareweb.app/health
curl https://SEU-SUBDOMINIO.squareweb.app/v1/budget \
  -H "Authorization: Bearer um-token-admin-forte"
```

## O que ainda falta para producao forte

1. Banco de dados real para clientes.
   Hoje `CUSTOMER_ACCOUNTS` e otimo para comecar controlado, mas para escalar precisa de Postgres/Supabase/Firebase ou outro banco.

2. Banco duravel para gift cards.
   Hoje o backend usa JSON em `ACCOUNT_DATA_FILE`. Funciona para comecar, mas para escalar precisa migrar gift cards, contas e senhas para Postgres/Supabase/Firebase ou outro banco.

3. Pagamento com webhook.
   Stripe/Mercado Pago devem ativar, pausar ou renovar clientes automaticamente.

4. Custo real pos-request.
   O projeto reserva custo antes da chamada. O proximo passo e consultar stats reais do OpenRouter por geracao e reconciliar reserva versus gasto real.

5. Observabilidade.
   Adicionar logs estruturados, alertas de orcamento e painel de uso por cliente.

6. Marca.
   A UI esta propositalmente parecida com Claude para prototipo, mas vender usando marca/nome muito parecido pode dar problema comercial. Para producao, o ideal e manter compatibilidade com Claude Code, mas ter marca propria.

## Verificacao feita

```bash
.venv/bin/pytest -q
.venv/bin/ruff check .
node --check frontier/shared.js && node --check frontier/client.js && node --check frontier/admin.js
```

Resultado: tudo passou.
