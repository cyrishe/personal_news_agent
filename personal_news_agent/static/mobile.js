let mobileCategory = "";
const mobileState = {
  topic: "张雪机车",
  categoryScope: [],
};
syncMobileChatContext();
syncMobileSessionState();

document.querySelectorAll("[data-auth-mode-target]").forEach((button) => {
  button.addEventListener("click", () => {
    const target = button.dataset.authModeTarget;
    document.querySelectorAll("[data-auth-panel]").forEach((panel) => {
      panel.hidden = panel.dataset.authPanel !== target;
    });
  });
});

async function refreshMobile() {
  await Promise.all([loadFeed(mobileCategory, 8), loadEvents("#events", mobileCategory, 5), loadTaskNotifications()]);
}

document.querySelectorAll("#mobileTabs button").forEach((button) => {
  button.addEventListener("click", async () => {
    document.querySelectorAll("#mobileTabs button").forEach((node) => node.classList.remove("active"));
    button.classList.add("active");
    mobileCategory = button.dataset.category || "";
    mobileState.categoryScope = mobileCategory ? [mobileCategory] : [];
    syncMobileChatContext();
    await refreshMobile();
  });
});

document.querySelector("#registerForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector("button");
  const status = document.querySelector("#registerStatus");
  if (button) button.disabled = true;
  if (status) status.textContent = "正在创建账号并进行实名手机号核验。";
  try {
    const result = await registerFromForm(form);
    document.querySelector("#registerStatus").textContent = `已创建：${result.user.display_name}`;
    syncMobileSessionState();
    showOnboardingForm();
    document.querySelector(".auth-card details").open = true;
    await loadProfileIntoForm("#onboardingForm");
    await refreshMobile();
  } catch (error) {
    document.querySelector("#registerStatus").textContent = error.message;
  } finally {
    if (button) button.disabled = false;
  }
});

document.querySelector("#onboardingForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector("button");
  const status = document.querySelector("#registerStatus");
  if (button) button.disabled = true;
  if (status) status.textContent = "正在保存初始化配置。";
  try {
    const result = await completeOnboardingFromForm(form);
    document.querySelector("#registerStatus").textContent = `初始化完成：${result.model.name}`;
    await refreshMobile();
  } catch (error) {
    document.querySelector("#registerStatus").textContent = error.message;
  } finally {
    if (button) button.disabled = false;
  }
});

document.querySelector("#loginForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector("button");
  const status = document.querySelector("#registerStatus");
  if (button) button.disabled = true;
  if (status) status.textContent = "正在登录。";
  try {
    const result = await loginFromForm(form);
    document.querySelector("#registerStatus").textContent = `已登录：${result.user.display_name}`;
    syncMobileSessionState();
    await loadProfileIntoForm("#onboardingForm");
    await refreshMobile();
  } catch (error) {
    document.querySelector("#registerStatus").textContent = error.message;
  } finally {
    if (button) button.disabled = false;
  }
});

document.querySelector("#chatForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = document.querySelector("#message");
  const message = input.value.trim();
  if (!message) return;
  await handleMobileAssistantInput(message);
  input.value = "";
});

document.querySelector("#editProfileMobile").addEventListener("click", async () => {
  document.querySelector(".auth-card details").open = true;
  showOnboardingForm();
  await loadProfileIntoForm("#onboardingForm");
});

document.querySelector("#enableBrowserPushMobile")?.addEventListener("click", async () => {
  await enableBrowserNotifications();
  await loadTaskNotifications();
});

bindAskButtons();
bindNotificationReads();
window.handleAssistantInput = handleMobileAssistantInput;
loadOnboardingOptions("#onboardingForm").then(() => loadProfileIntoForm("#onboardingForm"));
startTaskPushPolling();
refreshMobile();

function showOnboardingForm() {
  const form = document.querySelector("#onboardingForm");
  if (form) form.hidden = false;
}

async function handleMobileAssistantInput(message) {
  const command = parseAssistantCommand(message);
  if (!command) return sendChat(message);

  appendLocalTurn("user", message);
  const assistantNode = appendLocalTurn("assistant", "", "#messages", true);
  try {
    if (["search", "s", "news"].includes(command.name)) {
      applyMobileTopicCommand(command);
      return sendChatIntoTurn(`${mobileState.topic} 最新新闻，按来源搜索、正文抓取、证据合并和事件线处理。`, assistantNode);
    }
    if (["deep", "dive", "deep-dive"].includes(command.name)) {
      applyMobileTopicCommand(command);
      return sendChatIntoTurn(`围绕${mobileState.topic}做一次深度挖掘，按最新进展、关键主体和不确定性总结。`, assistantNode);
    }
    if (["topic", "t"].includes(command.name)) {
      applyMobileTopicCommand(command);
      setAssistantTurnText(assistantNode, `已切换：${mobileState.topic}`);
      return null;
    }
    if (["task", "track"].includes(command.name)) {
      applyMobileTopicCommand(command);
      const result = await request("/api/tasks", {
        method: "POST",
        body: JSON.stringify({
          user_id: activeUserId,
          task_type: commandArg(command, "type") || "topic_tracking",
          schedule: commandArg(command, "every", "schedule", "cron") || "*/20 * * * *",
          category_scope: mobileState.categoryScope,
          topics: [mobileState.topic],
          output_style: "事件线+关系网",
          delivery_channel: commandArg(command, "push", "channel", "delivery") || "in_app",
        }),
      });
      await loadTaskNotifications();
      setAssistantTurnText(assistantNode, `已保存跟踪：${mobileState.topic}`);
      return result;
    }
    if (["feed"].includes(command.name)) {
      mobileCategory = commandArg(command, "cat", "category") || commandText(command) || "";
      mobileState.categoryScope = mobileCategory ? [mobileCategory] : [];
      syncMobileChatContext();
      syncMobileTabs();
      await refreshMobile();
      setAssistantTurnText(assistantNode, `已更新信息流${mobileCategory ? `：${mobileCategory}` : "。"}。`);
      return null;
    }
    setAssistantTurnText(assistantNode, "可执行：/search、/topic、/task、/deep、/feed。");
    return null;
  } catch (error) {
    setAssistantTurnText(assistantNode, error.message);
    return null;
  }
}

function applyMobileTopicCommand(command) {
  const topic = commandText(command) || commandArg(command, "topic", "q", "query");
  const scope = commandScope(command, mobileState.categoryScope);
  if (topic) mobileState.topic = topic;
  mobileState.categoryScope = scope;
  mobileCategory = scope[0] || mobileCategory;
  syncMobileTabs();
  syncMobileChatContext();
}

function syncMobileChatContext() {
  window.currentChatContext = {
    topic: mobileState.topic,
    category_scope: mobileState.categoryScope,
    use_llm: true,
  };
}

function syncMobileTabs() {
  document.querySelectorAll("#mobileTabs button").forEach((button) => {
    button.classList.toggle("active", (button.dataset.category || "") === mobileCategory);
  });
}

function syncMobileSessionState() {
  const loggedIn = activeUserId && activeUserId !== "default";
  document.body.classList.toggle("mobile-logged-in", loggedIn);
  document.body.classList.toggle("mobile-logged-out", !loggedIn);
  const account = document.querySelector(".auth-card details");
  if (account) account.open = !loggedIn;
}
