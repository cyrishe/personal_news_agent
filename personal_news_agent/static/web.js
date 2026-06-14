if (activeUserId === "default") {
  window.location.replace("/auth");
} else {
  request(`/api/profile?user_id=${encodeURIComponent(activeUserId)}`)
    .then((data) => {
      if (!data.user) {
        localStorage.removeItem("pna_user_id");
        localStorage.removeItem("pna_user_name");
        localStorage.removeItem("pna_session_token");
        window.location.replace("/auth");
      }
    })
    .catch(() => {});
}

const consoleState = {
  topic: "张雪机车",
  categoryScope: ["sports"],
  view: "event-line",
  topicPayload: null,
};
syncChatContext();
syncContextDock();

document.querySelector("#refresh")?.addEventListener("click", () => refreshWeb());
document.querySelector("#feedCategory")?.addEventListener("change", () => loadFeedAndEvents());

document.querySelector("#openConfig")?.addEventListener("click", async () => {
  document.querySelector("#configDialog").showModal();
  await loadProfileIntoForm("#onboardingForm");
});

document.querySelector("#onboardingForm")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const result = await completeOnboardingFromForm(event.currentTarget);
    document.querySelector("#onboardingStatus").textContent = `已保存：${result.model.name}`;
    document.querySelector("#assistantPromptPreview").textContent = result.assistant_prompt;
    await refreshWeb();
  } catch (error) {
    document.querySelector("#onboardingStatus").textContent = error.message;
  }
});

document.querySelector("#topicForm")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  consoleState.topic = document.querySelector("#topicInput").value.trim() || consoleState.topic;
  consoleState.categoryScope = parseScope(document.querySelector("#categoryScope").value);
  syncChatContext();
  syncContextDock();
  syncTaskTopic();
  await loadTopicView();
  await loadDueUrls();
});

document.querySelector("#nativeIngest")?.addEventListener("click", async () => {
  await runNativeIngest();
});

document.querySelector("#deepDive")?.addEventListener("click", async () => {
  await runDeepDive();
});

document.querySelector("#createTopicTask")?.addEventListener("click", async () => {
  await createTrackingTask(document.querySelector("#taskForm"));
});

document.querySelector("#generateReport")?.addEventListener("click", async () => {
  await generateTopicReport();
});

document.querySelector("#enableBrowserPush")?.addEventListener("click", async () => {
  await enableBrowserNotifications();
  await loadTaskNotifications();
});

document.querySelectorAll("[data-action]").forEach((button) => {
  button.addEventListener("click", async () => {
    const action = button.dataset.action;
    if (action === "deep-dive-chat") {
      await runDeepDive();
      await sendChat(`围绕${consoleState.topic}做一次深度挖掘，按最新进展、关键主体和不确定性总结。`);
    } else if (action === "make-topic") {
      await loadTopicView();
      await sendChat(`把${consoleState.topic}整理成专题，给我事件线和关系网观察重点。`);
    } else if (action === "make-task") {
      await createTrackingTask(document.querySelector("#taskForm"));
      await sendChat(`已把${consoleState.topic}设为跟踪主题，告诉我后续应该重点盯哪些变化。`);
    } else if (action === "make-report") {
      await generateTopicReport();
    }
  });
});

document.querySelectorAll(".topic-card").forEach((button) => {
  button.addEventListener("click", async () => {
    document.querySelectorAll(".topic-card").forEach((item) => item.classList.toggle("active", item === button));
    consoleState.topic = button.dataset.topicTitle || button.textContent.trim();
    consoleState.categoryScope = parseScope(button.dataset.categoryScope || "");
    syncChatContext();
    syncContextDock();
    document.querySelector("#topicInput").value = consoleState.topic;
    document.querySelector("#categoryScope").value = button.dataset.categoryScope || "";
    syncTaskTopic();
    await loadTopicView();
    await loadDueUrls();
  });
});

document.querySelectorAll("[data-topic-view]").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll("[data-topic-view]").forEach((item) => item.classList.toggle("active", item === button));
    consoleState.view = button.dataset.topicView;
    syncContextDock();
    renderTopicVisual(consoleState.topicPayload, consoleState.view);
  });
});

document.querySelector("#chatForm")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = document.querySelector("#message");
  const message = input.value.trim();
  if (!message) return;
  await handleAssistantInput(message);
  input.value = "";
});

document.querySelector("#taskForm")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  await createTrackingTask(event.currentTarget);
});

bindAskButtons();
bindNotificationReads();
window.handleAssistantInput = handleAssistantInput;
loadOnboardingOptions("#onboardingForm").then(() => loadProfileIntoForm("#onboardingForm"));
startTaskPushPolling();
refreshWeb();

async function refreshWeb() {
  setStatus("正在刷新数据。");
  await Promise.all([loadSystemStatus(), loadSourceSummary(), loadFeedAndEvents(), loadDueUrls(), loadTasks(), loadTaskNotifications()]);
  await loadTopicView();
  setStatus("已更新。");
}

async function handleAssistantInput(message) {
  const command = parseAssistantCommand(message);
  if (!command) return sendChat(message);

  appendLocalTurn("user", message);
  const assistantNode = appendLocalTurn("assistant", "", "#messages", true);
  try {
    if (["search", "s", "news"].includes(command.name)) {
      await applyTopicCommand(command);
      return sendChatIntoTurn(`${consoleState.topic} 最新新闻，按来源搜索、正文抓取、证据合并和事件线处理。`, assistantNode);
    }
    if (["deep", "dive", "deep-dive"].includes(command.name)) {
      await applyTopicCommand(command);
      await runDeepDive();
      return sendChatIntoTurn(`围绕${consoleState.topic}做一次深度挖掘，按最新进展、关键主体和不确定性总结。`, assistantNode);
    }
    if (["topic", "t"].includes(command.name)) {
      await applyTopicCommand(command);
      setAssistantTurnText(assistantNode, `已切换：${consoleState.topic}`);
      return null;
    }
    if (["task", "track"].includes(command.name)) {
      await applyTopicCommand(command, { reload: false });
      applyTaskCommand(command);
      const result = await createTrackingTask(document.querySelector("#taskForm"));
      setAssistantTurnText(assistantNode, result ? `已保存跟踪：${consoleState.topic}` : "保存失败。");
      return result;
    }
    if (["report", "r"].includes(command.name)) {
      await applyTopicCommand(command);
      const result = await generateTopicReport({ chatFollowup: false });
      setAssistantTurnText(assistantNode, result ? `报告已生成：${result.report_id}` : "报告生成失败。");
      return result;
    }
    if (["ingest", "source"].includes(command.name)) {
      await applyTopicCommand(command);
      const result = await runNativeIngest();
      setAssistantTurnText(assistantNode, result ? `源搜索入库完成：${consoleState.topic}` : "源搜索入库失败。");
      return result;
    }
    if (["feed"].includes(command.name)) {
      const category = commandArg(command, "cat", "category") || commandText(command);
      if (document.querySelector("#feedCategory")) document.querySelector("#feedCategory").value = category;
      await loadFeedAndEvents();
      setAssistantTurnText(assistantNode, `已更新信息流${category ? `：${category}` : "。"}。`);
      return null;
    }
    setAssistantTurnText(assistantNode, "可执行：/search、/topic、/task、/deep、/report、/ingest、/feed。");
    return null;
  } catch (error) {
    setAssistantTurnText(assistantNode, error.message);
    return null;
  }
}

async function applyTopicCommand(command, options = {}) {
  const reload = options.reload !== false;
  const topic = commandText(command) || commandArg(command, "topic", "q", "query");
  const scope = commandScope(command, consoleState.categoryScope);
  if (topic) consoleState.topic = topic;
  consoleState.categoryScope = scope;
  const topicInput = document.querySelector("#topicInput");
  const categorySelect = document.querySelector("#categoryScope");
  if (topicInput) topicInput.value = consoleState.topic;
  if (categorySelect) categorySelect.value = consoleState.categoryScope.join(",");
  syncChatContext();
  syncContextDock();
  syncTaskTopic();
  if (reload) await Promise.all([loadTopicView(), loadDueUrls()]);
}

function applyTaskCommand(command) {
  const form = document.querySelector("#taskForm");
  if (!form) return;
  const schedule = commandArg(command, "every", "schedule", "cron");
  const taskType = commandArg(command, "type");
  const delivery = commandArg(command, "push", "channel", "delivery");
  if (schedule) form.elements.schedule.value = schedule;
  if (taskType) form.elements.task_type.value = taskType;
  if (delivery && form.elements.delivery_channel) form.elements.delivery_channel.value = delivery;
  if (consoleState.categoryScope[0]) form.elements.category.value = consoleState.categoryScope[0];
  form.elements.topic.value = consoleState.topic;
}

async function loadSystemStatus() {
  try {
    const data = await request("/api/news/search/backend");
    const es = data.elasticsearch || {};
    const urlStore = data.crawl_url_store || {};
    document.querySelector("[data-es-status]").textContent = `ES ${es.ready ? "ready" : "down"} · ${es.cluster_status || "--"}`;
    document.querySelector("[data-mysql-status]").textContent = `MySQL ${urlStore.mysql_ready ? "ready" : "down"}`;
  } catch (error) {
    document.querySelector("[data-es-status]").textContent = "ES --";
    document.querySelector("[data-mysql-status]").textContent = "MySQL --";
  }
}

async function loadSourceSummary() {
  try {
    const data = await request("/api/sources/summary");
    document.querySelector("[data-source-count]").textContent = `源 ${data.source_count || 0}`;
    document.querySelector("[data-metric-sources]").textContent = data.source_count || 0;
    document.querySelector("[data-metric-crawlable]").textContent = data.crawlable_sources || 0;
    document.querySelector("[data-metric-searchable]").textContent = data.searchable_sources || 0;
    const tags = Object.entries(data.categories || {})
      .sort((a, b) => b[1] - a[1])
      .slice(0, 8);
    document.querySelector("[data-source-tags]").innerHTML = tags
      .map(([name, count]) => `<span>${escapeHtml(name)} ${count}</span>`)
      .join("");
  } catch (error) {
    document.querySelector("[data-source-tags]").textContent = error.message;
  }
}

async function loadFeedAndEvents() {
  const category = document.querySelector("#feedCategory")?.value || "";
  await Promise.all([loadFeed(category, 8, "#feed"), loadEvents("#events", category, 6)]);
}

async function loadDueUrls() {
  const category = consoleState.categoryScope[0] || "";
  try {
    const data = await request(`/api/crawl/urls/due?limit=8${category ? `&category=${encodeURIComponent(category)}` : ""}`);
    renderDueUrls(data.items || []);
  } catch (error) {
    document.querySelector("[data-due-urls]").textContent = error.message;
  }
}

async function loadTasks() {
  const target = document.querySelector("[data-task-list]");
  if (!target || !activeUserId || activeUserId === "default") return;
  try {
    const data = await request(`/api/tasks?user_id=${encodeURIComponent(activeUserId)}&limit=8`);
    renderTasks(data.items || []);
  } catch (error) {
    target.textContent = error.message;
  }
}

async function loadTopicView() {
  syncChatContext();
  try {
    const payload = await request("/api/topics/view", {
      method: "POST",
      body: JSON.stringify({
        topic: consoleState.topic,
        category_scope: consoleState.categoryScope,
        max_articles: 18,
      }),
    });
    consoleState.topicPayload = payload;
    renderTopicHeader(payload);
    renderTopicVisual(payload, consoleState.view);
    renderEvidence(payload.source_articles || []);
  } catch (error) {
    document.querySelector("[data-topic-visual]").textContent = error.message;
  }
}

async function runNativeIngest() {
  setStatus("源搜索入库中。");
  const button = document.querySelector("#nativeIngest");
  button.disabled = true;
  try {
    const data = await request("/api/news/search/ingest", {
      method: "POST",
      body: JSON.stringify({
        query: consoleState.topic,
        category_scope: consoleState.categoryScope,
        max_results: 12,
        fetch_articles: 8,
        follow_depth: 1,
        follow_limit_per_article: 2,
      }),
    });
    setStatus(`入库完成：发现 ${data.discovered_count || 0}，索引 ${data.indexed_count || 0}。`);
    await Promise.all([loadTopicView(), loadDueUrls(), loadFeedAndEvents()]);
    return data;
  } catch (error) {
    setStatus(error.message);
    return null;
  } finally {
    button.disabled = false;
  }
}

async function runDeepDive() {
  setStatus("深度挖掘中。");
  const target = document.querySelector("[data-deep-dive-output]");
  target.textContent = "运行中";
  try {
    const data = await request("/api/news/deep-dive", {
      method: "POST",
      body: JSON.stringify({
        query: consoleState.topic,
        category_scope: consoleState.categoryScope,
        rounds: 2,
        breadth: 4,
      }),
    });
    renderDeepDive(data);
    setStatus("深度挖掘完成。");
  } catch (error) {
    target.textContent = error.message;
    setStatus(error.message);
  }
}

async function createTrackingTask(form) {
  if (!form) return null;
  const formData = new FormData(form);
  const category = formData.get("category");
  const taskType = formData.get("task_type") || "topic_tracking";
  try {
    const data = await request("/api/tasks", {
      method: "POST",
      body: JSON.stringify({
        user_id: activeUserId,
        task_type: taskType,
        schedule: formData.get("schedule") || "*/20 * * * *",
        category_scope: category ? [category] : [],
        topics: [formData.get("topic") || consoleState.topic],
        output_style: "事件线+关系网",
        delivery_channel: formData.get("delivery_channel") || "in_app",
      }),
    });
    document.querySelector("[data-task-status]").textContent = `已保存：${nextRunLabel(data.next_run_at)}`;
    setStatus(`任务已保存：${data.id || "task"}`);
    await loadTasks();
    return data;
  } catch (error) {
    document.querySelector("[data-task-status]").textContent = error.message;
    setStatus(error.message);
    return null;
  }
}

function renderTasks(items) {
  const target = document.querySelector("[data-task-list]");
  if (!target) return;
  if (!items.length) {
    target.innerHTML = `<div class="empty-state compact-empty">暂无任务</div>`;
    return;
  }
  target.innerHTML = items
    .map(
      (item) => `<article>
        <strong>${escapeHtml((item.topics || []).join("、") || taskTypeLabel(item.task_type))}</strong>
        <span>${escapeHtml(taskTypeLabel(item.task_type))} · ${escapeHtml(item.schedule_cron)} · ${escapeHtml(nextRunLabel(item.next_run_at))}</span>
      </article>`
    )
    .join("");
}

function taskTypeLabel(value) {
  const labels = {
    topic_tracking: "专题跟踪",
    daily_digest: "每日摘要",
    weekly_digest: "每周摘要",
  };
  return labels[value] || value || "任务";
}

function nextRunLabel(value) {
  if (!value) return "未定时";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "未定时";
  return date.toLocaleString("zh-CN", { hour12: false, month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

async function generateTopicReport(options = {}) {
  const chatFollowup = options.chatFollowup !== false;
  setStatus("正在生成专题报告。");
  try {
    const data = await request("/api/reports", {
      method: "POST",
      body: JSON.stringify({
        user_id: activeUserId,
        topic: consoleState.topic,
        category_scope: consoleState.categoryScope,
        time_range: "30d",
        report_type: "timeline_analysis",
      }),
    });
    renderReportCard(data);
    if (chatFollowup) {
      await sendChat(`基于当前${consoleState.topic}专题报告，提炼最适合继续追问的三个问题。`);
    }
    setStatus(`报告已生成：${data.report_id}`);
    return data;
  } catch (error) {
    setStatus(error.message);
    return null;
  }
}

function renderReportCard(data) {
  const target = document.querySelector("[data-deep-dive-output]");
  if (!target) return;
  const sections = data.sections || {};
  const summary = sections["一、结论摘要"] || sections.summary || "";
  target.innerHTML = `<div class="deep-section">
    <strong>${escapeHtml(data.topic || consoleState.topic)}</strong>
    <p>${escapeHtml(summary)}</p>
    <span>${escapeHtml(data.report_id || "")}</span>
  </div>`;
}

function renderTopicHeader(payload) {
  const events = payload.event_line?.items || [];
  const nodes = payload.relation_graph?.nodes || [];
  const articles = payload.source_articles || [];
  const latest = events[events.length - 1];
  document.querySelector("[data-topic-title]").textContent = payload.topic?.title || consoleState.topic;
  document.querySelector("[data-dialog-context]").textContent = `${payload.topic?.title || consoleState.topic} · ${articles.length || payload.build?.article_count || 0} 条证据，${events.length} 个事件。`;
  document.querySelector("[data-topic-summary]").textContent = latest
    ? `${latest.date} · ${latest.title}`
    : `${payload.build?.article_count || 0} 条资料用于构建专题。`;
  document.querySelector("[data-topic-article-count]").textContent = articles.length || payload.build?.article_count || 0;
  document.querySelector("[data-topic-event-count]").textContent = events.length;
  document.querySelector("[data-topic-node-count]").textContent = nodes.length;
  syncContextDock();
}

function renderTopicVisual(payload, viewType = "event-line") {
  const target = document.querySelector("[data-topic-visual]");
  if (!target || !payload) return;
  target.innerHTML = viewType === "relation-graph" ? relationGraphHtml(payload.relation_graph) : eventLineHtml(payload.event_line);
}

function eventLineHtml(eventLine) {
  const items = eventLine?.items || [];
  if (!items.length) return `<div class="empty-state">暂无事件</div>`;
  return `<div class="topic-event-line console-event-line">
    ${items
      .map((item) => {
        const tags = [...(item.actors || []), ...(item.keywords || [])].slice(0, 4);
        const dateLabel = item.date_source === "fetched_at" ? `抓取 ${item.date}` : item.date;
        return `<article class="topic-event ${escapeAttr(item.stage)}">
          <time>${escapeHtml(dateLabel)}</time>
          <div>
            <strong>${escapeHtml(item.title)}</strong>
            <p>${escapeHtml(item.summary)}</p>
            <span>${escapeHtml(tags.join(" / "))}</span>
          </div>
        </article>`;
      })
      .join("")}
  </div>`;
}

function relationGraphHtml(graph) {
  const nodes = graph?.nodes || [];
  const edges = graph?.edges || [];
  if (!nodes.length) return `<div class="empty-state">暂无关系节点</div>`;
  const positions = graphPositions(nodes, 760, 420);
  const lines = edges
    .map((edge) => {
      const source = positions[edge.source];
      const target = positions[edge.target];
      if (!source || !target) return "";
      return `<line x1="${source.x}" y1="${source.y}" x2="${target.x}" y2="${target.y}" stroke-width="${Math.min(5, 1 + edge.weight)}"><title>${escapeHtml(edge.label || "")}</title></line>`;
    })
    .join("");
  const circles = nodes
    .map((node) => {
      const pos = positions[node.id];
      const radius = node.type === "topic" ? 46 : Math.max(25, Math.min(38, 22 + node.weight * 3));
      return `<g class="graph-node ${escapeAttr(node.type)}" transform="translate(${pos.x}, ${pos.y})">
        <circle r="${radius}"></circle>
        <text text-anchor="middle" dominant-baseline="middle">${escapeHtml(shortLabel(node.label, 9))}</text>
        <title>${escapeHtml(`${node.label} · ${node.weight}`)}</title>
      </g>`;
    })
    .join("");
  return `<svg class="topic-relation-graph console-graph" viewBox="0 0 760 420" role="img" aria-label="专题关系网">${lines}${circles}</svg>`;
}

function renderEvidence(articles) {
  const target = document.querySelector("[data-evidence-strip]");
  if (!articles.length) {
    target.innerHTML = `<div class="empty-state">暂无来源证据</div>`;
    return;
  }
  target.innerHTML = articles
    .slice(0, 8)
    .map(
      (item) => `<article>
        <strong>${escapeHtml(item.title)}</strong>
        <span>${escapeHtml(item.source_id)} · ${escapeHtml(articleDateLabel(item))}</span>
      </article>`
    )
    .join("");
}

function renderDueUrls(items) {
  const target = document.querySelector("[data-due-urls]");
  if (!items.length) {
    target.innerHTML = `<div class="empty-state">暂无待抓 URL</div>`;
    return;
  }
  target.innerHTML = items
    .slice(0, 8)
    .map(
      (item) => `<article>
        <strong>${escapeHtml(item.title || item.url)}</strong>
        <span>${escapeHtml(item.source_id)} · ${escapeHtml(item.status)} · ${escapeHtml(item.fetch_count ?? 0)} 次</span>
      </article>`
    )
    .join("");
}

function renderDeepDive(data) {
  const target = document.querySelector("[data-deep-dive-output]");
  const queries = (data.expanded_queries || data.queries || []).map((item) => (typeof item === "string" ? item : item.query || item.rationale || ""));
  const items = data.items || data.results || [];
  const rounds = data.rounds || [];
  target.innerHTML = `
    <div class="deep-section">
      <strong>扩展词</strong>
      <p>${escapeHtml(queries.slice(0, 8).join(" / ") || "暂无")}</p>
    </div>
    <div class="deep-section">
      <strong>结果</strong>
      <p>${escapeHtml(String(items.length || rounds.length || 0))} 条</p>
    </div>
  `;
}

function graphPositions(nodes, width, height) {
  const positions = {};
  const center = { x: width / 2, y: height / 2 };
  const outer = nodes.filter((node) => node.id !== "topic");
  positions.topic = center;
  outer.forEach((node, index) => {
    const angle = (Math.PI * 2 * index) / Math.max(1, outer.length) - Math.PI / 2;
    const rx = width * 0.36;
    const ry = height * 0.34;
    positions[node.id] = { x: center.x + Math.cos(angle) * rx, y: center.y + Math.sin(angle) * ry };
  });
  return positions;
}

function parseScope(value) {
  return String(value || "")
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

function syncTaskTopic() {
  const form = document.querySelector("#taskForm");
  if (!form) return;
  form.elements.topic.value = consoleState.topic;
  if (consoleState.categoryScope[0]) form.elements.category.value = consoleState.categoryScope[0];
}

function syncChatContext() {
  window.currentChatContext = {
    topic: consoleState.topic,
    category_scope: consoleState.categoryScope,
    use_llm: true,
  };
}

function syncContextDock() {
  const topic = document.querySelector("[data-current-topic-chip]");
  const category = document.querySelector("[data-current-category-chip]");
  const view = document.querySelector("[data-current-view-chip]");
  if (topic) topic.textContent = consoleState.topic;
  if (category) category.textContent = consoleState.categoryScope.join(" / ") || "all";
  if (view) view.textContent = consoleState.view === "relation-graph" ? "关系网" : "事件线";
}

function setStatus(message) {
  const target = document.querySelector("[data-command-status]");
  if (target) target.textContent = message;
}

function shortLabel(value, maxLength) {
  const text = String(value || "");
  return text.length > maxLength ? `${text.slice(0, maxLength - 1)}…` : text;
}

function articleDateLabel(item) {
  if (item.published_at) return item.published_at.slice(0, 10);
  if (item.fetched_at) return `抓取 ${item.fetched_at.slice(0, 10)}`;
  return "--";
}
