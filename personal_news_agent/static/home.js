(function () {
  const assetVersion = new URLSearchParams(window.location.search).get("v") || "20260812-chat-workspace-1";
  const mobileQuery = window.matchMedia("(max-width: 760px)");
  const mode = mobileQuery.matches ? "mobile" : "web";
  const template = document.querySelector(`#${mode}Template`);

  if (!window.React || !window.ReactDOM) {
    document.body.textContent = "React 运行时加载失败。";
    return;
  }

  if (!template) {
    document.body.textContent = "页面模板加载失败。";
    return;
  }

  document.body.className = template.dataset.bodyClass || "";
  document.documentElement.classList.toggle("mobile-root", mode === "mobile");

  const rootNode = document.createElement("div");
  rootNode.id = "react-root";
  document.body.appendChild(rootNode);

  function loadPageScripts() {
    const sharedScript = document.createElement("script");
    sharedScript.src = `static/shared.js?v=${encodeURIComponent(assetVersion)}`;
    sharedScript.onload = () => {
      const pageScript = document.createElement("script");
      pageScript.src = `static/${mode}.js?v=${encodeURIComponent(assetVersion)}`;
      document.body.appendChild(pageScript);
    };
    document.body.appendChild(sharedScript);
  }

  function HomeShell() {
    React.useEffect(() => {
      loadPageScripts();
    }, []);

    return React.createElement("div", {
      className: `react-home-shell react-home-shell-${mode}`,
      dangerouslySetInnerHTML: { __html: template.innerHTML },
    });
  }

  ReactDOM.createRoot(rootNode).render(React.createElement(HomeShell));

  mobileQuery.addEventListener("change", () => {
    window.location.reload();
  });
})();
