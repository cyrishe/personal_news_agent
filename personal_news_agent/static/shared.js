let activeUserId = localStorage.getItem("pna_user_id") || "default";
let conversationId = localStorage.getItem("pna_conversation_id") || null;
const savedWebSearchPreference = localStorage.getItem(webSearchPreferenceKey());
let webSearchEnabled = savedWebSearchPreference === null ? true : savedWebSearchPreference === "1";
const DEFAULT_CHAT_MODEL_KEY = "yuanrong-personal-assistant";
const FALLBACK_CHAT_MODELS = [
  { key: DEFAULT_CHAT_MODEL_KEY, name: "元融大模型", description: "默认个人资讯助理角色" },
  { key: "qwen3.6", name: "Qwen 3.6", description: "层次清楚的通用分析角色" },
  { key: "deepseek-v4-flash", name: "DeepSeek V4 Flash", description: "快速直接的资讯问答角色" },
];
let chatModelOptions = FALLBACK_CHAT_MODELS;
const APP_BASE_PATH = (() => {
  const prefix = "/pna";
  return window.location.pathname === prefix || window.location.pathname.startsWith(`${prefix}/`) ? prefix : "";
})();
const TOPIC_DRIFT_NOTICE = "提示：这条追问和当前关注主题关联较弱，我会照常回答，但不会因此更改当前主题或新增关注卡片。";
const MULTI_FOCUS_DRIFT_NOTICE = "提示：这条消息里包含多个彼此关联较弱的热点，我会照常分别回答，但不会把它们合并成同一个主题或新增关注卡片。";
const TOPIC_DRIFT_NOTICES = [TOPIC_DRIFT_NOTICE, MULTI_FOCUS_DRIFT_NOTICE];

function webSearchPreferenceKey() {
  return `pna_web_search_enabled:${activeUserId || "default"}`;
}

function isWebSearchEnabled() {
  return webSearchEnabled;
}

function setWebSearchEnabled(enabled) {
  webSearchEnabled = Boolean(enabled);
  localStorage.setItem(webSearchPreferenceKey(), webSearchEnabled ? "1" : "0");
  document.querySelectorAll("[data-web-search-toggle]").forEach((input) => {
    input.checked = webSearchEnabled;
  });
  if (window.currentChatContext) window.currentChatContext.allow_web_search = webSearchEnabled;
}

function bindWebSearchToggles() {
  document.querySelectorAll("[data-web-search-toggle]").forEach((input) => {
    input.checked = webSearchEnabled;
    input.addEventListener("change", () => setWebSearchEnabled(input.checked));
  });
}

function chatModelPreferenceKey() {
  return `pna_chat_model:${activeUserId || "default"}`;
}

function getChatModelKey() {
  const saved = localStorage.getItem(chatModelPreferenceKey());
  return chatModelOptions.some((item) => item.key === saved) ? saved : DEFAULT_CHAT_MODEL_KEY;
}

function setChatModelKey(modelKey) {
  const resolved = chatModelOptions.some((item) => item.key === modelKey) ? modelKey : DEFAULT_CHAT_MODEL_KEY;
  localStorage.setItem(chatModelPreferenceKey(), resolved);
  document.querySelectorAll("[data-chat-model-select]").forEach((select) => {
    select.value = resolved;
  });
  const selected = chatModelOptions.find((item) => item.key === resolved) || chatModelOptions[0];
  document.querySelectorAll("[data-chat-model-hint]").forEach((node) => {
    node.textContent = resolved === DEFAULT_CHAT_MODEL_KEY
      ? `默认 · ${selected?.description || "个人资讯助理"}`
      : selected?.description || "对话角色";
  });
  if (window.currentChatContext) window.currentChatContext.model_key = resolved;
  return resolved;
}

async function loadChatModelSelectors() {
  try {
    const data = await request("/api/models");
    if (Array.isArray(data.items) && data.items.length) chatModelOptions = data.items;
  } catch (error) {
    chatModelOptions = FALLBACK_CHAT_MODELS;
  }
  const selectedKey = getChatModelKey();
  document.querySelectorAll("[data-chat-model-select]").forEach((select) => {
    select.innerHTML = chatModelOptions
      .map((item) => `<option value="${escapeAttr(item.key)}">${escapeHtml(item.name)}</option>`)
      .join("");
    select.value = selectedKey;
    select.addEventListener("change", () => setChatModelKey(select.value));
  });
  setChatModelKey(selectedKey);
}

bindWebSearchToggles();
loadChatModelSelectors();

function appUrl(path) {
  if (!path || !path.startsWith("/") || path.startsWith("//") || !APP_BASE_PATH) return path;
  if (path === APP_BASE_PATH || path.startsWith(`${APP_BASE_PATH}/`)) return path;
  return `${APP_BASE_PATH}${path}`;
}

async function request(path, options = {}) {
  const { timeoutMs = 0, timeoutMessage = "请求超时，请稍后重试。", ...fetchOptions } = options;
  const controller = timeoutMs > 0 ? new AbortController() : null;
  const timer = controller ? window.setTimeout(() => controller.abort(), timeoutMs) : null;
  try {
    const response = await fetch(appUrl(path), {
      headers: { "Content-Type": "application/json" },
      ...fetchOptions,
      ...(controller ? { signal: controller.signal } : {}),
    });
    if (!response.ok) throw new Error(await formatApiError(response));
    return response.json();
  } catch (error) {
    if (controller?.signal.aborted) throw new Error(timeoutMessage);
    throw error;
  } finally {
    if (timer !== null) window.clearTimeout(timer);
  }
}

async function formatApiError(response) {
  const fallback = `请求失败：${response.status}`;
  let payload;
  try {
    payload = await response.json();
  } catch (error) {
    try {
      return (await response.text()) || fallback;
    } catch (innerError) {
      return fallback;
    }
  }
  const detail = payload?.detail;
  if (Array.isArray(detail)) {
    return detail.map(validationErrorMessage).join("；") || fallback;
  }
  if (typeof detail === "string") return friendlyErrorMessage(detail);
  if (detail && typeof detail.message === "string") return friendlyErrorMessage(detail.message);
  if (detail) return friendlyErrorMessage(JSON.stringify(detail));
  return fallback;
}

function validationErrorMessage(item) {
  const field = Array.isArray(item.loc) ? item.loc[item.loc.length - 1] : "";
  const labels = {
    username: "用户名",
    password: "密码",
    confirm_password: "确认密码",
    real_name: "真实姓名",
    mobile: "手机号",
    challenge_id: "验证码凭据",
    verification_code: "短信验证码",
  };
  const label = labels[field] || field || "输入";
  if (item.type === "string_too_short" && item.ctx?.min_length) return `${label}至少需要 ${item.ctx.min_length} 个字符`;
  if (item.type === "string_too_long" && item.ctx?.max_length) return `${label}不能超过 ${item.ctx.max_length} 个字符`;
  return `${label}格式不正确`;
}

function friendlyErrorMessage(message) {
  const known = {
    "username already registered": "用户名已注册，请换一个用户名或直接登录。",
    "mobile already registered": "手机号已注册，请直接登录或换一个手机号。",
    "password and confirm_password do not match": "两次输入的密码不一致。",
    "mobile format is invalid": "手机号格式不正确，请输入 11 位中国大陆手机号。",
    "username must be at least 3 characters": "用户名至少需要 3 个字符。",
    "invalid email or password": "用户名或密码不正确。",
  };
  return known[message] || message;
}

async function runFormAction(form, statusSelector, loadingText, action) {
  const status = document.querySelector(statusSelector);
  const buttons = Array.from(form.querySelectorAll("button"));
  if (status) status.textContent = loadingText;
  form.setAttribute("aria-busy", "true");
  buttons.forEach((button) => {
    button.disabled = true;
  });
  try {
    return await action();
  } finally {
    form.removeAttribute("aria-busy");
    buttons.forEach((button) => {
      button.disabled = false;
    });
  }
}

function saveSession(authResult) {
  if (!authResult || !authResult.user) return;
  activeUserId = authResult.user.id;
  localStorage.setItem("pna_user_id", activeUserId);
  localStorage.setItem("pna_user_name", authResult.user.display_name || "");
  if (authResult.session) {
    localStorage.setItem("pna_session_token", authResult.session.token);
  }
  renderUser();
  loadChatModelSelectors();
}

function renderUser() {
  document.querySelectorAll("[data-user-name]").forEach((node) => {
    node.textContent = localStorage.getItem("pna_user_name") || (activeUserId === "default" ? "未注册用户" : activeUserId);
  });
}

function itemHtml(item) {
  return `<article class="item">
    <div class="title">${escapeHtml(item.title)}</div>
    <div class="meta">${escapeHtml(item.source || item.source_id)} · ${escapeHtml(item.category)} · ${escapeHtml(item.recommend_reason || "")}</div>
    <div class="summary">${escapeHtml(item.summary || "")}</div>
  </article>`;
}

function prototypeItemHtml(item, actionText = "追问") {
  const ask = `${item.title || ""} 继续展开说说`;
  return `<article class="prototype-item">
    <div>
      <div class="title">${escapeHtml(item.title)}</div>
      <div class="meta">${escapeHtml(item.source || item.source_id)} · ${escapeHtml(item.category)}</div>
      <div class="summary">${escapeHtml(item.summary || item.recommend_reason || "")}</div>
    </div>
    <button data-ask="${escapeAttr(ask)}">${escapeHtml(actionText)}</button>
  </article>`;
}

function eventHtml(item) {
  return `<article class="item" data-event-title="${escapeAttr(item.title || "")}" data-event-url="${escapeAttr(item.source_url || "")}" data-event-source-title="${escapeAttr(item.source_title || "")}">
    <div class="title">${escapeHtml(item.title)}</div>
    <div class="meta">${escapeHtml(item.category)} · ${item.article_count}篇 · 热度${item.hot_score}</div>
    <div class="summary">${escapeHtml((item.keywords || []).join(" / "))}</div>
  </article>`;
}

async function registerFromForm(form) {
  const formData = new FormData(form);
  const payload = {
    mobile: formData.get("mobile"),
    challenge_id: formData.get("challenge_id"),
    verification_code: formData.get("verification_code"),
    password: formData.get("password"),
    confirm_password: formData.get("confirm_password"),
  };
  const result = await request("/api/auth/register", {
    method: "POST",
    body: JSON.stringify(payload),
    timeoutMs: 20000,
    timeoutMessage: "注册服务响应超时，请稍后重试。",
  });
  saveSession(result);
  return result;
}

function ensureRegistrationChallenge(form, status) {
  if (String(form?.elements.challenge_id?.value || "").trim()) return true;
  if (status) status.textContent = "请先获取短信验证码。";
  return false;
}

async function requestRegistrationCodeFromForm(form) {
  const mobile = String(new FormData(form).get("mobile") || "").trim();
  const result = await request("/api/auth/registration-code", {
    method: "POST",
    body: JSON.stringify({ mobile }),
    timeoutMs: 15000,
    timeoutMessage: "短信服务响应超时，请稍后重试。",
  });
  form.elements.challenge_id.value = result.challenge_id || "";
  form.dataset.challengeMobile = mobile;
  if (form.elements.verification_code) form.elements.verification_code.value = "";
  return result;
}

function bindRegistrationCodeForm(formSelector, statusSelector) {
  const form = document.querySelector(formSelector);
  const button = form?.querySelector("[data-send-registration-code]");
  const mobile = form?.elements.mobile;
  const status = document.querySelector(statusSelector);
  if (!form || !button || !mobile) return;

  request("/api/auth/config")
    .then((config) => {
      form.dataset.phoneRegistrationAvailable = config.available ? "1" : "0";
      if (config.available) return;
      if (status) status.textContent = "短信服务暂时不可用，你仍可点击发送重试；如持续失败请联系管理员。";
    })
    .catch(() => {
      form.dataset.phoneRegistrationAvailable = "unknown";
      if (status && !status.textContent) status.textContent = "暂时无法读取短信服务状态，你可以点击发送重试。";
    });

  mobile.addEventListener("input", () => {
    if (form.dataset.challengeMobile && mobile.value.trim() !== form.dataset.challengeMobile) {
      form.elements.challenge_id.value = "";
      form.dataset.challengeMobile = "";
      if (form.elements.verification_code) form.elements.verification_code.value = "";
    }
  });
  button.addEventListener("click", async () => {
    if (button.disabled) return;
    button.disabled = true;
    if (status) status.textContent = "正在发送短信验证码。";
    try {
      const result = await requestRegistrationCodeFromForm(form);
      const debug = result.debug_code ? `；本地测试验证码：${result.debug_code}` : "";
      if (status) status.textContent = `验证码已发送至 ${result.mobile_masked || "该手机号"}${debug}`;
      startRegistrationCodeCountdown(button, result.resend_after_seconds || 60);
    } catch (error) {
      if (status) status.textContent = error.message;
      button.disabled = false;
    }
  });
}

function startRegistrationCodeCountdown(button, seconds) {
  let remaining = Math.max(1, Number(seconds) || 60);
  const original = button.dataset.defaultLabel || button.textContent || "发送验证码";
  button.dataset.defaultLabel = original;
  button.disabled = true;
  button.textContent = `${remaining}s 后重发`;
  const timer = window.setInterval(() => {
    remaining -= 1;
    if (remaining <= 0) {
      window.clearInterval(timer);
      button.disabled = false;
      button.textContent = original;
      return;
    }
    button.textContent = `${remaining}s 后重发`;
  }, 1000);
}

async function loadOnboardingOptions(formSelector) {
  const form = document.querySelector(formSelector);
  if (!form) return;
  const data = await request("/api/onboarding/options");
  const categories = form.querySelector("[data-category-options]");
  if (categories) {
    categories.innerHTML = data.categories
      .map((item) => {
        const checked = item.implemented && data.default_categories.includes(item.key) ? "checked" : "";
        const disabled = item.implemented ? "" : "disabled aria-disabled=\"true\"";
        const optionClass = item.implemented ? "" : " class=\"future-option\"";
        const disabledLabel = item.implemented ? "" : "（后续）";
        return `<label${optionClass}><input type="checkbox" name="preferred_categories" value="${escapeAttr(item.key)}" ${checked} ${disabled} /> ${escapeHtml(item.name)}${disabledLabel}</label>`;
      })
      .join("");
  }
  const modelSelect = form.querySelector("[name=model_key]");
  if (modelSelect) {
    modelSelect.innerHTML = data.models
      .map((item) => `<option value="${escapeAttr(item.key)}">${escapeHtml(item.name)}</option>`)
      .join("");
    modelSelect.value = data.default_model;
  }
  const styleSelect = form.querySelector("[name=output_style]");
  if (styleSelect && data.output_styles) {
    styleSelect.innerHTML = data.output_styles.map((item) => `<option value="${escapeAttr(item.name)}">${escapeHtml(item.name)}</option>`).join("");
  }
}

async function loadProfileIntoForm(formSelector) {
  const form = document.querySelector(formSelector);
  if (!form || !activeUserId || activeUserId === "default") return null;
  const data = await request(`/api/profile?user_id=${encodeURIComponent(activeUserId)}`);
  const profile = data.profile || {};
  if (form.elements.display_name && data.user) form.elements.display_name.value = data.user.display_name || "";
  if (form.elements.self_description) form.elements.self_description.value = profile.self_description || "";
  if (form.elements.age) form.elements.age.value = profile.age || "";
  if (form.elements.gender) form.elements.gender.value = profile.gender || "不透露";
  if (form.elements.zodiac) form.elements.zodiac.value = profile.zodiac || "不透露";
  if (form.elements.watch_keywords) form.elements.watch_keywords.value = (profile.interests || []).join(", ");
  if (form.elements.negative_keywords) form.elements.negative_keywords.value = (profile.negative_interests || []).join(", ");
  if (form.elements.model_key && profile.model_key) form.elements.model_key.value = profile.model_key;
  if (form.elements.output_style && profile.output_style) form.elements.output_style.value = profile.output_style;
  form.querySelectorAll("[name=preferred_categories]").forEach((input) => {
    input.checked = (profile.preferred_categories || []).includes(input.value);
  });
  return data;
}

async function completeOnboardingFromForm(form) {
  const formData = new FormData(form);
  const payload = {
    user_id: activeUserId,
    display_name: formData.get("display_name") || null,
    self_description: formData.get("self_description") || "",
    age: formData.get("age") ? Number(formData.get("age")) : null,
    gender: formData.get("gender") || "不透露",
    zodiac: formData.get("zodiac") || "不透露",
    preferred_categories: formData.getAll("preferred_categories"),
    watch_keywords: splitKeywords(formData.get("watch_keywords")),
    negative_keywords: splitKeywords(formData.get("negative_keywords")),
    model_key: formData.get("model_key") || "yuanrong-personal-assistant",
    output_style: formData.get("output_style") || "简洁分析型",
  };
  const result = await request("/api/onboarding/complete", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  if (payload.display_name) {
    localStorage.setItem("pna_user_name", payload.display_name);
    renderUser();
  }
  setChatModelKey(payload.model_key);
  return result;
}

function splitKeywords(value) {
  return String(value || "")
    .split(/[,，\s]+/)
    .map((item) => item.trim())
    .filter(Boolean);
}

async function loadPrototypeFeeds() {
  const data = await request(`/api/feed?limit=8&user_id=${encodeURIComponent(activeUserId)}`);
  const items = data.items || [];
  renderPrototypeList("[data-story-feed]", items.slice(0, 4), "发酵");
  renderPrototypeList("[data-radar-feed]", items.slice(0, 5), "定位");
  renderPrototypeList("[data-brief-feed]", items.slice(0, 4), "播报");
  renderPrototypeList("[data-source-feed]", items.slice(0, 6), "引用");
}

function renderPrototypeList(selector, items, actionText) {
  document.querySelectorAll(selector).forEach((target) => {
    target.innerHTML = items.map((item) => prototypeItemHtml(item, actionText)).join("");
  });
}

async function loginFromForm(form) {
  const formData = new FormData(form);
  const result = await request("/api/auth/login", {
    method: "POST",
    body: JSON.stringify({ mobile: formData.get("mobile"), password: formData.get("password") }),
  });
  saveSession(result);
  return result;
}

async function renderRealNameStatus(targetSelector) {
  const target = document.querySelector(targetSelector);
  if (!target) return;
  const status = await request("/api/auth/realname/status");
  target.textContent =
    status.provider === "mock"
      ? "实名手机认证：演示环境使用 mock 核验；正式环境需接入运营商三要素服务。"
      : `实名手机认证：${status.provider_name || status.provider}`;
}

async function renderWechatStatus(targetSelector) {
  const target = document.querySelector(targetSelector);
  if (!target) return;
  const status = await request("/api/auth/wechat/status");
  if (!status.configured) {
    target.textContent = "微信登录未配置：需要 WECHAT_APP_ID、WECHAT_APP_SECRET、WECHAT_REDIRECT_URI。";
    return;
  }
  const login = await request("/api/auth/wechat/login-url");
  target.innerHTML = `<a class="button-link" href="${escapeAttr(login.url)}">微信登录</a>`;
}

async function loadFeed(category = "", limit = 10, target = "#feed") {
  const data = await request(`/api/feed?limit=${limit}&user_id=${encodeURIComponent(activeUserId)}${category ? `&category=${category}` : ""}`);
  if (target === "#feed") {
    const count = document.querySelector("[data-related-article-count]");
    if (count) count.textContent = `${(data.items || []).length} 条`;
  }
  document.querySelector(target).innerHTML = data.items.map(itemHtml).join("");
}

async function loadEvents(target = "#events", category = "", limit = 8) {
  const data = await request(`/api/events?limit=${limit}${category ? `&category=${category}` : ""}`);
  document.querySelector(target).innerHTML = data.items.map(eventHtml).join("");
}

async function sendChat(message, target = "#messages") {
  const targetNode = document.querySelector(target) || document.querySelector("#messages");
  targetNode.classList.add("chat-stream");
  const userNode = chatTurn("user", message);
  targetNode.appendChild(userNode);
  const assistantNode = chatTurn("assistant", "", true);
  targetNode.appendChild(assistantNode);
  scrollChatToBottom(targetNode);
  return sendChatIntoTurn(message, assistantNode, target);
}

async function sendChatIntoTurn(message, assistantNode, target = "#messages") {
  const chatContext = window.currentChatContext || {};
  const targetNode = document.querySelector(target) || document.querySelector("#messages");
  targetNode.classList.add("chat-stream");
  const payload = {
    conversation_id: conversationId,
    user_id: activeUserId || "default",
    message,
    topic: chatContext.topic || null,
    category_scope: chatContext.category_scope || null,
    use_llm: Boolean(chatContext.use_llm),
    allow_web_search: Boolean(chatContext.allow_web_search),
    model_key: chatContext.model_key || getChatModelKey(),
  };
  let streamError = null;
  try {
    const streamed = await streamChat(payload, assistantNode, targetNode);
    if (streamed) return streamed;
  } catch (error) {
    streamError = error;
    assistantNode.innerHTML = chatTransportStatusHtml(
      "流式连接中断，正在继续等待完整结果…",
      "本轮问题已经提交，不需要重新输入。",
    );
    scrollChatToBottom(targetNode, "auto");
  }
  try {
    const data = await request("/api/chat", {
      method: "POST",
      body: JSON.stringify(payload),
      timeoutMs: 180_000,
      timeoutMessage: "生成结果超时，请稍后重新发送。",
    });
    conversationId = data.conversation_id;
    localStorage.setItem("pna_conversation_id", conversationId);
    setAssistantResponseHtml(assistantNode, chatResponseHtml(data));
    scrollChatToBottom(targetNode);
    syncChatResponseContext(data);
    await notifyConversationHistoryChanged();
    return data;
  } catch (error) {
    const reason = error?.message || streamError?.message || "请求失败，请稍后重试。";
    setAssistantResponseHtml(assistantNode, chatTransportErrorHtml(reason, message));
    scrollChatToBottom(targetNode, "auto");
    return null;
  }
}

function chatTransportStatusHtml(title, detail = "") {
  return `${assistantIdentityHtml("连接恢复中")}
    <section class="chat-transport-state" role="status" aria-live="polite">
      <strong>${escapeHtml(title)}</strong>
      ${detail ? `<p>${escapeHtml(detail)}</p>` : ""}
    </section>`;
}

function chatTransportErrorHtml(message, retryMessage) {
  return `<section class="chat-transport-state chat-transport-error" role="alert">
    <strong>本轮未能完成</strong>
    <p>${escapeHtml(message)}</p>
    <button type="button" class="secondary-button" data-chat-retry="${escapeAttr(retryMessage)}">重新发送</button>
  </section>`;
}

function focusFromChatMessage(message) {
  const text = String(message || "");
  const quoted =
    text.match(/围绕热点事件[“"《](.+?)[”"》]/) ||
    text.match(/基于资讯[“"《](.+?)[”"》]/);
  if (!quoted) return "";
  return cleanFocusTitle(quoted[1]);
}

function cleanFocusTitle(value) {
  let text = String(value || "").trim();
  text = text.replace(/^.*?相关热点[:：]/, "");
  text = text.replace(/^(围绕)?热点事件/, "");
  text = text.replace(/展开.*$/, "");
  text = text.replace(/[-—]+(中新网|人民网|新华网|央视网|中国新闻网|中国共产党新闻网)\s*$/, "");
  return text.trim().slice(0, 120);
}

async function notifyConversationHistoryChanged() {
  const refreshers = [window.refreshTopics].filter(
    (refresh) => typeof refresh === "function",
  );
  for (const refresh of refreshers) {
    try {
      await refresh();
    } catch (error) {
      // 回答已经保存，侧栏刷新失败不应影响本轮回答。
    }
  }
}

function chatMemoryKey() {
  const page = window.location.pathname || "/";
  return `pna_chat_memory:${activeUserId || "default"}:${page}`;
}

async function restoreChatMemory(target = "#messages") {
  if (!conversationId) return false;
  const targetNode = document.querySelector(target) || document.querySelector("#messages");
  if (!targetNode) return false;
  try {
    const data = await request(
      `/api/chat/conversations/${encodeURIComponent(conversationId)}?user_id=${encodeURIComponent(activeUserId || "default")}&limit=40`,
    );
    if (!data.turns?.length) return false;
    targetNode.innerHTML = "";
    targetNode.classList.add("chat-stream");
    data.turns.forEach((turn) => {
      targetNode.appendChild(chatTurn("user", turn.user_message || ""));
      const node = chatTurn("assistant", "");
      setAssistantResponseHtml(node, chatResponseHtml(turn.response || {
        conversation_id: conversationId,
        answer: turn.assistant_answer || "",
        context_relation: "restored_history",
      }));
      targetNode.appendChild(node);
    });
    localStorage.removeItem(chatMemoryKey());
    if (typeof window.applyChatConversationContext === "function") {
      window.applyChatConversationContext(data.context || {});
    }
    scrollChatToBottom(targetNode, "auto");
    return true;
  } catch (error) {
    return false;
  }
}

function syncChatResponseContext(response) {
  localStorage.removeItem(chatMemoryKey());
  if (isTopicChatResponse(response) && typeof window.applyChatConversationContext === "function") {
    window.applyChatConversationContext({
      topic: response?.topic || response?.focus_object?.text || null,
      category_scope: response?.category_scope || [],
    });
  }
  if (typeof window.handleChatResponseSideEffects === "function") {
    Promise.resolve(window.handleChatResponseSideEffects(response)).catch(() => {});
  }
}

function isTopicChatResponse(response) {
  if (!response) return false;
  if (response.context_relation === "query_moderation_blocked") return false;
  if (response.focus_object?.type === "topic") return Boolean(response.topic || response.focus_object?.text);
  return response.context_relation === "topic_agent_created";
}

function appendLocalTurn(role, text, target = "#messages", loading = false) {
  const targetNode = document.querySelector(target) || document.querySelector("#messages");
  targetNode.classList.add("chat-stream");
  const node = loading ? chatTurn("assistant", "", true) : chatTurn(role, text);
  targetNode.appendChild(node);
  scrollChatToBottom(targetNode);
  return node;
}

function setAssistantTurnText(node, text) {
  if (!node) return;
  setAssistantResponseHtml(node, `<div class="assistant-markdown"><p>${escapeHtml(text)}</p></div>`);
}

function setAssistantResponseHtml(node, html) {
  if (!node) return;
  node.innerHTML = `${html}${turnActionsHtml("assistant")}`;
  syncForcedRelationButtons(node);
  mountAssistantSections(node);
  mountRelatedMindMaps(node);
  mountMermaidDiagrams(node);
  scrollChatToBottom(node.closest(".messages"), "auto");
}

function mountAssistantSections(node) {
  const container = node?.querySelector(".assistant-markdown");
  if (!container || container.querySelector(":scope > .answer-section")) return;
  let section = null;
  [...container.children].forEach((child) => {
    if (child.tagName === "H3") {
      section = document.createElement("section");
      section.className = "answer-section";
      container.insertBefore(section, child);
      section.appendChild(child);
      return;
    }
    if (section) section.appendChild(child);
  });
}

async function streamChat(payload, assistantNode, targetNode) {
  const response = await fetch(appUrl("/api/chat/stream"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) throw new Error(await formatApiError(response));
  if (!response.body) throw new Error("浏览器未获得流式响应。 ");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  const state = { research_trace: [], answer: "", stream_status: "连接已建立。" };
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const parts = buffer.split("\n\n");
    buffer = parts.pop() || "";
    for (const part of parts) {
      const event = parseSseEvent(part);
      if (!event) continue;
      if (event.type === "start") {
        conversationId = event.conversation_id || conversationId;
        if (conversationId) localStorage.setItem("pna_conversation_id", conversationId);
        state.stream_status = event.message || "开始处理。";
        upsertResearchTrace(state.research_trace, {
          stage: "接收任务",
          status: "completed",
          message: state.stream_status,
        });
      } else if (event.type === "trace" && event.item) {
        upsertResearchTrace(state.research_trace, event.item);
        state.stream_status = event.item.message || event.item.stage || "执行中。";
      } else if (event.type === "final" && event.response) {
        event.response.research_trace = mergePublicExecutionTrace(
          state.research_trace,
          event.response.research_trace || [],
        );
        conversationId = event.response.conversation_id || conversationId;
        if (conversationId) localStorage.setItem("pna_conversation_id", conversationId);
        setAssistantResponseHtml(assistantNode, chatResponseHtml(event.response));
        syncChatResponseContext(event.response);
        await notifyConversationHistoryChanged();
        return event.response;
      } else if (event.type === "error") {
        throw new Error(event.message || "流式请求失败");
      }
      assistantNode.innerHTML = chatStreamingHtml(state);
      scrollChatToBottom(targetNode, "auto");
    }
  }
  throw new Error("流式响应在生成最终结果前结束。");
}

function scrollChatToBottom(targetNode, behavior = "smooth") {
  if (!targetNode) return;
  const applyScroll = () => {
    if (typeof targetNode.scrollTo === "function") {
      targetNode.scrollTo({ top: targetNode.scrollHeight, behavior });
    } else {
      targetNode.scrollTop = targetNode.scrollHeight;
    }
  };
  requestAnimationFrame(() => {
    applyScroll();
    requestAnimationFrame(applyScroll);
  });
}

function upsertResearchTrace(items, nextItem) {
  const stage = String(nextItem?.stage || "");
  const replaceIndex = items.findIndex((item) => item.stage === stage && item.status === "running");
  const isRunning = nextItem?.status === "running";
  if (isRunning && items.some((item) => item.stage === stage && item.status !== "running")) {
    return;
  }
  if (replaceIndex >= 0) {
    items.splice(replaceIndex, 1, nextItem);
    return;
  }
  items.push(nextItem);
}

function mergePublicExecutionTrace(streamItems, responseItems) {
  const merged = [];
  [...streamItems, ...responseItems].forEach((item) => {
    const duplicate = merged.some(
      (existing) => existing.stage === item.stage && existing.message === item.message && existing.status === item.status,
    );
    if (!duplicate) upsertResearchTrace(merged, item);
  });
  return merged;
}

function parseSseEvent(block) {
  const lines = block.split(/\r?\n/);
  let type = "message";
  let data = "";
  lines.forEach((line) => {
    if (line.startsWith("event:")) type = line.slice(6).trim();
    if (line.startsWith("data:")) data += line.slice(5).trim();
  });
  if (!data) return { type };
  try {
    const parsed = JSON.parse(data);
    return { type, ...parsed };
  } catch (error) {
    return { type, message: data };
  }
}

function chatTurn(role, text, loading = false) {
  const wrapper = document.createElement("article");
  wrapper.className = `chat-turn ${role === "user" ? "chat-user" : "chat-assistant"}`;
  if (loading) {
    wrapper.innerHTML = `${assistantIdentityHtml("正在研究")}
      <div class="trace-loading">正在理解问题并准备检索...</div>`;
  } else {
    wrapper.innerHTML = `<div class="chat-bubble">${escapeHtml(text)}</div>${turnActionsHtml(role)}`;
  }
  return wrapper;
}

function assistantIdentityHtml(status = "研究完成") {
  return `<div class="assistant-identity"><span aria-hidden="true">N</span><div><strong>News Agent</strong><small>${escapeHtml(status)}</small></div></div>`;
}

function turnActionsHtml(role) {
  if (role === "user") {
    return '<div class="turn-actions" aria-label="消息操作"><button type="button" class="edit-action" data-turn-action="edit" title="编辑并重新发送" aria-label="编辑并重新发送"><span aria-hidden="true">✎</span> 重新编辑</button></div>';
  }
  return '<div class="turn-actions" aria-label="消息操作"><button type="button" class="copy-action" data-turn-action="copy" title="复制回答" aria-label="复制回答"><span aria-hidden="true">⧉</span> 复制</button></div>';
}

document.addEventListener("click", async (event) => {
  const retryButton = event.target.closest("[data-chat-retry]");
  if (retryButton) {
    const turn = retryButton.closest(".chat-turn");
    const message = retryButton.dataset.chatRetry?.trim() || "";
    if (!turn || !message || retryButton.disabled) return;
    retryButton.disabled = true;
    turn.innerHTML = chatTransportStatusHtml("正在重新发送…", "输入框仍可继续编辑下一条问题。");
    await sendChatIntoTurn(message, turn);
    return;
  }
  const button = event.target.closest("[data-turn-action]");
  if (!button) return;
  const turn = button.closest(".chat-turn");
  if (!turn) return;
  const action = button.dataset.turnAction;
  if (action === "edit") {
    const input = document.querySelector("#message");
    const text = turn.querySelector(".chat-bubble")?.textContent?.trim() || "";
    if (!input || !text) return;
    input.value = text;
    input.focus();
    input.select?.();
    return;
  }
  if (action === "copy") {
    const text = turnCopyText(turn);
    if (!text) return;
    await copyTurnText(text);
    const original = button.textContent;
    button.textContent = "✓";
    button.setAttribute("aria-label", "已复制");
    window.setTimeout(() => {
      button.textContent = original;
      button.setAttribute("aria-label", "复制");
    }, 1200);
    return;
  }
  if (action === "mark-related") {
    await forceTurnRelation(turn, "related");
    return;
  }
  if (action === "mark-unrelated") {
    await forceTurnRelation(turn, "unrelated");
  }
});

async function forceTurnRelation(turn, relation) {
  if (!turn || !turn.classList.contains("chat-assistant")) return;
  const turnId = responseTurnId(turn);
  if (turnId) {
    try {
      const data = await request(`/api/chat/turns/${encodeURIComponent(turnId)}/relation`, {
        method: "POST",
        body: JSON.stringify({ user_id: activeUserId || "default", relation }),
      });
      if (data.response) {
        setAssistantResponseHtml(turn, chatResponseHtml(data.response));
        return;
      }
    } catch (error) {
      // 如果后端更新失败，继续使用本地显示切换，避免按钮无响应。
    }
  }
  turn.dataset.forcedRelation = relation;
  syncForcedRelationButtons(turn);
  if (relation === "related") {
    removeTurnDriftNotice(turn);
  } else {
    ensureTurnDriftNotice(turn);
  }
}

function ensureTurnDriftNotice(turn) {
  const markdown = turn.querySelector(".assistant-markdown");
  if (markdown) {
    if (turnHasDriftNotice(turn)) return;
    markdown.insertAdjacentHTML("afterbegin", `<blockquote class="manual-relation-notice">${escapeHtml(TOPIC_DRIFT_NOTICE)}</blockquote>`);
    return;
  }
  const bubble = turn.querySelector(".chat-bubble");
  if (!bubble || bubble.dataset.manualRelationNotice === "true") return;
  bubble.dataset.originalText = bubble.textContent || "";
  bubble.dataset.manualRelationNotice = "true";
  bubble.textContent = `${TOPIC_DRIFT_NOTICE}\n\n${bubble.dataset.originalText}`;
}

function removeTurnDriftNotice(turn) {
  turn.querySelectorAll("blockquote").forEach((node) => {
    if (TOPIC_DRIFT_NOTICES.some((notice) => node.textContent.includes(notice))) node.remove();
  });
  const bubble = turn.querySelector(".chat-bubble");
  if (bubble?.dataset.manualRelationNotice === "true") {
    bubble.textContent = bubble.dataset.originalText || "";
    delete bubble.dataset.manualRelationNotice;
    delete bubble.dataset.originalText;
  }
}

function turnHasDriftNotice(turn) {
  return Array.from(turn.querySelectorAll("blockquote")).some((node) =>
    TOPIC_DRIFT_NOTICES.some((notice) => node.textContent.includes(notice))
  );
}

function responseTurnId(turn) {
  return turn.querySelector("[data-response-turn-id]")?.dataset.responseTurnId || "";
}

function syncForcedRelationButtons(turn) {
  const relation = turn.dataset.forcedRelation || turn.querySelector("[data-response-turn-id]")?.dataset.forcedRelation || "";
  turn.querySelectorAll(".relation-toggle").forEach((button) => {
    button.classList.toggle("active", Boolean(relation) && button.dataset.turnAction === `mark-${relation}`);
  });
}

function turnCopyText(turn) {
  if (turn.classList.contains("chat-user")) {
    return turn.querySelector(".chat-bubble")?.textContent?.trim() || "";
  }
  return (
    turn.querySelector(".assistant-markdown")?.textContent?.trim()
    || turn.querySelector(".chat-bubble")?.textContent?.trim()
    || ""
  );
}

async function copyTurnText(text) {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return;
    } catch (error) {
      // 非安全上下文中继续使用本地复制回退。
    }
  }
  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.setAttribute("readonly", "");
  textarea.style.position = "fixed";
  textarea.style.opacity = "0";
  document.body.appendChild(textarea);
  textarea.select();
  document.execCommand("copy");
  textarea.remove();
}

function chatResponseHtml(data) {
  const trace = renderResearchTrace(data.research_trace || []);
  const mindMap = renderChatMindMapPlaceholder(data.mind_map);
  const isRelated = data.context_relation === "related_search"
    || data.context_relation === "related_search_cc_runtime"
    || String(data.mind_map?.type || "").startsWith("related_");
  const isFactcheck = data.skill_result?.command === "/factcheck";
  const answerText = isFactcheck
    ? stripFactcheckEvidenceMarkdown(data.markdown || data.answer || "")
    : isRelated
      ? stripRelatedGeneratedMarkdown(data.markdown || data.answer || "", true)
      : data.markdown || data.answer || "";
  const answer = renderMarkdown(answerText);
  const reportDownloads = renderReportDownloads(data);
  const evidenceIndex = isRelated ? renderChatEvidenceIndex(data.evidence || []) : "";
  const factcheckEvidence = isFactcheck ? renderFactcheckEvidenceCards(data.skill_result?.data || {}) : "";
  const timeline = renderChatEventLine(data.event_line, data.evidence || []);
  const responseMeta = data.turn_id
    ? `<span hidden data-response-turn-id="${escapeAttr(data.turn_id)}" data-forced-relation="${escapeAttr(data.forced_relation || "")}"></span>`
    : "";
  return `${responseMeta}${assistantIdentityHtml("已完成本轮研究")}${mindMap}<div class="assistant-markdown">${answer}</div>${reportDownloads}${evidenceIndex}${factcheckEvidence}${timeline}${trace}`;
}

function renderReportDownloads(data) {
  const result = data?.skill_result || {};
  const payload = result.data || {};
  const reportId = payload.report_id;
  if (result.command !== "/report" || !reportId) return "";
  const userId = activeUserId || "default";
  const base = appUrl(`/api/reports/${encodeURIComponent(reportId)}/download?user_id=${encodeURIComponent(userId)}`);
  return `<div class="report-downloads" aria-label="报告下载">
    <a href="${base}&format=pdf" download>下载 PDF</a>
    <a href="${base}&format=docx" download>下载 Word</a>
  </div>`;
}

function chatStreamingHtml(state) {
  const trace = renderResearchTrace(state.research_trace || []);
  return `${assistantIdentityHtml("正在研究")}${trace}<div class="stream-status">${escapeHtml(state.stream_status || "执行中。")}</div>`;
}

function renderResearchTrace(items) {
  items = mergePublicExecutionTrace([], items || []);
  if (!items.length) return "";
  const completed = items.filter((item) => item.status !== "running").length;
  const running = items.some((item) => item.status === "running");
  const statusLabel = running ? "执行中" : `${completed}/${items.length} 完成`;
  return `<details class="agent-run-trace"${running ? " open" : ""}>
    <summary><span><i></i>本轮过程</span><em>${escapeHtml(statusLabel)}</em></summary>
    <div class="research-trace" role="list">${items
    .map((item) => {
      const count = Number.isFinite(Number(item.count)) ? Number(item.count) : "";
      const state = item.status || "completed";
      const stateText = state === "running"
        ? "执行中"
        : state === "error"
          ? "异常"
          : state === "fallback"
            ? "已回退"
            : state === "skipped"
              ? "已跳过"
              : state === "warning"
                ? "需注意"
                : "完成";
      return `<div class="trace-step ${escapeAttr(state)}" role="listitem"><i aria-hidden="true"></i><div><strong>${escapeHtml(item.stage || "执行步骤")}</strong><small>${escapeHtml(stateText)}${count !== "" ? ` · ${escapeHtml(count)} 条` : ""}</small><p>${escapeHtml(item.message || "")}</p></div></div>`;
    })
    .join("")}</div></details>`;
}

function renderChatEventLine(eventLine, evidenceItems = []) {
  const items = (eventLine && eventLine.items) || [];
  if (!items.length) return "";
  return `<div class="chat-event-line">${items
    .slice(0, 6)
    .map((item) => renderChatEvent(item, evidenceItems))
    .join("")}</div>`;
}

function renderChatEvent(item, evidenceItems) {
  const url = eventItemUrl(item, evidenceItems);
  const tag = url ? "a" : "div";
  const attrs = url ? ` href="${escapeAttr(url)}" target="_blank" rel="noreferrer"` : "";
  return `<${tag} class="chat-event"${attrs}><time>${escapeHtml(item.date || "")}</time><strong>${escapeHtml(item.title || "")}</strong><p>${escapeHtml(item.summary || "")}</p></${tag}>`;
}

function eventItemUrl(item, evidenceItems) {
  if (item?.url) return item.url;
  const sourceIds = new Set((item?.source_article_ids || []).filter(Boolean));
  if (sourceIds.size) {
    const match = evidenceItems.find((evidence) => evidence.article_id && sourceIds.has(evidence.article_id) && evidence.url);
    if (match) return match.url;
  }
  const title = String(item?.title || "").trim();
  if (!title) return "";
  const match = evidenceItems.find((evidence) => String(evidence.title || "").trim() === title && evidence.url);
  return match?.url || "";
}

function renderChatMindMapPlaceholder(map) {
  const branches = (map && map.branches) || [];
  const steps = (map && map.steps) || [];
  if (!branches.length && !steps.length) return "";
  const id = `mind_map_${Date.now().toString(36)}_${Math.random().toString(36).slice(2)}`;
  window.__pnaMindMapPayloads = window.__pnaMindMapPayloads || {};
  window.__pnaMindMapPayloads[id] = map;
  return `<div class="related-mind-map-host" data-related-map-id="${escapeAttr(id)}">${renderMindMapFallback(map)}</div>`;
}

function mountRelatedMindMaps(scope = document) {
  if (!window.React || !window.ReactDOM) return;
  const hosts = scope.querySelectorAll("[data-related-map-id]:not([data-react-mounted='true'])");
  hosts.forEach((host) => {
    const map = (window.__pnaMindMapPayloads || {})[host.dataset.relatedMapId];
    if (!map) return;
    host.dataset.reactMounted = "true";
    try {
      const root = ReactDOM.createRoot(host);
      root.render(React.createElement(RelatedMindMapExplorer, { map }));
    } catch (error) {
      host.innerHTML = renderMindMapFallback(map);
    }
  });
}

function RelatedMindMapExplorer({ map }) {
  const steps = React.useMemo(() => normalizeRelatedResearchSteps(map), [map]);
  const [activeIndex, setActiveIndex] = React.useState(0);
  const activeStep = steps[activeIndex] || steps[0];
  const activePoints = (activeStep?.points || []).slice(0, 6);

  return React.createElement(
    "section",
    { className: "related-research-path", "aria-label": "关联研究路径" },
    React.createElement(
      "header",
      { className: "related-path-head" },
      React.createElement(
        "div",
        null,
        React.createElement("span", null, "RESEARCH PATH"),
        React.createElement("strong", null, "关联研究路径"),
        React.createElement("p", null, "展示 Agent 如何锚定语境、执行检索并形成结论；这是可核验的执行记录，不是隐藏思维链。")
      ),
      React.createElement(
        "div",
        { className: "related-path-stats" },
        React.createElement("span", null, `${steps.length} 个节点`),
        React.createElement("span", null, `${map.evidence_count || 0} 条证据`)
      )
    ),
    React.createElement(
      "div",
      { className: "related-path-context" },
      React.createElement("span", null, "当前话题"),
      React.createElement("strong", null, map.active_topic || map.topic || "当前主题"),
      React.createElement("i", { "aria-hidden": "true" }, "→"),
      React.createElement("span", null, "延展对象"),
      React.createElement("strong", null, map.requested_focus || map.topic || "指定对象")
    ),
    React.createElement(
      "div",
      { className: "related-path-body" },
      React.createElement(
        "ol",
        { className: "related-path-steps" },
        steps.map((step, index) =>
          React.createElement(
            "li",
            { key: `path_${index}`, className: index === activeIndex ? "active" : "" },
            React.createElement(
              "button",
              { type: "button", onClick: () => setActiveIndex(index), "aria-pressed": index === activeIndex },
              React.createElement("b", null, String(index + 1).padStart(2, "0")),
              React.createElement(
                "span",
                null,
                React.createElement("em", null, step.label),
                React.createElement("strong", null, truncateText(step.title, 64)),
                React.createElement("small", null, truncateText(step.detail, 110))
              ),
              React.createElement(
                "i",
                { className: `path-state ${step.status}` },
                step.status === "limited" ? "证据有限" : step.resultCount ? `${step.resultCount} 条` : "完成"
              )
            )
          )
        )
      ),
      React.createElement(
        "aside",
        { className: "related-path-detail" },
        React.createElement("span", null, activeStep?.label || "执行节点"),
        React.createElement("strong", null, activeStep?.title || "关联研究"),
        React.createElement("p", null, activeStep?.detail || "围绕当前主题核对关联信息。"),
        React.createElement(
          "div",
          { className: "path-evidence-list" },
          activePoints.length
            ? activePoints.map((point, index) =>
                React.createElement(
                  point.url ? "a" : "div",
                  point.url
                    ? { key: `${point.title}_${index}`, href: point.url, target: "_blank", rel: "noreferrer" }
                    : { key: `${point.title}_${index}` },
                  React.createElement("span", null, point.evidence_index || point.index ? `证据 #${point.evidence_index || point.index}` : `证据 ${index + 1}`),
                  React.createElement("strong", null, evidenceTitle(point.title || "相关证据")),
                  React.createElement("small", null, evidenceSnippet(point.summary || point.connection_reason || "暂无摘要"))
                )
              )
            : React.createElement("p", { className: "path-detail-empty" }, activeStep?.kind === "context" || activeStep?.kind === "resolution"
              ? "这是语境解析节点，不单独绑定新闻证据。"
              : "该节点没有可展示的来源，结论中会明确保留不确定性。")
        )
      )
    )
  );
}

function normalizeRelatedResearchSteps(map) {
  if (Array.isArray(map?.steps) && map.steps.length) {
    return map.steps.map((step) => ({
      kind: step.kind || "search",
      label: step.label || "执行节点",
      title: step.title || map.topic || "关联研究",
      detail: step.detail || "围绕当前主题核对关联信息。",
      status: step.status === "limited" ? "limited" : "completed",
      resultCount: Number(step.result_count || 0),
      points: step.points || [],
    }));
  }
  const branches = normalizeMindMapBranches(map);
  return [
    {
      kind: "context",
      label: "语境锚点",
      title: map?.topic || "当前主题",
      detail: "从这轮对话的主题与用户指定对象出发。",
      status: "completed",
      resultCount: 0,
      points: [],
    },
    ...branches.map((branch) => ({
      kind: "search",
      label: "本轮检索式",
      title: branch.title || "关联检索",
      detail: branch.edgeReason,
      status: branch.points.length ? "completed" : "limited",
      resultCount: Number(branch.count || branch.points.length || 0),
      points: branch.points,
    })),
  ];
}

function ExplorerLinks({ branches, layout, activeIndex }) {
  return React.createElement(
    "svg",
    { className: "explorer-link-layer", viewBox: `0 0 ${layout.width} ${layout.height}`, "aria-hidden": "true" },
    React.createElement(
      "defs",
      null,
      React.createElement(
        "filter",
        { id: "explorerGlow", x: "-20%", y: "-20%", width: "140%", height: "140%" },
        React.createElement("feGaussianBlur", { stdDeviation: "3", result: "blur" }),
        React.createElement("feMerge", null, React.createElement("feMergeNode", { in: "blur" }), React.createElement("feMergeNode", { in: "SourceGraphic" }))
      )
    ),
    branches.map((branch, index) => {
      const branchPosition = layout.branches[index];
      const active = index === activeIndex;
      const color = relationColor(branch.relationType);
      const startX = layout.center.x + layout.center.width;
      const startY = layout.center.y + layout.center.height / 2;
      const endX = branchPosition.x;
      const endY = branchPosition.y + branchPosition.height / 2;
      return React.createElement(
        React.Fragment,
        { key: `links_${index}` },
        React.createElement("path", {
          className: "explorer-link-rail",
          d: curvePath(startX, startY, endX, endY),
        }),
        React.createElement("path", {
          className: `explorer-link ${active ? "active" : ""}`,
          d: curvePath(startX, startY, endX, endY),
          style: { "--relation-color": color },
          filter: active ? "url(#explorerGlow)" : "",
        })
      );
    })
  );
}

function ExplorerNode({ kind, relationType = "other", active = false, x, y, width, height, eyebrow, title, detail, onClick }) {
  const props = {
    role: onClick ? "button" : undefined,
    tabIndex: onClick ? 0 : undefined,
    className: `explorer-node ${kind} ${active ? "active" : ""}`,
    style: { left: x, top: y, width, minHeight: height, "--relation-color": relationColor(relationType) },
    onClick,
    onKeyDown: onClick
      ? (event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            onClick();
          }
        }
      : undefined,
  };
  const content = [];
  if (eyebrow) {
    content.push(React.createElement("span", { key: "eyebrow" }, eyebrow));
  }
  if (title) {
    content.push(React.createElement("strong", { key: "title" }, truncateText(title, 38)));
  }
  if (detail) {
    content.push(React.createElement("small", { key: "detail" }, truncateText(detail, 56)));
  }
  return React.createElement("div", props, content);
}

function normalizeMindMapBranches(map) {
  return ((map && map.branches) || []).map((branch) => ({
    ...branch,
    relationType: flowRelationType(branch.relation_type),
    relationLabel: branch.relation_label || "语义相关",
    edgeReason: branch.edge_reason || branch.reason || "从这个方向补充关联信息。",
    points: branch.points || [],
  }));
}

function buildExplorerLayout(branches) {
  const width = 720;
  const height = Math.max(360, branches.length * 88 + 52);
  const center = { x: 36, y: 0, width: 220, height: 130 };
  const branchWidth = 210;
  const branchHeight = 86;
  const branchX = 402;
  const branchYs = branchYPositions(branches.length, height, branchHeight);
  center.y = height / 2 - center.height / 2;
  const branchesLayout = [];
  branches.forEach((_, branchIndex) => {
    const xOffset = branchIndex % 2 === 0 ? 18 : 78;
    branchesLayout.push({ x: branchX + xOffset, y: branchYs[branchIndex], width: branchWidth, height: branchHeight });
  });
  return { width, height, center, branches: branchesLayout };
}

function branchYPositions(count, height, branchHeight) {
  if (count <= 1) return [height / 2 - branchHeight / 2];
  const top = 26;
  const bottom = height - branchHeight - 26;
  return Array.from({ length: count }, (_, index) => top + ((bottom - top) * index) / Math.max(1, count - 1));
}

function curvePath(startX, startY, endX, endY) {
  const distance = Math.max(80, Math.abs(endX - startX) * 0.48);
  return `M ${startX} ${startY} C ${startX + distance} ${startY}, ${endX - distance} ${endY}, ${endX} ${endY}`;
}

function relationColor(type) {
  const colors = {
    latest: "#2563eb",
    background: "#7c3aed",
    actor: "#0891b2",
    impact: "#c2410c",
    follow_up: "#15803d",
    center: "#111827",
    other: "#475467",
  };
  return colors[type] || colors.other;
}

function flowRelationType(value) {
  const type = String(value || "other").replace(/[^a-z_]/gi, "").toLowerCase();
  return ["latest", "background", "actor", "impact", "follow_up", "other"].includes(type) ? type : "other";
}

function renderMindMapFallback(map) {
  const steps = normalizeRelatedResearchSteps(map);
  return `<section class="related-explorer-fallback">
    <strong>关联研究路径</strong>
      <p>${escapeHtml(steps.length)} 个执行节点 · ${escapeHtml(map.evidence_count || 0)} 条证据</p>
      <div>${steps
      .slice(0, 8)
      .map((step, index) => `<span>${String(index + 1).padStart(2, "0")} ${escapeHtml(step.label)}：${escapeHtml(truncateText(step.title || "", 28))}</span>`)
      .join("")}</div>
  </section>`;
}

function cleanEvidenceText(value) {
  return String(value || "")
    .replace(/!\[[^\]]*](?:\([^)]*\))?/g, "")
    .replace(/\[([^\]]+)](?:\([^)]*\))?/g, "$1")
    .replace(/https?:\/\/\S+/g, "")
    .replace(/#+\s*/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

function evidenceTitle(value) {
  return truncateText(cleanEvidenceText(value), 34);
}

function evidenceSnippet(value) {
  return truncateText(
    cleanEvidenceText(value),
    66
  );
}

function stripRelatedGeneratedMarkdown(markdown, stripEvidenceIndex = false) {
  const lines = String(markdown || "").split(/\r?\n/);
  const output = [];
  let skippingSection = false;
  for (const line of lines) {
    const trimmed = line.trim();
    if (skippingSection && trimmed.startsWith("## ")) {
      skippingSection = false;
    }
    if (trimmed === "## 相关思维导图" || (stripEvidenceIndex && trimmed === "## 证据索引")) {
      skippingSection = true;
      continue;
    }
    if (!skippingSection) output.push(line);
  }
  return output.join("\n").trim();
}

function stripFactcheckEvidenceMarkdown(markdown) {
  const lines = String(markdown || "").split(/\r?\n/);
  const output = [];
  let skippingSection = false;
  const structuredSections = (
    "### 支持证据|### 反向证据|### 检索到的全部证据|### 缺失证据|### 来源说明|### 下一步核查"
  ).split("|");
  for (const line of lines) {
    const trimmed = line.trim();
    if (skippingSection && trimmed.startsWith("## ")) {
      skippingSection = false;
    }
    if (structuredSections.some((heading) => trimmed.startsWith(heading))) {
      skippingSection = true;
      continue;
    }
    if (!skippingSection) output.push(line);
  }
  return output.join("\n").trim();
}

function renderFactcheckEvidenceCards(payload) {
  const allItems = Array.isArray(payload?.evidence) ? payload.evidence : [];
  const supporting = hydrateFactcheckEvidence(payload?.supporting_evidence, allItems);
  const contradicting = hydrateFactcheckEvidence(payload?.contradicting_evidence, allItems);
  const classified = new Set(
    [...supporting, ...contradicting].map((item) => factcheckEvidenceKey(item)).filter(Boolean),
  );
  const contextual = allItems.filter((item) => !classified.has(factcheckEvidenceKey(item)));
  const hasDetails = supporting.length || contradicting.length || contextual.length
    || payload?.missing_evidence?.length || payload?.source_notes?.length || payload?.next_checks?.length;
  if (!hasDetails) return "";
  const verdict = String(payload?.verdict || "insufficient");
  const verdictLabels = {
    supported: "有证据支持",
    contradicted: "关键事实不符",
    mixed: "部分成立 / 存在冲突",
    insufficient: "证据不足",
    not_checkable: "暂不可核查",
  };
  const confidence = Math.round(Math.max(0, Math.min(1, Number(payload?.confidence) || 0)) * 100);
  return `<section class="factcheck-workbench verdict-${escapeAttr(verdict)}" aria-label="事实核查证据台">
    <header class="factcheck-verdict">
      <div><span>FACT CHECK</span><strong>${escapeHtml(verdictLabels[verdict] || "核查完成")}</strong></div>
      <em>证据完整度 ${confidence}%</em>
    </header>
    ${renderFactcheckEvidenceGroup("支持原说法", supporting, "support")}
    ${renderFactcheckEvidenceGroup("反驳原说法", contradicting, "contradict")}
    ${renderFactcheckEvidenceGroup("背景材料（不直接决定结论）", contextual, "context")}
    ${renderFactcheckChecklist("仍缺少的证据", payload?.missing_evidence, "missing")}
    ${renderFactcheckChecklist("建议继续核查", payload?.next_checks, "next")}
    ${renderFactcheckChecklist("来源与局限", payload?.source_notes, "notes")}
  </section>`;
}

function hydrateFactcheckEvidence(refs, allItems) {
  if (!Array.isArray(refs)) return [];
  return refs.map((ref) => {
    if (!ref || typeof ref !== "object") return null;
    const match = allItems.find((item) =>
      (ref.index && item.index === ref.index) || (ref.url && item.url === ref.url),
    );
    return { ...(match || {}), ...ref };
  }).filter(Boolean);
}

function factcheckEvidenceKey(item) {
  return String(item?.url || item?.index || item?.title || "").trim();
}

function renderFactcheckEvidenceGroup(title, items, role) {
  if (!items.length) return "";
  return `<div class="factcheck-group factcheck-${escapeAttr(role)}">
    <h4>${escapeHtml(title)} <span>${items.length}</span></h4>
    <div class="factcheck-evidence-grid">${items.slice(0, 12).map((item) => renderFactcheckEvidenceItem(item, role)).join("")}</div>
  </div>`;
}

function renderFactcheckEvidenceItem(item, role) {
  const title = item.title || "未命名证据";
  const url = String(item.url || "").trim();
  const tag = url.startsWith("http") ? "a" : "article";
  const attrs = url.startsWith("http") ? ` href="${escapeAttr(url)}" target="_blank" rel="noreferrer"` : "";
  const date = item.published_at || "时间未知";
  const source = item.source_id || "来源未知";
  const origin = item.origin === "external" ? "外部来源" : "本地新闻引擎";
  const summary = truncateText(cleanEvidenceText(item.summary || item.content_excerpt || ""), 210);
  return `<${tag} class="factcheck-evidence-card evidence-${escapeAttr(role)}"${attrs}>
    <div class="factcheck-evidence-meta"><span>${escapeHtml(origin)}</span><time>${escapeHtml(date)}</time></div>
    <strong>${escapeHtml(title)}</strong>
    <p>${escapeHtml(source)}${summary ? ` · ${escapeHtml(summary)}` : ""}</p>
    ${url ? "<em>打开原始来源 ↗</em>" : ""}
  </${tag}>`;
}

function renderFactcheckChecklist(title, items, role) {
  const values = Array.isArray(items) ? items.filter(Boolean).slice(0, 6) : [];
  if (!values.length) return "";
  return `<div class="factcheck-checklist factcheck-${escapeAttr(role)}"><h4>${escapeHtml(title)}</h4><ul>${values
    .map((item) => `<li>${escapeHtml(item)}</li>`)
    .join("")}</ul></div>`;
}

function renderChatEvidenceIndex(items) {
  if (!items.length) return "";
  return `<div class="assistant-markdown related-evidence-index"><h3>证据索引</h3><ul>${items
    .map((item, index) => {
      const number = item.index || index + 1;
      const title = _markdownEvidenceLabel(`证据 ${number}｜${item.title || "未命名证据"}`);
      const source = item.source_id || "来源未知";
      const date = item.published_at || item.date || "发布时间未知";
      const summary = truncateText(cleanEvidenceText(item.summary || ""), 150);
      const url = String(item.url || "").trim();
      const titleHtml = url.startsWith("http")
        ? `<a href="${escapeAttr(url)}" target="_blank" rel="noreferrer">${escapeHtml(title)}</a>`
        : escapeHtml(title);
      return `<li>${titleHtml}（${escapeHtml(source)}，${escapeHtml(date)}）${summary ? `：${escapeHtml(summary)}` : ""}</li>`;
    })
    .join("")}</ul></div>`;
}

function _markdownEvidenceLabel(value) {
  return String(value || "").replace("[", "【").replace("]", "】").replace(/\s+/g, " ").trim();
}

function truncateText(value, maxLength) {
  const text = String(value || "").trim();
  return text.length > maxLength ? `${text.slice(0, maxLength - 1)}…` : text;
}

function renderMarkdown(markdown) {
  const lines = String(markdown || "").split(/\r?\n/);
  let html = "";
  let listType = "";
  const closeList = () => {
    if (listType) {
      html += `</${listType}>`;
      listType = "";
    }
  };
  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    const trimmed = line.trim();
    if (!trimmed) {
      closeList();
      continue;
    }
    if (isMarkdownTableStart(lines, index)) {
      closeList();
      const table = renderMarkdownTable(lines, index);
      html += table.html;
      index = table.endIndex;
      continue;
    }
    if (trimmed.startsWith("```")) {
      closeList();
      const language = trimmed.slice(3).replace(/[^a-z0-9_+-]/gi, "").slice(0, 24);
      const codeLines = [];
      index += 1;
      while (index < lines.length && !lines[index].trim().startsWith("```")) {
        codeLines.push(lines[index]);
        index += 1;
      }
      const code = codeLines.join("\n");
      html += language.toLowerCase() === "mermaid"
        ? renderMermaidBlock(code)
        : `<pre${language ? ` data-language="${escapeAttr(language)}"` : ""}><code>${escapeHtml(code)}</code></pre>`;
      continue;
    }
    if (/^(-{3,}|\*{3,})$/.test(trimmed)) {
      closeList();
      html += "<hr />";
      continue;
    }
    if (trimmed.startsWith("### ")) {
      closeList();
      html += `<h4>${renderInlineMarkdown(trimmed.slice(4))}</h4>`;
    } else if (trimmed.startsWith("## ")) {
      closeList();
      html += `<h3>${renderInlineMarkdown(trimmed.slice(3))}</h3>`;
    } else if (trimmed.startsWith("# ")) {
      closeList();
      html += `<h3>${renderInlineMarkdown(trimmed.slice(2))}</h3>`;
    } else if (trimmed.startsWith("- ") || trimmed.startsWith("* ")) {
      if (listType !== "ul") {
        closeList();
        html += "<ul>";
        listType = "ul";
      }
      html += `<li>${renderInlineMarkdown(trimmed.slice(2))}</li>`;
    } else if (/^\d+[.)]\s+/.test(trimmed)) {
      if (listType !== "ol") {
        closeList();
        html += "<ol>";
        listType = "ol";
      }
      html += `<li>${renderInlineMarkdown(trimmed.replace(/^\d+[.)]\s+/, ""))}</li>`;
    } else if (trimmed.startsWith("> ")) {
      closeList();
      html += `<blockquote>${renderInlineMarkdown(trimmed.slice(2))}</blockquote>`;
    } else {
      closeList();
      html += `<p>${renderInlineMarkdown(trimmed)}</p>`;
    }
  }
  closeList();
  return html;
}

function renderMermaidBlock(code) {
  const source = sanitizeMermaidSource(code);
  if (!source || source.length > 8000) {
    return `<pre data-language="mermaid"><code>${escapeHtml(source || "图谱内容为空")}</code></pre>`;
  }
  return `<section class="chat-mermaid" data-mermaid-source="${escapeAttr(source)}">
    <header class="chat-mermaid-toolbar"><div><strong>交互事件图谱</strong><span>点击图谱放大，选中节点可继续研究</span></div><button type="button" data-mermaid-expand>展开大图</button></header>
    <div class="chat-mermaid-canvas" aria-label="热点事件图谱" role="button" tabindex="0" title="点击展开事件图谱"></div>
    <p class="chat-mermaid-status">正在渲染事件图谱…</p>
    <details class="chat-mermaid-source"><summary>查看图谱源码</summary><pre data-language="mermaid"><code>${escapeHtml(source)}</code></pre></details>
  </section>`;
}

function sanitizeMermaidSource(code) {
  return String(code || "")
    .split(/\r?\n/)
    .filter((line) => !/^\s*(click\s+|%%\{)/i.test(line))
    .map((line) => line.replace(/<br\s*\/?\s*>/gi, " · ").replace(/<[^>]{1,200}>/g, ""))
    .join("\n")
    .replace(/\|"([^"\n|]{1,120})\|"(?=\s+[A-Za-z_][A-Za-z0-9_-]*(?:\s|$))/g, '|"$1"|')
    .trim()
    .slice(0, 8000);
}

let mermaidInitialized = false;
let mermaidSequence = 0;

async function mountMermaidDiagrams(root) {
  if (!root) return;
  const diagrams = root.querySelectorAll(".chat-mermaid:not([data-rendered])");
  if (!diagrams.length) return;
  if (!window.mermaid) {
    diagrams.forEach((node) => {
      node.dataset.rendered = "error";
      const status = node.querySelector(".chat-mermaid-status");
      if (status) status.textContent = "图谱渲染组件暂不可用，可展开查看源码。";
    });
    return;
  }
  if (!mermaidInitialized) {
    window.mermaid.initialize({
      startOnLoad: false,
      securityLevel: "strict",
      theme: "dark",
      flowchart: { htmlLabels: false, curve: "basis" },
    });
    mermaidInitialized = true;
  }
  for (const node of diagrams) {
    node.dataset.rendered = "running";
    const source = node.dataset.mermaidSource || "";
    const canvas = node.querySelector(".chat-mermaid-canvas");
    const status = node.querySelector(".chat-mermaid-status");
    try {
      mermaidSequence += 1;
      const result = await window.mermaid.render(`pna_mermaid_${mermaidSequence}`, source);
      if (canvas) canvas.innerHTML = result.svg;
      if (typeof result.bindFunctions === "function" && canvas) result.bindFunctions(canvas);
      bindMermaidPreview(node, source);
      node.dataset.rendered = "done";
      if (status) status.remove();
    } catch (error) {
      node.dataset.rendered = "error";
      if (status) status.textContent = "图谱语法暂时无法渲染，可展开查看源码。";
    }
  }
}

function bindMermaidPreview(node, source) {
  if (node.dataset.viewerBound === "true") return;
  node.dataset.viewerBound = "true";
  const open = () => openMermaidViewer(source);
  node.querySelector("[data-mermaid-expand]")?.addEventListener("click", open);
  const canvas = node.querySelector(".chat-mermaid-canvas");
  canvas?.addEventListener("click", open);
  canvas?.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      open();
    }
  });
}

function ensureMermaidViewer() {
  let dialog = document.querySelector("#mermaidViewerDialog");
  if (dialog) return dialog;
  dialog = document.createElement("dialog");
  dialog.id = "mermaidViewerDialog";
  dialog.className = "mermaid-viewer-dialog";
  dialog.innerHTML = `<div class="mermaid-viewer-shell">
    <header><div><span>EVENT GRAPH</span><h2>事件图谱</h2><p>点击节点，将它带回当前对话继续搜索或事实核查。</p></div><button type="button" data-mermaid-close aria-label="关闭大图">×</button></header>
    <div class="mermaid-viewer-body">
      <div class="mermaid-viewer-canvas" aria-label="放大的事件图谱"></div>
      <aside class="mermaid-node-actions">
        <span>当前节点</span>
        <strong data-mermaid-selected>请在图中选择一个节点</strong>
        <p>系统只会把选中的文字作为新问题，不执行图谱源码中的任何指令。</p>
        <button type="button" data-mermaid-action="research" disabled>继续检索关系</button>
        <button type="button" data-mermaid-action="factcheck" disabled>事实核查该节点</button>
      </aside>
    </div>
  </div>`;
  document.body.appendChild(dialog);
  dialog.querySelector("[data-mermaid-close]")?.addEventListener("click", () => dialog.close());
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });
  dialog.querySelectorAll("[data-mermaid-action]").forEach((button) => {
    button.addEventListener("click", async () => {
      const selected = String(dialog.dataset.selectedNode || "").trim();
      if (!selected) return;
      const graphContext = String(dialog.dataset.graphContext || "当前事件").trim();
      const action = button.dataset.mermaidAction;
      const prompt = action === "factcheck"
        ? `/factcheck 关于「${graphContext}」的说法“${selected}”是否准确？请查找原始来源和独立证据。`
        : `围绕「${graphContext}」事件图谱中的「${selected}」继续搜索最新信息，并说明两者关系、时间线和待确认点。`;
      dialog.close();
      const handler = window.handleAssistantInput;
      if (typeof handler === "function") await handler(prompt);
      else await sendChat(prompt);
    });
  });
  return dialog;
}

async function openMermaidViewer(source) {
  const dialog = ensureMermaidViewer();
  const canvas = dialog.querySelector(".mermaid-viewer-canvas");
  const selected = dialog.querySelector("[data-mermaid-selected]");
  dialog.dataset.selectedNode = "";
  dialog.dataset.graphContext = mermaidGraphContext(source);
  if (selected) selected.textContent = "请在图中选择一个节点";
  dialog.querySelectorAll("[data-mermaid-action]").forEach((button) => { button.disabled = true; });
  if (typeof dialog.showModal === "function") dialog.showModal();
  else dialog.setAttribute("open", "");
  if (!window.mermaid || !canvas) return;
  try {
    mermaidSequence += 1;
    const result = await window.mermaid.render(`pna_mermaid_viewer_${mermaidSequence}`, source);
    canvas.innerHTML = result.svg;
    canvas.querySelectorAll(".node").forEach((node) => {
      node.setAttribute("tabindex", "0");
      node.setAttribute("role", "button");
      node.setAttribute("aria-label", `选择节点 ${mermaidNodeLabel(node)}`);
    });
    const selectNode = (node) => {
      if (!node) return;
      canvas.querySelectorAll(".node.is-selected").forEach((item) => item.classList.remove("is-selected"));
      node.classList.add("is-selected");
      const label = mermaidNodeLabel(node);
      dialog.dataset.selectedNode = label;
      if (selected) selected.textContent = label;
      dialog.querySelectorAll("[data-mermaid-action]").forEach((button) => { button.disabled = !label; });
    };
    canvas.onclick = (event) => selectNode(event.target.closest?.(".node"));
    canvas.onkeydown = (event) => {
      if (event.key === "Enter" || event.key === " ") {
        const node = event.target.closest?.(".node");
        if (node) {
          event.preventDefault();
          selectNode(node);
        }
      }
    };
  } catch (error) {
    canvas.innerHTML = `<p class="mermaid-viewer-error">图谱暂时无法放大渲染，请返回回答查看源码。</p>`;
  }
}

function mermaidNodeLabel(node) {
  return String(node?.textContent || "")
    .replace(/\s+/g, " ")
    .replace(/^\[[^\]]+\]\s*/, "")
    .trim()
    .slice(0, 160);
}

function mermaidGraphContext(source) {
  const labels = new Map();
  const degrees = new Map();
  for (const line of String(source || "").split(/\r?\n/)) {
    if (/^\s*(?:flowchart|graph|classDef|class|style|linkStyle|subgraph|end)\b/i.test(line)) continue;
    const declaration = line.match(/^\s*([A-Za-z_][A-Za-z0-9_-]*)\s*[\[({]+\"?([^\"\])}]{2,160})/);
    if (declaration?.[1] && declaration?.[2]) {
      labels.set(declaration[1], declaration[2].replace(/\s+/g, " ").trim());
    }
    const edge = line.match(
      /^\s*([A-Za-z_][A-Za-z0-9_-]*)\s+.+?(?:-->|==>|-\.->)\s*([A-Za-z_][A-Za-z0-9_-]*)(?:\s|$)/,
    );
    if (edge) {
      degrees.set(edge[1], (degrees.get(edge[1]) || 0) + 1);
      degrees.set(edge[2], (degrees.get(edge[2]) || 0) + 1);
    }
  }
  const ranked = [...labels.entries()].sort((left, right) => (degrees.get(right[0]) || 0) - (degrees.get(left[0]) || 0));
  return ranked[0]?.[1] || "当前事件";
}

function isMarkdownTableStart(lines, index) {
  return splitMarkdownTableRow(lines[index]).length > 1 && isMarkdownTableSeparator(lines[index + 1]);
}

function isMarkdownTableSeparator(line) {
  const cells = splitMarkdownTableRow(line);
  return cells.length > 1 && cells.every((cell) => /^:?-{3,}:?$/.test(cell.trim()));
}

function splitMarkdownTableRow(line) {
  const trimmed = String(line || "").trim();
  if (!trimmed.includes("|")) return [];
  return trimmed.replace(/^\|/, "").replace(/\|$/, "").split("|").map((cell) => cell.trim());
}

function renderMarkdownTable(lines, startIndex) {
  const headers = splitMarkdownTableRow(lines[startIndex]);
  const rows = [];
  let index = startIndex + 2;
  while (index < lines.length) {
    const cells = splitMarkdownTableRow(lines[index]);
    if (!cells.length) break;
    rows.push(cells);
    index += 1;
  }
  const headerHtml = headers.map((cell) => `<th>${renderInlineMarkdown(cell)}</th>`).join("");
  const bodyHtml = rows
    .map((row) => `<tr>${headers.map((_, columnIndex) => `<td>${renderInlineMarkdown(row[columnIndex] || "")}</td>`).join("")}</tr>`)
    .join("");
  return {
    html: `<div class="markdown-table-wrap"><table><thead><tr>${headerHtml}</tr></thead><tbody>${bodyHtml}</tbody></table></div>`,
    endIndex: index - 1,
  };
}

function renderInlineMarkdown(value) {
  const text = String(value || "");
  const linkPattern = /\[([^\]]+)]\((https?:\/\/[^)\s]+)\)/g;
  let html = "";
  let lastIndex = 0;
  let match;
  while ((match = linkPattern.exec(text))) {
    html += renderInlinePlain(text.slice(lastIndex, match.index));
    html += `<a href="${escapeAttr(match[2])}" target="_blank" rel="noreferrer">${renderInlinePlain(match[1])}</a>`;
    lastIndex = linkPattern.lastIndex;
  }
  html += renderInlinePlain(text.slice(lastIndex));
  return html;
}

function renderInlinePlain(value) {
  return linkifyPlainUrls(escapeHtml(value)
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/`(.+?)`/g, "<code>$1</code>"));
}

function linkifyPlainUrls(value) {
  return String(value || "").replace(/https?:\/\/[^\s<]+/g, (url) => {
    const trailing = url.match(/[，。；、,.!?）)]$/)?.[0] || "";
    const cleanUrl = trailing ? url.slice(0, -trailing.length) : url;
    return `<a href="${escapeAttr(cleanUrl)}" target="_blank" rel="noreferrer">${cleanUrl}</a>${trailing}`;
  });
}

function bindAskButtons() {
  document.addEventListener("click", async (event) => {
    const commandButton = event.target.closest("[data-command]");
    if (commandButton) {
      event.preventDefault();
      const command = commandButton.dataset.command || "";
      const handler = window.handleAssistantInput;
      if (typeof handler === "function") {
        await handler(command);
        return;
      }
      await sendChat(command);
      return;
    }
    const button = event.target.closest("[data-ask]");
    if (!button) return;
    event.preventDefault();
    const ask = button.dataset.ask || button.textContent || "";
    const handler = window.handleAssistantInput;
    if (typeof handler === "function") {
      await handler(ask);
      return;
    }
    await sendChat(ask);
  });
}

const assistantSlashCommands = [
  {
    name: "factcheck",
    label: "事实核查",
    description: "核查关键说法，并区分事实、争议与未知",
    icon: "✓",
  },
  {
    name: "report",
    label: "生成专题报告",
    description: "汇总当前主题的进展、证据与关键结论",
    icon: "▤",
  },
  {
    name: "brief",
    label: "生成新闻简报",
    description: "把当前关注内容整理成一份快速简报",
    icon: "☀",
  },
  {
    name: "related",
    label: "查找相关新闻",
    description: "结合当前对话理解人物或事件，再联网查询它与主题的关系",
    icon: "⌁",
  },
  {
    name: "map",
    label: "生成事件图谱",
    description: "检索人物、组织、时间、地点和事件关系，生成可视化图谱",
    icon: "◇",
  },
  {
    name: "schedule",
    label: "创建定时报告",
    description: "解析周期、主题和报告样式，创建主动推送任务",
    icon: "◷",
  },
  {
    name: "sources",
    label: "审计新闻来源",
    description: "检查门户覆盖、配置状态、可信度结构与可用性",
    icon: "◎",
  },
];

function bindSlashCommandMenu(inputSelector = "#message") {
  const input = document.querySelector(inputSelector);
  const form = input?.closest("form");
  if (!input || !form || form.querySelector(".slash-command-menu")) return;

  const menu = document.createElement("div");
  menu.className = "slash-command-menu";
  menu.hidden = true;
  menu.setAttribute("role", "listbox");
  menu.setAttribute("aria-label", "新闻技能");
  form.appendChild(menu);

  let visibleCommands = [];
  let activeIndex = 0;

  const closeMenu = () => {
    menu.hidden = true;
    input.removeAttribute("aria-activedescendant");
  };

  const selectCommand = (command) => {
    input.value = `/${command.name} `;
    closeMenu();
    input.focus();
  };

  const renderMenu = () => {
    const value = input.value;
    if (!value.startsWith("/") || value.slice(1).includes(" ")) {
      closeMenu();
      return;
    }
    const query = value.slice(1).toLowerCase();
    visibleCommands = assistantSlashCommands.filter((command) =>
      `${command.name} ${command.label} ${command.description}`.toLowerCase().includes(query),
    );
    if (!visibleCommands.length) {
      closeMenu();
      return;
    }
    activeIndex = Math.min(activeIndex, visibleCommands.length - 1);
    menu.innerHTML = `
      <div class="slash-command-heading">技能</div>
      ${visibleCommands.map((command, index) => `
        <button type="button" id="slash-command-${command.name}" class="slash-command-item${index === activeIndex ? " active" : ""}" role="option" aria-selected="${index === activeIndex}" data-slash-command="${command.name}">
          <span class="slash-command-icon" aria-hidden="true">${command.icon}</span>
          <span class="slash-command-copy"><strong>/${command.name}</strong><span>${command.label}</span><small>${command.description}</small></span>
        </button>
      `).join("")}
    `;
    menu.hidden = false;
    input.setAttribute("aria-activedescendant", `slash-command-${visibleCommands[activeIndex].name}`);
  };

  input.addEventListener("input", () => {
    activeIndex = 0;
    renderMenu();
  });
  input.addEventListener("keydown", (event) => {
    if (menu.hidden) return;
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      const direction = event.key === "ArrowDown" ? 1 : -1;
      activeIndex = (activeIndex + direction + visibleCommands.length) % visibleCommands.length;
      renderMenu();
    } else if (event.key === "Enter" || event.key === "Tab") {
      event.preventDefault();
      selectCommand(visibleCommands[activeIndex]);
    } else if (event.key === "Escape") {
      event.preventDefault();
      closeMenu();
    }
  });
  menu.addEventListener("mousedown", (event) => event.preventDefault());
  menu.addEventListener("click", (event) => {
    const button = event.target.closest("[data-slash-command]");
    const command = assistantSlashCommands.find((item) => item.name === button?.dataset.slashCommand);
    if (command) selectCommand(command);
  });
  input.addEventListener("blur", () => window.setTimeout(closeMenu, 120));
}

function parseAssistantCommand(message) {
  const value = String(message || "").trim();
  if (!value.startsWith("/")) return null;
  const tokens = shellLikeTokens(value.slice(1));
  const name = (tokens.shift() || "").toLowerCase();
  if (!name) return null;
  const args = { _: [] };
  for (let index = 0; index < tokens.length; index += 1) {
    const token = tokens[index];
    if (token.startsWith("--")) {
      const inline = token.indexOf("=");
      if (inline > 2) {
        args[token.slice(2, inline)] = token.slice(inline + 1);
      } else {
        const key = token.slice(2);
        const next = tokens[index + 1];
        if (next && !next.startsWith("--")) {
          args[key] = next;
          index += 1;
        } else {
          args[key] = true;
        }
      }
    } else {
      args._.push(token);
    }
  }
  return { name, args, raw: value };
}

function shellLikeTokens(value) {
  const matches = String(value || "").match(/"[^"]*"|'[^']*'|\S+/g) || [];
  return matches.map((token) => {
    if ((token.startsWith('"') && token.endsWith('"')) || (token.startsWith("'") && token.endsWith("'"))) {
      return token.slice(1, -1);
    }
    return token;
  });
}

function commandText(command) {
  return (command?.args?._ || []).join(" ").trim();
}

function commandArg(command, ...keys) {
  for (const key of keys) {
    const value = command?.args?.[key];
    if (value !== undefined && value !== true) return String(value);
  }
  return "";
}

function commandScope(command, fallback = []) {
  const value = commandArg(command, "cat", "category", "categories", "scope");
  if (!value) return fallback;
  return value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

async function runDueTaskUpdates(limit = 5) {
  if (!activeUserId || activeUserId === "default") return { ran_count: 0, items: [], notifications: [] };
  return request("/api/tasks/due/run", {
    method: "POST",
    body: JSON.stringify({ user_id: activeUserId, limit }),
  });
}

async function loadTaskNotifications(targetSelector = "[data-notifications]", options = {}) {
  if (!activeUserId || activeUserId === "default") return [];
  const unreadOnly = options.unreadOnly ? "true" : "false";
  const limit = options.limit || 10;
  const data = await request(`/api/notifications?user_id=${encodeURIComponent(activeUserId)}&unread_only=${unreadOnly}&limit=${limit}`);
  renderTaskNotifications(targetSelector, data.items || []);
  announceBrowserNotifications(data.items || []);
  return data.items || [];
}

function renderTaskNotifications(targetSelector, items) {
  const target = document.querySelector(targetSelector);
  if (!target) return;
  if (!items.length) {
    target.innerHTML = `<div class="empty-state compact-empty">暂无更新</div>`;
    return;
  }
  target.innerHTML = items
    .map(
      (item) => `<article class="${item.read_at ? "read" : "unread"}">
        <div>
          <strong>${escapeHtml(item.title)}</strong>
          <p>${escapeHtml(item.body || "")}</p>
          <span>${escapeHtml(notificationTimeLabel(item.created_at))}</span>
        </div>
        ${item.read_at ? "" : `<button type="button" data-notification-read="${escapeAttr(item.id)}" title="点此标为已读">未读</button>`}
      </article>`
    )
    .join("");
}

function bindNotificationReads(targetSelector = "[data-notifications]") {
  document.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-notification-read]");
    if (!button) return;
    event.preventDefault();
    await request(`/api/notifications/${encodeURIComponent(button.dataset.notificationRead)}/read`, {
      method: "POST",
      body: JSON.stringify({ user_id: activeUserId }),
    });
    await loadTaskNotifications(targetSelector);
  });
}

function startTaskPushPolling(targetSelector = "[data-notifications]", intervalMs = 60000) {
  const tick = async () => {
    if (!activeUserId || activeUserId === "default") return;
    try {
      await runDueTaskUpdates(5);
      await loadTaskNotifications(targetSelector, { limit: 8 });
    } catch (error) {
      // Keep polling quiet; visible panels still show explicit action errors.
    }
  };
  tick();
  return window.setInterval(tick, intervalMs);
}

async function enableBrowserNotifications(statusSelector = "[data-notification-status]") {
  const status = document.querySelector(statusSelector);
  if (!("Notification" in window)) {
    if (status) status.textContent = "当前浏览器不支持系统提醒。";
    return false;
  }
  const permission = await Notification.requestPermission();
  if (status) status.textContent = permission === "granted" ? "浏览器提醒已开启。" : "浏览器提醒未开启。";
  return permission === "granted";
}

function announceBrowserNotifications(items) {
  if (!("Notification" in window) || Notification.permission !== "granted") return;
  const key = `pna_notified_${activeUserId}`;
  const seen = new Set(JSON.parse(localStorage.getItem(key) || "[]"));
  const nextSeen = new Set(seen);
  items
    .filter((item) => !item.read_at && !seen.has(item.id))
    .slice(0, 3)
    .forEach((item) => {
      new Notification(item.title, { body: item.body || "" });
      nextSeen.add(item.id);
    });
  localStorage.setItem(key, JSON.stringify(Array.from(nextSeen).slice(-100)));
}

function notificationTimeLabel(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleString("zh-CN", { hour12: false, month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function escapeAttr(value) {
  return escapeHtml(value).replaceAll("'", "&#39;");
}

renderUser();
