document.querySelectorAll("[data-auth-mode-target]").forEach((button) => {
  button.addEventListener("click", () => {
    const target = button.dataset.authModeTarget;
    document.querySelectorAll("[data-auth-panel]").forEach((panel) => {
      panel.hidden = panel.dataset.authPanel !== target;
    });
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
    document.querySelector("#registerStatus").textContent = `已创建：${result.user.display_name}，进入主界面后请完善个人配置。`;
    window.location.href = "/web";
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
  const status = document.querySelector("#loginStatus");
  if (button) button.disabled = true;
  if (status) status.textContent = "正在登录。";
  try {
    const result = await loginFromForm(form);
    document.querySelector("#loginStatus").textContent = `已登录：${result.user.display_name}`;
    window.location.href = "/web";
  } catch (error) {
    document.querySelector("#loginStatus").textContent = error.message;
  } finally {
    if (button) button.disabled = false;
  }
});
