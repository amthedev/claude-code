const adminLogin = document.querySelector("#adminLogin");
const adminApp = document.querySelector("#adminApp");
const giftCardForm = document.querySelector("#giftCardForm");

function showAdminApp() {
  adminLogin.classList.add("hidden");
  adminApp.classList.remove("hidden");
}

function rememberAdminDevice() {
  localStorage.setItem(ClaudeApp.ADMIN_SESSION_KEY, "1");
}

function forgetAdminDevice() {
  localStorage.removeItem(ClaudeApp.ADMIN_SESSION_KEY);
  sessionStorage.removeItem(ClaudeApp.ADMIN_SESSION_KEY);
}

async function unlockRememberedAdminDevice() {
  try {
    await refreshFromServer();
  } catch {
    if (localStorage.getItem(ClaudeApp.ADMIN_SESSION_KEY) === "1") forgetAdminDevice();
    return false;
  }
  rememberAdminDevice();
  showAdminApp();
  renderAll();
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

async function refreshFromServer() {
  const [giftCards, accounts] = await Promise.all([
    adminRequest("/v1/admin/gift-cards"),
    adminRequest("/v1/admin/accounts"),
  ]);
  ClaudeApp.saveGiftCards(giftCards.data || []);
  ClaudeApp.saveAccounts(accounts.data || []);
}

function renderPreview() {
  const values = Object.fromEntries(new FormData(giftCardForm).entries());
  const limit = ClaudeApp.calculateLimit(values.price, values.model, values.manualLimit);
  document.querySelector("#previewTokens").textContent =
    `${ClaudeApp.integer.format(limit.dailyLimit)} tokens/dia`;
  document.querySelector("#previewCost").textContent = `${ClaudeApp.usd.format(limit.maxCostUsd)}/mês`;
}

function renderMetrics(accounts) {
  const active = accounts.filter((account) => account.active);
  const revenue = active.reduce((sum, account) => sum + account.price, 0);
  const usage = active.reduce((sum, account) => sum + account.usedToday, 0);
  const limits = active.reduce((sum, account) => sum + account.dailyLimit, 0);

  document.querySelector("#metricRevenue").textContent = ClaudeApp.brl.format(revenue);
  document.querySelector("#metricProfit").textContent = ClaudeApp.brl.format(
    revenue * ClaudeApp.MIN_PROFIT_MARGIN,
  );
  document.querySelector("#metricLimit").textContent = ClaudeApp.integer.format(limits);
  document.querySelector("#metricUsage").textContent = ClaudeApp.integer.format(usage);
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
        ClaudeApp.backendModelForPlan(account.modelKey),
        account.active ? "true" : "false",
      ].join("|"),
    )
    .join(";");
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
      const model = ClaudeApp.models[card.modelKey] || ClaudeApp.models.sonnet;
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
            ${ClaudeApp.escapeHtml(card.plan)}
            <div class="muted">${model.label}</div>
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
      const model = ClaudeApp.models[account.modelKey] || ClaudeApp.models.sonnet;
      return `
        <tr>
          <td>
            <strong>${ClaudeApp.escapeHtml(account.name)}</strong>
            <div class="muted">${ClaudeApp.escapeHtml(account.login)}</div>
            <div class="muted">Gift card: ${ClaudeApp.escapeHtml(account.giftCardCode || "-")}</div>
            <div class="muted">API: ${ClaudeApp.escapeHtml(account.apiToken)}</div>
          </td>
          <td>
            ${ClaudeApp.escapeHtml(account.plan)}
            <div class="muted">${model.label}</div>
            <div class="muted">${ClaudeApp.brl.format(account.price)}/mês</div>
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

function renderAll() {
  renderGiftCards();
  renderAccounts();
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
      code: "CLAUDE-DEMO-PRO",
      plan: "Claude Sonnet 4.6",
      price: 149.9,
      model: "sonnet",
      manualLimit: "",
      active: "true",
    }),
    uniqueGiftCard({
      code: "CLAUDE-DEMO-ULTRA",
      plan: "Claude Opus 4.7",
      price: 299.9,
      model: "opus",
      manualLimit: "45000",
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
  ClaudeApp.saveApiSettings({ ...settings, token: values.apiToken || settings.token });
  try {
    await refreshFromServer();
  } catch (error) {
    document.querySelector("#adminLoginError").textContent =
      error.fallback ? "API admin indisponível." : error.message;
    return;
  }
  rememberAdminDevice();
  showAdminApp();
  renderAll();
});

document.querySelector("#adminLogout").addEventListener("click", () => {
  forgetAdminDevice();
  location.reload();
});

document.querySelectorAll("[data-admin-tab]").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll("[data-admin-tab]").forEach((item) => item.classList.remove("active"));
    document.querySelectorAll(".admin-panel").forEach((panel) => panel.classList.remove("active"));
    button.classList.add("active");
    document.querySelector(`#${button.dataset.adminTab}`).classList.add("active");
  });
});

giftCardForm.addEventListener("input", renderPreview);

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
  event.currentTarget.elements.plan.value = "Claude Sonnet 4.6";
  event.currentTarget.elements.price.value = "149.90";
  event.currentTarget.elements.model.value = "sonnet";
  event.currentTarget.elements.active.value = "true";
  renderPreview();
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

document.querySelector("#seedDemo").addEventListener("click", seedDemo);

renderPreview();

unlockRememberedAdminDevice().then((unlocked) => {
  if (!unlocked) renderAll();
});
