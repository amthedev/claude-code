let currentAccountId = localStorage.getItem(ClaudeApp.CLIENT_SESSION_KEY);
let activeConversation = [];
let activeRecognition = null;
let activeVoiceButton = null;
let activeSupportTicket = null;
let supportPollTimer = null;
let pendingAttachments = [];
let activeConversationId = null;
let serverHistory = [];

function account() {
  return ClaudeApp.accounts().find((item) => item.id === currentAccountId) || null;
}

function openAuthModal(tab = "clientLoginForm") {
  document.querySelector("#authModal").classList.remove("hidden");
  setAuthTab(tab);
}

function closeAuthModal() {
  document.querySelector("#authModal").classList.add("hidden");
}

function setAuthTab(tabId) {
  document.querySelectorAll("[data-auth-tab]").forEach((button) => {
    button.classList.toggle("active", button.dataset.authTab === tabId);
  });
  document.querySelectorAll(".auth-pane").forEach((pane) => {
    pane.classList.toggle("active", pane.id === tabId);
  });
}

function fillModelSelects() {
  const settings = ClaudeApp.apiSettings();
  document.querySelector("#heroModel").innerHTML = ClaudeApp.modelOptions(settings.model);
  document.querySelector("#bottomModel").innerHTML = ClaudeApp.modelOptions(settings.model);
  const apiModel = document.querySelector("#apiModel");
  if (apiModel) apiModel.innerHTML = ClaudeApp.modelOptions(settings.model);
}

function loadApiForm() {
  const settings = ClaudeApp.apiSettings();
  const form = document.querySelector("#apiForm");
  form.elements.baseUrl.value = settings.baseUrl;
  form.elements.token.value = settings.token;
  form.elements.model.value = settings.model;
  document.querySelector("#heroModel").value = settings.model;
  document.querySelector("#bottomModel").value = settings.model;
  renderApiInstallGuide();
}

function shellQuote(value) {
  return `'${String(value).replaceAll("'", "'\"'\"'")}'`;
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

function renderCodeBlock(title, code) {
  const escapedTitle = ClaudeApp.escapeHtml(title);
  const escapedCode = ClaudeApp.escapeHtml(code);
  return `
    <article class="api-step">
      <div class="api-step-head">
        <strong>${escapedTitle}</strong>
        <button type="button" class="copy-icon-button" data-copy-value="${escapedCode}" aria-label="Copiar código">
          <svg viewBox="0 0 24 24"><path d="M8 4v12a2 2 0 0 0 2 2h8a2 2 0 0 0 2-2V7.5L16.5 4H8Z" /><path d="M16 4v4h4M4 12v6a2 2 0 0 0 2 2h6" /></svg>
        </button>
      </div>
      <textarea class="api-command-box" readonly spellcheck="false" aria-label="${escapedTitle}">${escapedCode}</textarea>
    </article>
  `;
}

function renderPrimaryCommand(code) {
  const escapedCode = ClaudeApp.escapeHtml(code);
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
      <textarea class="api-command-box api-command-box-primary" readonly spellcheck="false" aria-label="Comando para terminal">${escapedCode}</textarea>
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
  const loginHint = config.hasAccount
    ? "Configuracao pronta para esta conta."
    : "Entre em uma conta ativa para gerar a configuracao personalizada.";

  guide.innerHTML = `
    ${renderPrimaryCommand(sessionCommand)}
    <section class="api-summary">
      <div>
        <span class="overline">Acesso da conta</span>
        <strong>${ClaudeApp.escapeHtml(loginHint)}</strong>
      </div>
      <div class="api-kv">
        <code>URL da API: ${ClaudeApp.escapeHtml(config.baseUrl)}</code>
        <code>Plano: ${ClaudeApp.escapeHtml(config.plan)}</code>
        <code>API Key: ${ClaudeApp.escapeHtml(config.token)}</code>
      </div>
    </section>
    <ol class="api-steps">
      <li>O comando acima ja vem com os dados da conta logada e o modelo escolhido.</li>
      <li>Use o instalador Python: ele pergunta se quer usar esta API e configura terminal/extensao.</li>
      <li>No terminal, depois da instalacao, ao rodar <code>claude</code> ele pergunta antes de usar a API.</li>
      <li>Na extensao, a pergunta acontece no instalador antes de gravar o settings.json.</li>
    </ol>
    ${renderCodeBlock("Instalador Python com pergunta", installCommand)}
    ${renderCodeBlock("Somente extensão: salvar settings.json", settingsCommand)}
    ${renderCodeBlock("Testar conexão", curlTest)}
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
      Authorization: `Bearer ${settings.token}`,
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
  const accounts = ClaudeApp.accounts();
  const index = accounts.findIndex(
    (item) => item.id === accountData.id || item.login === accountData.login,
  );
  if (index >= 0) {
    accounts[index] = { ...accounts[index], ...accountData };
  } else {
    accounts.push(accountData);
  }
  ClaudeApp.saveAccounts(accounts);
  return ClaudeApp.accounts().find((item) => item.id === accountData.id) || accountData;
}

function syncCustomerApiToken(current) {
  if (!current?.apiToken) return;
  const settings = ClaudeApp.apiSettings();
  if (settings.token === current.apiToken) return;
  ClaudeApp.saveApiSettings({ ...settings, token: current.apiToken });
  loadApiForm();
}

function renderAccount() {
  const current = account();
  const authOpen = document.querySelector("#authOpen");
  const logout = document.querySelector("#clientLogout");

  if (!current || !current.active) {
    document.querySelector("#planBadge").textContent = "Entrar para usar";
    document.querySelector("#welcomeTitle").textContent = "Bem-vindo ao Claude";
    document.querySelector("#usageTitle").textContent = "Entre para usar o chat";
    document.querySelector("#usageText").textContent =
      "O envio exige uma conta ativa.";
    document.querySelector("#usageFill").style.width = "0%";
    document.querySelector("#accountDetails").innerHTML = `
      <code>Status: aguardando login</code>
      <code>Chat: entre com uma conta ativa</code>
      <code>API: Claude Code API</code>
    `;
    document.querySelector("#previewNotice").classList.remove("hidden");
    document.querySelectorAll(".auth-only").forEach((item) => item.classList.add("hidden"));
    authOpen.classList.remove("hidden");
    logout.classList.add("hidden");
    logout.textContent = "";
    stopSupportPolling();
    return;
  }

  const preferredName = (current.displayName || current.name || "Você").trim();
  syncCustomerApiToken(current);
  document.querySelector("#planBadge").textContent = "Conta ativa";
  document.querySelector("#welcomeTitle").textContent = `${preferredName} está de volta!`;
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
  document.querySelectorAll(".auth-only").forEach((item) => item.classList.remove("hidden"));
  authOpen.classList.add("hidden");
  logout.classList.remove("hidden");
  logout.textContent = preferredName.charAt(0).toUpperCase();
  startSupportPolling();
}

function setPanel(panelId) {
  document.querySelectorAll(".client-panel").forEach((panel) => panel.classList.remove("active"));
  document.querySelector(`#${panelId}`).classList.add("active");
  document.querySelectorAll(".icon-rail [data-panel]").forEach((button) => {
    button.classList.toggle("active", button.dataset.panel === panelId);
  });
  renderSidePanels();
  if (panelId === "supportPanel") refreshSupportTicket();
}

function addMessage(role, text) {
  const index = activeConversation.push({ role, content: text }) - 1;
  const thread = document.querySelector("#chatThread");
  const node = document.createElement("div");
  node.className = `message ${role}`;
  node.textContent = text;
  thread.appendChild(node);
  document.querySelector("#emptyState").classList.add("hidden");
  document.querySelector("#chatThread").classList.remove("hidden");
  document.querySelector("#bottomComposer").classList.remove("hidden");
  node.scrollIntoView({ block: "end" });
  return { index, node };
}

function updateMessage(message, text) {
  const nextText = text || "Pensando...";
  activeConversation[message.index].content = nextText;
  message.node.textContent = nextText;
  message.node.scrollIntoView({ block: "end" });
}

async function conversationRequest(path, options = {}) {
  const settings = ClaudeApp.apiSettings();
  const baseUrl = settings.baseUrl.replace(/\/$/, "");
  const response = await fetch(`${baseUrl}${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${settings.token}`,
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
    serverHistory = data.data || [];
    renderSidePanels();
  } catch {
    serverHistory = [];
    renderSidePanels();
  }
}

async function saveConversation() {
  if (!activeConversation.length || !account()?.active) return;

  try {
    const data = await conversationRequest("/v1/conversations", {
      method: "POST",
      body: JSON.stringify({
        id: activeConversationId,
        messages: activeConversation,
      }),
    });
    activeConversationId = data.conversation.id;
    await loadServerHistory();
  } catch {
    showChatNotice("Não consegui salvar a conversa no banco agora.");
  }
}

function renderConversationMessages(messages) {
  activeConversation = [];
  const thread = document.querySelector("#chatThread");
  thread.innerHTML = "";

  messages.forEach((message) => {
    if (message.role === "user" || message.role === "assistant") {
      addMessage(message.role, message.content || "");
    }
  });

  if (!activeConversation.length) {
    thread.classList.add("hidden");
    document.querySelector("#bottomComposer").classList.add("hidden");
    document.querySelector("#emptyState").classList.remove("hidden");
  }
}

async function openConversation(conversationId) {
  try {
    const data = await conversationRequest(`/v1/conversations/${conversationId}`);
    activeConversationId = data.conversation.id;
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
      if (delta.type === "text_delta" && delta.text) onText(delta.text);
      if (typeof delta.text === "string" && delta.text) onText(delta.text);

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

async function callGateway(selectedModel, messages, onText) {
  const settings = ClaudeApp.apiSettings();
  const response = await fetch(`${settings.baseUrl.replace(/\/$/, "")}/v1/messages`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${settings.token}`,
    },
    body: JSON.stringify({
      model: selectedModel,
      max_tokens: 1200,
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
      answer += text;
      onText(answer.trimStart());
    });
  }

  return answer.trim() || "Sem resposta.";
}

async function submitPrompt(prompt, selectedModel, attachments = []) {
  const current = account();
  if (!current || !current.active) {
    openAuthModal("clientLoginForm");
    throw new Error("Entre com uma conta ativa para usar o chat.");
  }

  const estimatedInput = ClaudeApp.estimateTokens(prompt);
  const reservedOutput = 700;
  const reservedTotal = estimatedInput + reservedOutput;
  const remaining = current.dailyLimit - current.usedToday;

  if (reservedTotal > remaining) {
    throw new Error(`Limite diário insuficiente. Restam ${ClaudeApp.integer.format(Math.max(0, remaining))} tokens.`);
  }

  const visiblePrompt = attachments.length ? `${prompt}\n\nAnexos: ${attachmentLabel(attachments)}` : prompt;
  addMessage("user", visiblePrompt);
  const outgoingMessages = activeConversation
    .filter((item) => item.role === "user" || item.role === "assistant")
    .map((item) => ({ role: item.role, content: item.content }));
  outgoingMessages[outgoingMessages.length - 1].content = buildMessageContent(prompt, attachments);
  const assistantMessage = addMessage("assistant", "Pensando...");

  const settings = ClaudeApp.apiSettings();
  let answer = "";
  answer = await callGateway(selectedModel, outgoingMessages, (partialAnswer) => {
    updateMessage(assistantMessage, partialAnswer);
  });
  updateMessage(assistantMessage, answer);

  const accounts = ClaudeApp.accounts();
  const index = accounts.findIndex((item) => item.id === currentAccountId);
  if (index >= 0) {
    accounts[index].usedToday += reservedTotal;
    ClaudeApp.saveAccounts(accounts);
  }

  await saveConversation();
  createArtifactIfUseful(prompt, answer);
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
    <button class="result-item history-item" type="button" data-conversation-id="${ClaudeApp.escapeHtml(item.id)}">
      <span class="history-title">${ClaudeApp.escapeHtml(item.title)}</span>
      <span class="history-meta">
        <span>${historyDate(item)}</span>
        <span>Abrir</span>
      </span>
    </button>
  `;
}

function renderSidePanels() {
  document.querySelector("#historyList").innerHTML = serverHistory.length
    ? serverHistory
        .map((item) => historyItemMarkup(item))
        .join("")
    : `<div class="result-item"><p>Nenhuma conversa salva no banco ainda.</p></div>`;

  const projects = ClaudeApp.projects();
  document.querySelector("#projectList").innerHTML = projects.length
    ? projects
        .map(
          (item) => `
            <div class="result-item">
              <strong>${ClaudeApp.escapeHtml(item.name)}</strong>
              <p>${ClaudeApp.escapeHtml(item.context || "Sem contexto adicional.")}</p>
            </div>
          `,
        )
        .join("")
    : `<div class="result-item"><p>Nenhum projeto local.</p></div>`;

  const artifacts = ClaudeApp.artifacts();
  document.querySelector("#artifactList").innerHTML = artifacts.length
    ? artifacts
        .map(
          (item) => `
            <div class="result-item">
              <strong>${ClaudeApp.escapeHtml(item.title)}</strong>
              <p>${ClaudeApp.escapeHtml(item.body.slice(0, 180))}</p>
            </div>
          `,
        )
        .join("")
    : `<div class="result-item"><p>Nenhum artefato gerado.</p></div>`;
}

function searchLocal(query) {
  const q = query.trim().toLowerCase();
  const results = serverHistory.filter((item) => JSON.stringify(item).toLowerCase().includes(q));
  document.querySelector("#searchResults").innerHTML = results.length
    ? results
        .map((item) => historyItemMarkup(item))
        .join("")
    : `<div class="result-item"><p>Nenhum resultado.</p></div>`;
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

async function resetChat() {
  await saveConversation();
  activeConversationId = null;
  activeConversation = [];
  document.querySelector("#chatThread").innerHTML = "";
  document.querySelector("#chatThread").classList.add("hidden");
  document.querySelector("#bottomComposer").classList.add("hidden");
  document.querySelector("#emptyState").classList.remove("hidden");
  setPanel("chatPanel");
  renderSidePanels();
}

document.querySelector("#clientLoginForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  document.querySelector("#clientLoginError").textContent = "";
  const values = Object.fromEntries(new FormData(event.currentTarget).entries());
  let found = null;
  try {
    const data = await authRequest("/v1/auth/login", values);
    found = saveServerAccount(data.account);
  } catch (error) {
    document.querySelector("#clientLoginError").textContent =
      error.fallback ? "API indisponível para validar login." : error.message;
    return;
  }

  if (!found) {
    document.querySelector("#clientLoginError").textContent = "Login ou senha inválidos.";
    return;
  }

  if (!found.active) {
    document.querySelector("#clientLoginError").textContent =
      "Conta pausada. Fale com o suporte para reativar.";
    return;
  }

  currentAccountId = found.id;
  localStorage.setItem(ClaudeApp.CLIENT_SESSION_KEY, found.id);
  closeAuthModal();
  renderAccount();
  loadServerHistory();
});

document.querySelector("#clientSignupForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const message = document.querySelector("#clientSignupMessage");
  message.textContent = "";
  const values = Object.fromEntries(new FormData(event.currentTarget).entries());
  const accounts = ClaudeApp.accounts();
  const login = values.login.trim();
  const exists = accounts.some((item) => item.login.toLowerCase() === login.toLowerCase());
  if (exists) {
    message.textContent = "Esse login já existe.";
    return;
  }

  try {
    const data = await authRequest("/v1/auth/signup", values);
    const account = saveServerAccount(data.account);
    currentAccountId = account.id;
    localStorage.setItem(ClaudeApp.CLIENT_SESSION_KEY, account.id);
    event.currentTarget.reset();
    closeAuthModal();
    renderAccount();
    loadServerHistory();
    return;
  } catch (error) {
    message.textContent = error.fallback ? "API indisponível para criar conta." : error.message;
    return;
  }
});

document.querySelectorAll("[data-panel]").forEach((button) => {
  button.addEventListener("click", () => setPanel(button.dataset.panel));
});

document.querySelector("#railNewChat").addEventListener("click", resetChat);

document.querySelector("#historyList").addEventListener("click", (event) => {
  const item = event.target.closest("[data-conversation-id]");
  if (item) openConversation(item.dataset.conversationId);
});

document.querySelector("#searchResults").addEventListener("click", (event) => {
  const item = event.target.closest("[data-conversation-id]");
  if (item) openConversation(item.dataset.conversationId);
});

document.querySelectorAll(".voice-button").forEach((button) => {
  button.addEventListener("click", () => {
    startDictation(button);
  });
});

document.querySelectorAll(".attach-button").forEach((button) => {
  button.addEventListener("click", (event) => {
    event.preventDefault();
    triggerAttachmentPicker();
  });
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
    const textarea = document.querySelector("#heroComposer textarea");
    textarea.value = button.dataset.prompt;
    textarea.focus();
  });
});

document.querySelectorAll("#heroComposer textarea, #bottomComposer textarea").forEach((textarea) => {
  textarea.addEventListener("keydown", submitComposerOnEnter);
});

document.querySelector("#heroComposer").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const prompt = form.elements.prompt.value.trim() || (pendingAttachments.length ? "Analise os anexos." : "");
  if (!prompt) return;
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
  if (!prompt) return;
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
  ClaudeApp.saveApiSettings({
    baseUrl: values.baseUrl || "http://127.0.0.1:8787",
    token: values.token || "local-dev-token",
    model: values.model || ClaudeApp.apiSettings().model,
  });
  fillModelSelects();
  loadApiForm();
});

document.querySelector("#apiInstallGuide").addEventListener("click", async (event) => {
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
  const projects = ClaudeApp.projects();
  projects.unshift({
    id: `project_${Date.now()}`,
    name: values.name,
    context: values.context,
    createdAt: new Date().toISOString(),
  });
  ClaudeApp.saveProjects(projects);
  event.currentTarget.reset();
  renderSidePanels();
});

document.querySelector("#supportForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const message = form.elements.message.value.trim();
  if (!message) return;
  document.querySelector("#supportError").textContent = "";
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

document.querySelector("#newChat").addEventListener("click", resetChat);

document.querySelector("#clientLogout").addEventListener("click", async () => {
  await saveConversation();
  stopSupportPolling();
  currentAccountId = null;
  activeConversationId = null;
  serverHistory = [];
  localStorage.removeItem(ClaudeApp.CLIENT_SESSION_KEY);
  activeConversation = [];
  document.querySelector("#chatThread").innerHTML = "";
  document.querySelector("#chatThread").classList.add("hidden");
  document.querySelector("#bottomComposer").classList.add("hidden");
  document.querySelector("#emptyState").classList.remove("hidden");
  renderAccount();
});

document.querySelector("#authOpen").addEventListener("click", () => openAuthModal("clientLoginForm"));
document.querySelector("#authClose").addEventListener("click", closeAuthModal);
document.querySelector("#authModal").addEventListener("click", (event) => {
  if (event.target.id === "authModal") closeAuthModal();
});
document.querySelectorAll("[data-auth-tab]").forEach((button) => {
  button.addEventListener("click", () => setAuthTab(button.dataset.authTab));
});

fillModelSelects();
loadApiForm();
renderSidePanels();
renderSupport();

if (currentAccountId && !account()) {
  currentAccountId = null;
  localStorage.removeItem(ClaudeApp.CLIENT_SESSION_KEY);
}

renderAccount();
loadServerHistory();
