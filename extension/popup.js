document.addEventListener("DOMContentLoaded", () => {
  const urlInput = document.getElementById("url");
  const status = document.getElementById("status");

  chrome.storage.local.get(["targetUrl"], ({ targetUrl }) => {
    if (targetUrl) urlInput.value = targetUrl;
    if (targetUrl) {
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
