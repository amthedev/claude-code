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
  const PLAN_LIMIT_USD_PER_TOKEN = 0.00000087;

  const models = {
    haiku: {
      publicModel: "claude-code-economy",
      label: "Claude Haiku 4.5",
      usdPerToken: 0.000000224,
    },
    sonnet: {
      publicModel: "claude-code-pro",
      label: "Claude Sonnet 4.6",
      usdPerToken: 0.00000087,
    },
    opus: {
      publicModel: "claude-code-ultra",
      label: "Claude Opus 4.7",
      usdPerToken: 0.00000087,
    },
  };

  const planCatalog = [
    {
      id: "free",
      name: "Grátis",
      description: "Para testar com respostas básicas.",
      price: 0,
      modelKey: "haiku",
      manualLimit: 200,
      checkoutMode: "instant",
    },
    {
      id: "starter",
      name: "Pro",
      description: "Para conversas, estudos e tarefas do dia a dia.",
      price: 65,
      modelKey: "haiku",
      manualLimit: 16000,
      checkoutMode: "mercado_pago",
    },
    {
      id: "pro",
      name: "5X",
      description: "Mais limite e força para trabalho diário.",
      price: 125,
      modelKey: "sonnet",
      manualLimit: 50000,
      checkoutMode: "mercado_pago",
    },
    {
      id: "twentyx",
      name: "20X",
      description: "Mais força e limite para uso pesado.",
      price: 280,
      modelKey: "opus",
      manualLimit: 100000,
      checkoutMode: "mercado_pago",
    },
    {
      id: "ultra",
      name: "30X",
      description: "O maior limite para equipes e rotinas intensas.",
      price: 390,
      modelKey: "opus",
      manualLimit: 150000,
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
    if (value.includes("claude-code-economy")) return models.haiku.publicModel;
    if (value.includes("claude-code-pro")) return models.sonnet.publicModel;
    if (value.includes("claude-code-ultra")) return models.opus.publicModel;
    if (value.includes("opus")) return models.opus.publicModel;
    if (value.includes("haiku")) return models.haiku.publicModel;
    if (value.includes("economy")) return models.haiku.publicModel;
    if (value.includes("ultra")) return models.opus.publicModel;
    return models.opus.publicModel;
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
    const shouldUseSameOrigin =
      sameOriginApi !== "http://127.0.0.1:8787" &&
      (storedBaseUrl.includes("127.0.0.1") || storedBaseUrl.includes("localhost"));
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

  function recalculateAccount(account) {
    const price = Number(account.price) || 0;
    const trialExpiresAt = String(account.trialExpiresAt || "");
    const trialActive = Boolean(trialExpiresAt && new Date(trialExpiresAt).getTime() > Date.now());
    const isSignupFreeAccount = price <= 0 && !account.giftCardCode && !trialActive;
    const normalizedAccount = isSignupFreeAccount
      ? {
          ...account,
          plan: "Grátis",
          price: 0,
          modelKey: "haiku",
          manualLimit: 200,
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
      .map((model) => {
        const selected = model.publicModel === selectedModel ? "selected" : "";
        return `<option value="${model.publicModel}" ${selected}>${model.label}</option>`;
      })
      .join("");
  }

  function allowedPublicModelsForAccount(account) {
    if (!account?.active) return [models.haiku.publicModel];
    const key = normalizeModelKey(account?.modelKey);
    if (key === "haiku") return [models.haiku.publicModel];
    if (key === "sonnet") return [models.haiku.publicModel, models.sonnet.publicModel];
    return [models.haiku.publicModel, models.sonnet.publicModel, models.opus.publicModel];
  }

  function modelOptionsForAccount(account, selectedPublicModel) {
    const selectedModel = normalizePublicModel(selectedPublicModel);
    const allowed = new Set(allowedPublicModelsForAccount(account));
    return Object.values(models)
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
  };
})();
