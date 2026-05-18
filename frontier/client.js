let currentAccountId = localStorage.getItem(ClaudeApp.CLIENT_SESSION_KEY);
let activeConversation = [];
let activeRecognition = null;
let activeVoiceButton = null;
let activeSupportTicket = null;
let supportPollTimer = null;

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
}

function loadApiForm() {
  const settings = ClaudeApp.apiSettings();
  const form = document.querySelector("#apiForm");
  form.elements.baseUrl.value = settings.baseUrl;
  form.elements.token.value = settings.token;
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
  const modelKey = ClaudeApp.normalizeModelKey(current?.modelKey || "sonnet");
  return {
    baseUrl,
    token,
    model: ClaudeApp.backendModelForPlan(modelKey),
    plan: current?.plan || "Plano ativo",
    hasAccount: Boolean(current?.apiToken),
  };
}

function pythonInstaller(config) {
  return `python3 - <<'PY'
from pathlib import Path
import platform
import subprocess

base_url = ${JSON.stringify(config.baseUrl)}
api_token = ${JSON.stringify(config.token)}
selected_model = ${JSON.stringify(config.model)}

profile = Path.home() / (".zshrc" if platform.system() == "Darwin" else ".bashrc")
lines = [
    f'export ANTHROPIC_BASE_URL="{base_url}"',
    f'export ANTHROPIC_AUTH_TOKEN="{api_token}"',
    'export ANTHROPIC_API_KEY=""',
    f'export ANTHROPIC_DEFAULT_SONNET_MODEL="{selected_model}"',
    f'export CLAUDE_CODE_SUBAGENT_MODEL="{selected_model}"',
]

existing = profile.read_text() if profile.exists() else ""
start = "# assistente_api_config"
end = "# /assistente_api_config"
block = start + "\\n" + "\\n".join(lines) + "\\n" + end + "\\n"

if start in existing and end in existing:
    before = existing.split(start)[0].rstrip()
    after = existing.split(end, 1)[1].lstrip()
    profile.write_text(before + "\\n\\n" + block + "\\n" + after)
else:
    profile.write_text(existing.rstrip() + "\\n\\n" + block)

print(f"Configurado em {profile}")
print("Abra um terminal novo ou cole estes comandos agora:")
for line in lines:
    print(line)

try:
    result = subprocess.run(
        ["claude", "--version"],
        check=False,
        capture_output=True,
        text=True,
    )
    print("Claude Code encontrado:", result.stdout.strip() or result.stderr.strip())
except FileNotFoundError:
    print("Claude Code ainda nao esta instalado ou nao esta no PATH.")
PY`;
}

function renderCodeBlock(title, code, copyLabel = "Copiar") {
  const escaped = ClaudeApp.escapeHtml(code);
  return `
    <article class="api-step">
      <div class="api-step-head">
        <strong>${ClaudeApp.escapeHtml(title)}</strong>
        <button type="button" data-copy-value="${ClaudeApp.escapeHtml(code)}">${copyLabel}</button>
      </div>
      <pre><code>${escaped}</code></pre>
    </article>
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
  const loginHint = config.hasAccount
    ? "Configuracao pronta para esta conta."
    : "Entre em uma conta ativa para gerar a configuracao personalizada.";

  guide.innerHTML = `
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
      <li>Copie o comando unico e cole no terminal do Mac ou Linux.</li>
      <li>Depois abra um terminal novo e rode o Claude Code normalmente.</li>
      <li>O Claude web do navegador nao aceita API externa; use o Claude Code ou app compativel.</li>
    </ol>
    ${renderCodeBlock("Configurar este computador", installCommand, "Copiar comando")}
    ${renderCodeBlock("Testar conexao", curlTest)}
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
  document.querySelector("#planBadge").textContent = current.plan;
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
    <code>Plano: ${ClaudeApp.escapeHtml(current.plan)}</code>
    <code>Modelo permitido: ${ClaudeApp.models[current.modelKey]?.label || "Claude Code Pro"}</code>
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
  activeConversation.push({ role, content: text });
  const thread = document.querySelector("#chatThread");
  const node = document.createElement("div");
  node.className = `message ${role}`;
  node.textContent = text;
  thread.appendChild(node);
  document.querySelector("#emptyState").classList.add("hidden");
  document.querySelector("#chatThread").classList.remove("hidden");
  document.querySelector("#bottomComposer").classList.remove("hidden");
  node.scrollIntoView({ block: "end" });
}

function saveConversation() {
  if (!activeConversation.length) return;
  const title = activeConversation.find((item) => item.role === "user")?.content.slice(0, 70) || "Chat";
  const history = ClaudeApp.history();
  history.unshift({
    id: `chat_${Date.now()}`,
    accountId: currentAccountId,
    title,
    messages: activeConversation,
    createdAt: new Date().toISOString(),
  });
  ClaudeApp.saveHistory(history.slice(0, 40));
}

async function callGateway(prompt, selectedModel) {
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
      messages: activeConversation
        .filter((item) => item.role === "user" || item.role === "assistant")
        .map((item) => ({ role: item.role, content: item.content })),
    }),
  });

  if (!response.ok) {
    throw new Error(`API respondeu ${response.status}`);
  }

  const data = await response.json();
  return (data.content || []).map((part) => part.text || "").join("\n").trim() || "Sem resposta.";
}

async function submitPrompt(prompt, selectedModel) {
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

  addMessage("user", prompt);

  const settings = ClaudeApp.apiSettings();
  let answer = "";
  answer = await callGateway(prompt, selectedModel);

  const accounts = ClaudeApp.accounts();
  const index = accounts.findIndex((item) => item.id === currentAccountId);
  if (index >= 0) {
    accounts[index].usedToday += reservedTotal;
    ClaudeApp.saveAccounts(accounts);
  }

  addMessage("assistant", answer);
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

function renderSidePanels() {
  const history = ClaudeApp.history().filter((item) => item.accountId === currentAccountId);
  document.querySelector("#historyList").innerHTML = history.length
    ? history
        .map(
          (item) => `
            <div class="result-item">
              <strong>${ClaudeApp.escapeHtml(item.title)}</strong>
              <p>${new Date(item.createdAt).toLocaleString("pt-BR")}</p>
            </div>
          `,
        )
        .join("")
    : `<div class="result-item"><p>Nenhuma conversa salva ainda.</p></div>`;

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
  const results = ClaudeApp.history()
    .filter((item) => item.accountId === currentAccountId)
    .filter((item) => JSON.stringify(item).toLowerCase().includes(q));
  document.querySelector("#searchResults").innerHTML = results.length
    ? results
        .map(
          (item) => `
            <div class="result-item">
              <strong>${ClaudeApp.escapeHtml(item.title)}</strong>
              <p>${ClaudeApp.escapeHtml(item.messages.map((message) => message.content).join(" ").slice(0, 220))}</p>
            </div>
          `,
        )
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

function resetChat() {
  saveConversation();
  activeConversation = [];
  document.querySelector("#chatThread").innerHTML = "";
  document.querySelector("#chatThread").classList.add("hidden");
  document.querySelector("#bottomComposer").classList.add("hidden");
  document.querySelector("#emptyState").classList.remove("hidden");
  setPanel("chatPanel");
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
    return;
  } catch (error) {
    message.textContent = error.fallback ? "API indisponível para criar conta." : error.message;
    return;
  }
});

document.querySelectorAll("[data-panel]").forEach((button) => {
  button.addEventListener("click", () => setPanel(button.dataset.panel));
});

document.querySelectorAll(".voice-button").forEach((button) => {
  button.addEventListener("click", () => {
    startDictation(button);
  });
});

document.querySelectorAll(".quick-actions button").forEach((button) => {
  button.addEventListener("click", () => {
    const textarea = document.querySelector("#heroComposer textarea");
    textarea.value = button.dataset.prompt;
    textarea.focus();
  });
});

document.querySelector("#heroComposer").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const prompt = form.elements.prompt.value.trim();
  if (!prompt) return;
  stopDictation();
  form.elements.prompt.value = "";
  document.querySelector("#chatError").textContent = "";
  try {
    await submitPrompt(prompt, document.querySelector("#heroModel").value);
  } catch (error) {
    document.querySelector("#chatError").textContent = error.message;
  }
});

document.querySelector("#bottomComposer").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const prompt = form.elements.prompt.value.trim();
  if (!prompt) return;
  stopDictation();
  form.elements.prompt.value = "";
  document.querySelector("#chatError").textContent = "";
  try {
    await submitPrompt(prompt, document.querySelector("#bottomModel").value);
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
    model: ClaudeApp.apiSettings().model,
  });
  fillModelSelects();
  loadApiForm();
});

document.querySelector("#apiInstallGuide").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-copy-value]");
  if (!button) return;
  const original = button.textContent;
  try {
    await navigator.clipboard.writeText(button.dataset.copyValue);
    button.textContent = "Copiado";
  } catch {
    button.textContent = "Selecione o texto";
  }
  window.setTimeout(() => {
    button.textContent = original;
  }, 1600);
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

document.querySelector("#clientLogout").addEventListener("click", () => {
  saveConversation();
  stopSupportPolling();
  currentAccountId = null;
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
