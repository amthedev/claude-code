const adminLogin = document.querySelector("#adminLogin");
const adminApp = document.querySelector("#adminApp");
const giftCardForm = document.querySelector("#giftCardForm");
const apiTokenForm = document.querySelector("#apiTokenForm");
let supportState = { waiting: [], active: [], closed: [] };
let activeSupportTicket = null;
let supportPollTimer = null;
let adminSetupConfigured = true;
let purchaseState = [];
let gatewayHealthState = null;
let benchmarkState = null;

const adminTabTitles = {
  accountsPanel: "Gift cards e clientes",
  purchasesPanel: "Compras e upgrades",
  supportPanel: "Atendimento",
  financePanel: "Financeiro",
  gatewayPanel: "Gateway e produção",
};

function setAdminTab(tabId) {
  document.querySelectorAll("[data-admin-tab]").forEach((item) => {
    item.classList.toggle("active", item.dataset.adminTab === tabId);
  });
  document.querySelectorAll(".admin-panel").forEach((panel) => {
    panel.classList.toggle("active", panel.id === tabId);
  });
  document.querySelector(".admin-topbar h1").textContent = adminTabTitles[tabId] || "Painel";
  document.querySelector("#seedDemo").classList.toggle("hidden", tabId !== "accountsPanel");
}

function showAdminApp() {
  adminLogin.classList.add("hidden");
  adminApp.classList.remove("hidden");
}

function fillAdminLoginForm() {
  const form = document.querySelector("#adminLoginForm");
  form.elements.login.value = form.elements.login.value || "admin";
}

function fillAdminApiTargetForm() {
  const settings = ClaudeApp.apiSettings();
  const form = document.querySelector("#adminApiTargetForm");
  if (!form) return;
  form.elements.baseUrl.value = settings.baseUrl;
}

function rememberAdminDevice() {
  localStorage.setItem(ClaudeApp.ADMIN_SESSION_KEY, "1");
}

function forgetAdminDevice() {
  localStorage.removeItem(ClaudeApp.ADMIN_SESSION_KEY);
  sessionStorage.removeItem(ClaudeApp.ADMIN_SESSION_KEY);
}

async function unlockRememberedAdminDevice() {
  const settings = adminApiSettings();
  if (!settings.token) {
    forgetAdminDevice();
    return false;
  }

  try {
    await refreshFromServer();
  } catch {
    if (localStorage.getItem(ClaudeApp.ADMIN_SESSION_KEY) === "1") forgetAdminDevice();
    return false;
  }
  rememberAdminDevice();
  showAdminApp();
  fillAdminApiTargetForm();
  renderAll();
  startSupportPolling();
  return true;
}

function adminApiSettings() {
  return ClaudeApp.apiSettings();
}

async function adminRequest(path, options = {}) {
  const settings = adminApiSettings();
  const baseUrl = settings.baseUrl.replace(/\/$/, "");
  let response;
  try {
    response = await fetch(`${baseUrl}${path}`, {
      ...options,
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${settings.token}`,
        ...(options.headers || {}),
      },
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

async function loadAdminSetupStatus() {
  const settings = ClaudeApp.apiSettings();
  const baseUrl = settings.baseUrl.replace(/\/$/, "");
  const hint = document.querySelector("#adminSetupHint");
  const submit = document.querySelector("#adminLoginForm button[type='submit']");
  try {
    const response = await fetch(`${baseUrl}/v1/admin/setup-status`);
    const data = await response.json();
    adminSetupConfigured = Boolean(data.configured);
  } catch {
    adminSetupConfigured = true;
  }

  if (!adminSetupConfigured) {
    hint.textContent = "Primeiro acesso: crie a senha admin. Ela sera salva como hash no backend.";
    submit.textContent = "Criar senha admin";
  } else {
    hint.textContent = "";
    submit.textContent = "Entrar";
  }
}

async function adminAuthRequest(path, payload) {
  const settings = ClaudeApp.apiSettings();
  const baseUrl = settings.baseUrl.replace(/\/$/, "");
  const response = await fetch(`${baseUrl}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
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

async function refreshFromServer() {
  const [giftCards, accounts, purchases, health] = await Promise.all([
    adminRequest("/v1/admin/gift-cards"),
    adminRequest("/v1/admin/accounts"),
    adminRequest("/v1/admin/purchases"),
    adminRequest("/v1/admin/health"),
  ]);
  ClaudeApp.saveGiftCards(giftCards.data || []);
  ClaudeApp.saveAccounts(accounts.data || []);
  purchaseState = purchases.data || [];
  gatewayHealthState = health;
  ClaudeApp.savePurchases(purchaseState);
}

async function refreshSupportFromServer() {
  const data = await adminRequest("/v1/admin/support/tickets");
  supportState = {
    waiting: data.waiting || [],
    active: data.active || [],
    closed: data.closed || [],
  };
  activeSupportTicket = supportState.active[0] || activeSupportTicket;
  if (activeSupportTicket) {
    activeSupportTicket =
      [...supportState.active, ...supportState.waiting, ...supportState.closed].find(
        (ticket) => ticket.id === activeSupportTicket.id,
      ) || activeSupportTicket;
  }
  renderSupportAdmin();
}

function startSupportPolling() {
  if (supportPollTimer) return;
  refreshSupportFromServer().catch(() => {});
  supportPollTimer = window.setInterval(() => {
    refreshSupportFromServer().catch(() => {});
  }, 2200);
}

function stopSupportPolling() {
  if (supportPollTimer) window.clearInterval(supportPollTimer);
  supportPollTimer = null;
}

function renderPreview() {
  const values = Object.fromEntries(new FormData(giftCardForm).entries());
  const limit = ClaudeApp.calculateLimit(values.price, values.model, values.manualLimit);
  document.querySelector("#previewTokens").textContent =
    `${ClaudeApp.integer.format(limit.dailyLimit)} tokens/dia`;
  document.querySelector("#previewCost").textContent = `${ClaudeApp.usd.format(limit.maxCostUsd)}/mês`;
}

function renderApiPreview() {
  const values = Object.fromEntries(new FormData(apiTokenForm).entries());
  const limit = ClaudeApp.calculateApiOnlyLimit(values.price, values.durationHours);
  document.querySelector("#apiPreviewProfit").textContent = ClaudeApp.brl.format(limit.protectedProfitBrl);
  document.querySelector("#apiPreviewTokens").textContent =
    `${ClaudeApp.integer.format(limit.dailyLimit)} tokens/dia`;
  document.querySelector("#apiPreviewCost").textContent = ClaudeApp.usd.format(limit.maxCostUsd);
}

function accountRechargePayload(account) {
  const tokensText = prompt(
    `Adicionar quantos tokens ao limite diário de ${account.name}?\nDeixe vazio para recarregar por valor em R$.`,
    "",
  );
  if (tokensText === null) return null;
  const normalizedTokens = tokensText.trim().replace(/\./g, "").replace(",", ".");
  const addTokens = Math.floor(Number(normalizedTokens) || 0);
  if (addTokens > 0) return { addTokens };

  const brlText = prompt("Valor da recarga em R$ para converter em tokens:", "50");
  if (brlText === null) return null;
  const rechargeBrl = Number(brlText.trim().replace(/\./g, "").replace(",", ".")) || 0;
  if (rechargeBrl <= 0) return null;
  const preview = ClaudeApp.calculateApiOnlyLimit(rechargeBrl, 24);
  if (!confirm(`Adicionar ${ClaudeApp.integer.format(preview.dailyLimit)} tokens/dia por ${ClaudeApp.brl.format(rechargeBrl)}?`)) {
    return null;
  }
  return { rechargeBrl };
}

function renderMetrics(accounts) {
  const active = accounts.filter((account) => account.active);
  const revenue = active.reduce((sum, account) => sum + account.price, 0);
  const collected = purchaseState
    .filter((purchase) => purchase.status === "paid")
    .reduce((sum, purchase) => sum + Number(purchase.price || 0), 0);
  const usage = active.reduce((sum, account) => sum + account.usedToday, 0);
  const limits = active.reduce((sum, account) => sum + account.dailyLimit, 0);

  document.querySelector("#metricRevenue").textContent = ClaudeApp.brl.format(revenue);
  document.querySelector("#metricCollected").textContent = ClaudeApp.brl.format(collected);
  document.querySelector("#metricProfit").textContent = ClaudeApp.brl.format(
    active.reduce(
      (sum, account) =>
        sum + Number(account.price || 0) * (account.apiOnly ? ClaudeApp.API_ONLY_PROFIT_MARGIN : ClaudeApp.MIN_PROFIT_MARGIN),
      0,
    ),
  );
  document.querySelector("#metricLimit").textContent = ClaudeApp.integer.format(limits);
  document.querySelector("#metricUsage").textContent = ClaudeApp.integer.format(usage);
}

function purchaseStatusLabel(status) {
  if (status === "paid") return "Pago";
  if (status === "canceled") return "Cancelado";
  return "Pendente";
}

function renderPurchases() {
  const table = document.querySelector("#purchasesTable");
  if (!table) return;
  if (!purchaseState.length) {
    table.innerHTML = `<tr><td colspan="5" class="muted">Nenhum pedido de upgrade.</td></tr>`;
    return;
  }

  table.innerHTML = purchaseState
    .map(
      (purchase) => `
        <tr>
          <td>
            <strong>${ClaudeApp.escapeHtml(purchase.name)}</strong>
            <div class="muted">${ClaudeApp.escapeHtml(purchase.login)}</div>
            <div class="muted">${new Date(purchase.createdAt).toLocaleString("pt-BR")}</div>
          </td>
          <td>
            ${ClaudeApp.escapeHtml(purchase.plan)}
            <div class="muted">${ClaudeApp.integer.format(purchase.dailyLimit)} tokens/dia</div>
          </td>
          <td>${ClaudeApp.brl.format(purchase.price)}</td>
          <td>
            <span class="badge ${purchase.status === "paid" ? "ok" : purchase.status === "canceled" ? "bad" : ""}">
              ${purchaseStatusLabel(purchase.status)}
            </span>
          </td>
          <td>
            ${
              purchase.status === "pending"
                ? `<div class="row-actions">
                    <button data-purchase-action="approve" data-id="${purchase.id}">Confirmar pagamento</button>
                    <button data-purchase-action="cancel" data-id="${purchase.id}">Cancelar</button>
                  </div>`
                : ""
            }
          </td>
        </tr>
      `,
    )
    .join("");
}

function renderGatewaySnippet(accounts) {
  const target = document.querySelector("#customerEnvSnippet");
  if (!target) return;

  target.value = accounts
    .map((account) =>
      [
        account.apiToken,
        account.name,
        Number(account.price || 0).toFixed(2),
        account.dailyLimit,
        "*",
        account.active ? "true" : "false",
      ].join("|"),
    )
    .join(";");
}

function renderProductionChecklist() {
  const target = document.querySelector("#productionChecklist");
  if (!target) return;
  if (!gatewayHealthState) {
    target.innerHTML = `<article><span class="badge bad">Pendente</span><strong>Health indisponível</strong><p>Entre novamente ou confira a URL da API.</p></article>`;
    return;
  }
  const readiness = gatewayHealthState.production_readiness || {};
  const webSearch = gatewayHealthState.web_search || {};
  const publicTrial = gatewayHealthState.public_trial || {};
  const items = [
    ["OpenRouter", readiness.openrouter, "Chave principal para respostas do modelo."],
    ["Pesquisa web", readiness.web_search, `${webSearch.model || "gpt-5.5"} / ${webSearch.context_size || "low"}`],
    ["Mercado Pago", readiness.mercado_pago, "Checkout e webhook para upgrades."],
    ["Senha admin", readiness.admin_password, "Login protegido por senha/hash no backend."],
    ["CORS restrito", readiness.cors_restricted, "Origens liberadas para o navegador."],
    ["OpenAPI privado", readiness.openapi_private, "Docs públicas fechadas por padrão."],
    ["Banco persistente", readiness.persistent_storage, readiness.account_data_file || "SQLite configurado"],
    ["Hosts restritos", readiness.trusted_hosts_restricted, "Use domínio explícito em produção."],
  ];
  if (publicTrial.configured || publicTrial.enabled || publicTrial.active) {
    const detail = publicTrial.active
      ? `${publicTrial.label || "Teste grátis"} até ${new Date(publicTrial.endAt).toLocaleString("pt-BR")}`
      : "Configurado, mas inativo ou expirado.";
    items.push(["Teste público", publicTrial.active, detail]);
  }
  target.innerHTML = items
    .map(([label, ok, detail]) => `
      <article>
        <span class="badge ${ok ? "ok" : "bad"}">${ok ? "OK" : "Ação"}</span>
        <strong>${ClaudeApp.escapeHtml(label)}</strong>
        <p>${ClaudeApp.escapeHtml(detail)}</p>
      </article>
    `)
    .join("");
}

function renderBenchmark() {
  const summary = document.querySelector("#benchmarkSummary");
  const table = document.querySelector("#benchmarkTable");
  const advice = document.querySelector("#benchmarkAdvice");
  const status = document.querySelector("#benchmarkStatus");
  if (!summary || !table || !advice) return;
  if (status) status.textContent = "";

  if (!benchmarkState) {
    summary.innerHTML = `
      <article>
        <span>Aguardando</span>
        <strong>Nenhum benchmark rodado</strong>
      </article>
    `;
    table.innerHTML = `<tr><td colspan="5" class="muted">Clique em Rodar benchmark para testar setup, rotas, custo e pesquisa web.</td></tr>`;
    advice.innerHTML = "";
    return;
  }

  if (benchmarkState.running) {
    summary.innerHTML = `
      <article>
        <span>Status</span>
        <strong>Rodando...</strong>
      </article>
    `;
    table.innerHTML = `<tr><td colspan="5" class="muted">Testando setup, rotas, custo e pesquisa web.</td></tr>`;
    advice.innerHTML = "";
    return;
  }

  if (benchmarkState.errorMessage && status) {
    status.textContent = benchmarkState.errorMessage;
  }

  const data = benchmarkState.summary || {};
  summary.innerHTML = `
    <article>
      <span>Status</span>
      <strong>${data.status === "ok" ? "OK" : "Atenção"}</strong>
    </article>
    <article>
      <span>Passaram</span>
      <strong>${ClaudeApp.integer.format(data.passed || 0)}</strong>
    </article>
    <article>
      <span>Falhas</span>
      <strong>${ClaudeApp.integer.format(data.failed || 0)}</strong>
    </article>
    <article>
      <span>Roteador mediano</span>
      <strong>${ClaudeApp.escapeHtml(String(data.route_median_ms || 0))} ms</strong>
    </article>
  `;

  const rows = benchmarkState.results || [];
  table.innerHTML = rows
    .map((row) => {
      const statusClass = row.status === "OK" ? "ok" : row.status === "FAIL" ? "bad" : "";
      const route =
        row.category === "route"
          ? `${row.mode || "-"} / ${row.task_type || "-"}${row.orchestration ? " / pipeline" : " / direto"}`
          : row.severity === "warning"
            ? "Aviso"
            : "Setup";
      const cost =
        row.category === "route" && row.cost_ratio !== undefined && row.cost_ratio !== null
          ? `${Math.round(Number(row.cost_ratio) * 1000) / 10}% do Claude`
          : "-";
      const detail =
        row.category === "route"
          ? `${row.selected_model || "-"}${row.web_search ? " / web" : ""}`
          : row.detail || "";
      const note = row.notes && row.notes !== detail ? row.notes : "";
      return `
        <tr>
          <td>
            <strong>${ClaudeApp.escapeHtml(row.label || row.id)}</strong>
            <div class="muted">${ClaudeApp.escapeHtml(row.id || "")}</div>
          </td>
          <td><span class="badge ${statusClass}">${ClaudeApp.escapeHtml(row.status || "-")}</span></td>
          <td>${ClaudeApp.escapeHtml(route)}</td>
          <td>${ClaudeApp.escapeHtml(cost)}</td>
          <td>
            ${ClaudeApp.escapeHtml(detail)}
            ${note ? `<div class="muted">${ClaudeApp.escapeHtml(note)}</div>` : ""}
          </td>
        </tr>
      `;
    })
    .join("");

  advice.innerHTML = (benchmarkState.advice || [])
    .map((item) => `<p>${ClaudeApp.escapeHtml(item)}</p>`)
    .join("");
}

function giftCardStatus(card) {
  if (card.usedByAccountId) return "Resgatado";
  return card.active ? "Disponível" : "Pausado";
}

function renderGiftCards() {
  const cards = ClaudeApp.giftCards();
  ClaudeApp.saveGiftCards(cards);

  const table = document.querySelector("#giftCardsTable");
  if (!cards.length) {
    table.innerHTML = `<tr><td colspan="5" class="muted">Nenhum gift card gerado.</td></tr>`;
    return;
  }

  table.innerHTML = cards
    .map((card) => {
      const status = giftCardStatus(card);
      const canPause = !card.usedByAccountId;
      return `
        <tr>
          <td>
            <strong>${ClaudeApp.escapeHtml(card.code)}</strong>
            <div class="muted">Criado em ${new Date(card.createdAt).toLocaleString("pt-BR")}</div>
            ${
              card.usedByLogin
                ? `<div class="muted">Usado por ${ClaudeApp.escapeHtml(card.usedByLogin)}</div>`
                : ""
            }
          </td>
          <td>
            ${ClaudeApp.escapeHtml(ClaudeApp.planDisplayName(card.plan))}
            <div class="muted">Modelos liberados no app</div>
            <div class="muted">${ClaudeApp.brl.format(card.price)}/mês</div>
          </td>
          <td>
            ${ClaudeApp.integer.format(card.dailyLimit)} por dia
            <div class="muted">${ClaudeApp.usd.format(card.maxCostUsd)} de custo máximo</div>
          </td>
          <td>
            <span class="badge ${status === "Disponível" ? "ok" : "bad"}">${status}</span>
          </td>
          <td>
            <div class="row-actions">
              <button data-action="copy" data-id="${card.id}">Copiar</button>
              ${
                canPause
                  ? `<button data-action="toggle" data-id="${card.id}">
                      ${card.active ? "Pausar" : "Ativar"}
                    </button>`
                  : ""
              }
              <button data-action="delete" data-id="${card.id}">Excluir</button>
            </div>
          </td>
        </tr>
      `;
    })
    .join("");
}

function renderAccounts() {
  const accounts = ClaudeApp.accounts();
  ClaudeApp.saveAccounts(accounts);
  renderMetrics(accounts);
  renderGatewaySnippet(accounts);

  const table = document.querySelector("#accountsTable");
  if (!accounts.length) {
    table.innerHTML = `<tr><td colspan="5" class="muted">Nenhuma conta resgatada.</td></tr>`;
    return;
  }

  table.innerHTML = accounts
    .map((account) => {
      const remaining = Math.max(0, account.dailyLimit - account.usedToday);
      const isApiOnly = Boolean(account.apiOnly);
      const expiresAt = account.expiresAt || account.trialExpiresAt || "";
      const expirationLabel = expiresAt
        ? new Date(expiresAt).toLocaleString("pt-BR", {
            day: "2-digit",
            month: "2-digit",
            hour: "2-digit",
            minute: "2-digit",
          })
        : "";
      return `
        <tr>
          <td>
            <strong>${ClaudeApp.escapeHtml(account.name)}</strong>
            <div class="muted">${isApiOnly ? "Sem login de cliente" : ClaudeApp.escapeHtml(account.login)}</div>
            <div class="muted">${
              isApiOnly
                ? `API avulsa${expirationLabel ? ` ate ${ClaudeApp.escapeHtml(expirationLabel)}` : ""}`
                : `Gift card: ${ClaudeApp.escapeHtml(account.giftCardCode || "-")}`
            }</div>
            <div class="muted">API: ${ClaudeApp.escapeHtml(account.apiToken)}</div>
          </td>
          <td>
            ${ClaudeApp.escapeHtml(ClaudeApp.planDisplayName(account.plan))}
            <div class="muted">${isApiOnly ? "Modelo liberado na API" : "Modelos liberados no app"}</div>
            <div class="muted">${ClaudeApp.brl.format(account.price)}${isApiOnly ? "/teste" : "/mês"}</div>
          </td>
          <td>
            ${ClaudeApp.integer.format(account.usedToday)} usados
            <div class="muted">${ClaudeApp.integer.format(remaining)} restantes</div>
            <div class="muted">${ClaudeApp.integer.format(account.dailyLimit)} por dia</div>
          </td>
          <td>
            <span class="badge ${account.active ? "ok" : "bad"}">
              ${account.active ? "Ativo" : "Pausado"}
            </span>
          </td>
          <td>
            <div class="row-actions">
              <button data-action="copy-token" data-id="${account.id}">Copiar API</button>
              <button data-action="recharge" data-id="${account.id}">Recarregar</button>
              <button data-action="toggle" data-id="${account.id}">
                ${account.active ? "Pausar" : "Ativar"}
              </button>
              <button data-action="reset" data-id="${account.id}">Zerar uso</button>
              <button data-action="delete" data-id="${account.id}">Excluir</button>
            </div>
          </td>
        </tr>
      `;
    })
    .join("");
}

function supportBadge(ticket) {
  if (ticket.status === "waiting") return "Na fila";
  if (ticket.status === "active") return "Atendendo";
  return "Finalizado";
}

function renderSupportAdmin() {
  const queue = document.querySelector("#supportQueue");
  const title = document.querySelector("#activeSupportTitle");
  const messages = document.querySelector("#adminSupportMessages");
  const closeButton = document.querySelector("#closeSupportTicket");
  if (!queue || !title || !messages) return;

  queue.innerHTML = supportState.waiting.length
    ? supportState.waiting
        .map(
          (ticket, index) => `
            <article class="support-ticket">
              <strong>${index + 1}. ${ClaudeApp.escapeHtml(ticket.customerName)}</strong>
              <p>${ClaudeApp.escapeHtml(ticket.subject)}</p>
              <span>${ClaudeApp.escapeHtml(ticket.customerLogin)}</span>
              <button type="button" data-support-action="claim" data-id="${ticket.id}">
                ${supportState.active.length ? "Aguardando" : "Atender"}
              </button>
            </article>
          `,
        )
        .join("")
    : `<div class="support-empty">Fila vazia.</div>`;

  const active = supportState.active[0] || (activeSupportTicket?.status === "active" ? activeSupportTicket : null);
  activeSupportTicket = active;
  closeButton.disabled = !active;

  if (!active) {
    title.textContent = "Nenhum cliente em atendimento";
    messages.innerHTML = `<div class="support-empty">Quando assumir um cliente da fila, a conversa aparece aqui.</div>`;
    return;
  }

  title.textContent = `${active.customerName} - ${supportBadge(active)}`;
  messages.innerHTML = active.messages
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
  messages.scrollTop = messages.scrollHeight;
}

function renderAll() {
  renderGiftCards();
  renderAccounts();
  renderPurchases();
  renderSupportAdmin();
  renderProductionChecklist();
  renderBenchmark();
}

function uniqueGiftCard(values) {
  const cards = ClaudeApp.giftCards();
  let card = ClaudeApp.makeGiftCard(values);
  while (cards.some((item) => item.code === card.code)) {
    card = ClaudeApp.makeGiftCard({ ...values, code: "" });
  }
  return card;
}

function seedDemo() {
  const cards = ClaudeApp.giftCards();
  cards.unshift(
    uniqueGiftCard({
      code: "FRONTIER-DEMO-PRO",
      plan: "5X",
      price: 125,
      model: "sonnet",
      manualLimit: "",
      active: "true",
    }),
    uniqueGiftCard({
      code: "FRONTIER-DEMO-ULTRA",
      plan: "30X",
      price: 390,
      model: "opus",
      manualLimit: "1200000",
      active: "true",
    }),
  );
  ClaudeApp.saveGiftCards(cards);
  renderAll();
}

document.querySelector("#adminLoginForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const values = Object.fromEntries(new FormData(event.currentTarget).entries());
  document.querySelector("#adminLoginError").textContent = "";
  const settings = ClaudeApp.apiSettings();
  let authData;
  try {
    authData = await adminAuthRequest(adminSetupConfigured ? "/v1/admin/login" : "/v1/admin/setup", values);
  } catch (error) {
    document.querySelector("#adminLoginError").textContent = error.message;
    return;
  }

  const token = authData.admin?.token;
  if (!token) {
    document.querySelector("#adminLoginError").textContent = "Login admin nao retornou sessao.";
    return;
  }
  ClaudeApp.saveApiSettings({ ...settings, token });
  try {
    await refreshFromServer();
  } catch (error) {
    document.querySelector("#adminLoginError").textContent =
      error.fallback ? "API admin indisponível." : error.message;
    return;
  }
  rememberAdminDevice();
  showAdminApp();
  fillAdminApiTargetForm();
  renderAll();
  startSupportPolling();
});

document.querySelector("#adminLogout").addEventListener("click", () => {
  stopSupportPolling();
  forgetAdminDevice();
  location.reload();
});

document.querySelector("#adminApiTargetForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const values = Object.fromEntries(new FormData(event.currentTarget).entries());
  const message = document.querySelector("#adminApiTargetMessage");
  message.textContent = "";
  const settings = ClaudeApp.apiSettings();
  ClaudeApp.saveApiSettings({
    ...settings,
    baseUrl: values.baseUrl || settings.baseUrl,
  });
  try {
    await refreshFromServer();
    rememberAdminDevice();
    fillAdminApiTargetForm();
    renderAll();
    startSupportPolling();
  } catch (error) {
    message.textContent = error.fallback ? "API admin indisponível." : error.message;
  }
});

document.querySelector("#runBenchmark").addEventListener("click", async () => {
  const button = document.querySelector("#runBenchmark");
  button.disabled = true;
  button.textContent = "Rodando...";
  benchmarkState = { running: true };
  renderBenchmark();
  try {
    benchmarkState = await adminRequest("/v1/admin/benchmark", {
      method: "POST",
      body: JSON.stringify({}),
    });
  } catch (error) {
    const errorMessage = error.fallback ? "API admin indisponível." : error.message;
    benchmarkState = {
      errorMessage,
      summary: { status: "fail", passed: 0, failed: 1, warnings: 0, route_median_ms: 0 },
      results: [
        {
          category: "setup",
          id: "benchmark_request",
          label: "Benchmark indisponível",
          status: "FAIL",
          severity: "required",
          detail: errorMessage,
        },
      ],
      advice: ["Confira a URL da API e o token admin antes de rodar novamente."],
    };
  } finally {
    button.disabled = false;
    button.textContent = "Rodar benchmark";
    renderBenchmark();
  }
});

document.querySelectorAll("[data-admin-tab]").forEach((button) => {
  button.addEventListener("click", () => {
    setAdminTab(button.dataset.adminTab);
  });
});

giftCardForm.addEventListener("input", renderPreview);
apiTokenForm.addEventListener("input", renderApiPreview);

giftCardForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  document.querySelector("#giftCardError").textContent = "";

  const values = Object.fromEntries(new FormData(event.currentTarget).entries());
  const cards = ClaudeApp.giftCards();
  const requestedCode = ClaudeApp.normalizeGiftCode(values.code);
  if (requestedCode && cards.some((card) => card.code === requestedCode)) {
    document.querySelector("#giftCardError").textContent = "Esse gift card já existe.";
    return;
  }

  try {
    const data = await adminRequest("/v1/admin/gift-cards", {
      method: "POST",
      body: JSON.stringify(values),
    });
    cards.unshift(data.giftCard);
    ClaudeApp.saveGiftCards(cards);
  } catch (error) {
    if (!error.fallback) {
      document.querySelector("#giftCardError").textContent = error.message;
      return;
    }
    cards.unshift(uniqueGiftCard(values));
    ClaudeApp.saveGiftCards(cards);
  }

  event.currentTarget.reset();
  event.currentTarget.elements.plan.value = "Pro";
  event.currentTarget.elements.price.value = "65.00";
  event.currentTarget.elements.active.value = "true";
  renderPreview();
  renderAll();
});

apiTokenForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  document.querySelector("#apiTokenError").textContent = "";

  const values = Object.fromEntries(new FormData(event.currentTarget).entries());
  const accounts = ClaudeApp.accounts();
  try {
    const data = await adminRequest("/v1/admin/api-tokens", {
      method: "POST",
      body: JSON.stringify(values),
    });
    accounts.unshift(data.account);
    ClaudeApp.saveAccounts(accounts);
  } catch (error) {
    document.querySelector("#apiTokenError").textContent = error.fallback ? "API admin indisponível." : error.message;
    return;
  }

  event.currentTarget.reset();
  event.currentTarget.elements.name.value = "Fornecedor API";
  event.currentTarget.elements.price.value = "50.00";
  event.currentTarget.elements.durationHours.value = "28";
  event.currentTarget.elements.model.value = "opus";
  renderApiPreview();
  renderAll();
});

document.querySelector("#giftCardsTable").addEventListener("click", async (event) => {
  const button = event.target.closest("button");
  if (!button) return;

  const cards = ClaudeApp.giftCards();
  const index = cards.findIndex((card) => card.id === button.dataset.id);
  if (index < 0) return;

  if (button.dataset.action === "copy") {
    try {
      await navigator.clipboard.writeText(cards[index].code);
      button.textContent = "Copiado";
    } catch {
      prompt("Copie o gift card:", cards[index].code);
    }
    return;
  }

  if (button.dataset.action === "toggle" && !cards[index].usedByAccountId) {
    const nextActive = !cards[index].active;
    try {
      const data = await adminRequest(`/v1/admin/gift-cards/${cards[index].id}`, {
        method: "PATCH",
        body: JSON.stringify({ active: nextActive }),
      });
      cards[index] = data.giftCard;
    } catch (error) {
      if (!error.fallback) {
        alert(error.message);
        return;
      }
      cards[index].active = nextActive;
    }
  }

  if (button.dataset.action === "delete") {
    if (!confirm(`Excluir gift card ${cards[index].code}?`)) return;
    try {
      await adminRequest(`/v1/admin/gift-cards/${cards[index].id}`, { method: "DELETE" });
      cards.splice(index, 1);
    } catch (error) {
      if (!error.fallback) {
        alert(error.message);
        return;
      }
      cards.splice(index, 1);
    }
  }

  ClaudeApp.saveGiftCards(cards);
  renderAll();
});

document.querySelector("#accountsTable").addEventListener("click", async (event) => {
  const button = event.target.closest("button");
  if (!button) return;

  const accounts = ClaudeApp.accounts();
  const index = accounts.findIndex((account) => account.id === button.dataset.id);
  if (index < 0) return;

  if (button.dataset.action === "copy-token") {
    try {
      await navigator.clipboard.writeText(accounts[index].apiToken);
      button.textContent = "Copiado";
    } catch {
      prompt("Copie a API:", accounts[index].apiToken);
    }
    return;
  }

  if (button.dataset.action === "toggle") {
    const nextActive = !accounts[index].active;
    try {
      const data = await adminRequest(`/v1/admin/accounts/${accounts[index].id}`, {
        method: "PATCH",
        body: JSON.stringify({ active: nextActive }),
      });
      accounts[index] = data.account;
    } catch (error) {
      if (!error.fallback) {
        alert(error.message);
        return;
      }
      accounts[index].active = nextActive;
    }
  }

  if (button.dataset.action === "reset") {
    try {
      const data = await adminRequest(`/v1/admin/accounts/${accounts[index].id}`, {
        method: "PATCH",
        body: JSON.stringify({ resetUsage: true }),
      });
      accounts[index] = data.account;
    } catch (error) {
      if (!error.fallback) {
        alert(error.message);
        return;
      }
      accounts[index].usedToday = 0;
    }
  }

  if (button.dataset.action === "recharge") {
    const payload = accountRechargePayload(accounts[index]);
    if (!payload) return;
    try {
      const data = await adminRequest(`/v1/admin/accounts/${accounts[index].id}`, {
        method: "PATCH",
        body: JSON.stringify(payload),
      });
      accounts[index] = data.account;
    } catch (error) {
      if (!error.fallback) {
        alert(error.message);
        return;
      }
      const addTokens = payload.addTokens || ClaudeApp.calculateApiOnlyLimit(payload.rechargeBrl, 24).dailyLimit;
      accounts[index].dailyLimit += addTokens;
      accounts[index].computedDailyTokens += addTokens;
      accounts[index].manualLimit = accounts[index].dailyLimit;
      if (payload.rechargeBrl) accounts[index].price += payload.rechargeBrl;
    }
  }

  if (button.dataset.action === "delete") {
    if (!confirm(`Excluir ${accounts[index].login}?`)) return;
    try {
      await adminRequest(`/v1/admin/accounts/${accounts[index].id}`, { method: "DELETE" });
      accounts.splice(index, 1);
    } catch (error) {
      if (!error.fallback) {
        alert(error.message);
        return;
      }
      accounts.splice(index, 1);
    }
  }

  ClaudeApp.saveAccounts(accounts);
  renderAll();
});

document.querySelector("#purchasesTable").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-purchase-action]");
  if (!button) return;

  const index = purchaseState.findIndex((purchase) => purchase.id === button.dataset.id);
  if (index < 0) return;
  const action = button.dataset.purchaseAction;
  const endpoint = action === "approve" ? "approve" : "cancel";
  if (action === "approve" && !confirm(`Confirmar pagamento de ${purchaseState[index].login}?`)) return;
  if (action === "cancel" && !confirm(`Cancelar pedido de ${purchaseState[index].login}?`)) return;

  try {
    const data = await adminRequest(`/v1/admin/purchases/${purchaseState[index].id}/${endpoint}`, {
      method: "POST",
    });
    purchaseState[index] = data.purchase;
    await refreshFromServer();
  } catch (error) {
    alert(error.message);
  }
  renderAll();
});

document.querySelector("#supportQueue").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-support-action='claim']");
  if (!button || button.textContent.trim() === "Aguardando") return;
  document.querySelector("#adminSupportError").textContent = "";
  try {
    const data = await adminRequest(`/v1/admin/support/tickets/${button.dataset.id}/claim`, {
      method: "POST",
    });
    activeSupportTicket = data.ticket;
    await refreshSupportFromServer();
  } catch (error) {
    document.querySelector("#adminSupportError").textContent = error.message;
  }
});

document.querySelector("#adminSupportForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const message = form.elements.message.value.trim();
  if (!message || !activeSupportTicket) return;
  document.querySelector("#adminSupportError").textContent = "";
  try {
    const data = await adminRequest(`/v1/admin/support/tickets/${activeSupportTicket.id}/messages`, {
      method: "POST",
      body: JSON.stringify({ message }),
    });
    activeSupportTicket = data.ticket;
    form.reset();
    await refreshSupportFromServer();
  } catch (error) {
    document.querySelector("#adminSupportError").textContent = error.message;
  }
});

document.querySelector("#closeSupportTicket").addEventListener("click", async () => {
  if (!activeSupportTicket) return;
  document.querySelector("#adminSupportError").textContent = "";
  try {
    await adminRequest(`/v1/admin/support/tickets/${activeSupportTicket.id}/close`, {
      method: "POST",
    });
    activeSupportTicket = null;
    await refreshSupportFromServer();
  } catch (error) {
    document.querySelector("#adminSupportError").textContent = error.message;
  }
});

document.querySelector("#seedDemo").addEventListener("click", seedDemo);

renderPreview();
renderApiPreview();
fillAdminLoginForm();
fillAdminApiTargetForm();
loadAdminSetupStatus();

unlockRememberedAdminDevice().then((unlocked) => {
  if (!unlocked) renderAll();
});
