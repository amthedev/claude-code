let currentAccountId = localStorage.getItem(ClaudeApp.CLIENT_SESSION_KEY);
let activeConversation = [];
let activeRecognition = null;
let activeVoiceButton = null;
let activeSupportTicket = null;
let supportPollTimer = null;
let pendingAttachments = [];
let activeConversationId = null;
let activeChatSessionKey = `chat_${Date.now()}`;
let serverHistory = [];
let activeFloatingMenu = null;
let activeModelSelectId = "heroModel";
let incognitoMode = false;
let apiSecretsVisible = false;
let highlightedPlanId = "";
let sessionStatusMessage = "";
let pendingPlanId = "";
let pendingAuthIntent = "";

const CLIENT_API_TOKEN_SESSION_KEY = "claude_frontier_client_api_tokens";

function newChatSessionKey() {
  return `chat_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
}

const promptSuggestions = {
  code: {
    title: "Código",
    items: [
      "Desenhar wireframes de UI/UX",
      "Avalie minha abordagem para depuração de problemas",
      "Projetar sistemas de registro",
      "Projetar planos de escalabilidade",
      "Planejar um roadmap de desenvolvimento",
    ],
  },
  writing: {
    title: "Escrever",
    items: [
      "Escrever estudos de caso",
      "Desenvolver modelos de conteúdo",
      "Criar roteiros de apresentação",
      "Crie algo que seja lido diferentemente dependendo do humor do leitor",
      "Criar recursos de FAQ",
    ],
  },
  learning: {
    title: "Aprender",
    items: [
      "Criar resumos de estudo",
      "Criar cronogramas de aprendizado",
      "Desenvolva uma abordagem de aprendizado que abraça contradições interessantes",
      "Preparar para uma prova ou entrevista",
      "Desenvolver estruturas de aprendizagem",
    ],
  },
  personal: {
    title: "Assuntos pessoais",
    items: [
      "Organize minhas tarefas de hoje",
      "Planeje uma semana equilibrada",
      "Crie uma lista de compras por refeições",
      "Ajude a escrever uma mensagem difícil",
      "Monte um plano de viagem enxuto",
    ],
  },
  choice: {
    title: "Escolha do Claude",
    items: [
      "Escolha o melhor modelo para esta tarefa",
      "Transforme uma ideia solta em plano",
      "Faça perguntas para entender meu objetivo",
      "Compare abordagens e escolha uma",
      "Sugira o próximo passo mais útil",
    ],
  },
};

function account() {
  const current = ClaudeApp.accounts().find((item) => item.id === currentAccountId) || null;
  if (!current) return null;
  const apiToken = sessionApiTokenFor(current.id);
  return apiToken ? { ...current, apiToken } : current;
}

function sessionApiTokens() {
  try {
    return JSON.parse(sessionStorage.getItem(CLIENT_API_TOKEN_SESSION_KEY) || "{}");
  } catch {
    return {};
  }
}

function saveSessionApiTokens(tokens) {
  sessionStorage.setItem(CLIENT_API_TOKEN_SESSION_KEY, JSON.stringify(tokens));
}

function sessionApiTokenFor(accountId) {
  if (!accountId) return "";
  return sessionApiTokens()[accountId] || "";
}

function rememberSessionApiToken(accountId, apiToken) {
  if (!accountId || !apiToken) return;
  saveSessionApiTokens({ ...sessionApiTokens(), [accountId]: apiToken });
}

function forgetSessionApiToken(accountId) {
  const tokens = sessionApiTokens();
  if (accountId) delete tokens[accountId];
  saveSessionApiTokens(tokens);
}

function withoutApiToken(accountData) {
  const { apiToken, ...safeAccount } = accountData;
  return safeAccount;
}

function stripStoredAccountTokens() {
  const accounts = ClaudeApp.accounts();
  if (!accounts.some((item) => item.apiToken)) return;
  ClaudeApp.saveAccounts(accounts.map(withoutApiToken));
}

function repairDuplicatedToken(token) {
  let repaired = String(token || "");
  for (let pass = 0; pass < 3; pass += 1) {
    const next = repairDuplicatedTokenOnce(repaired);
    if (next === repaired) return repaired;
    repaired = next;
  }
  return repaired;
}

function repairDuplicatedTokenOnce(token) {
  if (token.length < 4) return token;
  const folded = token.toLocaleLowerCase("pt-BR");
  if (token.length % 2 === 0) {
    const half = token.length / 2;
    if (folded.slice(0, half) === folded.slice(half)) return token.slice(0, half);
  }
  for (let size = Math.min(4, Math.floor(token.length / 2)); size > 0; size -= 1) {
    const fragment = folded.slice(0, size);
    if (folded.startsWith(fragment + fragment)) return token.slice(size);
  }
  for (let size = Math.floor(token.length / 2); size > 1; size -= 1) {
    const stem = token.slice(0, -size);
    const suffix = token.slice(-size);
    if (stem.length < Math.max(4, size + 1)) continue;
    if (stem.toLocaleLowerCase("pt-BR").endsWith(suffix.toLocaleLowerCase("pt-BR"))) {
      return stem;
    }
  }
  if (
    token.length >= 6 &&
    folded.slice(-1) === folded.slice(-2, -1) &&
    ["a", "e", "i", "o", "u"].includes(folded.slice(-1))
  ) {
    return token.slice(0, -1);
  }
  return token;
}

function repairDuplicatedText(text) {
  const value = String(text || "");
  if (!value) return value;
  let current = value
    .replace(/(\*\*|__|\*|_)\s+\1/g, "$1")
    .replace(/\*(\d+[–-]\d+\s*min)\*\*/g, "$1");
  for (let pass = 0; pass < 4; pass += 1) {
    const previous = current;
    current = removeRestartedAnswer(current)
      .replace(/\b([\p{L}\p{N}]+)(\s+)\1\b/giu, "$1")
      .replace(/\b([\p{L}\p{N}]{1,8})(\s+)(\1[\p{L}\p{N}]{2,})\b/giu, "$3");
    if (current === previous) break;
  }
  const tokens = current.split(/(\s+)/);
  const repaired = [];
  for (let index = 0; index < tokens.length; index += 1) {
    const token = tokens[index];
    if (!token || /^\s+$/.test(token)) {
      repaired.push(token);
      continue;
    }
    const nextToken = tokens[index + 2];
    if (nextToken && token.toLowerCase() === nextToken.toLowerCase()) {
      repaired.push(repairDuplicatedToken(token));
      index += 2;
      continue;
    }
    const word = token.match(/^([^\p{L}\p{N}]*)([\p{L}\p{N}'’]+)([^\p{L}\p{N}]*)$/u);
    if (word) {
      repaired.push(`${word[1]}${repairDuplicatedToken(word[2])}${word[3]}`);
      continue;
    }
    repaired.push(repairDuplicatedToken(token));
  }
  return repairGluedPhrases(repaired.join(""));
}

function removeRestartedAnswer(text) {
  const value = String(text || "");
  const markers = ["Para aprender", "Paraprender", "Aprender inglês rápido"];
  for (const marker of markers) {
    const first = value.indexOf(marker);
    if (first < 0) continue;
    const second = value.indexOf(marker, first + marker.length);
    if (second < 0) continue;
    const prefix = value.slice(0, second);
    const suffix = value.slice(second);
    if (suffix.length < prefix.length * 0.5) return prefix.trimEnd();
    const tail = prefix.slice(-48).trimEnd();
    if (prefix.length > 180 && !/[.!?:\n]$/.test(tail)) return suffix;
    return prefix.trimEnd();
  }
  return value;
}

function repairGluedPhrases(text) {
  return String(text || "")
    .replace(/\bParaprender\b/gi, "Para aprender")
    .replace(/\bdentendimento\b/gi, "de entendimento")
    .replace(/\bfrasesobre\b/gi, "frases sobre")
    .replace(/\bfrasesimples\b/gi, "frases simples")
    .replace(/\bpalavrasem\b/gi, "palavras sem")
    .replace(/\bpalavrasoltas\b/gi, "palavras soltas")
    .replace(/\bsemanasem\b/gi, "semanas sem")
    .replace(/\bmetasemanais\b/gi, "metas semanais")
    .replace(/\bqueu\b/gi, "que eu")
    .replace(/\bquevita\b/gi, "que evita")
    .replace(/\bO\s+quev\b/gi, "O que evita")
    .replace(/\bConversasimples\b/g, "Conversa simples")
    .replace(/\bComprensão\b/g, "Compreensão")
    .replace(/\bIso\b/g, "Isso")
    .replace(/\bEscutativa\b/gi, "Escuta ativa")
    .replace(/\bcoffe\b/gi, "coffee")
    .replace(/\bBroklyn\b/gi, "Brooklyn")
    .replace(/\bSpeak\s+or\s+conversa\b/gi, "Speak ou converse")
    .replace(/\bConteúdo\s+seu\s+interesse\b/gi, "Conteúdo do seu interesse")
    .replace(/\bpensem\s+frases\b/gi, "pense em frases")
    .replace(/\bmedo\s+derrar\b/gi, "medo de errar")
    .replace(/\bPoso\b/gi, "Posso")
    .replace(/\b(\d+)hoje\s+nada\b/g, "$1h hoje e nada")
    .replace(/\bAprendas\s+\*?1\.0[–-]2\.0\b/g, "Aprenda as **1.000–2.000")
    .replace(/\bFoquem\s+frases\b/g, "Foque em frases");
}

function repairStoredDuplicateArtifacts() {
  const artifacts = ClaudeApp.artifacts();
  let changed = false;
  const repaired = artifacts.map((artifact) => {
    const title = repairDuplicatedText(artifact.title);
    const body = repairDuplicatedText(artifact.body);
    changed ||= title !== artifact.title || body !== artifact.body;
    return { ...artifact, title, body };
  });
  if (changed) ClaudeApp.saveArtifacts(repaired);
}

function openAuthModal(tab = "clientLoginForm") {
  document.querySelector("#authModal").classList.remove("hidden");
  setAuthTab(tab);
  document.querySelector("#clientLoginError").textContent = "";
  document.querySelector("#clientSignupMessage").textContent = "";
  updateAuthContext();
}

function closeAuthModal() {
  document.querySelector("#authModal").classList.add("hidden");
}

function setSidebarOpen(open) {
  document.querySelector("#clientApp").classList.toggle("sidebar-open", open);
  document.querySelector("#sidebarOpen").setAttribute("aria-expanded", String(open));
}

function openSidebar() {
  setSidebarOpen(true);
}

function closeSidebar() {
  setSidebarOpen(false);
}

function closeFloatingMenus() {
  document.querySelectorAll(".floating-menu, #accountMenu").forEach((menu) => {
    menu.classList.add("hidden");
  });
  activeFloatingMenu = null;
}

function positionMenu(menu, anchor, align = "left") {
  const rect = anchor.getBoundingClientRect();
  const width = menu.offsetWidth || 280;
  const height = menu.offsetHeight || 320;
  const left =
    align === "right"
      ? Math.max(10, Math.min(window.innerWidth - width - 10, rect.right - width))
      : Math.max(10, Math.min(window.innerWidth - width - 10, rect.left));
  const preferredTop = rect.bottom + 8;
  const top = Math.max(10, Math.min(window.innerHeight - height - 10, preferredTop));
  menu.style.left = `${left}px`;
  menu.style.top = `${top}px`;
}

function toggleFloatingMenu(menuId, anchor, align = "left") {
  const menu = document.querySelector(menuId);
  if (!menu) return;
  const wasOpen = !menu.classList.contains("hidden") && activeFloatingMenu === menu;
  closeFloatingMenus();
  if (wasOpen) return;
  menu.classList.remove("hidden");
  positionMenu(menu, anchor, align);
  activeFloatingMenu = menu;
}

function setAuthTab(tabId) {
  document.querySelectorAll("[data-auth-tab]").forEach((button) => {
    button.classList.toggle("active", button.dataset.authTab === tabId);
  });
  document.querySelectorAll(".auth-pane").forEach((pane) => {
    pane.classList.toggle("active", pane.id === tabId);
  });
  updateAuthContext();
}

function planById(planId) {
  return ClaudeApp.paidPlans().find((plan) => plan.id === planId) || null;
}

function defaultAuthCopy() {
  return {
    login: "Use o e-mail e a senha criados no cadastro.",
    signup: "Crie uma conta grátis agora. Se tiver gift card, ele libera o plano comprado.",
  };
}

function authIntentCopy() {
  const plan = planById(pendingPlanId);
  if (pendingAuthIntent === "plan" && plan) {
    return {
      login: `Entre para continuar a compra do plano ${plan.name}.`,
      signup: `Crie sua conta para comprar o plano ${plan.name}. Depois do cadastro, continuamos o pedido.`,
    };
  }
  if (pendingAuthIntent === "project") {
    return {
      login: "Entre para criar e salvar projetos.",
      signup: "Crie sua conta para guardar projetos e contexto de trabalho.",
    };
  }
  if (pendingAuthIntent === "artifact") {
    return {
      login: "Entre para criar e salvar artefatos.",
      signup: "Crie sua conta para transformar ideias em artefatos salvos.",
    };
  }
  if (pendingAuthIntent === "chat") {
    return {
      login: "Entre para enviar sua mensagem.",
      signup: "Crie sua conta para conversar e manter seu histórico.",
    };
  }
  return defaultAuthCopy();
}

function updateAuthContext() {
  const copy = authIntentCopy();
  const loginIntro = document.querySelector("#clientLoginIntro");
  const signupIntro = document.querySelector("#clientSignupIntro");
  if (loginIntro) loginIntro.textContent = copy.login;
  if (signupIntro) signupIntro.textContent = copy.signup;
}

function openAuthForIntent(intent, tab = "clientSignupForm", planId = "") {
  pendingAuthIntent = intent;
  pendingPlanId = planId || "";
  if (pendingPlanId) highlightedPlanId = pendingPlanId;
  openAuthModal(tab);
}

function fillModelSelects() {
  const settings = ClaudeApp.apiSettings();
  const current = account();
  const allowed = ClaudeApp.allowedPublicModelsForAccount(current);
  const selected = allowed.includes(settings.model) ? settings.model : allowed[0];
  if (selected !== settings.model) ClaudeApp.saveApiSettings({ ...settings, model: selected });
  document.querySelector("#heroModel").innerHTML = ClaudeApp.modelOptionsForAccount(current, selected);
  document.querySelector("#bottomModel").innerHTML = ClaudeApp.modelOptionsForAccount(current, selected);
  const apiModel = document.querySelector("#apiModel");
  if (apiModel) apiModel.innerHTML = ClaudeApp.modelOptionsForAccount(current, selected);
  updateModelButtons();
}

function modelLabel(value) {
  const option = Array.from(document.querySelectorAll("#heroModel option")).find(
    (item) => item.value === value,
  );
  return option?.textContent || "Sonnet 4.6";
}

function updateModelButtons() {
  document.querySelectorAll("[data-model-label]").forEach((label) => {
    const select = document.querySelector(`#${label.dataset.modelLabel}`);
    label.textContent = select ? modelLabel(select.value) : "Sonnet 4.6";
  });
}

function loadApiForm() {
  const settings = ClaudeApp.apiSettings();
  const current = account();
  const form = document.querySelector("#apiForm");
  form.elements.baseUrl.value = settings.baseUrl;
  form.elements.token.value = settings.token || current?.apiToken || "";
  form.elements.model.value = settings.model;
  document.querySelector("#heroModel").value = settings.model;
  document.querySelector("#bottomModel").value = settings.model;
  updateModelButtons();
  renderApiInstallGuide();
}

function activeApiToken() {
  return account()?.apiToken || ClaudeApp.apiSettings().token;
}

function shellQuote(value) {
  return `'${String(value).replaceAll("'", "'\"'\"'")}'`;
}

function openAiCompatBaseUrl(baseUrl) {
  const normalized = String(baseUrl || "").replace(/\/$/, "");
  return normalized.endsWith("/v1") ? normalized : `${normalized}/v1`;
}

function apiConfigForCurrentUser() {
  const settings = ClaudeApp.apiSettings();
  const current = account();
  const baseUrl = (settings.baseUrl || window.location.origin || "http://127.0.0.1:8787").replace(
    /\/$/,
    "",
  );
  const token = current?.apiToken || settings.token || "TOKEN_DA_SUA_CONTA";
  return {
    baseUrl,
    token,
    model: settings.model,
    plan: ClaudeApp.planDisplayName(current?.plan),
    hasAccount: Boolean(current?.apiToken),
  };
}

function maskSecret(value) {
  const text = String(value || "");
  if (!text || text === "TOKEN_DA_SUA_CONTA") return "TOKEN_DA_SUA_CONTA";
  if (text.length <= 12) return "••••••••";
  return `${text.slice(0, 6)}••••••••${text.slice(-4)}`;
}

function redactSecret(text, secret) {
  const value = String(secret || "");
  if (!value || value === "TOKEN_DA_SUA_CONTA") return text;
  return String(text).replaceAll(value, maskSecret(value));
}

function accountInitial(current) {
  const label = (current?.displayName || current?.name || current?.login || "").trim();
  return label ? label.charAt(0).toUpperCase() : "";
}

function pythonInstaller(config) {
  return `python3 - <<'PY'
from pathlib import Path
import json
import os
import subprocess
import sys
import textwrap

base_url = ${JSON.stringify(config.baseUrl)}
api_token = ${JSON.stringify(config.token)}
selected_model = ${JSON.stringify(config.model)}

def ask(question, default=True):
    suffix = " [S/n] " if default else " [s/N] "
    answer = input(question + suffix).strip().lower()
    if not answer:
        return default
    return answer in {"s", "sim", "y", "yes"}

print("Claude Code API")
print(f"API: {base_url}")
print(f"Modelo padrao: {selected_model}")
print()

if not ask("Quer configurar o Claude Code para usar esta API?", True):
    print("Cancelado. Nada foi alterado.")
    raise SystemExit(0)

claude_dir = Path.home() / ".claude"
claude_dir.mkdir(parents=True, exist_ok=True)
launcher_path = claude_dir / "claude_api_prompt.py"
settings_path = claude_dir / "settings.json"

launcher_code = f'''#!/usr/bin/env python3
import json
import os
import shutil
import subprocess
import sys

BASE_URL = {base_url!r}
API_TOKEN = {api_token!r}
SELECTED_MODEL = {selected_model!r}
GATEWAY_KEYS = {{
    "ANTHROPIC_BASE_URL": BASE_URL,
    "ANTHROPIC_AUTH_TOKEN": API_TOKEN,
    "ANTHROPIC_API_KEY": API_TOKEN,
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "claude-code-economy",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": SELECTED_MODEL,
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-code-ultra",
    "CLAUDE_CODE_SUBAGENT_MODEL": SELECTED_MODEL,
    "CLAUDE_CODE_ENABLE_AWAY_SUMMARY": "0",
    "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "16000",
}}

def wants_gateway():
    try:
        answer = input(f"Usar Claude Code API hospedada ({{BASE_URL}})? [S/n] ").strip().lower()
    except EOFError:
        answer = "s"
    return answer not in {{"n", "nao", "não", "no"}}

def clean_gateway_env(env):
    cleaned = dict(env)
    for key, value in GATEWAY_KEYS.items():
        if cleaned.get(key) == value:
            cleaned.pop(key, None)
    return cleaned

def main():
    claude_bin = shutil.which("claude")
    if not claude_bin:
        print("Claude Code nao foi encontrado no PATH.")
        print("Instale o Claude Code e rode este comando de novo.")
        return 127

    env = os.environ.copy()
    if wants_gateway():
        env.update(GATEWAY_KEYS)
        settings_arg = json.dumps({{"env": GATEWAY_KEYS}})
        print("OK, usando a API configurada.")
        return subprocess.call([
            claude_bin,
            "--settings",
            settings_arg,
            "--setting-sources",
            "local",
            *sys.argv[1:],
        ], env=env)
    else:
        env = clean_gateway_env(env)
        print("OK, abrindo Claude Code sem esta API.")
        return subprocess.call([claude_bin, *sys.argv[1:]], env=env)

if __name__ == "__main__":
    raise SystemExit(main())
'''

launcher_path.write_text(launcher_code)
launcher_path.chmod(0o755)

shell = Path(os.environ.get("SHELL", "")).name
profile = Path.home() / (".zshrc" if shell == "zsh" else ".bashrc")
start = "# assistente_api_claude_launcher"
end = "# /assistente_api_claude_launcher"
block = textwrap.dedent(f"""
{start}
claude() {{
  python3 "{launcher_path}" "$@"
}}
{end}
""").strip() + "\\n"

existing = profile.read_text() if profile.exists() else ""
if start in existing and end in existing:
    before = existing.split(start)[0].rstrip()
    after = existing.split(end, 1)[1].lstrip()
    profile.write_text(before + "\\n\\n" + block + "\\n" + after)
else:
    profile.write_text(existing.rstrip() + "\\n\\n" + block + "\\n")

if ask("Quer configurar tambem a extensao em ~/.claude/settings.json?", True):
    try:
        settings = json.loads(settings_path.read_text())
    except Exception:
        settings = {}
    env = settings.setdefault("env", {})
    env.update({
        "ANTHROPIC_BASE_URL": base_url,
        "ANTHROPIC_AUTH_TOKEN": api_token,
        "ANTHROPIC_API_KEY": api_token,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": "claude-code-economy",
        "ANTHROPIC_DEFAULT_SONNET_MODEL": selected_model,
        "ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-code-ultra",
        "CLAUDE_CODE_SUBAGENT_MODEL": selected_model,
        "CLAUDE_CODE_ENABLE_AWAY_SUMMARY": "0",
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "16000",
    })
    settings_path.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\\n")
    print(f"Extensao configurada em {settings_path}")

print()
print(f"Launcher instalado em {launcher_path}")
print(f"Atalho do terminal salvo em {profile}")
print("Abra um terminal novo ou rode:")
print(f"source {profile}")
print()
print("Depois rode: claude")
print("O terminal vai perguntar se quer usar esta API antes de abrir.")
PY`;
}

function terminalCommand(config) {
  const settings = {
    env: {
      ANTHROPIC_BASE_URL: config.baseUrl,
      ANTHROPIC_AUTH_TOKEN: config.token,
      ANTHROPIC_API_KEY: config.token,
      ANTHROPIC_DEFAULT_HAIKU_MODEL: "claude-code-economy",
      ANTHROPIC_DEFAULT_SONNET_MODEL: config.model,
      ANTHROPIC_DEFAULT_OPUS_MODEL: "claude-code-ultra",
      CLAUDE_CODE_SUBAGENT_MODEL: config.model,
      CLAUDE_CODE_ENABLE_AWAY_SUMMARY: "0",
      CLAUDE_CODE_MAX_OUTPUT_TOKENS: "16000",
    },
  };
  return `claude --settings ${shellQuote(JSON.stringify(settings))} --setting-sources local`;
}

function claudeSettingsCommand(config) {
  return `python3 - <<'PY'
from pathlib import Path
import json

settings_path = Path.home() / ".claude" / "settings.json"
settings_path.parent.mkdir(parents=True, exist_ok=True)

try:
    settings = json.loads(settings_path.read_text())
except Exception:
    settings = {}

env = settings.setdefault("env", {})
env.update({
    "ANTHROPIC_BASE_URL": ${JSON.stringify(config.baseUrl)},
    "ANTHROPIC_AUTH_TOKEN": ${JSON.stringify(config.token)},
    "ANTHROPIC_API_KEY": ${JSON.stringify(config.token)},
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "claude-code-economy",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": ${JSON.stringify(config.model)},
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-code-ultra",
    "CLAUDE_CODE_SUBAGENT_MODEL": ${JSON.stringify(config.model)},
    "CLAUDE_CODE_ENABLE_AWAY_SUMMARY": "0",
    "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "16000",
})

settings_path.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\\n")
print(f"Configurado em {settings_path}")
print("Feche e abra o Claude Code ou reinicie a extensao para carregar a configuracao.")
PY`;
}

function codexConfigToml(config) {
  return `model = ${JSON.stringify(config.model)}
model_provider = "claude_gateway"

[model_providers.claude_gateway]
name = "Claude Gateway"
base_url = ${JSON.stringify(openAiCompatBaseUrl(config.baseUrl))}
env_key = "OPENAI_API_KEY"
wire_api = "responses"`;
}

function openAiEnvCommand(config) {
  return `export OPENAI_API_KEY=${shellQuote(config.token)}`;
}

function renderCodeBlock(title, code, displayCode = code) {
  const escapedTitle = ClaudeApp.escapeHtml(title);
  const escapedCode = ClaudeApp.escapeHtml(code);
  const escapedDisplayCode = ClaudeApp.escapeHtml(displayCode);
  return `
    <article class="api-step">
      <div class="api-step-head">
        <strong>${escapedTitle}</strong>
        <button type="button" class="copy-icon-button" data-copy-value="${escapedCode}" aria-label="Copiar código">
          <svg viewBox="0 0 24 24"><path d="M8 4v12a2 2 0 0 0 2 2h8a2 2 0 0 0 2-2V7.5L16.5 4H8Z" /><path d="M16 4v4h4M4 12v6a2 2 0 0 0 2 2h6" /></svg>
        </button>
      </div>
      <textarea class="api-command-box" readonly spellcheck="false" aria-label="${escapedTitle}">${escapedDisplayCode}</textarea>
    </article>
  `;
}

function renderPrimaryCommand(code, displayCode = code) {
  const escapedCode = ClaudeApp.escapeHtml(code);
  const escapedDisplayCode = ClaudeApp.escapeHtml(displayCode);
  return `
    <section class="api-primary-command">
      <div class="api-primary-head">
        <div>
          <span class="overline">Terminal</span>
          <strong>Copie e cole para usar agora</strong>
        </div>
        <button type="button" class="copy-icon-button copy-primary" data-copy-value="${escapedCode}" aria-label="Copiar comando">
          <svg viewBox="0 0 24 24"><path d="M8 4v12a2 2 0 0 0 2 2h8a2 2 0 0 0 2-2V7.5L16.5 4H8Z" /><path d="M16 4v4h4M4 12v6a2 2 0 0 0 2 2h6" /></svg>
        </button>
      </div>
      <textarea class="api-command-box api-command-box-primary" readonly spellcheck="false" aria-label="Comando para terminal">${escapedDisplayCode}</textarea>
    </section>
  `;
}

function renderApiInstallGuide() {
  const guide = document.querySelector("#apiInstallGuide");
  if (!guide) return;
  const config = apiConfigForCurrentUser();
  const curlTest = `curl ${shellQuote(`${config.baseUrl}/v1/messages`)} \\
  -H ${shellQuote(`Anthropic-Auth-Token: ${config.token}`)} \\
  -H ${shellQuote("Content-Type: application/json")} \\
  -d ${shellQuote(
    JSON.stringify({
      model: config.model,
      max_tokens: 120,
      messages: [{ role: "user", content: "Responda apenas: API funcionando" }],
    }),
  )}`;
  const installCommand = pythonInstaller(config);
  const sessionCommand = terminalCommand(config);
  const settingsCommand = claudeSettingsCommand(config);
  const codexConfig = codexConfigToml(config);
  const openAiEnv = openAiEnvCommand(config);
  const openAiCurlTest = `curl ${shellQuote(`${openAiCompatBaseUrl(config.baseUrl)}/responses`)} \\
  -H ${shellQuote(`Authorization: Bearer ${config.token}`)} \\
  -H ${shellQuote("Content-Type: application/json")} \\
  -d ${shellQuote(
    JSON.stringify({
      model: config.model,
      max_output_tokens: 120,
      input: "Responda apenas: API funcionando",
    }),
  )}`;
  const loginHint = config.hasAccount
    ? "Configuracao pronta para esta conta."
    : "Entre em uma conta ativa para gerar a configuracao personalizada.";
  const displayToken = apiSecretsVisible ? config.token : maskSecret(config.token);
  const displayValue = (code) => (apiSecretsVisible ? code : redactSecret(code, config.token));
  const revealLabel = apiSecretsVisible ? "Ocultar token" : "Revelar token";

  guide.innerHTML = `
    <section class="api-summary">
      <div>
        <span class="overline">Acesso da conta</span>
        <strong>${ClaudeApp.escapeHtml(loginHint)}</strong>
      </div>
      <ol class="api-steps compact">
        <li>Copie o comando abaixo.</li>
        <li>Cole no terminal.</li>
        <li>Abra o Claude Code e use o modelo escolhido.</li>
      </ol>
      ${renderPrimaryCommand(sessionCommand, displayValue(sessionCommand))}
      <div class="api-kv">
        <code>URL da API: ${ClaudeApp.escapeHtml(config.baseUrl)}</code>
        <code>Plano: ${ClaudeApp.escapeHtml(config.plan)}</code>
        <code>API Key: ${ClaudeApp.escapeHtml(displayToken)}</code>
        <button type="button" class="secondary" data-copy-value="${ClaudeApp.escapeHtml(config.token)}">Copiar token</button>
        <button type="button" class="secondary" data-api-secret-toggle>${revealLabel}</button>
      </div>
    </section>
    <details class="api-advanced">
      <summary>Configurações avançadas</summary>
      ${renderCodeBlock("ChatGPT/Codex: ~/.codex/config.toml", codexConfig)}
      ${renderCodeBlock("ChatGPT/Codex: chave OpenAI-compatible", openAiEnv, displayValue(openAiEnv))}
      ${renderCodeBlock("ChatGPT/Codex: testar Responses API", openAiCurlTest, displayValue(openAiCurlTest))}
      ${renderCodeBlock("Instalador Python com pergunta", installCommand, displayValue(installCommand))}
      ${renderCodeBlock("Somente extensão: salvar settings.json", settingsCommand, displayValue(settingsCommand))}
      ${renderCodeBlock("Testar conexão", curlTest, displayValue(curlTest))}
    </details>
  `;
}

async function authRequest(path, payload) {
  const settings = ClaudeApp.apiSettings();
  const baseUrl = settings.baseUrl.replace(/\/$/, "");
  let response;
  try {
    response = await fetch(`${baseUrl}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  } catch (error) {
    error.fallback = true;
    throw error;
  }

  if (response.status === 404) {
    const error = new Error("Endpoint não encontrado.");
    error.fallback = true;
    throw error;
  }

  if (!response.ok) {
    let detail = `API respondeu ${response.status}`;
    try {
      const data = await response.json();
      detail = data.detail || detail;
    } catch {
      // Keep the status message.
    }
    throw new Error(detail);
  }

  return response.json();
}

async function supportRequest(path, options = {}) {
  const settings = ClaudeApp.apiSettings();
  const baseUrl = settings.baseUrl.replace(/\/$/, "");
  const response = await fetch(`${baseUrl}${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${activeApiToken()}`,
      ...(options.headers || {}),
    },
  });

  if (!response.ok) {
    let detail = `API respondeu ${response.status}`;
    try {
      const data = await response.json();
      detail = data.detail || detail;
    } catch {
      // Keep status text.
    }
    throw new Error(detail);
  }

  return response.json();
}

async function customerRequest(path, options = {}) {
  const settings = ClaudeApp.apiSettings();
  const current = account();
  const baseUrl = settings.baseUrl.replace(/\/$/, "");
  const response = await fetch(`${baseUrl}${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${activeApiToken()}`,
      ...(options.headers || {}),
    },
  });

  if (!response.ok) {
    let detail = `API respondeu ${response.status}`;
    try {
      const data = await response.json();
      detail = data.detail || detail;
    } catch {
      // Keep status text.
    }
    throw new Error(detail);
  }

  return response.json();
}

function saveServerAccount(accountData) {
  rememberSessionApiToken(accountData.id, accountData.apiToken);
  const safeAccount = withoutApiToken(accountData);
  const accounts = ClaudeApp.accounts();
  const index = accounts.findIndex(
    (item) => item.id === safeAccount.id || item.login === safeAccount.login,
  );
  if (index >= 0) {
    accounts[index] = withoutApiToken({ ...accounts[index], ...safeAccount });
  } else {
    accounts.push(safeAccount);
  }
  ClaudeApp.saveAccounts(accounts);
  return { ...safeAccount, apiToken: accountData.apiToken };
}

function syncCustomerApiToken(current) {
  if (!current?.apiToken) return;
  loadApiForm();
}

function modelKeyLabel(modelKey) {
  const key = ClaudeApp.normalizeModelKey(modelKey);
  if (key === "haiku") return "Haiku 4.5";
  if (key === "sonnet") return "Sonnet 4.6";
  return "Opus 4.7";
}

function isCurrentPaidPlan(current, plan) {
  if (!current || !plan) return false;
  const currentPrice = Number(current.price) || 0;
  const planPrice = Number(plan.price) || 0;
  if (currentPrice <= 0 || planPrice <= 0) return false;
  const currentPlan = ClaudeApp.planDisplayName(current.plan).toLowerCase();
  const planName = ClaudeApp.planDisplayName(plan.name).toLowerCase();
  const planId = String(plan.id || "").toLowerCase();
  return currentPlan === planName || currentPlan === planId || currentPrice === planPrice;
}

function renderPlanCards() {
  const current = account();
  const target = document.querySelector("#planCards");
  if (!target) return;
  const notice = document.querySelector("#planUpgradeNotice");
  if (notice) {
    notice.classList.toggle("hidden", !highlightedPlanId);
    notice.textContent =
      highlightedPlanId === "ultra"
        ? "Opus 4.7 está no plano Max 30X. Faça upgrade para liberar respostas mais fortes."
        : "";
  }
  target.innerHTML = ClaudeApp.paidPlans()
    .map((plan) => {
      const currentPlan = isCurrentPaidPlan(current, plan);
      const highlighted = highlightedPlanId === plan.id || (!highlightedPlanId && plan.id === "ultra");
      const badges = [
        currentPlan ? `<span class="plan-badge current">Seu plano atual</span>` : "",
        plan.id === "ultra" ? `<span class="plan-badge featured">Recomendado</span>` : "",
        highlightedPlanId === plan.id ? `<span class="plan-badge upgrade">Recomendado para Opus</span>` : "",
      ].join("");
      const buyLabel = !current
        ? `Entrar para comprar ${plan.name}`
        : currentPlan
          ? `Plano atual: ${plan.name}`
          : `Solicitar upgrade para ${plan.name}`;
      const focusAttribute = highlightedPlanId === plan.id ? `tabindex="-1"` : "";
      return `
        <article class="plan-card ${currentPlan ? "current" : ""} ${highlighted ? "highlighted" : ""}" data-plan-card="${ClaudeApp.escapeHtml(plan.id)}" ${focusAttribute}>
          <div>
            <div class="plan-badges">${badges}</div>
            <span class="overline">${modelKeyLabel(plan.modelKey)}</span>
            <h2>${ClaudeApp.escapeHtml(plan.name)}</h2>
            <p>${ClaudeApp.escapeHtml(plan.description)}</p>
            <p class="plan-model-note">${plan.id === "ultra" ? "Inclui Opus 4.7 para trabalhos mais pesados." : plan.id === "pro" ? "Inclui Sonnet 4.6 para trabalho diário." : "Inclui Haiku 4.5 para tarefas leves."}</p>
          </div>
          <strong>${ClaudeApp.brl.format(plan.price)}<small>/mês</small></strong>
          <span>${ClaudeApp.integer.format(plan.manualLimit)} tokens/dia</span>
          <button class="primary" type="button" data-buy-plan="${ClaudeApp.escapeHtml(plan.id)}" aria-label="${ClaudeApp.escapeHtml(buyLabel)}" ${currentPlan ? "disabled" : ""}>
            ${!current ? "Entre para comprar" : currentPlan ? "Plano atual" : "Solicitar upgrade"}
          </button>
        </article>
      `;
    })
    .join("");
}

function renderPurchases() {
  const purchases = ClaudeApp.purchases().filter((item) => !account() || item.accountId === account()?.id);
  const target = document.querySelector("#purchaseList");
  if (!target) return;
  if (!purchases.length) {
    target.innerHTML = `<div class="empty-workspace"><p>Nenhum pedido de plano nesta conta.</p></div>`;
    return;
  }
  target.innerHTML = purchases
    .map(
      (purchase) => `
        <article class="purchase-row">
          <div>
            <strong>${ClaudeApp.escapeHtml(purchase.plan)}</strong>
            <span>${new Date(purchase.createdAt).toLocaleString("pt-BR")}</span>
          </div>
          <span>${ClaudeApp.brl.format(purchase.price)}</span>
          <span class="badge ${purchase.status === "paid" ? "ok" : purchase.status === "canceled" ? "bad" : ""}">
            ${purchase.status === "paid" ? "Pago" : purchase.status === "canceled" ? "Cancelado" : "Pendente"}
          </span>
        </article>
      `,
    )
    .join("");
}

function renderBilling() {
  renderPlanCards();
  renderPurchases();
}

function focusHighlightedPlan() {
  if (!highlightedPlanId) return;
  window.requestAnimationFrame(() => {
    const card = Array.from(document.querySelectorAll("[data-plan-card]")).find(
      (item) => item.dataset.planCard === highlightedPlanId,
    );
    if (!card) return;
    card.scrollIntoView({ block: "center", behavior: "smooth" });
    card.focus({ preventScroll: true });
  });
}

function promptPaymentDocument() {
  const value = window.prompt("Digite o CPF ou CNPJ do pagador para continuar no Mercado Pago:");
  if (value === null) return null;
  const digits = String(value || "").replace(/\D+/g, "");
  if (![11, 14].includes(digits.length)) {
    showChatNotice("Informe um CPF com 11 dígitos ou CNPJ com 14 dígitos.");
    return null;
  }
  return digits;
}

async function loadPurchases() {
  if (!account()?.apiToken) return;
  try {
    const data = await customerRequest("/v1/billing/purchases");
    ClaudeApp.savePurchases(data.data || []);
    renderBilling();
  } catch {
    renderBilling();
  }
}

async function requestPlanPurchase(planId, button = null) {
  const plan = planById(planId);
  if (!plan) {
    showChatNotice("Plano não encontrado.");
    return;
  }
  const original = button?.textContent || "";
  if (button) {
    button.disabled = true;
    button.textContent = "Registrando...";
  } else {
    showChatNotice(`Criando pedido do plano ${plan.name}...`);
  }
  const payerDocument = promptPaymentDocument();
  if (!payerDocument) {
    if (button) {
      button.disabled = false;
      button.textContent = original;
    }
    return;
  }
  try {
    const data = await customerRequest("/v1/billing/purchases", {
      method: "POST",
      body: JSON.stringify({ planId, payerDocument }),
    });
    const purchases = ClaudeApp.purchases();
    purchases.unshift(data.purchase);
    ClaudeApp.savePurchases(purchases);
    pendingPlanId = "";
    pendingAuthIntent = "";
    renderBilling();
    if (data.purchase.checkoutUrl) {
      showChatNotice("Abrindo Mercado Pago...");
      window.location.href = data.purchase.checkoutUrl;
      return;
    }
    showChatNotice("Pedido criado. Configure Mercado Pago para liberar checkout automático.");
  } catch (error) {
    showChatNotice(error.message);
    if (button) {
      button.disabled = false;
      button.textContent = original;
    }
  }
}

async function continuePendingAuthFlow() {
  if (!pendingPlanId || !account()?.active) return;
  const planId = pendingPlanId;
  await requestPlanPurchase(planId);
}

function renderAccount() {
  const current = account();
  const authOpen = document.querySelector("#authOpen");
  const logout = document.querySelector("#clientLogout");
  const sidebarName = document.querySelector("#sidebarAccountName");
  const sidebarPlan = document.querySelector("#sidebarAccountPlan");
  const sidebarAvatar = document.querySelector("#sidebarAccountAvatar");

  if (!current || !current.active) {
    document.querySelector("#planBadge").textContent = "Entrar para usar";
    document.querySelector("#welcomeTitle").textContent = incognitoMode ? "Olá, seja quem for você" : "Como posso ajudar hoje?";
    document.querySelector("#accountMenuLogin").textContent = "Entre para usar";
    sidebarName.textContent = "Entrar";
    sidebarPlan.textContent = "Entre para usar";
    sidebarAvatar.textContent = "";
    sidebarAvatar.classList.add("empty");
    document.querySelector("#usageTitle").textContent = "Entre para usar o chat";
    document.querySelector("#usageText").textContent =
      "O envio exige uma conta ativa.";
    document.querySelector("#usageFill").style.width = "0%";
    document.querySelector("#accountDetails").innerHTML = `
      <div class="settings-login-cta">
        <strong>Entre para ver sua conta</strong>
        <p>${ClaudeApp.escapeHtml(sessionStatusMessage || "Depois do login, você vê plano, uso, API e configurações em um só lugar.")}</p>
        <button class="primary" type="button" data-auth-settings>Entrar ou cadastrar</button>
      </div>
    `;
    document.querySelector("#previewNotice").textContent =
      sessionStatusMessage || "Entre em uma conta ativa para usar o chat.";
    document.querySelector("#previewNotice").classList.remove("hidden");
    document.querySelectorAll(".auth-only").forEach((item) => item.classList.add("hidden"));
    authOpen.classList.remove("hidden");
    logout.classList.add("hidden");
    logout.textContent = "";
    document.querySelector("#openProjectModal").textContent = "Entrar para criar projeto";
    document.querySelector("#newArtifact").textContent = "Entrar para criar artefato";
    renderBilling();
    fillModelSelects();
    renderSidePanels();
    stopSupportPolling();
    return;
  }

  const preferredName = (current.displayName || current.name || "Você").trim();
  syncCustomerApiToken(current);
  document.querySelector("#planBadge").textContent = ClaudeApp.planDisplayName(current.plan);
  document.querySelector("#welcomeTitle").textContent = incognitoMode
    ? "Olá, seja quem for você"
    : `De volta ao trabalho, ${preferredName}?`;
  document.querySelector("#accountMenuLogin").textContent = current.login || preferredName;
  sidebarName.textContent = preferredName;
  sidebarPlan.textContent = ClaudeApp.planDisplayName(current.plan);
  sidebarAvatar.textContent = accountInitial(current);
  sidebarAvatar.classList.toggle("empty", !sidebarAvatar.textContent);
  document.querySelector("#usageTitle").textContent =
    `${ClaudeApp.integer.format(current.usedToday)} de ${ClaudeApp.integer.format(current.dailyLimit)} tokens`;
  document.querySelector("#usageText").textContent =
    `${ClaudeApp.integer.format(Math.max(0, current.dailyLimit - current.usedToday))} tokens restantes hoje.`;
  document.querySelector("#usageFill").style.width =
    `${Math.min(100, (current.usedToday / Math.max(1, current.dailyLimit)) * 100)}%`;
  document.querySelector("#accountDetails").innerHTML = `
    <code>Cliente: ${ClaudeApp.escapeHtml(current.name)}</code>
    <code>Nome no chat: ${ClaudeApp.escapeHtml(preferredName)}</code>
    <code>Login: ${ClaudeApp.escapeHtml(current.login)}</code>
    <code>Gift card: ${ClaudeApp.escapeHtml(current.giftCardCode || "-")}</code>
    <code>Plano: ${ClaudeApp.escapeHtml(ClaudeApp.planDisplayName(current.plan))}</code>
    <code>Modelo: escolha no seletor do chat</code>
    <code>Limite diario: ${ClaudeApp.integer.format(current.dailyLimit)} tokens</code>
  `;
  document.querySelector("#previewNotice").classList.add("hidden");
  document.querySelector("#previewNotice").textContent = "Entre em uma conta ativa para usar o chat.";
  document.querySelectorAll(".auth-only").forEach((item) => item.classList.remove("hidden"));
  authOpen.classList.add("hidden");
  logout.classList.remove("hidden");
  logout.textContent = accountInitial(current);
  document.querySelector("#openProjectModal").textContent = "Novo projeto";
  document.querySelector("#newArtifact").textContent = "Novo artefato";
  renderBilling();
  fillModelSelects();
  startSupportPolling();
}

function setPanel(panelId) {
  closeFloatingMenus();
  if (panelId !== "searchPanel") {
    document.querySelector("#searchInput")?.blur();
  }
  document.querySelectorAll(".client-panel").forEach((panel) => panel.classList.remove("active"));
  document.querySelector(`#${panelId}`)?.classList.add("active");
  document.querySelectorAll(".icon-rail [data-panel]").forEach((button) => {
    button.classList.toggle("active", button.dataset.panel === panelId);
  });
  document.querySelectorAll("[data-sidebar-panel]").forEach((button) => {
    button.classList.toggle("active", button.dataset.sidebarPanel === panelId);
  });
  document.querySelector("#sidebarNewChat").classList.toggle("active", panelId === "chatPanel");
  renderSidePanels();
  if (panelId === "supportPanel") refreshSupportTicket();
  if (panelId === "plansPanel") {
    renderBilling();
    focusHighlightedPlan();
  }
  if (panelId === "searchPanel") {
    searchLocal(document.querySelector("#searchInput")?.value || "");
    window.setTimeout(() => document.querySelector("#searchInput")?.focus(), 0);
  }
}

function renderInlineMarkdown(text) {
  return ClaudeApp.escapeHtml(text)
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/__([^_]+)__/g, "<strong>$1</strong>")
    .replace(/`([^`]+)`/g, "<code>$1</code>");
}

function renderMarkdownTable(lines) {
  const rawRows = lines
    .filter((line) => !/^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$/.test(line))
    .filter((line) => !/^\s*-{2,}\s*$/.test(line.trim()))
    .map((line) =>
      line
        .trim()
        .replace(/^\||\|$/g, "")
        .split(line.includes("|") ? "|" : "\t")
        .map((cell) => cell.trim())
        .filter((cell) => !/^:?-{2,}:?$/.test(cell))
        .map((cell) => renderInlineMarkdown(cell)),
    );
  const rows = [];
  const expectedColumns = rawRows[0]?.length || 0;
  rawRows.forEach((row, index) => {
    if (index > 0 && expectedColumns > 1 && row.length > expectedColumns) {
      for (let cellIndex = 0; cellIndex < row.length; cellIndex += expectedColumns) {
        rows.push(row.slice(cellIndex, cellIndex + expectedColumns));
      }
      return;
    }
    rows.push(row);
  });
  if (!rows.length) return "";
  const [head, ...body] = rows;
  return `
    <div class="message-table-wrap">
      <table>
        <thead><tr>${head.map((cell) => `<th>${cell}</th>`).join("")}</tr></thead>
        <tbody>${body.map((row) => `<tr>${row.map((cell) => `<td>${cell}</td>`).join("")}</tr>`).join("")}</tbody>
      </table>
    </div>
  `;
}

function renderAssistantMarkdown(text) {
  const cleaned = repairDuplicatedText(text);
  const lines = cleaned.split(/\r?\n/);
  const html = [];
  let listItems = [];
  let listType = "ul";
  let codeLines = [];
  let tableLines = [];
  let inCode = false;

  const flushList = () => {
    if (!listItems.length) return;
    html.push(`<${listType}>${listItems.map((item) => `<li>${renderInlineMarkdown(item)}</li>`).join("")}</${listType}>`);
    listItems = [];
    listType = "ul";
  };
  const flushTable = () => {
    if (!tableLines.length) return;
    html.push(renderMarkdownTable(tableLines));
    tableLines = [];
  };

  lines.forEach((line) => {
    if (line.trim().startsWith("```")) {
      if (inCode) {
        html.push(`<pre><code>${ClaudeApp.escapeHtml(codeLines.join("\n"))}</code></pre>`);
        codeLines = [];
        inCode = false;
      } else {
        flushList();
        flushTable();
        inCode = true;
      }
      return;
    }
    if (inCode) {
      codeLines.push(line);
      return;
    }
    if (/^\s*\|.+\|\s*$/.test(line) || line.includes("\t")) {
      flushList();
      tableLines.push(line);
      return;
    }
    flushTable();
    const trimmed = line.trim();
    if (!trimmed) {
      flushList();
      return;
    }
    if (/^---+$/.test(trimmed)) {
      flushList();
      html.push("<hr />");
      return;
    }
    const heading = trimmed.match(/^(#{1,6})\s+(.+)$/);
    if (heading) {
      flushList();
      const level = Math.min(4, Math.max(2, heading[1].length));
      html.push(`<h${level}>${renderInlineMarkdown(heading[2])}</h${level}>`);
      return;
    }
    const bullet = trimmed.match(/^[-*]\s+(.+)$/);
    if (bullet) {
      if (listItems.length && listType !== "ul") flushList();
      listType = "ul";
      listItems.push(bullet[1]);
      return;
    }
    const numbered = trimmed.match(/^\d+[.)]\s+(.+)$/);
    if (numbered) {
      if (listItems.length && listType !== "ol") flushList();
      listType = "ol";
      listItems.push(numbered[1]);
      return;
    }
    flushList();
    html.push(`<p>${renderInlineMarkdown(trimmed)}</p>`);
  });

  flushList();
  flushTable();
  if (inCode) html.push(`<pre><code>${ClaudeApp.escapeHtml(codeLines.join("\n"))}</code></pre>`);
  return html.join("");
}

function renderMessageNode(node, role, text) {
  if (role === "assistant") {
    const messageIndex = node.dataset.messageIndex || "";
    const isPending = !text || text === "Pensando...";
    node.innerHTML = `
      <div class="message-body">${renderAssistantMarkdown(text)}</div>
      <div class="message-actions ${isPending ? "hidden" : ""}">
        <button type="button" class="message-copy-button" data-copy-message-index="${ClaudeApp.escapeHtml(messageIndex)}" aria-label="Copiar resposta">
          Copiar
        </button>
      </div>
    `;
    return;
  }
  node.textContent = text;
}

function addMessage(role, text) {
  const index = activeConversation.push({ role, content: text }) - 1;
  const thread = document.querySelector("#chatThread");
  const node = document.createElement("div");
  node.className = `message ${role}`;
  node.dataset.messageIndex = String(index);
  node.dataset.chatSession = activeChatSessionKey;
  renderMessageNode(node, role, text);
  thread.appendChild(node);
  document.querySelector("#emptyState").classList.add("hidden");
  document.querySelector("#chatThread").classList.remove("hidden");
  document.querySelector("#bottomComposer").classList.remove("hidden");
  node.scrollIntoView({ block: "end" });
  return { index, node, sessionKey: activeChatSessionKey };
}

function updateMessage(message, text) {
  if (message.sessionKey !== activeChatSessionKey) return;
  if (!activeConversation[message.index]) return;
  const nextText = text || "Pensando...";
  activeConversation[message.index].content = nextText;
  renderMessageNode(message.node, activeConversation[message.index].role, nextText);
  message.node.scrollIntoView({ block: "end" });
}

async function conversationRequest(path, options = {}) {
  const settings = ClaudeApp.apiSettings();
  const baseUrl = settings.baseUrl.replace(/\/$/, "");
  const response = await fetch(`${baseUrl}${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${activeApiToken()}`,
      ...(options.headers || {}),
    },
  });

  if (!response.ok) {
    let detail = `API respondeu ${response.status}`;
    try {
      const data = await response.json();
      detail = data.detail || detail;
    } catch {
      // Keep the status message.
    }
    throw new Error(detail);
  }

  return response.json();
}

async function loadServerHistory() {
  if (!account()?.active) {
    serverHistory = [];
    renderSidePanels();
    return;
  }

  try {
    const data = await conversationRequest("/v1/conversations");
    serverHistory = (data.data || []).map((item) => ({
      ...item,
      title: repairDuplicatedText(item.title),
    }));
    renderSidePanels();
  } catch {
    serverHistory = [];
    renderSidePanels();
  }
}

function conversationTitle(messages) {
  const firstUser = messages.find((message) => message.role === "user")?.content || "";
  const cleaned = titleFromPrompt(firstUser);
  if (!cleaned) return "Nova conversa";
  return cleaned.length <= 54 ? cleaned : `${cleaned.slice(0, 54).trim()}...`;
}

function titleFromPrompt(prompt) {
  const cleaned = repairDuplicatedText(prompt)
    .replace(/\n+/g, " ")
    .split("Anexos:", 1)[0]
    .replace(/[?!.,;:]+$/g, "")
    .replace(/\s+/g, " ")
    .trim();
  if (!cleaned) return "";

  const lower = cleaned.toLocaleLowerCase("pt-BR");
  const patterns = [
    [/^(como|quero|queria|preciso)\s+(aprender|estudar|entender)\s+(.+)$/i, "Guia para $2 $3"],
    [/^(me\s+ensine|ensine)\s+(.+)$/i, "Guia de $2"],
    [/^(crie|criar|faça|faca|monte|desenvolva)\s+(.+)$/i, "Pedido: $2"],
    [/^(corrija|corrigir|arrume|conserte)\s+(.+)$/i, "Correção: $2"],
    [/^(planeje|planejar)\s+(.+)$/i, "Plano: $2"],
  ];
  for (const [pattern, replacement] of patterns) {
    if (pattern.test(cleaned)) return toTitleCase(cleaned.replace(pattern, replacement));
  }
  if (lower === cleaned || cleaned === cleaned.toUpperCase()) return toTitleCase(cleaned);
  return cleaned;
}

function toTitleCase(value) {
  const small = new Set(["a", "o", "os", "as", "um", "uma", "de", "do", "da", "dos", "das", "e", "em", "para", "por", "com"]);
  return String(value || "")
    .toLocaleLowerCase("pt-BR")
    .split(" ")
    .map((word, index) => {
      if (!word) return word;
      if (index > 0 && small.has(word)) return word;
      return word.charAt(0).toLocaleUpperCase("pt-BR") + word.slice(1);
    })
    .join(" ");
}

function upsertHistoryConversation(conversation) {
  if (!conversation?.id) return;
  const index = serverHistory.findIndex((item) => item.id === conversation.id);
  if (index >= 0) {
    serverHistory[index] = { ...serverHistory[index], ...conversation };
  } else {
    serverHistory.unshift(conversation);
  }
  serverHistory.sort(
    (left, right) =>
      new Date(right.updatedAt || right.createdAt).getTime() -
      new Date(left.updatedAt || left.createdAt).getTime(),
  );
  renderSidePanels();
}

function optimisticActiveConversation() {
  const now = new Date().toISOString();
  return {
    id: activeConversationId || `local_${Date.now()}`,
    title: conversationTitle(activeConversation),
    createdAt: now,
    updatedAt: now,
    messages: activeConversation.map((message) => ({
      ...message,
      content: repairDuplicatedText(message.content),
    })),
  };
}

async function saveConversationSnapshot(
  snapshot = activeConversation,
  conversationId = activeConversationId,
  updateActiveId = true,
  sessionKey = activeChatSessionKey,
) {
  if (incognitoMode || !snapshot.length || !account()?.active) return;

  const previousConversation = activeConversation;
  const previousConversationId = activeConversationId;
  activeConversation = snapshot;
  activeConversationId = conversationId;
  const optimistic = optimisticActiveConversation();
  activeConversation = previousConversation;
  activeConversationId = previousConversationId;
  upsertHistoryConversation(optimistic);
  showChatNotice("Salvando conversa...");
  try {
    const data = await conversationRequest("/v1/conversations", {
      method: "POST",
      body: JSON.stringify({
        id: optimistic.id.startsWith("local_") ? "" : optimistic.id,
        title: optimistic.title,
        messages: optimistic.messages,
      }),
    });
    if (
      updateActiveId &&
      sessionKey === activeChatSessionKey &&
      (conversationId === activeConversationId || !activeConversationId)
    ) {
      activeConversationId = data.conversation.id;
    }
    upsertHistoryConversation(data.conversation);
    showChatNotice("Conversa salva.");
    await loadServerHistory();
  } catch {
    showChatNotice("Não consegui salvar no banco agora. Mantive a conversa nesta tela.");
  }
}

async function saveConversation() {
  await saveConversationSnapshot();
}

function renderConversationMessages(messages) {
  activeConversation = [];
  const thread = document.querySelector("#chatThread");
  thread.innerHTML = "";

  messages.forEach((message) => {
    if (message.role === "user" || message.role === "assistant") {
      addMessage(message.role, repairDuplicatedText(message.content || ""));
    }
  });

  if (!activeConversation.length) {
    thread.classList.add("hidden");
    document.querySelector("#bottomComposer").classList.add("hidden");
    document.querySelector("#emptyState").classList.remove("hidden");
  }
}

async function openConversation(conversationId) {
  const cached = serverHistory.find((item) => item.id === conversationId);
  if (cached?.messages?.length) {
    activeConversationId = cached.id;
    activeChatSessionKey = newChatSessionKey();
    renderConversationMessages(cached.messages);
    setPanel("chatPanel");
    return;
  }

  try {
    const data = await conversationRequest(`/v1/conversations/${conversationId}`);
    activeConversationId = data.conversation.id;
    activeChatSessionKey = newChatSessionKey();
    renderConversationMessages(data.conversation.messages || []);
    setPanel("chatPanel");
  } catch (error) {
    showChatNotice(error.message);
  }
}

function parseGatewayStreamChunk(buffer, onText) {
  const events = buffer.split("\n\n");
  const remainder = events.pop() || "";

  events.forEach((eventText) => {
    const dataLines = eventText
      .split("\n")
      .filter((line) => line.startsWith("data: "))
      .map((line) => line.slice(6));
    if (!dataLines.length) return;

    const raw = dataLines.join("\n");
    if (raw === "[DONE]") return;

    try {
      const event = JSON.parse(raw);
      const delta = event.delta || {};
      if (typeof event.delta === "string" && event.delta) {
        onText(event.delta);
        return;
      }
      if (delta.type === "text_delta" && typeof delta.text === "string" && delta.text) {
        onText(delta.text);
        return;
      }
      if (typeof delta.text === "string" && delta.text) {
        onText(delta.text);
        return;
      }

      const choices = Array.isArray(event.choices) ? event.choices : [];
      choices.forEach((choice) => {
        const choiceDelta = choice?.delta || {};
        if (typeof choiceDelta.content === "string" && choiceDelta.content) {
          onText(choiceDelta.content);
        }
        if (Array.isArray(choiceDelta.content)) {
          choiceDelta.content.forEach((part) => {
            if (part?.type === "text" && typeof part.text === "string") onText(part.text);
          });
        }
      });
    } catch {
      // Ignore malformed stream events and keep reading the next chunk.
    }
  });

  return remainder;
}

function overlapLength(left, right) {
  const max = Math.min(left.length, right.length);
  for (let size = max; size > 0; size -= 1) {
    if (left.slice(-size) === right.slice(0, size)) return size;
  }
  return 0;
}

function mergeStreamText(current, incoming) {
  const text = String(incoming || "");
  if (!text) return current;
  if (!current) return text;
  if (text === current || current.endsWith(text)) return current;
  if (text.startsWith(current)) return text;

  const overlap = overlapLength(current, text);
  if (overlap > 0) return current + text.slice(overlap);

  const stripped = text.trimStart();
  if (stripped && stripped !== text) {
    if (current.endsWith(stripped)) return current;
    const strippedOverlap = overlapLength(current, stripped);
    if (strippedOverlap > 0) return current + stripped.slice(strippedOverlap);
  }

  return repairDuplicatedText(current + text);
}

function outputTokenLimitForAccount(current, estimatedInput) {
  const remaining = Math.max(0, Number(current?.dailyLimit || 0) - Number(current?.usedToday || 0));
  const availableOutput = remaining - estimatedInput;
  if (availableOutput <= 0) return 0;
  return Math.max(1, Math.min(1200, availableOutput));
}

async function callGateway(selectedModel, messages, onText, maxTokens = 1200) {
  const settings = ClaudeApp.apiSettings();
  const response = await fetch(`${settings.baseUrl.replace(/\/$/, "")}/v1/messages`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${activeApiToken()}`,
    },
    body: JSON.stringify({
      model: selectedModel,
      max_tokens: Math.max(1, Math.min(1200, Math.floor(Number(maxTokens) || 1))),
      stream: true,
      messages,
    }),
  });

  if (!response.ok) {
    let detail = "";
    try {
      const data = await response.json();
      detail = data?.detail ? `: ${typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail)}` : "";
    } catch {
      try {
        detail = `: ${await response.text()}`;
      } catch {
        detail = "";
      }
    }
    throw new Error(`API respondeu ${response.status}${detail}`);
  }

  if (!response.body) {
    const data = await response.json();
    return (data.content || []).map((part) => part.text || "").join("\n").trim() || "Sem resposta.";
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let answer = "";

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    buffer = parseGatewayStreamChunk(buffer, (text) => {
      answer = mergeStreamText(answer, text);
      onText(answer.trimStart());
    });
  }

  return repairDuplicatedText(answer).trim() || "Sem resposta.";
}

async function submitPrompt(prompt, selectedModel, attachments = []) {
  const sessionKey = activeChatSessionKey;
  const current = account();
  if (!current || !current.active) {
    openAuthForIntent("chat", "clientSignupForm");
    throw new Error("Entre com uma conta ativa para usar o chat.");
  }

  const estimatedInput = ClaudeApp.estimateTokens(prompt);
  const reservedOutput = outputTokenLimitForAccount(current, estimatedInput);
  const reservedTotal = estimatedInput + reservedOutput;
  const remaining = current.dailyLimit - current.usedToday;

  if (reservedOutput <= 0 || reservedTotal > remaining) {
    throw new Error(`Limite diário insuficiente. Restam ${ClaudeApp.integer.format(Math.max(0, remaining))} tokens.`);
  }

  const visiblePrompt = attachments.length ? `${prompt}\n\nAnexos: ${attachmentLabel(attachments)}` : prompt;
  addMessage("user", visiblePrompt);
  const outgoingMessages = activeConversation
    .filter((item) => item.role === "user" || item.role === "assistant")
    .map((item) => ({ role: item.role, content: item.content }));
  outgoingMessages[outgoingMessages.length - 1].content = buildMessageContent(prompt, attachments);
  const assistantMessage = addMessage("assistant", "Pensando...");

  let answer = "";
  answer = await callGateway(selectedModel, outgoingMessages, (partialAnswer) => {
    updateMessage(assistantMessage, partialAnswer);
  }, reservedOutput);
  updateMessage(assistantMessage, answer);

  const accounts = ClaudeApp.accounts();
  const index = accounts.findIndex((item) => item.id === currentAccountId);
  if (index >= 0) {
    accounts[index].usedToday += reservedTotal;
    ClaudeApp.saveAccounts(accounts);
  }

  if (!incognitoMode) {
    if (sessionKey !== activeChatSessionKey) return;
    const completedSnapshot = activeConversation.map((message) => ({ ...message }));
    await saveConversationSnapshot(completedSnapshot, activeConversationId, true, sessionKey);
    if (sessionKey === activeChatSessionKey) createArtifactIfUseful(prompt, answer);
  }
  renderAccount();
  renderSidePanels();
}

function createArtifactIfUseful(prompt, answer) {
  const text = `${prompt}\n${answer}`.toLowerCase();
  if (!text.includes("codigo") && !text.includes("html") && !text.includes("api")) return;

  const artifacts = ClaudeApp.artifacts();
  artifacts.unshift({
    id: `artifact_${Date.now()}`,
    title: prompt.slice(0, 60) || "Artefato",
    body: answer,
    createdAt: new Date().toISOString(),
  });
  ClaudeApp.saveArtifacts(artifacts.slice(0, 30));
}

function historyDate(item) {
  return new Date(item.updatedAt || item.createdAt).toLocaleString("pt-BR", {
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function historyItemMarkup(item) {
  return `
    <button class="table-row history-item" type="button" data-conversation-id="${ClaudeApp.escapeHtml(item.id)}">
      <span>
        <strong>${ClaudeApp.escapeHtml(item.title)}</strong>
        <span>${historyDate(item)}</span>
      </span>
    </button>
  `;
}

function commandItemMarkup(item) {
  return `
    <button class="command-option" type="button" data-conversation-id="${ClaudeApp.escapeHtml(item.id)}">
      <span class="command-type">Conversa</span>
      <strong>${ClaudeApp.escapeHtml(item.title)}</strong>
      <span>${historyDate(item)}</span>
    </button>
  `;
}

function sidebarRecentMarkup(item) {
  return `
    <button class="sidebar-recent-item" type="button" data-conversation-id="${ClaudeApp.escapeHtml(item.id)}">
      ${ClaudeApp.escapeHtml(item.title)}
    </button>
  `;
}

function renderSidePanels() {
  if (!account()?.active) {
    document.querySelector("#historyList").innerHTML =
      `<div class="empty-workspace"><p>Entre para ver suas conversas.</p></div>`;
    document.querySelector("#sidebarRecentList").innerHTML =
      `<div class="sidebar-empty">Entre para ver suas conversas.</div>`;
    document.querySelector("#projectList").innerHTML =
      `<div class="empty-workspace"><p>Entre para ver seus projetos.</p></div>`;
    document.querySelector("#artifactList").innerHTML =
      `<div class="empty-workspace"><p>Entre para ver seus artefatos.</p></div>`;
    return;
  }

  const historyQuery = (document.querySelector("#historySearch")?.value || "").trim().toLowerCase();
  const visibleHistory = historyQuery
    ? serverHistory.filter((item) => JSON.stringify(item).toLowerCase().includes(historyQuery))
    : serverHistory;
  document.querySelector("#historyList").innerHTML = visibleHistory.length
    ? visibleHistory
        .map((item) => historyItemMarkup(item))
        .join("")
    : `<div class="empty-workspace"><p>${historyQuery ? "Nenhuma conversa encontrada." : "Nenhuma conversa salva no banco ainda."}</p></div>`;
  document.querySelector("#sidebarRecentList").innerHTML = serverHistory.length
    ? serverHistory
        .slice(0, 8)
        .map((item) => sidebarRecentMarkup(item))
        .join("")
    : `<div class="sidebar-empty">Nenhuma conversa recente.</div>`;

  const projectQuery = (document.querySelector("#projectSearch")?.value || "").trim().toLowerCase();
  const projects = ClaudeApp.projects();
  const visibleProjects = projectQuery
    ? projects.filter((item) => `${item.name} ${item.context}`.toLowerCase().includes(projectQuery))
    : projects;
  document.querySelector("#projectList").innerHTML = visibleProjects.length
    ? visibleProjects
        .map(
          (item) => `
            <article class="project-card">
              <div>
                <strong>${ClaudeApp.escapeHtml(item.name)}</strong>
                <p>${ClaudeApp.escapeHtml(item.context || "Sem contexto adicional.")}</p>
                <span class="badge">Projeto local</span>
              </div>
            </article>
          `,
        )
        .join("")
    : `<div class="empty-workspace"><p>${projectQuery ? "Nenhum projeto encontrado." : "Nenhum projeto local. Crie um projeto para guardar contexto e instruções."}</p></div>`;

  const artifactQuery = (document.querySelector("#artifactSearch")?.value || "").trim().toLowerCase();
  const artifacts = ClaudeApp.artifacts();
  const visibleArtifacts = artifactQuery
    ? artifacts.filter((item) => `${item.title} ${item.body}`.toLowerCase().includes(artifactQuery))
    : artifacts;
  document.querySelector("#artifactList").innerHTML = visibleArtifacts.length
    ? visibleArtifacts
        .map(
          (item) => `
            <article class="artifact-card">
              <strong>${ClaudeApp.escapeHtml(item.title)}</strong>
              <p>${ClaudeApp.escapeHtml(item.body.slice(0, 180))}</p>
            </article>
          `,
        )
        .join("")
    : `<div class="empty-workspace"><strong>${artifactQuery ? "Nenhum artefato encontrado." : "O que você vai construir com artefatos?"}</strong><p>${artifactQuery ? "Tente outra busca." : "Transforme apps, jogos, templates e ferramentas de ideias em realidade."}</p></div>`;
}

function searchLocal(query) {
  if (!account()?.active) {
    document.querySelector("#searchResults").innerHTML =
      `<div class="command-option"><p>Entre para pesquisar suas conversas, projetos e artefatos.</p></div>`;
    return;
  }
  const q = query.trim().toLowerCase();
  const conversationResults = q
    ? serverHistory.filter((item) => JSON.stringify(item).toLowerCase().includes(q))
    : serverHistory;
  const projects = q
    ? ClaudeApp.projects().filter((item) => `${item.name} ${item.context}`.toLowerCase().includes(q))
    : [];
  const artifacts = q
    ? ClaudeApp.artifacts().filter((item) => `${item.title} ${item.body}`.toLowerCase().includes(q))
    : [];
  document.querySelector("#searchResults").innerHTML =
    conversationResults.length || projects.length || artifacts.length
      ? [
          ...conversationResults.map((item) => commandItemMarkup(item)),
          ...projects.map(
            (item) => `
              <button class="command-option" type="button" data-open-panel="projectsPanel">
                <span class="command-type">Projeto</span>
                <strong>${ClaudeApp.escapeHtml(item.name)}</strong>
                <span>Projeto local</span>
              </button>
            `,
          ),
          ...artifacts.map(
            (item) => `
              <button class="command-option" type="button" data-open-panel="artifactsPanel">
                <span class="command-type">Artefato</span>
                <strong>${ClaudeApp.escapeHtml(item.title)}</strong>
                <span>Artefato local</span>
              </button>
            `,
          ),
        ].join("")
      : `<div class="command-option"><p>Nenhum resultado.</p></div>`;
}

function supportStatusText(ticket) {
  if (!ticket) return "Nenhum atendimento aberto.";
  if (ticket.status === "waiting") return "Na fila. O suporte vai assumir assim que finalizar o atendimento atual.";
  if (ticket.status === "active") return "Em atendimento agora.";
  return "Atendimento finalizado.";
}

function renderSupport() {
  const status = document.querySelector("#supportStatus");
  const list = document.querySelector("#supportMessages");
  if (!status || !list) return;
  status.textContent = supportStatusText(activeSupportTicket);
  if (!activeSupportTicket?.messages?.length) {
    list.innerHTML = `<div class="support-empty">Envie uma mensagem para entrar na fila.</div>`;
    return;
  }

  list.innerHTML = activeSupportTicket.messages
    .map(
      (message) => `
        <div class="support-message ${message.sender}">
          <strong>${ClaudeApp.escapeHtml(message.author)}</strong>
          <p>${ClaudeApp.escapeHtml(message.body)}</p>
          <span>${new Date(message.createdAt).toLocaleTimeString("pt-BR", {
            hour: "2-digit",
            minute: "2-digit",
          })}</span>
        </div>
      `,
    )
    .join("");
  list.scrollTop = list.scrollHeight;
}

async function refreshSupportTicket() {
  if (!account()?.active) return;
  try {
    const data = await supportRequest("/v1/support/tickets/current");
    activeSupportTicket = data.ticket;
    renderSupport();
  } catch {
    // Keep the last visible state; chat errors are shown when the user sends.
  }
}

function startSupportPolling() {
  if (supportPollTimer || !account()?.active) return;
  refreshSupportTicket();
  supportPollTimer = window.setInterval(refreshSupportTicket, 2200);
}

function stopSupportPolling() {
  if (supportPollTimer) window.clearInterval(supportPollTimer);
  supportPollTimer = null;
  activeSupportTicket = null;
  renderSupport();
}

function showChatNotice(message) {
  const error = document.querySelector("#chatError");
  error.textContent = message;
  window.setTimeout(() => {
    if (error.textContent === message) error.textContent = "";
  }, 2600);
}

function fileToDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.addEventListener("load", () => resolve(String(reader.result || "")));
    reader.addEventListener("error", () => reject(new Error(`Não consegui ler ${file.name}.`)));
    reader.readAsDataURL(file);
  });
}

function fileToText(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.addEventListener("load", () => resolve(String(reader.result || "")));
    reader.addEventListener("error", () => reject(new Error(`Não consegui ler ${file.name}.`)));
    reader.readAsText(file);
  });
}

async function readAttachment(file) {
  if (file.size > 4 * 1024 * 1024) {
    throw new Error(`${file.name} é maior que 4 MB.`);
  }

  if (file.type.startsWith("image/")) {
    const dataUrl = await fileToDataUrl(file);
    const data = dataUrl.split(",", 2)[1] || "";
    return {
      name: file.name,
      kind: "image",
      mediaType: file.type || "image/png",
      data,
    };
  }

  const text = await fileToText(file);
  return {
    name: file.name,
    kind: "text",
    text: text.slice(0, 20000),
    truncated: text.length > 20000,
  };
}

function attachmentLabel(attachments = pendingAttachments) {
  if (!attachments.length) return "";
  return attachments.map((file) => file.name).join(", ");
}

function buildMessageContent(prompt, attachments) {
  if (!attachments.length) return prompt;

  const content = [{ type: "text", text: prompt }];
  attachments.forEach((attachment) => {
    if (attachment.kind === "image") {
      content.push({
        type: "image",
        source: {
          type: "base64",
          media_type: attachment.mediaType,
          data: attachment.data,
        },
      });
      return;
    }

    content.push({
      type: "text",
      text: [
        `Arquivo anexado: ${attachment.name}`,
        attachment.truncated ? "Conteúdo truncado nos primeiros 20000 caracteres." : "",
        "```",
        attachment.text,
        "```",
      ]
        .filter(Boolean)
        .join("\n"),
    });
  });

  return content;
}

function triggerAttachmentPicker() {
  const input = document.querySelector("#attachmentInput");
  if (!input) return;
  input.value = "";
  input.click();
}

function textareaForFormId(formId) {
  return document.querySelector(`#${formId} textarea`);
}

function updateComposerDraftState(textarea) {
  const form = textarea.closest("form");
  form?.classList.toggle("has-draft", Boolean(textarea.value.trim()));
}

function fillHeroPrompt(text) {
  const textarea = textareaForFormId("heroComposer");
  if (!textarea) return;
  textarea.value = text;
  textarea.focus();
  updateComposerDraftState(textarea);
}

function renderSuggestionPanel(category) {
  const panel = document.querySelector("#suggestionPanel");
  const config = promptSuggestions[category];
  if (!panel || !config) return;
  panel.innerHTML = `
    <div class="suggestion-head">
      <span>${ClaudeApp.escapeHtml(config.title)}</span>
      <button type="button" data-close-suggestions aria-label="Fechar sugestões">×</button>
    </div>
    <div class="suggestion-list">
      ${config.items
        .map(
          (item) => `
            <button type="button" data-suggestion-prompt="${ClaudeApp.escapeHtml(item)}">
              ${ClaudeApp.escapeHtml(item)}
            </button>
          `,
        )
        .join("")}
    </div>
  `;
  panel.classList.remove("hidden");
}

function closeSuggestionPanel() {
  const panel = document.querySelector("#suggestionPanel");
  if (panel) panel.classList.add("hidden");
}

function setIncognitoMode(enabled) {
  if (enabled && (activeConversation.length || activeConversationId)) {
    const previousConversation = activeConversation.map((message) => ({ ...message }));
    const previousConversationId = activeConversationId;
    clearActiveChat({ savePrevious: false });
    saveConversationSnapshot(previousConversation, previousConversationId, false);
  }
  incognitoMode = enabled;
  if (enabled) {
    closeSidebar();
    setPanel("chatPanel");
  }
  document.querySelector("#clientApp").classList.toggle("incognito-mode", enabled);
  document.querySelector("#incognitoNotice").classList.toggle("hidden", !enabled);
  document.querySelector("#incognitoToggle").classList.toggle("active", enabled);
  document.querySelector("#incognitoToggle").setAttribute(
    "aria-label",
    enabled ? "Sair do modo anônimo" : "Usar incógnito",
  );
  renderAccount();
}

function submitComposerOnEnter(event) {
  if (event.key !== "Enter" || event.shiftKey) return;
  event.preventDefault();
  event.currentTarget.closest("form")?.requestSubmit();
}

function speechRecognitionConstructor() {
  return window.SpeechRecognition || window.webkitSpeechRecognition || null;
}

function resetDictationUi() {
  if (activeVoiceButton) {
    activeVoiceButton.classList.remove("recording");
    activeVoiceButton.setAttribute("aria-label", "Gravar áudio");
  }
  activeRecognition = null;
  activeVoiceButton = null;
}

function stopDictation() {
  if (activeRecognition) {
    activeRecognition.stop();
  }
  resetDictationUi();
}

function startDictation(button) {
  const SpeechRecognition = speechRecognitionConstructor();
  if (!SpeechRecognition) {
    showChatNotice("Seu navegador não oferece reconhecimento de fala. Use Chrome ou Edge.");
    return;
  }

  if (activeVoiceButton === button) {
    stopDictation();
    return;
  }

  stopDictation();

  const form = button.closest("form");
  const textarea = form?.querySelector("textarea");
  if (!textarea) return;

  const recognition = new SpeechRecognition();
  const initialText = textarea.value.trim();
  recognition.lang = "pt-BR";
  recognition.continuous = true;
  recognition.interimResults = true;
  recognition.maxAlternatives = 1;

  activeRecognition = recognition;
  activeVoiceButton = button;
  button.classList.add("recording");
  button.setAttribute("aria-label", "Parar gravação");
  showChatNotice("Ouvindo... fale sua mensagem.");

  recognition.addEventListener("result", (event) => {
    let finalText = "";
    let interimText = "";
    for (let index = 0; index < event.results.length; index += 1) {
      const transcript = event.results[index][0].transcript.trim();
      if (event.results[index].isFinal) {
        finalText += `${transcript} `;
      } else {
        interimText += `${transcript} `;
      }
    }

    const base = initialText ? `${initialText} ` : "";
    textarea.value = `${base}${finalText}${interimText}`.trimStart();
    textarea.focus();
  });

  recognition.addEventListener("error", (event) => {
    const message =
      event.error === "not-allowed"
        ? "Permita o microfone no navegador para usar áudio."
        : "Não consegui capturar o áudio. Tente novamente.";
    showChatNotice(message);
    stopDictation();
  });

  recognition.addEventListener("end", () => {
    if (activeRecognition === recognition) {
      resetDictationUi();
    }
  });

  try {
    recognition.start();
  } catch {
    showChatNotice("A gravação já está ativa. Aguarde um instante.");
  }
}

function clearActiveChat({ savePrevious = true } = {}) {
  const previousConversation = activeConversation.map((message) => ({ ...message }));
  const previousConversationId = activeConversationId;
  const previousSessionKey = activeChatSessionKey;
  activeConversationId = null;
  activeChatSessionKey = newChatSessionKey();
  activeConversation = [];
  pendingAttachments = [];
  closeSuggestionPanel();
  document.querySelector("#chatThread").innerHTML = "";
  document.querySelector("#chatThread").classList.add("hidden");
  document.querySelector("#bottomComposer").classList.add("hidden");
  document.querySelector("#emptyState").classList.remove("hidden");
  document.querySelector("#heroComposer").reset();
  document.querySelector("#bottomComposer").reset();
  document.querySelector("#heroComposer").classList.remove("has-draft");
  document.querySelector("#bottomComposer").classList.remove("has-draft");
  setPanel("chatPanel");
  renderSidePanels();
  if (savePrevious && previousConversation.length) {
    saveConversationSnapshot(previousConversation, previousConversationId, false, previousSessionKey);
  }
}

function resetChat() {
  clearActiveChat();
}

function setFormMessage(target, message, field = null) {
  target.textContent = message;
  if (field) window.setTimeout(() => field.focus(), 0);
}

function validateLoginForm(form, target) {
  const login = form.elements.login;
  const password = form.elements.password;
  if (!login.value.trim()) {
    setFormMessage(target, "Informe seu e-mail.", login);
    return false;
  }
  if (!login.validity.valid) {
    setFormMessage(target, "Digite um e-mail válido.", login);
    return false;
  }
  if (!password.value.trim()) {
    setFormMessage(target, "Informe sua senha.", password);
    return false;
  }
  return true;
}

function validateSignupForm(form, target) {
  const name = form.elements.name;
  const login = form.elements.login;
  const password = form.elements.password;
  if (!name.value.trim()) {
    setFormMessage(target, "Informe seu nome.", name);
    return false;
  }
  if (!login.value.trim()) {
    setFormMessage(target, "Informe seu e-mail.", login);
    return false;
  }
  if (!login.validity.valid) {
    setFormMessage(target, "Digite um e-mail válido.", login);
    return false;
  }
  if (!password.value.trim()) {
    setFormMessage(target, "Crie uma senha.", password);
    return false;
  }
  return true;
}

document.querySelector("#clientLoginForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const errorTarget = document.querySelector("#clientLoginError");
  errorTarget.textContent = "";
  if (!validateLoginForm(form, errorTarget)) return;
  const values = Object.fromEntries(new FormData(event.currentTarget).entries());
  let found = null;
  try {
    const data = await authRequest("/v1/auth/login", values);
    found = saveServerAccount(data.account);
  } catch (error) {
    errorTarget.textContent =
      error.fallback
        ? "API indisponível para validar login."
        : /401|403|invalid|inválid|senha|login/i.test(error.message)
          ? "E-mail ou senha inválidos."
          : error.message;
    return;
  }

  if (!found) {
    errorTarget.textContent = "E-mail ou senha inválidos.";
    return;
  }

  if (!found.active) {
    errorTarget.textContent =
      "Conta pausada. Fale com o suporte para reativar.";
    return;
  }

  currentAccountId = found.id;
  sessionStatusMessage = "";
  localStorage.setItem(ClaudeApp.CLIENT_SESSION_KEY, found.id);
  closeAuthModal();
  renderAccount();
  loadServerHistory();
  loadPurchases();
  continuePendingAuthFlow();
});

document.querySelector("#clientSignupForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const message = document.querySelector("#clientSignupMessage");
  message.textContent = "";
  if (!validateSignupForm(form, message)) return;
  const values = Object.fromEntries(new FormData(form).entries());
  const accounts = ClaudeApp.accounts();
  const login = values.login.trim();
  const exists = accounts.some((item) => item.login.toLowerCase() === login.toLowerCase());
  if (exists) {
    setFormMessage(message, "Esse e-mail já tem conta. Tente entrar.", form.elements.login);
    return;
  }

  try {
    const data = await authRequest("/v1/auth/signup", values);
    const account = saveServerAccount(data.account);
    currentAccountId = account.id;
    sessionStatusMessage = "";
    localStorage.setItem(ClaudeApp.CLIENT_SESSION_KEY, account.id);
    form.reset();
    closeAuthModal();
    renderAccount();
    loadServerHistory();
    loadPurchases();
    continuePendingAuthFlow();
    return;
  } catch (error) {
    message.textContent = error.fallback ? "API indisponível para criar conta." : error.message;
    return;
  }
});

document.querySelectorAll("[data-panel]").forEach((button) => {
  button.addEventListener("click", () => setPanel(button.dataset.panel));
});

document.querySelector("#sidebarOpen").addEventListener("click", openSidebar);
document.querySelector("#sidebarClose").addEventListener("click", closeSidebar);
document.querySelector("#sidebarNewChat").addEventListener("click", resetChat);
document.querySelectorAll("[data-sidebar-panel]").forEach((button) => {
  button.addEventListener("click", () => setPanel(button.dataset.sidebarPanel));
});

document.querySelector("#railNewChat").addEventListener("click", resetChat);

document.querySelector("#historyList").addEventListener("click", (event) => {
  const item = event.target.closest("[data-conversation-id]");
  if (item) openConversation(item.dataset.conversationId);
});

document.querySelector("#sidebarRecentList").addEventListener("click", (event) => {
  const item = event.target.closest("[data-conversation-id]");
  if (item) openConversation(item.dataset.conversationId);
});

document.querySelector("#searchResults").addEventListener("click", (event) => {
  const item = event.target.closest("[data-conversation-id]");
  if (item) {
    openConversation(item.dataset.conversationId);
    return;
  }
  const panel = event.target.closest("[data-open-panel]");
  if (panel) setPanel(panel.dataset.openPanel);
});

document.querySelectorAll(".voice-button").forEach((button) => {
  button.addEventListener("click", () => {
    startDictation(button);
  });
});

document.querySelectorAll(".attach-button").forEach((button) => {
  button.addEventListener("click", (event) => {
    event.stopPropagation();
    event.preventDefault();
    toggleFloatingMenu("#attachMenu", button);
  });
});

document.querySelector("#attachMenu").addEventListener("click", (event) => {
  event.stopPropagation();
  const button = event.target.closest("[data-attach-action]");
  if (!button) return;
  const action = button.dataset.attachAction;
  closeFloatingMenus();
  if (action === "files") {
    triggerAttachmentPicker();
    showChatNotice("Escolha arquivos ou fotos para anexar.");
    return;
  }
  if (action === "project") {
    setPanel("projectsPanel");
    return;
  }
});

document.querySelectorAll("[data-model-trigger]").forEach((button) => {
  button.addEventListener("click", (event) => {
    event.stopPropagation();
    event.preventDefault();
    activeModelSelectId = button.dataset.modelTrigger;
    toggleFloatingMenu("#modelMenu", button, "right");
  });
});

document.querySelector("#modelMenu").addEventListener("click", (event) => {
  event.stopPropagation();
  const item = event.target.closest("[data-model-value]");
  if (!item) return;
  const current = account();
  const selectedRequiresPlan = item.dataset.modelValue !== "claude-code-economy";
  const selectedAllowed =
    current?.active && ClaudeApp.allowedPublicModelsForAccount(current).includes(item.dataset.modelValue);
  if ((!current?.active && selectedRequiresPlan) || (current?.active && !selectedAllowed)) {
    closeFloatingMenus();
    highlightedPlanId = item.dataset.modelValue === "claude-code-ultra" ? "ultra" : "";
    setPanel("plansPanel");
    focusHighlightedPlan();
    showChatNotice(
      item.dataset.modelValue === "claude-code-ultra"
        ? "Opus 4.7 está no plano Max 30X."
        : "Esse modelo exige upgrade de plano.",
    );
    return;
  }
  const select = document.querySelector(`#${activeModelSelectId}`);
  if (!select) return;
  select.value = item.dataset.modelValue;
  const settings = ClaudeApp.apiSettings();
  ClaudeApp.saveApiSettings({ ...settings, model: select.value });
  fillModelSelects();
  loadApiForm();
  closeFloatingMenus();
});

document.querySelector("#planCards").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-buy-plan]");
  if (!button || button.disabled) return;
  const planId = button.dataset.buyPlan;
  if (!account()?.active) {
    openAuthForIntent("plan", "clientSignupForm", planId);
    return;
  }
  await requestPlanPurchase(planId, button);
});

document.querySelector("#attachmentInput").addEventListener("change", async (event) => {
  const files = Array.from(event.currentTarget.files || []);
  if (!files.length) return;

  try {
    pendingAttachments = await Promise.all(files.slice(0, 4).map(readAttachment));
    showChatNotice(`Anexado: ${attachmentLabel()}`);
  } catch (error) {
    pendingAttachments = [];
    showChatNotice(error.message);
  }
});

document.querySelectorAll(".quick-actions button").forEach((button) => {
  button.addEventListener("click", () => {
    if (activeConversation.length || activeConversationId) {
      resetChat();
    } else {
      setPanel("chatPanel");
      document.querySelector("#emptyState").classList.remove("hidden");
    }
    renderSuggestionPanel(button.dataset.suggestionCategory);
  });
});

document.querySelector("#suggestionPanel").addEventListener("click", (event) => {
  if (event.target.closest("[data-close-suggestions]")) {
    closeSuggestionPanel();
    return;
  }
  const suggestion = event.target.closest("[data-suggestion-prompt]");
  if (!suggestion) return;
  fillHeroPrompt(suggestion.dataset.suggestionPrompt);
});

document.querySelector("#chatThread").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-copy-message-index]");
  if (!button) return;
  const index = Number(button.dataset.copyMessageIndex);
  const message = Number.isInteger(index) ? activeConversation[index] : null;
  if (!message?.content) return;
  const original = button.textContent;
  try {
    await navigator.clipboard.writeText(repairDuplicatedText(message.content));
    button.textContent = "Copiado";
  } catch {
    button.textContent = "Erro ao copiar";
  }
  window.setTimeout(() => {
    button.textContent = original;
  }, 1600);
});

document.querySelectorAll("#heroComposer textarea, #bottomComposer textarea").forEach((textarea) => {
  textarea.addEventListener("keydown", submitComposerOnEnter);
  textarea.addEventListener("input", () => updateComposerDraftState(textarea));
});

document.querySelector("#heroComposer").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const prompt = form.elements.prompt.value.trim() || (pendingAttachments.length ? "Analise os anexos." : "");
  if (!prompt) {
    showChatNotice("Digite uma mensagem para enviar.");
    form.elements.prompt.focus();
    return;
  }
  if (!account()?.active) {
    openAuthForIntent("chat", "clientSignupForm");
    showChatNotice("Entre ou crie uma conta para enviar sua mensagem.");
    return;
  }
  const attachments = pendingAttachments;
  pendingAttachments = [];
  stopDictation();
  form.elements.prompt.value = "";
  document.querySelector("#chatError").textContent = "";
  try {
    await submitPrompt(prompt, document.querySelector("#heroModel").value, attachments);
  } catch (error) {
    document.querySelector("#chatError").textContent = error.message;
  }
});

document.querySelector("#bottomComposer").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const prompt = form.elements.prompt.value.trim() || (pendingAttachments.length ? "Analise os anexos." : "");
  if (!prompt) {
    showChatNotice("Digite uma mensagem para enviar.");
    form.elements.prompt.focus();
    return;
  }
  if (!account()?.active) {
    openAuthForIntent("chat", "clientSignupForm");
    showChatNotice("Entre ou crie uma conta para enviar sua mensagem.");
    return;
  }
  const attachments = pendingAttachments;
  pendingAttachments = [];
  stopDictation();
  form.elements.prompt.value = "";
  document.querySelector("#chatError").textContent = "";
  try {
    await submitPrompt(prompt, document.querySelector("#bottomModel").value, attachments);
  } catch (error) {
    document.querySelector("#chatError").textContent = error.message;
  }
});

document.querySelector("#apiForm").addEventListener("submit", (event) => {
  event.preventDefault();
  const values = Object.fromEntries(new FormData(event.currentTarget).entries());
  const current = account();
  const manualToken = values.token === current?.apiToken ? "" : values.token;
  ClaudeApp.saveApiSettings({
    baseUrl: values.baseUrl || "http://127.0.0.1:8787",
    token: manualToken || "",
    model: values.model || ClaudeApp.apiSettings().model,
  });
  fillModelSelects();
  loadApiForm();
});

document.querySelector("#apiInstallGuide").addEventListener("click", async (event) => {
  const toggle = event.target.closest("[data-api-secret-toggle]");
  if (toggle) {
    if (!apiSecretsVisible && !window.confirm("Revelar o token completo nesta tela?")) return;
    apiSecretsVisible = !apiSecretsVisible;
    renderApiInstallGuide();
    return;
  }
  const button = event.target.closest("[data-copy-value]");
  if (!button) return;
  const originalSvg = button.innerHTML;
  try {
    await navigator.clipboard.writeText(button.dataset.copyValue);
    button.innerHTML = `<svg viewBox="0 0 24 24"><path d="M20 6L9 17l-5-5" /></svg>`;
    button.classList.add("copied");
  } catch {
    button.innerHTML = `<svg viewBox="0 0 24 24"><path d="M12 9v3m0 0v3m0-3h3m-3 0H9m12 0a9 9 0 11-18 0 9 9 0 0118 0z" /></svg>`;
  }
  window.setTimeout(() => {
    button.innerHTML = originalSvg;
    button.classList.remove("copied");
  }, 1600);
});

document.querySelector("#apiInstallGuide").addEventListener("focusin", (event) => {
  if (!event.target.matches(".api-command-box")) return;
  event.target.select();
});

document.querySelector("#heroModel").addEventListener("change", (event) => {
  const settings = ClaudeApp.apiSettings();
  ClaudeApp.saveApiSettings({ ...settings, model: event.target.value });
  fillModelSelects();
  loadApiForm();
});

document.querySelector("#bottomModel").addEventListener("change", (event) => {
  const settings = ClaudeApp.apiSettings();
  ClaudeApp.saveApiSettings({ ...settings, model: event.target.value });
  fillModelSelects();
  loadApiForm();
});

document.querySelector("#projectForm").addEventListener("submit", (event) => {
  event.preventDefault();
  const values = Object.fromEntries(new FormData(event.currentTarget).entries());
  const error = document.querySelector("#projectError");
  error.textContent = "";
  if (!String(values.name || "").trim()) {
    error.textContent = "Informe um nome para criar o projeto.";
    event.currentTarget.elements.name.focus();
    return;
  }
  const projects = ClaudeApp.projects();
  projects.unshift({
    id: `project_${Date.now()}`,
    name: values.name,
    context: values.context,
    createdAt: new Date().toISOString(),
  });
  ClaudeApp.saveProjects(projects);
  event.currentTarget.reset();
  closeProjectModal();
  renderSidePanels();
});

document.querySelector("#supportForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const message = form.elements.message.value.trim();
  document.querySelector("#supportError").textContent = "";
  if (!message) {
    document.querySelector("#supportError").textContent = "Digite uma mensagem para enviar ao suporte.";
    form.elements.message.focus();
    return;
  }
  try {
    const path = activeSupportTicket
      ? `/v1/support/tickets/${activeSupportTicket.id}/messages`
      : "/v1/support/tickets";
    const data = await supportRequest(path, {
      method: "POST",
      body: JSON.stringify({ message }),
    });
    activeSupportTicket = data.ticket;
    form.reset();
    renderSupport();
    startSupportPolling();
  } catch (error) {
    document.querySelector("#supportError").textContent = error.message;
  }
});

document.querySelector("#searchInput").addEventListener("input", (event) => {
  searchLocal(event.target.value);
});

document.querySelector("#historySearch").addEventListener("input", renderSidePanels);
document.querySelector("#projectSearch").addEventListener("input", renderSidePanels);
document.querySelector("#artifactSearch").addEventListener("input", renderSidePanels);

document.querySelector("#searchClose").addEventListener("click", () => {
  setPanel("chatPanel");
});

document.querySelector("#newChat").addEventListener("click", resetChat);

document.querySelector("#openProjectModal").addEventListener("click", () => {
  if (!account()?.active) {
    openAuthForIntent("project", "clientSignupForm");
    showChatNotice("Entre ou crie uma conta para criar projetos.");
    return;
  }
  document.querySelector("#projectModal").classList.remove("hidden");
  window.setTimeout(() => document.querySelector("#projectForm input[name='name']")?.focus(), 0);
});

function closeProjectModal() {
  document.querySelector("#projectModal").classList.add("hidden");
  document.querySelector("#projectError").textContent = "";
}

document.querySelector("#projectModalClose").addEventListener("click", closeProjectModal);
document.querySelector("#projectModalCancel").addEventListener("click", closeProjectModal);
document.querySelector("#projectModal").addEventListener("click", (event) => {
  if (event.target.id === "projectModal") closeProjectModal();
});

document.querySelector("#newArtifact").addEventListener("click", () => {
  if (!account()?.active) {
    openAuthForIntent("artifact", "clientSignupForm");
    showChatNotice("Entre ou crie uma conta para criar artefatos.");
    return;
  }
  document.querySelector("#artifactStartModal").classList.remove("hidden");
});

document.querySelector("#artifactStartClose").addEventListener("click", () => {
  document.querySelector("#artifactStartModal").classList.add("hidden");
});

document.querySelector("#artifactStartModal").addEventListener("click", (event) => {
  if (event.target.id === "artifactStartModal") {
    document.querySelector("#artifactStartModal").classList.add("hidden");
    return;
  }
  const button = event.target.closest("[data-artifact-prompt]");
  if (!button) return;
  document.querySelector("#artifactStartModal").classList.add("hidden");
  resetChat();
  if (button.dataset.artifactPrompt) fillHeroPrompt(button.dataset.artifactPrompt);
});

async function logoutClient({ confirmOpenConversation = true } = {}) {
  if (
    confirmOpenConversation &&
    (activeConversation.length || activeConversationId) &&
    !window.confirm("Sair da conta e fechar a conversa atual?")
  ) {
    return;
  }
  await saveConversation();
  stopSupportPolling();
  forgetSessionApiToken(currentAccountId);
  currentAccountId = null;
  sessionStatusMessage = "";
  activeConversationId = null;
  serverHistory = [];
  localStorage.removeItem(ClaudeApp.CLIENT_SESSION_KEY);
  activeConversation = [];
  document.querySelector("#chatThread").innerHTML = "";
  document.querySelector("#chatThread").classList.add("hidden");
  document.querySelector("#bottomComposer").classList.add("hidden");
  document.querySelector("#emptyState").classList.remove("hidden");
  renderAccount();
}

document.querySelector("#clientLogout").addEventListener("click", () => {
  logoutClient();
});

document.querySelector("#sidebarAccountButton").addEventListener("click", (event) => {
  if (!account()?.active) {
    openAuthForIntent("", "clientLoginForm");
    return;
  }
  toggleFloatingMenu("#accountMenu", event.currentTarget);
});

document.querySelector("#accountMenu").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-account-action]");
  if (!button) return;
  closeFloatingMenus();
  const action = button.dataset.accountAction;
  if (action === "settings") setPanel("settingsPanel");
  if (action === "support") setPanel("supportPanel");
  if (action === "apps") setPanel("apiPanel");
  if (action === "logout") {
    logoutClient();
  }
});

document.querySelector("#accountDetails").addEventListener("click", (event) => {
  const button = event.target.closest("[data-auth-settings]");
  if (!button) return;
  openAuthForIntent("", "clientLoginForm");
});

document.querySelector("#incognitoToggle").addEventListener("click", () => {
  setIncognitoMode(!incognitoMode);
});

document.querySelector("#authOpen").addEventListener("click", () => openAuthForIntent("", "clientLoginForm"));
document.querySelector("#authClose").addEventListener("click", closeAuthModal);
document.querySelector("#authModal").addEventListener("click", (event) => {
  if (event.target.id === "authModal") closeAuthModal();
});
document.querySelectorAll("[data-auth-tab]").forEach((button) => {
  button.addEventListener("click", () => setAuthTab(button.dataset.authTab));
});

document.addEventListener("click", (event) => {
  if (
    event.target.closest(
      ".floating-menu, #accountMenu, .attach-button, .model-trigger, #sidebarAccountButton",
    )
  ) {
    return;
  }
  closeFloatingMenus();
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    closeFloatingMenus();
    if (document.querySelector("#searchPanel").classList.contains("active")) setPanel("chatPanel");
    closeProjectModal();
    document.querySelector("#artifactStartModal").classList.add("hidden");
  }
});

fillModelSelects();
stripStoredAccountTokens();
repairStoredDuplicateArtifacts();
loadApiForm();
renderSidePanels();
renderSupport();

if (currentAccountId && !account()) {
  currentAccountId = null;
  sessionStatusMessage = "Sua sessão expirou. Entre novamente para continuar.";
  localStorage.removeItem(ClaudeApp.CLIENT_SESSION_KEY);
}

renderAccount();
if (sessionStatusMessage) showChatNotice(sessionStatusMessage);
loadServerHistory();
