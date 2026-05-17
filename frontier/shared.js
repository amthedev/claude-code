const ClaudeApp = (() => {
  const ACCOUNTS_KEY = "claude_frontier_accounts";
  const CLIENT_SESSION_KEY = "claude_frontier_client_session";
  const ADMIN_SESSION_KEY = "claude_frontier_admin_session";
  const API_SETTINGS_KEY = "claude_frontier_api_settings";
  const HISTORY_KEY = "claude_frontier_history";
  const PROJECTS_KEY = "claude_frontier_projects";
  const ARTIFACTS_KEY = "claude_frontier_artifacts";

  const USD_TO_BRL = 5.5;
  const MIN_PROFIT_MARGIN = 0.5;

  const models = {
    haiku: {
      publicModel: "claude-haiku-4.5",
      label: "Claude Haiku 4.5",
      usdPerToken: 0.000000224,
    },
    sonnet: {
      publicModel: "claude-sonnet-4.6",
      label: "Claude Sonnet 4.6",
      usdPerToken: 0.00000087,
    },
    opus: {
      publicModel: "claude-opus-4.7",
      label: "Claude Opus 4.7",
      usdPerToken: 0.00000087,
    },
  };

  const brl = new Intl.NumberFormat("pt-BR", { style: "currency", currency: "BRL" });
  const usd = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });
  const integer = new Intl.NumberFormat("pt-BR");

  function load(key, fallback) {
    const raw = localStorage.getItem(key);
    if (!raw) return fallback;
    try {
      return JSON.parse(raw);
    } catch {
      return fallback;
    }
  }

  function save(key, value) {
    localStorage.setItem(key, JSON.stringify(value));
  }

  function accounts() {
    return load(ACCOUNTS_KEY, []).map(recalculateAccount);
  }

  function saveAccounts(value) {
    save(ACCOUNTS_KEY, value.map(recalculateAccount));
  }

  function normalizeModelKey(modelKey) {
    const aliases = {
      economy: "haiku",
      pro: "sonnet",
      ultra: "opus",
      ui: "sonnet",
      haiku: "haiku",
      sonnet: "sonnet",
      opus: "opus",
    };
    return aliases[modelKey] || "sonnet";
  }

  function normalizePublicModel(publicModel) {
    const value = String(publicModel || "").toLowerCase();
    if (value.includes("opus")) return models.opus.publicModel;
    if (value.includes("haiku")) return models.haiku.publicModel;
    if (value.includes("economy")) return models.haiku.publicModel;
    if (value.includes("ultra")) return models.opus.publicModel;
    return models.sonnet.publicModel;
  }

  function backendModelForPlan(modelKey) {
    const aliases = {
      haiku: "claude-code-economy",
      economy: "claude-code-economy",
      sonnet: "claude-code-pro",
      pro: "claude-code-pro",
      opus: "claude-code-ultra",
      ultra: "claude-code-ultra",
      ui: "claude-code-ui",
    };
    return aliases[modelKey] || "claude-code-pro";
  }

  function apiSettings() {
    const sameOriginApi =
      window.location.origin && window.location.origin !== "null"
        ? window.location.origin
        : "http://127.0.0.1:8787";
    const settings = load(API_SETTINGS_KEY, {
      baseUrl: sameOriginApi,
      token: "local-dev-token",
      model: models.sonnet.publicModel,
      demoMode: true,
    });
    return {
      ...settings,
      model: normalizePublicModel(settings.model),
    };
  }

  function saveApiSettings(value) {
    save(API_SETTINGS_KEY, value);
  }

  function history() {
    return load(HISTORY_KEY, []);
  }

  function saveHistory(value) {
    save(HISTORY_KEY, value);
  }

  function projects() {
    return load(PROJECTS_KEY, []);
  }

  function saveProjects(value) {
    save(PROJECTS_KEY, value);
  }

  function artifacts() {
    return load(ARTIFACTS_KEY, []);
  }

  function saveArtifacts(value) {
    save(ARTIFACTS_KEY, value);
  }

  function calculateLimit(priceBrl, modelKey, manualLimit) {
    const normalizedModelKey = normalizeModelKey(modelKey);
    const monthlyRevenue = Math.max(0, Number(priceBrl) || 0);
    const maxCostBrl = monthlyRevenue * (1 - MIN_PROFIT_MARGIN);
    const maxCostUsd = maxCostBrl / USD_TO_BRL;
    const dailyCostUsd = maxCostUsd / 30;
    const price = models[normalizedModelKey]?.usdPerToken || models.sonnet.usdPerToken;
    const computedDailyTokens = Math.floor(dailyCostUsd / price);
    const manual = Number(manualLimit) || 0;
    const dailyLimit = manual > 0 ? Math.min(manual, computedDailyTokens) : computedDailyTokens;

    return {
      dailyLimit: Math.max(0, dailyLimit),
      computedDailyTokens: Math.max(0, computedDailyTokens),
      maxCostUsd,
      protectedProfitBrl: monthlyRevenue * MIN_PROFIT_MARGIN,
    };
  }

  function recalculateAccount(account) {
    const limit = calculateLimit(account.price, account.modelKey, account.manualLimit);
    return {
      ...account,
      apiToken:
        account.apiToken ||
        `cus_${Math.random().toString(36).slice(2)}${Date.now().toString(36)}`,
      modelKey: normalizeModelKey(account.modelKey),
      dailyLimit: limit.dailyLimit,
      computedDailyTokens: limit.computedDailyTokens,
      maxCostUsd: limit.maxCostUsd,
    };
  }

  function makeAccount(values) {
    const account = {
      id: `acct_${Date.now()}_${Math.random().toString(16).slice(2)}`,
      apiToken:
        values.apiToken ||
        `cus_${Math.random().toString(36).slice(2)}${Date.now().toString(36)}`,
      name: values.name.trim(),
      displayName: (values.displayName || values.name).trim(),
      login: values.login.trim(),
      password: values.password,
      plan: values.plan.trim() || "Claude Sonnet 4.6",
      price: Number(values.price) || 0,
      modelKey: normalizeModelKey(values.model),
      manualLimit: Number(values.manualLimit) || 0,
      active: values.active !== "false",
      usedToday: 0,
      createdAt: new Date().toISOString(),
    };
    return recalculateAccount(account);
  }

  function estimateTokens(text) {
    return Math.ceil(String(text || "").length / 3.8) + 24;
  }

  function escapeHtml(value) {
    return String(value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function modelOptions(selectedPublicModel) {
    const selectedModel = normalizePublicModel(selectedPublicModel);
    return Object.values(models)
      .map((model) => {
        const selected = model.publicModel === selectedModel ? "selected" : "";
        return `<option value="${model.publicModel}" ${selected}>${model.label}</option>`;
      })
      .join("");
  }

  function demoReply(account, prompt, usedTokens) {
    return [
      "Resposta em modo demo do Claude.",
      "",
      `Plano: ${account.plan}`,
      `Modelo: ${models[normalizeModelKey(account.modelKey)]?.label || "Claude Sonnet 4.6"}`,
      `Tokens reservados nesta mensagem: ${integer.format(usedTokens)}`,
      "",
      "Para usar a IA real, abra API, configure a Claude Code API local e desative ou mantenha o fallback demo.",
      "",
      `Pedido recebido: ${prompt}`,
    ].join("\n");
  }

  return {
    ADMIN_SESSION_KEY,
    CLIENT_SESSION_KEY,
    accounts,
    saveAccounts,
    apiSettings,
    saveApiSettings,
    history,
    saveHistory,
    projects,
    saveProjects,
    artifacts,
    saveArtifacts,
    calculateLimit,
    makeAccount,
    estimateTokens,
    escapeHtml,
    modelOptions,
    normalizeModelKey,
    normalizePublicModel,
    backendModelForPlan,
    demoReply,
    models,
    brl,
    usd,
    integer,
    MIN_PROFIT_MARGIN,
  };
})();
