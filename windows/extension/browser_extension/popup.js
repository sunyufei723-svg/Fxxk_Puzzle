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

  const silent = document.getElementById("silent");
  chrome.storage.local.get(["silentStart"], ({ silentStart }) => {
    silent.checked = Boolean(silentStart);
  });
  silent.addEventListener("change", () => {
    chrome.storage.local.set({ silentStart: silent.checked });
    // 扩展自己写不了文件，把改动推给程序去写 settings.json —— 和程序「设置」页
    // 里那个勾选框是同一个开关，谁后改谁生效。
    chrome.tabs.create({ url: `fxxk-puzzle://settings?show=${silent.checked ? 0 : 1}` });
    status.textContent = "已发送，浏览器弹确认框时请点「打开」";
    status.style.color = "#16a34a";
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
