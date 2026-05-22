from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True, slots=True)
class AssistantSkill:
    id: str
    title: str
    purpose: str
    triggers: tuple[str, ...]
    guidance: str


SKILL_CATALOG: tuple[AssistantSkill, ...] = (
    AssistantSkill(
        "github_connect",
        "Conectar GitHub",
        "Conectar conta/perfil GitHub, listar repos e escolher projeto sem URL manual.",
        ("github", "hub", "perfil", "repositorio", "repositório", "token", "chave"),
        "Use o fluxo por perfil/organização, branch e chave. Não peça URL manual quando a conexão GitHub estiver disponível.",
    ),
    AssistantSkill(
        "github_publish",
        "Publicar Alterações no GitHub",
        "Enviar alterações de workspace GitHub de volta para o repositório.",
        ("push", "publicar", "enviar github", "atualizar github", "commit", "mandar pro github"),
        "Antes de publicar, salve arquivos, rode verificações relevantes e explique branch, arquivos alterados e resultado.",
    ),
    AssistantSkill(
        "workspace_zip",
        "Workspace ZIP",
        "Importar, editar e baixar ZIPs como projetos extraídos.",
        ("zip", "compactado", "baixar projeto", "download", "extrair"),
        "Trate o ZIP como workspace extraído. Toda edição deve atualizar os arquivos do workspace e o download deve refletir o estado atual.",
    ),
    AssistantSkill(
        "workspace_folder",
        "Workspace Pasta",
        "Importar e editar pastas locais enviadas pelo navegador.",
        ("pasta", "folder", "diretorio", "diretório", "arquivos locais"),
        "Preserve caminhos relativos, ignore pastas pesadas e explique que o download final sai como ZIP atualizado.",
    ),
    AssistantSkill(
        "code_review",
        "Revisão de Código",
        "Encontrar bugs, regressões, riscos e lacunas de teste.",
        ("review", "revisao", "revisão", "analise o codigo", "analise o código", "risco"),
        "Priorize achados com arquivo/linha, severidade e impacto. Dê resumo só depois dos problemas.",
    ),
    AssistantSkill(
        "bug_fix",
        "Correção de Bug",
        "Diagnosticar causa raiz e corrigir comportamento quebrado.",
        ("bug", "erro", "falha", "quebrando", "corrija", "conserte", "traceback", "exception"),
        "Reproduza mentalmente ou com teste, corrija no menor escopo seguro e valide o caminho afetado.",
    ),
    AssistantSkill(
        "test_runner",
        "Testes e Terminal",
        "Escolher e rodar comandos seguros de teste/build/lint.",
        ("teste", "testes", "pytest", "npm test", "build", "lint", "terminal", "validar"),
        "Use comandos permitidos do workspace quando fizer sentido. Resuma comando, saída importante e conclusão.",
    ),
    AssistantSkill(
        "frontend_polish",
        "Frontend e UI",
        "Melhorar telas, CSS, layout responsivo e estados visuais.",
        ("frontend", "ui", "css", "layout", "tela", "botao", "botão", "responsivo"),
        "Aplique hierarquia visual, estados, acessibilidade e responsividade sem inventar design destoante.",
    ),
    AssistantSkill(
        "visual_response",
        "Resposta Visual",
        "Responder com tabelas, destaques, passos e blocos intuitivos.",
        ("grafico", "gráfico", "visual", "bonito", "cores", "intuitivo", "tabela"),
        "Use seções curtas, tabelas, callouts, listas progressivas e Markdown legível quando isso ajudar a compreensão.",
    ),
    AssistantSkill(
        "backend_api",
        "Backend e API",
        "Alterar endpoints, contratos HTTP, validações e erros.",
        ("endpoint", "api", "backend", "fastapi", "request", "response", "payload"),
        "Mantenha compatibilidade de contrato, valide entrada no servidor e use mensagens de erro acionáveis.",
    ),
    AssistantSkill(
        "auth_access",
        "Autenticação e Acesso",
        "Lidar com login, tokens, sessões, permissões e escopo por conta.",
        ("login", "auth", "autentic", "token", "sessao", "sessão", "permissao", "permissão"),
        "Nunca exponha segredos. Separe autenticação do app, token do cliente e tokens externos como GitHub.",
    ),
    AssistantSkill(
        "billing_payment",
        "Planos e Pagamentos",
        "Ajustar planos, Pix, assinatura, Mercado Pago e upgrades.",
        ("pagamento", "pix", "mercado pago", "plano", "assinatura", "checkout", "billing"),
        "Preserve idempotência, status de compra e mensagens claras para pagamento pendente/aprovado.",
    ),
    AssistantSkill(
        "conversation_history",
        "Histórico de Conversas",
        "Salvar, titular, listar e abrir conversas recentes.",
        ("historico", "histórico", "conversa", "recentes", "titulo", "título", "chat salvo"),
        "Use títulos semânticos quando houver intenção clara e mantenha saudações simples literais.",
    ),
    AssistantSkill(
        "stream_quality",
        "Qualidade do Streaming",
        "Corrigir duplicação, letras comidas e merge de deltas.",
        ("repetida", "repetido", "duplic", "letra", "comendo", "palavra errada", "stream"),
        "Seja conservador ao reparar texto. Evite cortar palavras legítimas e teste casos de regressão.",
    ),
    AssistantSkill(
        "security_review",
        "Segurança",
        "Avaliar riscos de segredo, path traversal, comando, upload e autorização.",
        ("segurança", "security", "vulnerabilidade", "segredo", "xss", "csrf", "path traversal"),
        "Cheque fronteiras de confiança, sanitização, autorização por conta e risco de execução de comandos.",
    ),
    AssistantSkill(
        "performance",
        "Performance",
        "Reduzir lentidão, payloads grandes, loops caros e render pesado.",
        ("lento", "performance", "otimizar", "travando", "demora", "latencia", "latência"),
        "Meça o caminho crítico, limite payloads e prefira mudanças pequenas que reduzem custo perceptível.",
    ),
    AssistantSkill(
        "accessibility",
        "Acessibilidade",
        "Melhorar navegação por teclado, contraste e labels.",
        ("acessibilidade", "accessibility", "aria", "contraste", "teclado", "screen reader"),
        "Garanta labels, foco, contraste, semântica e controles previsíveis.",
    ),
    AssistantSkill(
        "database_migration",
        "Banco e Migração",
        "Alterar SQLite, schema, migração compatível e dados existentes.",
        ("sqlite", "banco", "database", "schema", "migracao", "migração", "tabela"),
        "Mantenha migrações compatíveis com bancos existentes e teste criação limpa e banco antigo.",
    ),
    AssistantSkill(
        "docs_copy",
        "Documentação e Copy",
        "Melhorar textos, mensagens de erro, instruções e documentação.",
        ("documentacao", "documentação", "copy", "texto", "mensagem", "explicação", "explicacao"),
        "Escreva em português claro, curto e orientado à ação. Evite jargão quando o usuário final verá o texto.",
    ),
    AssistantSkill(
        "refactor",
        "Refatoração",
        "Organizar código sem mudar comportamento.",
        ("refatorar", "refactor", "organizar", "limpar codigo", "limpar código", "duplicacao"),
        "Preserve comportamento, faça passos pequenos e adicione teste quando a área for compartilhada.",
    ),
    AssistantSkill(
        "architecture",
        "Arquitetura",
        "Planejar módulos, contratos, fluxos e limites do sistema.",
        ("arquitetura", "fluxo", "estrutura", "design tecnico", "design técnico", "sistema"),
        "Explique fronteiras, dados, responsabilidades e tradeoffs com decisões práticas.",
    ),
    AssistantSkill(
        "deployment_ci",
        "Deploy e CI",
        "Ajustar build, GitHub Actions, deploy e configuração de ambiente.",
        ("deploy", "ci", "actions", "github actions", "ambiente", "env", "produção", "producao"),
        "Separe configuração local de produção, não vaze .env e valide comandos de build/teste.",
    ),
    AssistantSkill(
        "localization_ptbr",
        "Português e Localização",
        "Corrigir português, labels e tom em pt-BR.",
        ("portugues", "português", "pt-br", "tradução", "traducao", "gramatica", "gramática"),
        "Corrija ortografia e tom sem alterar sentido. Mantenha termos técnicos quando forem esperados.",
    ),
    AssistantSkill(
        "support_ops",
        "Suporte e Operação",
        "Tratar tickets, atendimento, filas e suporte ao cliente.",
        ("suporte", "ticket", "atendimento", "fila", "cliente"),
        "Priorize clareza operacional, status visível e ação do usuário/administrador.",
    ),
    AssistantSkill(
        "incident_response",
        "Incidente e Produção",
        "Responder a falhas em produção com triagem, contenção, correção e prevenção.",
        ("incidente", "produção caiu", "producao caiu", "fora do ar", "urgente", "rollback", "hotfix"),
        "Trate como produção: identifique impacto, contenha primeiro, preserve dados, proponha rollback seguro e deixe prevenção explícita.",
    ),
    AssistantSkill(
        "observability",
        "Observabilidade",
        "Melhorar logs, métricas, tracing e diagnósticos operacionais.",
        ("logs", "log", "metrica", "métrica", "monitoramento", "observabilidade", "trace", "telemetria"),
        "Adicione sinais acionáveis: IDs de correlação, eventos de erro, latência, contadores e mensagens úteis sem expor dados sensíveis.",
    ),
    AssistantSkill(
        "api_contract",
        "Contrato de API",
        "Projetar e preservar contratos entre frontend, backend e clientes externos.",
        ("contrato", "breaking change", "compatibilidade", "schema api", "openapi", "versao da api", "versão da api"),
        "Mantenha compatibilidade retroativa, documente campos, valide tipos e trate ausência de campos antigos com fallback.",
    ),
    AssistantSkill(
        "error_handling",
        "Tratamento de Erros",
        "Criar fluxos robustos de erro, fallback, retry e mensagens úteis.",
        ("erro", "fallback", "retry", "tentar novamente", "mensagem de erro", "falhou"),
        "Separe erro técnico de mensagem ao usuário, evite engolir exceções e retorne ação clara para recuperação.",
    ),
    AssistantSkill(
        "data_privacy",
        "Privacidade de Dados",
        "Proteger dados pessoais, tokens, documentos e conteúdo de usuário.",
        ("privacidade", "lgpd", "dados pessoais", "cpf", "cnpj", "email", "e-mail", "token"),
        "Minimize coleta, mascare dados sensíveis, evite persistir segredo no servidor e explique retenção quando necessário.",
    ),
    AssistantSkill(
        "secrets_management",
        "Gestão de Segredos",
        "Tratar chaves, .env, tokens de API e credenciais externas com segurança.",
        ("secret", "segredo", ".env", "api key", "apikey", "github_pat", "access token", "credencial"),
        "Nunca registre segredo em log, diff ou histórico. Prefira armazenamento efêmero/cliente quando o token for do usuário.",
    ),
    AssistantSkill(
        "dependency_upgrade",
        "Dependências e Atualizações",
        "Atualizar bibliotecas, lockfiles e compatibilidade de versões.",
        ("dependencia", "dependência", "upgrade", "atualizar pacote", "package", "pip", "npm", "vulneravel"),
        "Cheque changelog quando necessário, preserve lockfile, rode testes e destaque mudanças incompatíveis.",
    ),
    AssistantSkill(
        "release_management",
        "Release e Changelog",
        "Preparar versão, changelog, notas de release e critérios de pronto.",
        ("release", "changelog", "versao", "versão", "nota de release", "pronto para produção"),
        "Liste mudanças por impacto, migrações, riscos, testes feitos e passos de rollback.",
    ),
    AssistantSkill(
        "git_hygiene",
        "Higiene Git",
        "Organizar commits, branches, diffs, PRs e escopo de alterações.",
        ("git", "branch", "commit", "pr", "pull request", "diff", "merge"),
        "Mantenha commits coesos, não misture mudanças, cite validações e preserve alterações do usuário.",
    ),
    AssistantSkill(
        "product_requirements",
        "Requisitos de Produto",
        "Transformar pedido ambíguo em comportamento, estados e critérios de aceite.",
        ("requisito", "produto", "fluxo do usuario", "fluxo do usuário", "critério", "criterio", "regra de negócio"),
        "Converta desejo em escopo verificável: atores, estados, exceções, critérios de aceite e impacto no usuário.",
    ),
    AssistantSkill(
        "ux_workflows",
        "Fluxos UX",
        "Desenhar jornadas, estados vazios, carregamento, erro e sucesso.",
        ("jornada", "ux", "estado vazio", "loading", "carregando", "modal", "menu", "onboarding"),
        "Projete o caminho completo: entrada, progresso, sucesso, erro, cancelamento e retomada.",
    ),
    AssistantSkill(
        "state_management",
        "Estado Frontend",
        "Organizar estado de UI, cache local, sessão e sincronização.",
        ("estado", "state", "localstorage", "sessionstorage", "cache", "sincronizar", "sessão"),
        "Defina fonte da verdade, persistência mínima, invalidação e atualização visual previsível.",
    ),
    AssistantSkill(
        "mobile_responsive",
        "Responsivo Mobile",
        "Garantir funcionamento em desktop, tablet e celular.",
        ("mobile", "celular", "responsivo", "tablet", "viewport", "quebra no celular"),
        "Evite overflow, clipping e controles pequenos. Use layouts fluidos, toque confortável e conteúdo escaneável.",
    ),
    AssistantSkill(
        "browser_verification",
        "Verificação no Navegador",
        "Validar UI real com navegador, screenshots e fluxos clicáveis.",
        ("browser", "navegador", "screenshot", "clicar", "testar tela", "visual"),
        "Verifique fluxo real quando possível: abrir página, clicar controles, observar estados e corrigir layout quebrado.",
    ),
    AssistantSkill(
        "file_upload_safety",
        "Upload Seguro",
        "Projetar upload de ZIP, pasta, imagens e arquivos com limites e sanitização.",
        ("upload", "arquivo", "zip", "pasta", "anexo", "extrair", "compactado"),
        "Aplique limites de tamanho/quantidade, sanitize caminhos, ignore diretórios pesados e proteja contra path traversal.",
    ),
    AssistantSkill(
        "concurrency_consistency",
        "Concorrência e Consistência",
        "Evitar race conditions, escritas perdidas e estado concorrente incorreto.",
        ("concorrencia", "concorrência", "race", "simultaneo", "simultâneo", "travamento", "lock"),
        "Identifique escrita compartilhada, ordem de eventos, idempotência, locks e comportamento em retry.",
    ),
    AssistantSkill(
        "caching_strategy",
        "Cache e Invalidação",
        "Projetar cache, cache-busting, sessão e atualização de dados.",
        ("cache", "cache-busting", "stale", "atualizar tela", "recarregar", "versao css", "versão css"),
        "Defina quando cachear, quando invalidar, como evitar dado velho e como atualizar UI sem recarregar demais.",
    ),
    AssistantSkill(
        "data_import_export",
        "Importação e Exportação",
        "Criar fluxos de import/export para ZIP, CSV, JSON e backups.",
        ("importar", "exportar", "backup", "csv", "json", "baixar", "download"),
        "Preserve integridade, nomes seguros, compatibilidade de formato e mensagens claras em falhas parciais.",
    ),
    AssistantSkill(
        "ai_prompt_quality",
        "Qualidade de IA e Prompt",
        "Melhorar prompts internos, roteamento, instruções e qualidade das respostas.",
        ("prompt", "resposta da ia", "ia", "modelo", "skill", "roteamento", "alucina", "qualidade"),
        "Use instruções específicas, critérios de saída, fallback, avaliação de resposta e evite vazar prompts internos.",
    ),
    AssistantSkill(
        "senior_delivery",
        "Entrega Senior",
        "Fechar tarefas ponta a ponta com implementação, validação e comunicação objetiva.",
        ("profissional", "senior", "produção", "finalizar", "entregar", "completo", "ponta a ponta"),
        "Pense em blast radius, testes, UX, segurança, rollback, manutenção e explique só o essencial para a decisão.",
    ),
)


def select_skills(prompt: str, task_type: str = "", limit: int = 6) -> list[AssistantSkill]:
    normalized = _normalize(prompt)
    scored: list[tuple[int, int, AssistantSkill]] = []
    for index, skill in enumerate(SKILL_CATALOG):
        score = sum(1 for trigger in skill.triggers if _normalize(trigger) in normalized)
        if skill.id == "frontend_polish" and task_type == "frontend":
            score += 2
        if skill.id == "bug_fix" and task_type == "debugging":
            score += 2
        if skill.id == "test_runner" and task_type == "testing":
            score += 2
        if skill.id == "code_review" and task_type == "review":
            score += 2
        if skill.id == "architecture" and task_type == "architecture":
            score += 2
        if skill.id == "backend_api" and task_type in {"simple_code", "file_edit"}:
            score += 1
        if score:
            scored.append((score, -index, skill))
    scored.sort(reverse=True)
    selected = [skill for _, _, skill in scored[: max(1, limit)]]
    if selected:
        return selected
    return [next(skill for skill in SKILL_CATALOG if skill.id == "docs_copy")]


def render_skill_prompt(skills: Iterable[AssistantSkill]) -> str:
    lines = [
        "Automatic senior skill routing is active. Use the selected skill guidance silently to choose how to answer or act.",
        "Senior operating protocol:",
        "- Start from user impact, data flow, trust boundaries, and likely failure modes.",
        "- Prefer small reversible changes with clear ownership and low blast radius.",
        "- Define acceptance criteria before implementing or recommending a fix.",
        "- Validate with the most relevant tests, lint, browser checks, or manual reasoning available.",
        "- Call out residual risk only when it changes what the user should do next.",
        "Selected skills:",
    ]
    for skill in skills:
        lines.append(
            f"- {skill.title}: {skill.purpose} Senior guidance: {skill.guidance} "
            "Apply this with concrete files, commands, states, edge cases, and verification when relevant."
        )
    lines.append("If multiple skills apply, combine them naturally. Do not tell the user that hidden skills were selected.")
    return "\n".join(lines)


def _normalize(value: str) -> str:
    replacements = str.maketrans("áàãâéêíóôõúç", "aaaaeeiooouc")
    return " ".join(str(value or "").lower().translate(replacements).split())
