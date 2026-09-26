// 扩展职责：检测目标网址 → 通过协议钩子启动本地程序。

const handled = new Set();
let lastLaunchAt = 0;
const LAUNCH_COOLDOWN_MS = 5000;

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

function launchProgram(targetUrl) {
  const target = encodeURIComponent(targetUrl);
  chrome.tabs.create({ url: `fxxk-puzzle://launch?target=${target}` });
}

// 自动检测：标签页 URL 匹配时启动程序
chrome.tabs.onUpdated.addListener(async (tabId, changeInfo, tab) => {
  if (!tab.url) return;
  const targetUrl = await configuredTargetUrl();
  if (!targetUrl || !tab.url.includes(targetUrl)) {
    handled.delete(tabId);
    return;
  }
  if (changeInfo.status !== "loading" || handled.has(tabId)) return;
  const now = Date.now();
  if (now - lastLaunchAt < LAUNCH_COOLDOWN_MS) return;
  // 只有真的启动了才标记已处理：在冷却判断之前 add 会让被冷却吞掉的那个标签页
  // 永久失去资格（同一次加载不会再回到 loading，重新加载页面也被 handled 挡住）。
  handled.add(tabId);
  lastLaunchAt = now;
  launchProgram(targetUrl);
});

chrome.tabs.onRemoved.addListener((tabId) => handled.delete(tabId));
