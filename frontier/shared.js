const ClaudeApp = (() => {
  const ACCOUNTS_KEY = "claude_frontier_accounts";
  const CLIENT_SESSION_KEY = "claude_frontier_client_session";
  const ADMIN_SESSION_KEY = "claude_frontier_admin_session";
  const API_SETTINGS_KEY = "claude_frontier_api_settings";
  const HISTORY_KEY = "claude_frontier_history";
  const PROJECTS_KEY = "claude_frontier_projects";
  const ARTIFACTS_KEY = "claude_frontier_artifacts";
  const GIFT_CARDS_KEY = "claude_frontier_gift_cards";
  const PURCHASES_KEY = "claude_frontier_purchases";

  const USD_TO_BRL = 5.5;
  const MIN_PROFIT_MARGIN = 0.5;
  const API_ONLY_PROFIT_MARGIN = 0.2;
  const PLAN_LIMIT_USD_PER_TOKEN = 0.00000087;
  const TOKEN_VALUE_MULTIPLIER = 8;

  const models = {
    haiku: {
      publicModel: "claude-code-pro",
      label: "Claude 4.5",
      usdPerToken: 0.000000224,
      tokenMultiplier: 1,
    },
    sonnet: {
      publicModel: "claude-code-pro",
      label: "Claude 4.5",
      usdPerToken: 0.00000087,
      tokenMultiplier: 1,
    },
    opus: {
      publicModel: "claude-code-ultra",
      label: "Claude Opus 4.7",
      usdPerToken: 0.00000087,
      tokenMultiplier: 1.5,
    },
  };

  const planCatalog = [
    {
      id: "free",
      name: "Grátis",
      description: "Para testar com respostas básicas.",
      price: 0,
      modelKey: "haiku",
      manualLimit: 1600,
      checkoutMode: "instant",
    },
    {
      id: "starter",
      name: "Pro",
      description: "Para conversas, estudos e tarefas do dia a dia.",
      price: 65,
      modelKey: "haiku",
      manualLimit: 128000,
      checkoutMode: "mercado_pago",
    },
    {
      id: "pro",
      name: "5X",
      description: "Mais limite e força para trabalho diário.",
      price: 125,
      modelKey: "sonnet",
      manualLimit: 400000,
      checkoutMode: "mercado_pago",
    },
    {
      id: "twentyx",
      name: "20X",
      description: "Mais força e limite para uso pesado.",
      price: 280,
      modelKey: "opus",
      manualLimit: 800000,
      checkoutMode: "mercado_pago",
    },
    {
      id: "ultra",
      name: "30X",
      description: "O maior limite para equipes e rotinas intensas.",
      price: 390,
      modelKey: "opus",
      manualLimit: 1200000,
      checkoutMode: "mercado_pago",
    },
  ];

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

  function clearAccounts() {
    localStorage.removeItem(ACCOUNTS_KEY);
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
    return aliases[modelKey] || "opus";
  }

  function normalizePublicModel(publicModel) {
    const value = String(publicModel || "").toLowerCase();
    if (value.includes("4.7") || value.includes("ultra") || value.includes("opus")) return models.opus.publicModel;
    if (value === "qwen-14b" || value.includes("qwen")) return models.sonnet.publicModel;
    if (value === "claude-code-ultra") return models.opus.publicModel;
    if (value.includes("claude-code")) return models.sonnet.publicModel;
    if (value.includes("haiku")) return models.sonnet.publicModel;
    if (value.includes("sonnet")) return models.sonnet.publicModel;
    if (value.includes("economy")) return models.sonnet.publicModel;
    return models.sonnet.publicModel;
  }

  function apiSettings() {
    const sameOriginApi =
      window.location.origin && window.location.origin !== "null"
        ? window.location.origin
        : "http://127.0.0.1:8787";
    const settings = load(API_SETTINGS_KEY, {
      baseUrl: sameOriginApi,
      token: "",
      model: models.sonnet.publicModel,
    });
    const storedBaseUrl = String(settings.baseUrl || "");
    const hostedSquare = /\.squareweb\.app$/i.test(window.location.hostname || "");
    const storedSquare = /\.squareweb\.app$/i.test((storedBaseUrl || "").replace(/^https?:\/\//i, "").split("/")[0]);
    const shouldUseSameOrigin =
      sameOriginApi !== "http://127.0.0.1:8787" &&
      (hostedSquare || storedSquare || storedBaseUrl.includes("127.0.0.1") || storedBaseUrl.includes("localhost"));
    return {
      ...settings,
      baseUrl: shouldUseSameOrigin ? sameOriginApi : settings.baseUrl || sameOriginApi,
      demoMode: false,
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

  function giftCards() {
    return load(GIFT_CARDS_KEY, []).map(recalculateGiftCard);
  }

  function saveGiftCards(value) {
    save(GIFT_CARDS_KEY, value.map(recalculateGiftCard));
  }

  function purchases() {
    return load(PURCHASES_KEY, []);
  }

  function savePurchases(value) {
    save(PURCHASES_KEY, value);
  }

  function calculateLimit(priceBrl, modelKey, manualLimit) {
    const monthlyRevenue = Math.max(0, Number(priceBrl) || 0);
    const manual = Number(manualLimit) || 0;
    if (monthlyRevenue <= 0 && manual > 0) {
      return {
        dailyLimit: manual,
        computedDailyTokens: manual,
        maxCostUsd: 0,
        protectedProfitBrl: 0,
      };
    }
    const protectedMargin = Math.max(0.5, MIN_PROFIT_MARGIN);
    const maxCostBrl = monthlyRevenue * (1 - protectedMargin);
    const maxCostUsd = maxCostBrl / USD_TO_BRL;
    const dailyCostUsd = maxCostUsd / 30;
    const computedDailyTokens = Math.floor(dailyCostUsd / PLAN_LIMIT_USD_PER_TOKEN);
    const dailyLimit = manual > 0 ? Math.min(manual, computedDailyTokens) : computedDailyTokens;

    return {
      dailyLimit: Math.max(0, dailyLimit),
      computedDailyTokens: Math.max(0, computedDailyTokens),
      maxCostUsd,
      protectedProfitBrl: monthlyRevenue * protectedMargin,
    };
  }

  function calculateApiOnlyLimit(priceBrl, durationHours = 24) {
    const revenue = Math.max(0, Number(priceBrl) || 0);
    const hours = Math.max(1, Number(durationHours) || 24);
    const maxCostBrl = revenue * (1 - API_ONLY_PROFIT_MARGIN);
    const maxCostUsd = maxCostBrl / USD_TO_BRL;
    const days = Math.max(1, Math.ceil(hours / 24));
    const rawTokens = Math.floor((maxCostUsd / days) / PLAN_LIMIT_USD_PER_TOKEN);
    const dailyLimit = Math.max(0, rawTokens * TOKEN_VALUE_MULTIPLIER);

    return {
      dailyLimit,
      computedDailyTokens: dailyLimit,
      maxCostUsd,
      protectedProfitBrl: revenue * API_ONLY_PROFIT_MARGIN,
    };
  }

  function recalculateAccount(account) {
    const price = Number(account.price) || 0;
    const isApiOnly = Boolean(account.apiOnly || account.giftCardCode === "__api_only__");
    if (isApiOnly) {
      const dailyLimit = Number(account.dailyLimit) || 0;
      return {
        ...account,
        apiOnly: true,
        expiresAt: account.expiresAt || account.trialExpiresAt || "",
        modelKey: normalizeModelKey(account.modelKey),
        dailyLimit,
        computedDailyTokens: Number(account.computedDailyTokens) || dailyLimit,
        maxCostUsd: Number(account.maxCostUsd) || 0,
      };
    }
    const trialExpiresAt = String(account.trialExpiresAt || "");
    const trialActive = Boolean(trialExpiresAt && new Date(trialExpiresAt).getTime() > Date.now());
    const isSignupFreeAccount = price <= 0 && !account.giftCardCode && !trialActive;
    const normalizedAccount = isSignupFreeAccount
      ? {
          ...account,
          plan: "Grátis",
          price: 0,
          modelKey: "haiku",
          manualLimit: 1600,
        }
      : account;
    const limit = calculateLimit(
      normalizedAccount.price,
      normalizedAccount.modelKey,
      normalizedAccount.manualLimit,
    );
    return {
      ...normalizedAccount,
      modelKey: normalizeModelKey(normalizedAccount.modelKey),
      dailyLimit: limit.dailyLimit,
      computedDailyTokens: limit.computedDailyTokens,
      maxCostUsd: limit.maxCostUsd,
    };
  }

  function normalizeGiftCode(code) {
    return String(code || "")
      .trim()
      .toUpperCase()
      .replace(/[^A-Z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "");
  }

  function generateGiftCode() {
    const chunk = () => Math.random().toString(36).slice(2, 6).toUpperCase();
    return `CLAUDE-${chunk()}-${chunk()}-${chunk()}`;
  }

  function recalculateGiftCard(card) {
    const limit = calculateLimit(card.price, card.modelKey, card.manualLimit);
    return {
      ...card,
      code: normalizeGiftCode(card.code),
      modelKey: normalizeModelKey(card.modelKey),
      dailyLimit: limit.dailyLimit,
      computedDailyTokens: limit.computedDailyTokens,
      maxCostUsd: limit.maxCostUsd,
    };
  }

  function makeGiftCard(values) {
    const modelKey = normalizeModelKey(values.model);
    return recalculateGiftCard({
      id: `gift_${Date.now()}_${Math.random().toString(16).slice(2)}`,
      code: normalizeGiftCode(values.code) || generateGiftCode(),
      plan: values.plan.trim() || "Plano Padrão",
      price: Number(values.price) || 0,
      modelKey,
      manualLimit: Number(values.manualLimit) || 0,
      active: values.active !== "false",
      usedByAccountId: "",
      usedByLogin: "",
      usedAt: "",
      createdAt: new Date().toISOString(),
    });
  }

  function makeAccount(values) {
    const account = {
      id: `acct_${Date.now()}_${Math.random().toString(16).slice(2)}`,
      name: values.name.trim(),
      displayName: (values.displayName || values.name).trim(),
      login: values.login.trim(),
      plan: values.plan.trim() || "Plano Padrão",
      price: Number(values.price) || 0,
      modelKey: normalizeModelKey(values.model),
      manualLimit: Number(values.manualLimit) || 0,
      active: values.active !== "false",
      giftCardCode: values.giftCardCode || "",
      usedToday: 0,
      createdAt: new Date().toISOString(),
    };
    if (values.apiToken) account.apiToken = values.apiToken;
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
      .filter((model, index, all) => all.findIndex((item) => item.publicModel === model.publicModel) === index)
      .map((model) => {
        const selected = model.publicModel === selectedModel ? "selected" : "";
        return `<option value="${model.publicModel}" ${selected}>${model.label}</option>`;
      })
      .join("");
  }

  function allowedPublicModelsForAccount(account) {
    return [models.sonnet.publicModel, models.opus.publicModel];
  }

  function modelOptionsForAccount(account, selectedPublicModel) {
    const selectedModel = normalizePublicModel(selectedPublicModel);
    const allowed = new Set(allowedPublicModelsForAccount(account));
    return Object.values(models)
      .filter((model, index, all) => all.findIndex((item) => item.publicModel === model.publicModel) === index)
      .map((model) => {
        const selected = model.publicModel === selectedModel ? "selected" : "";
        const disabled = allowed.has(model.publicModel) ? "" : "disabled";
        return `<option value="${model.publicModel}" ${selected} ${disabled}>${model.label}</option>`;
      })
      .join("");
  }

  function paidPlans() {
    return planCatalog.filter((plan) => plan.id !== "free");
  }

  function planDisplayName(plan) {
    const value = String(plan || "").trim();
    if (!value) return "Plano ativo";
    if (/claude\s+(haiku|sonnet|opus)|claude\s+code/i.test(value)) return "Plano ativo";
    return value;
  }

  return {
    ADMIN_SESSION_KEY,
    CLIENT_SESSION_KEY,
    accounts,
    saveAccounts,
    clearAccounts,
    apiSettings,
    saveApiSettings,
    history,
    saveHistory,
    projects,
    saveProjects,
    artifacts,
    saveArtifacts,
    giftCards,
    saveGiftCards,
    purchases,
    savePurchases,
    calculateLimit,
    calculateApiOnlyLimit,
    makeAccount,
    makeGiftCard,
    estimateTokens,
    escapeHtml,
    modelOptions,
    modelOptionsForAccount,
    allowedPublicModelsForAccount,
    paidPlans,
    planCatalog,
    planDisplayName,
    normalizeModelKey,
    normalizePublicModel,
    normalizeGiftCode,
    models,
    brl,
    usd,
    integer,
    MIN_PROFIT_MARGIN,
    API_ONLY_PROFIT_MARGIN,
  };
})();
