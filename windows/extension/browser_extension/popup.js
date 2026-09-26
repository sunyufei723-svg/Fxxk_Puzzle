document.addEventListener("DOMContentLoaded", async () => {
  const urlInput = document.getElementById("url");
  const status = document.getElementById("status");

  chrome.storage.local.get(["targetUrl"], async ({ targetUrl }) => {
    if (!targetUrl) {
      try {
        const response = await fetch(chrome.runtime.getURL("config.json"));
        const config = await response.json();
        targetUrl = config.targetUrl || "";
        if (targetUrl) await chrome.storage.local.set({ targetUrl });
      } catch {
        status.textContent = "未读取到安装配置";
      }
    }
    if (targetUrl) {
      urlInput.value = targetUrl;
      status.textContent = "已配置";
      status.style.color = "#16a34a";
    }
  });

  document.getElementById("save").addEventListener("click", () => {
    const targetUrl = urlInput.value.trim();
    if (!targetUrl) { status.textContent = "请输入网址关键词"; status.style.color = "#dc2626"; return; }
    chrome.storage.local.set({ targetUrl }, () => {
      status.textContent = "已保存";
      status.style.color = "#16a34a";
    });
  });
});
