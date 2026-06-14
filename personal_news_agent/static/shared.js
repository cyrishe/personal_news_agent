let activeUserId = localStorage.getItem("pna_user_id") || "default";
let conversationId = localStorage.getItem("pna_conversation_id") || null;

async function request(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) throw new Error(await formatApiError(response));
  return response.json();
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
  return `<article class="item">
    <div class="title">${escapeHtml(item.title)}</div>
    <div class="meta">${escapeHtml(item.category)} · ${item.article_count}篇 · 热度${item.hot_score}</div>
    <div class="summary">${escapeHtml((item.keywords || []).join(" / "))}</div>
  </article>`;
}

async function registerFromForm(form) {
  const formData = new FormData(form);
  const payload = {
    username: formData.get("username"),
    password: formData.get("password"),
    confirm_password: formData.get("confirm_password"),
    real_name: formData.get("real_name"),
    mobile: formData.get("mobile"),
  };
  const result = await request("/api/auth/register", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  saveSession(result);
  return result;
}

async function loadOnboardingOptions(formSelector) {
  const form = document.querySelector(formSelector);
  if (!form) return;
  const data = await request("/api/onboarding/options");
  const categories = form.querySelector("[data-category-options]");
  if (categories) {
    categories.innerHTML = data.categories
      .map((item) => {
        const checked = data.default_categories.includes(item.key) ? "checked" : "";
        const disabledLabel = item.implemented ? "" : "（后续）";
        return `<label><input type="checkbox" name="preferred_categories" value="${escapeAttr(item.key)}" ${checked} /> ${escapeHtml(item.name)}${disabledLabel}</label>`;
      })
      .join("");
  }
  const modelSelect = form.querySelector("[name=model_key]");
  if (modelSelect) {
    modelSelect.innerHTML = data.models
      .map((item) => `<option value="${escapeAttr(item.key)}">${escapeHtml(item.name)} · ${escapeHtml(item.provider_model)}</option>`)
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
    body: JSON.stringify({ username: formData.get("username"), password: formData.get("password") }),
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
  document.querySelector(target).innerHTML = data.items.map(itemHtml).join("");
}

async function loadEvents(target = "#events", category = "", limit = 8) {
  const data = await request(`/api/events?limit=${limit}${category ? `&category=${category}` : ""}`);
  document.querySelector(target).innerHTML = data.items.map(eventHtml).join("");
}

async function sendChat(message, target = "#messages") {
  const targetNode = document.querySelector(target) || document.querySelector("#messages");
  targetNode.classList.add("chat-stream");
  targetNode.appendChild(chatTurn("user", message));
  const assistantNode = chatTurn("assistant", "", true);
  targetNode.appendChild(assistantNode);
  targetNode.scrollTop = targetNode.scrollHeight;
  return sendChatIntoTurn(message, assistantNode, target);
}

async function sendChatIntoTurn(message, assistantNode, target = "#messages") {
  const chatContext = window.currentChatContext || {};
  const targetNode = document.querySelector(target) || document.querySelector("#messages");
  targetNode.classList.add("chat-stream");
  const payload = {
    conversation_id: conversationId,
    message,
    topic: chatContext.topic || null,
    category_scope: chatContext.category_scope || null,
    use_llm: Boolean(chatContext.use_llm),
  };
  try {
    const streamed = await streamChat(payload, assistantNode, targetNode);
    if (streamed) return streamed;
  } catch (error) {
    assistantNode.innerHTML = `<div class="trace-loading">流式连接中断，切换为普通请求...</div>`;
  }
  const data = await request("/api/chat", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  conversationId = data.conversation_id;
  localStorage.setItem("pna_conversation_id", conversationId);
  assistantNode.innerHTML = chatResponseHtml(data);
  targetNode.scrollTop = targetNode.scrollHeight;
  return data;
}

function appendLocalTurn(role, text, target = "#messages", loading = false) {
  const targetNode = document.querySelector(target) || document.querySelector("#messages");
  targetNode.classList.add("chat-stream");
  const node = loading ? chatTurn("assistant", "", true) : chatTurn(role, text);
  targetNode.appendChild(node);
  targetNode.scrollTop = targetNode.scrollHeight;
  return node;
}

function setAssistantTurnText(node, text) {
  if (!node) return;
  node.innerHTML = `<div class="assistant-markdown"><p>${escapeHtml(text)}</p></div>`;
}

async function streamChat(payload, assistantNode, targetNode) {
  const response = await fetch("/api/chat/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok || !response.body) return null;
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
      } else if (event.type === "trace" && event.item) {
        state.research_trace.push(event.item);
        state.stream_status = event.item.message || event.item.stage || "执行中。";
      } else if (event.type === "final" && event.response) {
        conversationId = event.response.conversation_id || conversationId;
        if (conversationId) localStorage.setItem("pna_conversation_id", conversationId);
        assistantNode.innerHTML = chatResponseHtml(event.response);
        targetNode.scrollTop = targetNode.scrollHeight;
        return event.response;
      } else if (event.type === "error") {
        throw new Error(event.message || "流式请求失败");
      }
      assistantNode.innerHTML = chatStreamingHtml(state);
      targetNode.scrollTop = targetNode.scrollHeight;
    }
  }
  return null;
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
    wrapper.innerHTML = `<div class="trace-loading">搜集线索中...</div>`;
  } else {
    wrapper.innerHTML = `<div class="chat-bubble">${escapeHtml(text)}</div>`;
  }
  return wrapper;
}

function chatResponseHtml(data) {
  const trace = renderResearchTrace(data.research_trace || []);
  const answer = renderMarkdown(data.markdown || data.answer || "");
  const timeline = renderChatEventLine(data.event_line);
  const sources = renderChatSources(data.evidence || []);
  return `${trace}<div class="assistant-markdown">${answer}</div>${timeline}${sources}`;
}

function chatStreamingHtml(state) {
  const trace = renderResearchTrace(state.research_trace || []);
  return `${trace}<div class="stream-status">${escapeHtml(state.stream_status || "执行中。")}</div>`;
}

function renderResearchTrace(items) {
  if (!items.length) return "";
  return `<div class="research-trace">${items
    .map((item) => {
      const count = Number.isFinite(Number(item.count)) ? Number(item.count) : "";
      return `<div class="trace-step ${escapeAttr(item.status || "")}"><strong>${escapeHtml(item.stage || "")}</strong><span>${escapeHtml(count)}</span><p>${escapeHtml(item.message || "")}</p></div>`;
    })
    .join("")}</div>`;
}

function renderChatEventLine(eventLine) {
  const items = (eventLine && eventLine.items) || [];
  if (!items.length) return "";
  return `<div class="chat-event-line">${items
    .slice(0, 6)
    .map(
      (item) =>
        `<div class="chat-event"><time>${escapeHtml(item.date || "")}</time><strong>${escapeHtml(item.title || "")}</strong><p>${escapeHtml(item.summary || "")}</p></div>`
    )
    .join("")}</div>`;
}

function renderChatSources(items) {
  if (!items.length) return "";
  return `<details class="chat-sources"><summary>证据 ${items.length}</summary>${items
    .slice(0, 8)
    .map(
      (item) =>
        `<a href="${escapeAttr(item.url || "#")}" target="_blank" rel="noreferrer"><span>[${escapeHtml(item.index || "")}]</span>${escapeHtml(item.title || "")}<small>${escapeHtml(item.source_id || "")} ${escapeHtml(item.published_at || "")}</small></a>`
    )
    .join("")}</details>`;
}

function renderMarkdown(markdown) {
  const lines = String(markdown || "").split(/\r?\n/);
  let html = "";
  let inList = false;
  const closeList = () => {
    if (inList) {
      html += "</ul>";
      inList = false;
    }
  };
  lines.forEach((line) => {
    const trimmed = line.trim();
    if (!trimmed) {
      closeList();
      return;
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
    } else if (trimmed.startsWith("- ")) {
      if (!inList) {
        html += "<ul>";
        inList = true;
      }
      html += `<li>${renderInlineMarkdown(trimmed.slice(2))}</li>`;
    } else if (trimmed.startsWith("> ")) {
      closeList();
      html += `<blockquote>${renderInlineMarkdown(trimmed.slice(2))}</blockquote>`;
    } else {
      closeList();
      html += `<p>${renderInlineMarkdown(trimmed)}</p>`;
    }
  });
  closeList();
  return html;
}

function renderInlineMarkdown(value) {
  return escapeHtml(value)
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/`(.+?)`/g, "<code>$1</code>");
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
    await sendChat(button.dataset.ask || button.textContent || "");
  });
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
        ${item.read_at ? "" : `<button type="button" data-notification-read="${escapeAttr(item.id)}">已读</button>`}
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
