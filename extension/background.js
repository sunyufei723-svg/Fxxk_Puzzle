// 扩展职责：检测目标网址 → 通过协议钩子启动本地程序。

let handled = new Set();

function launchProgram() {
  // 通过协议钩子启动 main.exe --auto
  chrome.tabs.create({ url: "fxxk-puzzle://launch" });
}

// 自动检测：标签页 URL 匹配时启动程序
chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.status !== "loading" || !tab.url) return;
  if (handled.has(tabId)) handled.delete(tabId);
  chrome.storage.local.get(["targetUrl"], ({ targetUrl }) => {
    if (!targetUrl || !tab.url.includes(targetUrl)) return;
    if (handled.has(tabId)) return;
    handled.add(tabId);
    launchProgram();
  });
});

chrome.tabs.onRemoved.addListener((tabId) => handled.delete(tabId));
