// 扩展职责：检测目标网址 → 通过协议钩子启动本地程序。

let handled = new Set();

async function configuredTargetUrl() {
  const { targetUrl } = await chrome.storage.local.get(["targetUrl"]);
  if (targetUrl) return targetUrl;
  try {
    const response = await fetch(chrome.runtime.getURL("config.json"));
    const config = await response.json();
    if (config.targetUrl) {
      await chrome.storage.local.set({ targetUrl: config.targetUrl });
      return config.targetUrl;
    }
  } catch (error) {
    console.warn("读取安装配置失败", error);
  }
  return "";
}

function launchProgram() {
  chrome.tabs.create({ url: "fxxk-puzzle://launch" });
}

// 自动检测：标签页 URL 匹配时启动程序
chrome.tabs.onUpdated.addListener(async (tabId, changeInfo, tab) => {
  if (changeInfo.status !== "loading" || !tab.url) return;
  if (handled.has(tabId)) handled.delete(tabId);
  const targetUrl = await configuredTargetUrl();
  if (!targetUrl || !tab.url.includes(targetUrl)) return;
  if (handled.has(tabId)) return;
  handled.add(tabId);
  launchProgram();
});

chrome.tabs.onRemoved.addListener((tabId) => handled.delete(tabId));
