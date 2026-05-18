let currentAccountId = localStorage.getItem(ClaudeApp.CLIENT_SESSION_KEY);
let activeConversation = [];
let activeRecognition = null;
let activeVoiceButton = null;

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
  form.elements.demoMode.checked = Boolean(settings.demoMode);
  document.querySelector("#heroModel").value = settings.model;
  document.querySelector("#bottomModel").value = settings.model;
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
    document.querySelector("#planBadge").textContent = "plano Gratuito";
    document.querySelector("#welcomeTitle").textContent = "Bem-vindo ao Claude";
    document.querySelector("#usageTitle").textContent = "Entre para usar o chat";
    document.querySelector("#usageText").textContent =
      "Você pode ver a interface, mas o envio exige uma conta paga e ativa.";
    document.querySelector("#usageFill").style.width = "0%";
    document.querySelector("#accountDetails").innerHTML = `
      <code>Status: prévia</code>
      <code>Chat: bloqueado até login em plano pago</code>
      <code>API: Claude Code API</code>
    `;
    document.querySelector("#previewNotice").classList.remove("hidden");
    document.querySelectorAll(".auth-only").forEach((item) => item.classList.add("hidden"));
    authOpen.classList.remove("hidden");
    logout.classList.add("hidden");
    logout.textContent = "";
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
    <code>Modelo permitido: ${ClaudeApp.models[current.modelKey]?.label || "Claude Sonnet 4.6"}</code>
    <code>Limite diario: ${ClaudeApp.integer.format(current.dailyLimit)} tokens</code>
  `;
  document.querySelector("#previewNotice").classList.add("hidden");
  document.querySelectorAll(".auth-only").forEach((item) => item.classList.remove("hidden"));
  authOpen.classList.add("hidden");
  logout.classList.remove("hidden");
  logout.textContent = preferredName.charAt(0).toUpperCase();
}

function setPanel(panelId) {
  document.querySelectorAll(".client-panel").forEach((panel) => panel.classList.remove("active"));
  document.querySelector(`#${panelId}`).classList.add("active");
  document.querySelectorAll(".icon-rail [data-panel]").forEach((button) => {
    button.classList.toggle("active", button.dataset.panel === panelId);
  });
  renderSidePanels();
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
    throw new Error("Entre com uma conta paga e ativa para usar o chat.");
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
  try {
    if (settings.demoMode) throw new Error("Modo demo ativo.");
    answer = await callGateway(prompt, selectedModel);
  } catch (error) {
    if (!settings.demoMode) throw error;
    answer = ClaudeApp.demoReply(current, prompt, reservedTotal);
  }

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
    demoMode: Boolean(values.demoMode),
  });
  fillModelSelects();
  loadApiForm();
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

document.querySelector("#searchInput").addEventListener("input", (event) => {
  searchLocal(event.target.value);
});

document.querySelector("#newChat").addEventListener("click", resetChat);

document.querySelector("#clientLogout").addEventListener("click", () => {
  saveConversation();
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

if (currentAccountId && !account()) {
  currentAccountId = null;
  localStorage.removeItem(ClaudeApp.CLIENT_SESSION_KEY);
}

renderAccount();
