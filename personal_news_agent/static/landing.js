(() => {
  const trackingViz = `
    <div class="tracking-viz">
      <div class="viz-kpis">
        <span><strong>6</strong><small>交叉来源</small></span>
        <span><strong>23</strong><small>近 6 小时更新</small></span>
        <span><strong>+68%</strong><small>事件热度</small></span>
      </div>
      <svg viewBox="0 0 560 112" role="img" aria-label="事件热度连续上升曲线">
        <defs><linearGradient id="tracking-area" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#22d3ee" stop-opacity=".42"/><stop offset="1" stop-color="#2563eb" stop-opacity="0"/></linearGradient></defs>
        <path class="viz-grid" d="M10 26H550M10 58H550M10 90H550"/>
        <path class="trend-area" d="M12 86 C80 80 96 72 150 74 S234 55 288 62 S368 42 414 45 S492 20 548 24 L548 104 L12 104Z"/>
        <path class="trend-line" d="M12 86 C80 80 96 72 150 74 S234 55 288 62 S368 42 414 45 S492 20 548 24"/>
        <circle cx="150" cy="74" r="4"/><circle cx="288" cy="62" r="4"/><circle cx="414" cy="45" r="4"/><circle cx="548" cy="24" r="5"/>
      </svg>
      <div class="source-stream"><span>气象预警</span><span>铁路调整</span><span>航班变化</span><span>港口安排</span></div>
    </div>`;

  const analysisViz = `
    <div class="timeline-viz">
      <article><time>07:30</time><div><strong>预警等级调整</strong><small>权威气象信息首先出现变化</small></div><em>起点</em></article>
      <article><time>09:10</time><div><strong>城市响应升级</strong><small>应急部门公布重点防范区域</small></div><em>响应</em></article>
      <article><time>11:40</time><div><strong>交通运行受影响</strong><small>铁路、航空和港口陆续更新安排</small></div><em>影响</em></article>
      <article><time>待观察</time><div><strong>路径与恢复节奏</strong><small>后续强度和交通恢复时间仍不确定</small></div><em>变量</em></article>
    </div>`;

  const factcheckViz = `
    <div class="factcheck-viz">
      <div class="verdict-gauge"><span>综合判断</span><strong>部分属实</strong><small>范围被明显夸大</small></div>
      <div class="evidence-stack">
        <article class="verified"><span>已证实</span><strong>部分线路临时调整</strong><small>运营公告与两家媒体报道一致</small></article>
        <article class="conflict"><span>未支持</span><strong>“全市公共交通停运”</strong><small>暂无权威信息支持全域停运说法</small></article>
        <article class="pending"><span>待更新</span><strong>晚高峰恢复时间</strong><small>以运营方后续公告为准</small></article>
      </div>
    </div>`;

  const mapViz = `
    <div class="graph-viz">
      <div class="graph-stage">
        <svg viewBox="0 0 560 220" aria-hidden="true"><path d="M280 109L98 52M280 109L98 170M280 109L462 48M280 109L468 172M98 52L462 48M98 170L468 172"/></svg>
        <button type="button" class="graph-node core" style="--x:50%;--y:50%" data-graph-node="event">台风影响</button>
        <button type="button" class="graph-node authority" style="--x:17%;--y:23%" data-graph-node="weather">气象部门</button>
        <button type="button" class="graph-node response" style="--x:17%;--y:77%" data-graph-node="response">应急响应</button>
        <button type="button" class="graph-node transport" style="--x:83%;--y:22%" data-graph-node="rail">铁路 / 航空</button>
        <button type="button" class="graph-node place" style="--x:84%;--y:78%" data-graph-node="city">沿海城市</button>
      </div>
      <p class="graph-detail" data-graph-detail><strong>点击任一节点</strong><span>继续搜索关联信息，或直接发起事实核查。</span></p>
    </div>`;

  const scenarios = {
    tracking: {
      status: "多源信号持续更新",
      prompt: "帮我持续追踪台风对华南沿海交通的影响，有重要变化就提醒我。",
      agent: "正在合并门户索引页、近期主题和相同事件的更新频率。",
      steps: [
        ["发现异常信号", "多个新闻源在短时间内集中更新", "完成"],
        ["合并同一事件", "正文、URL 与事件语义多重去重", "完成"],
        ["计算变化热度", "综合来源数、更新频率与用户兴趣", "追踪中"],
      ],
      label: "追踪信号",
      meta: "演示数据 · 6 个来源",
      title: "沿海交通影响热度连续三个时段上升",
      copy: "相关报道从气象预警扩展到铁路、航班和港口安排，已达到主动提醒阈值。",
      visualLabel: "自动追踪演示图",
      visual: trackingViz,
    },
    analysis: {
      status: "事件脉络已整理",
      prompt: "这次交通调整是怎么一步步发生的？哪些影响已经明确？",
      agent: "正在阅读全文并补齐人物、机构、地点、时间与因果关系。",
      steps: [
        ["识别关键实体", "定位气象、应急、交通和受影响区域", "完成"],
        ["还原事件时间线", "按消息发布时间校正先后顺序", "完成"],
        ["区分事实与研判", "标注已发生影响和仍待观察变量", "完成"],
      ],
      label: "深度分析",
      meta: "4 个阶段 · 7 个关键实体",
      title: "从预警升级到交通调整，影响链路已经形成",
      copy: "Agent 将多篇报道重组为事件时间线，明确已发生变化、关键转折和下一步观察点。",
      visualLabel: "事件时间线演示图",
      visual: analysisViz,
    },
    factcheck: {
      status: "关键主张已核查",
      prompt: "网传‘全市公共交通全部停运’，这个消息是真的吗？",
      agent: "正在拆分主张，并对照运营公告、权威发布和独立媒体报道。",
      steps: [
        ["拆分可核查主张", "范围、时间和交通类型分别验证", "完成"],
        ["查找一手证据", "优先读取运营方和主管部门公告", "完成"],
        ["交叉比对结论", "识别共同事实、冲突和证据缺口", "完成"],
      ],
      label: "事实核查",
      meta: "3 类证据 · 1 项待更新",
      title: "部分线路调整属实，但‘全部停运’表述被夸大",
      copy: "现有证据支持局部临时调整，不支持全市、全类型公共交通统一停运的说法。",
      visualLabel: "事实核查证据面板",
      visual: factcheckViz,
    },
    map: {
      status: "关联关系可继续探索",
      prompt: "把这次事件涉及的机构、交通系统和重点城市画成关系图。",
      agent: "正在从证据中抽取人物、机构、地点、事件和时间关系。",
      steps: [
        ["抽取图谱节点", "识别事件、机构、地点和影响对象", "完成"],
        ["绑定证据关系", "每条关系回指新闻原文或权威来源", "完成"],
        ["生成可交互图谱", "节点支持继续搜索、分析与核查", "可探索"],
      ],
      label: "关联事件图谱",
      meta: "5 个节点 · 6 条关系",
      title: "从事件中心展开机构、地点与交通影响关系",
      copy: "图谱不是静态插图：点击节点即可查看关系解释，并作为下一轮对话和搜索的起点。",
      visualLabel: "可交互事件关系图",
      visual: mapViz,
    },
  };

  const tabs = [...document.querySelectorAll("[data-preview-key]")];
  const panel = document.querySelector("#preview-demo-panel");
  const fields = {
    status: document.querySelector("[data-preview-demo-status]"),
    prompt: document.querySelector("[data-preview-prompt]"),
    agent: document.querySelector("[data-preview-agent-copy]"),
    label: document.querySelector("[data-preview-label]"),
    meta: document.querySelector("[data-preview-result-meta]"),
    title: document.querySelector("[data-preview-title]"),
    copy: document.querySelector("[data-preview-copy]"),
    visual: document.querySelector("[data-preview-viz]"),
  };
  const stepTitles = [...document.querySelectorAll("[data-preview-step-title]")];
  const stepCopies = [...document.querySelectorAll("[data-preview-step-copy]")];
  const stepStates = [...document.querySelectorAll(".preview-steps li > em")];

  if (!tabs.length || !panel || Object.values(fields).some((field) => !field)) return;

  const graphDetails = {
    event: ["台风影响", "事件中心：串联预警、响应、交通变化与重点区域。"],
    weather: ["气象部门", "提供路径、强度和预警等级，是判断风险变化的核心来源。"],
    response: ["应急响应", "关联防汛等级、重点区域措施与公众行动建议。"],
    rail: ["铁路 / 航空", "承接事件影响，持续发布线路、航班和恢复安排。"],
    city: ["沿海城市", "对应具体影响地点，可继续追踪当地权威发布和实时变化。"],
  };

  const bindGraphNodes = () => {
    const detail = fields.visual.querySelector("[data-graph-detail]");
    fields.visual.querySelectorAll("[data-graph-node]").forEach((node) => {
      node.addEventListener("click", () => {
        fields.visual.querySelectorAll("[data-graph-node]").forEach((item) => item.classList.remove("active"));
        node.classList.add("active");
        const [title, copy] = graphDetails[node.dataset.graphNode] || graphDetails.event;
        detail.innerHTML = `<strong>${title}</strong><span>${copy}</span>`;
      });
    });
  };

  const activate = (key, { focus = false } = {}) => {
    const scenario = scenarios[key];
    if (!scenario) return;

    panel.classList.add("is-switching");
    tabs.forEach((tab) => {
      const active = tab.dataset.previewKey === key;
      tab.classList.toggle("active", active);
      tab.setAttribute("aria-selected", String(active));
      tab.tabIndex = active ? 0 : -1;
      if (active && focus) tab.focus();
    });

    window.setTimeout(() => {
      fields.status.textContent = scenario.status;
      fields.prompt.textContent = scenario.prompt;
      fields.agent.textContent = scenario.agent;
      fields.label.textContent = scenario.label;
      fields.meta.textContent = scenario.meta;
      fields.title.textContent = scenario.title;
      fields.copy.textContent = scenario.copy;
      fields.visual.setAttribute("aria-label", scenario.visualLabel);
      fields.visual.innerHTML = scenario.visual;
      scenario.steps.forEach(([title, copy, state], index) => {
        stepTitles[index].textContent = title;
        stepCopies[index].textContent = copy;
        stepStates[index].textContent = state;
      });
      if (key === "map") bindGraphNodes();
      panel.classList.remove("is-switching");
    }, 120);
  };

  tabs.forEach((tab, index) => {
    tab.addEventListener("click", () => activate(tab.dataset.previewKey));
    tab.addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
      event.preventDefault();
      const nextIndex = event.key === "ArrowRight" ? (index + 1) % tabs.length : (index - 1 + tabs.length) % tabs.length;
      activate(tabs[nextIndex].dataset.previewKey, { focus: true });
    });
  });

  activate("tracking");
})();
