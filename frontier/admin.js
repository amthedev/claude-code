const adminLogin = document.querySelector("#adminLogin");
const adminApp = document.querySelector("#adminApp");
const accountForm = document.querySelector("#accountForm");

function showAdminApp() {
  adminLogin.classList.add("hidden");
  adminApp.classList.remove("hidden");
}

function renderPreview() {
  const values = Object.fromEntries(new FormData(accountForm).entries());
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

function renderAccounts() {
  const accounts = ClaudeApp.accounts();
  ClaudeApp.saveAccounts(accounts);
  renderMetrics(accounts);

  const table = document.querySelector("#accountsTable");
  if (!accounts.length) {
    table.innerHTML = `<tr><td colspan="5" class="muted">Nenhuma conta criada.</td></tr>`;
    return;
  }

  table.innerHTML = accounts
    .map((account) => {
      const remaining = Math.max(0, account.dailyLimit - account.usedToday);
      const model = ClaudeApp.models[account.modelKey] || ClaudeApp.models.pro;
      return `
        <tr>
          <td>
            <strong>${ClaudeApp.escapeHtml(account.name)}</strong>
            <div class="muted">${ClaudeApp.escapeHtml(account.login)}</div>
            <div class="muted">Senha: ${ClaudeApp.escapeHtml(account.password)}</div>
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

function seedDemo() {
  const demo = [
    ClaudeApp.makeAccount({
      name: "Ana Martins",
      displayName: "Ana",
      login: "ana@demo.com",
      password: "123456",
      plan: "Claude Sonnet 4.6",
      price: 149.9,
      model: "sonnet",
      manualLimit: "",
      active: "true",
    }),
    ClaudeApp.makeAccount({
      name: "Rafael Costa",
      displayName: "Rafael",
      login: "rafael@demo.com",
      password: "123456",
      plan: "Claude Opus 4.7",
      price: 299.9,
      model: "opus",
      manualLimit: "45000",
      active: "true",
    }),
  ];
  ClaudeApp.saveAccounts(demo);
  renderAccounts();
}

document.querySelector("#adminLoginForm").addEventListener("submit", (event) => {
  event.preventDefault();
  const values = Object.fromEntries(new FormData(event.currentTarget).entries());
  if (values.login !== "admin" || values.password !== "admin") {
    document.querySelector("#adminLoginError").textContent = "Login admin inválido.";
    return;
  }
  sessionStorage.setItem(ClaudeApp.ADMIN_SESSION_KEY, "1");
  showAdminApp();
  renderAccounts();
});

document.querySelector("#adminLogout").addEventListener("click", () => {
  sessionStorage.removeItem(ClaudeApp.ADMIN_SESSION_KEY);
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

accountForm.addEventListener("input", renderPreview);

accountForm.addEventListener("submit", (event) => {
  event.preventDefault();
  document.querySelector("#accountError").textContent = "";

  const values = Object.fromEntries(new FormData(event.currentTarget).entries());
  const accounts = ClaudeApp.accounts();
  const exists = accounts.some(
    (account) => account.login.toLowerCase() === values.login.trim().toLowerCase(),
  );
  if (exists) {
    document.querySelector("#accountError").textContent = "Esse login já existe.";
    return;
  }

  accounts.push(ClaudeApp.makeAccount(values));
  ClaudeApp.saveAccounts(accounts);
  event.currentTarget.reset();
  event.currentTarget.elements.plan.value = "Claude Sonnet 4.6";
  event.currentTarget.elements.price.value = "149.90";
  event.currentTarget.elements.model.value = "sonnet";
  event.currentTarget.elements.active.value = "true";
  renderPreview();
  renderAccounts();
});

document.querySelector("#accountsTable").addEventListener("click", (event) => {
  const button = event.target.closest("button");
  if (!button) return;

  const accounts = ClaudeApp.accounts();
  const index = accounts.findIndex((account) => account.id === button.dataset.id);
  if (index < 0) return;

  if (button.dataset.action === "toggle") {
    accounts[index].active = !accounts[index].active;
  }

  if (button.dataset.action === "reset") {
    accounts[index].usedToday = 0;
  }

  if (button.dataset.action === "delete") {
    if (!confirm(`Excluir ${accounts[index].login}?`)) return;
    accounts.splice(index, 1);
  }

  ClaudeApp.saveAccounts(accounts);
  renderAccounts();
});

document.querySelector("#seedDemo").addEventListener("click", seedDemo);

renderPreview();

if (sessionStorage.getItem(ClaudeApp.ADMIN_SESSION_KEY) === "1") {
  showAdminApp();
  renderAccounts();
}
