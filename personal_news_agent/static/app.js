let conversationId = null;

async function request(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) throw new Error(await response.text());
  return response.json();
}

function itemHtml(item) {
  return `<article class="item">
    <div class="title">${escapeHtml(item.title)}</div>
    <div class="meta">${escapeHtml(item.source || item.source_id)} · ${escapeHtml(item.category)} · ${escapeHtml(item.recommend_reason || "")}</div>
    <div class="summary">${escapeHtml(item.summary || "")}</div>
  </article>`;
}

function eventHtml(item) {
  return `<article class="item">
    <div class="title">${escapeHtml(item.title)}</div>
    <div class="meta">${escapeHtml(item.category)} · ${item.article_count}篇 · 热度${item.hot_score}</div>
    <div class="summary">${escapeHtml((item.keywords || []).join(" / "))}</div>
  </article>`;
}

async function loadFeed() {
  const category = document.querySelector("#category").value;
  const data = await request(`/api/feed?limit=10${category ? `&category=${category}` : ""}`);
  document.querySelector("#feed").innerHTML = data.items.map(itemHtml).join("");
}

async function loadEvents() {
  const data = await request("/api/events?limit=8");
  document.querySelector("#events").innerHTML = data.items.map(eventHtml).join("");
}

document.querySelector("#refresh").addEventListener("click", () => {
  loadFeed();
  loadEvents();
});

document.querySelector("#category").addEventListener("change", loadFeed);

document.querySelector("#chatForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = document.querySelector("#message");
  const message = input.value.trim();
  if (!message) return;
  const data = await request("/api/chat", {
    method: "POST",
    body: JSON.stringify({ conversation_id: conversationId, message }),
  });
  conversationId = data.conversation_id;
  document.querySelector("#messages").textContent += `你：${message}\n助手：${data.answer}\n\n`;
  input.value = "";
});

document.querySelector("#reportForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const topic = document.querySelector("#topic").value.trim();
  const data = await request("/api/reports", {
    method: "POST",
    body: JSON.stringify({ topic, category_scope: ["auto", "economy"], time_range: "30d" }),
  });
  document.querySelector("#report").textContent = JSON.stringify(data, null, 2);
});

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

loadFeed();
loadEvents();
