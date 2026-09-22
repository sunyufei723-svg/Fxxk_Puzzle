#pragma once

#ifndef UNICODE
#define UNICODE
#endif
#ifndef _UNICODE
#define _UNICODE
#endif

#include <windows.h>
#include <shlobj.h>
#include <tlhelp32.h>
#include <uiautomation.h>
#include <wrl/client.h>

#include <algorithm>
#include <chrono>
#include <filesystem>
#include <fstream>
#include <functional>
#include <set>
#include <stdexcept>
#include <string>
#include <string_view>
#include <thread>
#include <vector>

#include "generated_positions.hpp"

#pragma comment(lib, "user32.lib")
#pragma comment(lib, "advapi32.lib")

namespace native {

namespace fs = std::filesystem;
using Microsoft::WRL::ComPtr;

inline std::runtime_error Error(const std::wstring& message) {
    const int size = WideCharToMultiByte(CP_UTF8, 0, message.c_str(), -1, nullptr, 0, nullptr, nullptr);
    std::string utf8(static_cast<size_t>(size > 0 ? size : 0), '\0');
    if (size > 0) WideCharToMultiByte(CP_UTF8, 0, message.c_str(), -1, utf8.data(), size,
                                      nullptr, nullptr);
    if (!utf8.empty() && utf8.back() == '\0') utf8.pop_back();
    return std::runtime_error(utf8);
}

inline void Check(bool condition, const std::wstring& message) {
    if (!condition) throw Error(message);
}

inline std::wstring ModuleDirectory() {
    std::wstring buffer(32768, L'\0');
    const DWORD length = GetModuleFileNameW(nullptr, buffer.data(), static_cast<DWORD>(buffer.size()));
    Check(length > 0 && length < buffer.size(), L"无法定位安装程序目录");
    buffer.resize(length);
    return fs::path(buffer).parent_path().wstring();
}

inline fs::path LocalAppData() {
    PWSTR value = nullptr;
    const HRESULT result = SHGetKnownFolderPath(FOLDERID_LocalAppData, 0, nullptr, &value);
    Check(SUCCEEDED(result) && value, L"无法定位当前用户的 LocalAppData");
    fs::path path(value);
    CoTaskMemFree(value);
    return path;
}

inline std::string Utf8(const std::wstring& value) {
    if (value.empty()) return {};
    const int size = WideCharToMultiByte(CP_UTF8, 0, value.data(), static_cast<int>(value.size()),
                                         nullptr, 0, nullptr, nullptr);
    std::string result(static_cast<size_t>(size), '\0');
    WideCharToMultiByte(CP_UTF8, 0, value.data(), static_cast<int>(value.size()),
                        result.data(), size, nullptr, nullptr);
    return result;
}

inline std::wstring WideFromUtf8(const char* value) {
    if (!value || !*value) return {};
    const int size = MultiByteToWideChar(CP_UTF8, 0, value, -1, nullptr, 0);
    if (size <= 0) return {};
    std::wstring result(static_cast<size_t>(size), L'\0');
    MultiByteToWideChar(CP_UTF8, 0, value, -1, result.data(), size);
    if (!result.empty() && result.back() == L'\0') result.pop_back();
    return result;
}

inline std::string JsonEscape(const std::wstring& value) {
    std::string result;
    for (const unsigned char ch : Utf8(value)) {
        switch (ch) {
            case '\\': result += "\\\\"; break;
            case '"': result += "\\\""; break;
            case '\n': result += "\\n"; break;
            case '\r': result += "\\r"; break;
            case '\t': result += "\\t"; break;
            default: result.push_back(static_cast<char>(ch)); break;
        }
    }
    return result;
}

inline void WriteUtf8(const fs::path& path, const std::string& content) {
    std::ofstream stream(path, std::ios::binary | std::ios::trunc);
    Check(stream.good(), L"无法写入配置文件：" + path.wstring());
    stream.write(content.data(), static_cast<std::streamsize>(content.size()));
    Check(stream.good(), L"写入配置文件失败：" + path.wstring());
}

inline std::wstring ReadRegistryString(HKEY root, const std::wstring& subkey) {
    HKEY key = nullptr;
    if (RegOpenKeyExW(root, subkey.c_str(), 0, KEY_READ, &key) != ERROR_SUCCESS) return {};
    DWORD type = 0;
    DWORD bytes = 0;
    if (RegQueryValueExW(key, nullptr, nullptr, &type, nullptr, &bytes) != ERROR_SUCCESS ||
        (type != REG_SZ && type != REG_EXPAND_SZ)) {
        RegCloseKey(key);
        return {};
    }
    std::wstring value(bytes / sizeof(wchar_t), L'\0');
    const LONG result = RegQueryValueExW(key, nullptr, nullptr, &type,
                                         reinterpret_cast<BYTE*>(value.data()), &bytes);
    RegCloseKey(key);
    if (result != ERROR_SUCCESS) return {};
    while (!value.empty() && value.back() == L'\0') value.pop_back();
    if (type == REG_EXPAND_SZ) {
        std::wstring expanded(32768, L'\0');
        const DWORD length = ExpandEnvironmentStringsW(value.c_str(), expanded.data(),
                                                        static_cast<DWORD>(expanded.size()));
        if (length > 0 && length <= expanded.size()) {
            expanded.resize(length - 1);
            value = expanded;
        }
    }
    return value;
}

inline fs::path FindBrowser(const std::wstring& browser) {
    const std::wstring executable = browser == L"chrome" ? L"chrome.exe" : L"msedge.exe";
    const std::wstring appPath = L"Software\\Microsoft\\Windows\\CurrentVersion\\App Paths\\" + executable;
    for (HKEY root : {HKEY_CURRENT_USER, HKEY_LOCAL_MACHINE}) {
        const auto registered = ReadRegistryString(root, appPath);
        if (!registered.empty() && fs::is_regular_file(registered)) return registered;
    }
    const auto local = LocalAppData();
    const auto environmentPath = [](const wchar_t* name) {
        const DWORD size = GetEnvironmentVariableW(name, nullptr, 0);
        if (!size) return fs::path{};
        std::wstring value(size, L'\0');
        const DWORD length = GetEnvironmentVariableW(name, value.data(), size);
        if (!length || length >= size) return fs::path{};
        value.resize(length);
        return fs::path(value);
    };
    const fs::path programFiles = environmentPath(L"ProgramFiles");
    const fs::path programFilesX86 = environmentPath(L"ProgramFiles(x86)");
    const std::vector<fs::path> candidates = browser == L"chrome"
        ? std::vector<fs::path>{
            local / L"Google/Chrome/Application/chrome.exe",
            programFiles / L"Google/Chrome/Application/chrome.exe",
            programFilesX86 / L"Google/Chrome/Application/chrome.exe"}
        : std::vector<fs::path>{
            programFilesX86 / L"Microsoft/Edge/Application/msedge.exe",
            programFiles / L"Microsoft/Edge/Application/msedge.exe",
            local / L"Microsoft/Edge/Application/msedge.exe"};
    for (const auto& path : candidates) if (fs::is_regular_file(path)) return path;
    return {};
}

inline void SetRegistryString(HKEY key, const wchar_t* name, const std::wstring& value) {
    const DWORD bytes = static_cast<DWORD>((value.size() + 1) * sizeof(wchar_t));
    Check(RegSetValueExW(key, name, 0, REG_SZ, reinterpret_cast<const BYTE*>(value.c_str()), bytes)
              == ERROR_SUCCESS,
          L"写入注册表失败");
}

inline void RegisterProtocol(const fs::path& executable) {
    HKEY key = nullptr;
    Check(RegCreateKeyExW(HKEY_CURRENT_USER, L"Software\\Classes\\fxxk-puzzle", 0, nullptr, 0,
                          KEY_WRITE, nullptr, &key, nullptr) == ERROR_SUCCESS,
          L"无法注册 fxxk-puzzle 协议");
    SetRegistryString(key, nullptr, L"URL:fxxk-puzzle");
    SetRegistryString(key, L"URL Protocol", L"");
    RegCloseKey(key);
    Check(RegCreateKeyExW(HKEY_CURRENT_USER,
                          L"Software\\Classes\\fxxk-puzzle\\shell\\open\\command", 0,
                          nullptr, 0, KEY_WRITE, nullptr, &key, nullptr) == ERROR_SUCCESS,
          L"无法注册 fxxk-puzzle 启动命令");
    SetRegistryString(key, nullptr, L"\"" + executable.wstring() + L"\" \"%1\"");
    RegCloseKey(key);
}

inline uint64_t DirectorySize(const fs::path& root) {
    uint64_t total = 0;
    std::error_code error;
    for (fs::recursive_directory_iterator it(root, error), end; !error && it != end; it.increment(error)) {
        if (it->is_regular_file(error)) total += it->file_size(error);
    }
    return total;
}

inline void RegisterUninstall(const fs::path& installDir, const fs::path& executable) {
    constexpr auto path = L"Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\FxxkPuzzleExtension";
    HKEY key = nullptr;
    Check(RegCreateKeyExW(HKEY_CURRENT_USER, path, 0, nullptr, 0, KEY_WRITE, nullptr,
                          &key, nullptr) == ERROR_SUCCESS,
          L"无法登记 Windows 卸载信息");
    const auto uninstaller = installDir / L"Uninstall.exe";
    SetRegistryString(key, L"DisplayName", L"拼图助手（Extension 版）");
    SetRegistryString(key, L"DisplayVersion", L"1.0.0");
    SetRegistryString(key, L"Publisher", L"Fxxk Puzzle");
    SetRegistryString(key, L"InstallLocation", installDir.wstring());
    SetRegistryString(key, L"DisplayIcon", executable.wstring());
    SetRegistryString(key, L"UninstallString", L"\"" + uninstaller.wstring() + L"\"");
    SetRegistryString(key, L"QuietUninstallString",
                      L"\"" + uninstaller.wstring() + L"\" --quiet");
    SYSTEMTIME now{};
    GetLocalTime(&now);
    wchar_t date[16]{};
    swprintf_s(date, L"%04u%02u%02u", now.wYear, now.wMonth, now.wDay);
    SetRegistryString(key, L"InstallDate", date);
    const DWORD estimated = static_cast<DWORD>(DirectorySize(installDir) / 1024);
    const DWORD one = 1;
    RegSetValueExW(key, L"EstimatedSize", 0, REG_DWORD,
                   reinterpret_cast<const BYTE*>(&estimated), sizeof(estimated));
    RegSetValueExW(key, L"NoModify", 0, REG_DWORD,
                   reinterpret_cast<const BYTE*>(&one), sizeof(one));
    RegSetValueExW(key, L"NoRepair", 0, REG_DWORD,
                   reinterpret_cast<const BYTE*>(&one), sizeof(one));
    RegCloseKey(key);
}

inline void CopyPayload(const fs::path& source, const fs::path& destination,
                        const std::wstring& url, const std::wstring& selectKey,
                        const std::wstring& executeKey, const std::wstring& cancelKey) {
    Check(fs::is_regular_file(source / L"app/Fxxk_Puzzle.exe"),
          L"安装包缺少 app\\Fxxk_Puzzle.exe");
    Check(fs::is_regular_file(source / L"extensions/manifest.json"),
          L"安装包缺少 extensions\\manifest.json");
    Check(fs::is_regular_file(source / L"Uninstall.exe"), L"安装包缺少 Uninstall.exe");
    fs::create_directories(destination);
    fs::copy(source / L"app", destination / L"app",
             fs::copy_options::recursive | fs::copy_options::overwrite_existing);
    fs::copy(source / L"extensions", destination / L"extensions",
             fs::copy_options::recursive | fs::copy_options::overwrite_existing);
    fs::copy_file(source / L"Uninstall.exe", destination / L"Uninstall.exe",
                  fs::copy_options::overwrite_existing);
    WriteUtf8(destination / L"extensions/config.json",
              "{\n  \"targetUrl\": \"" + JsonEscape(url) + "\"\n}\n");
    WriteUtf8(destination / L"app/settings.json",
              "{\n  \"drag_speed\": 600,\n  \"hotkeys\": {\n"
              "    \"select\": \"" + JsonEscape(selectKey) + "\",\n"
              "    \"execute\": \"" + JsonEscape(executeKey) + "\",\n"
              "    \"cancel\": \"" + JsonEscape(cancelKey) + "\"\n  }\n}\n");
}

inline std::wstring ProcessNameForWindow(HWND window) {
    DWORD pid = 0;
    GetWindowThreadProcessId(window, &pid);
    if (!pid) return {};
    HANDLE process = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, pid);
    if (!process) return {};
    std::wstring path(32768, L'\0');
    DWORD length = static_cast<DWORD>(path.size());
    const BOOL ok = QueryFullProcessImageNameW(process, 0, path.data(), &length);
    CloseHandle(process);
    if (!ok) return {};
    path.resize(length);
    std::wstring name = fs::path(path).filename().wstring();
    std::transform(name.begin(), name.end(), name.begin(), towlower);
    return name;
}

inline std::vector<HWND> BrowserWindows(const std::wstring& executableName) {
    struct Context { const std::wstring* name; std::vector<HWND>* windows; } context{
        &executableName, new std::vector<HWND>()};
    EnumWindows([](HWND window, LPARAM raw) -> BOOL {
        auto* context = reinterpret_cast<Context*>(raw);
        wchar_t className[128]{};
        GetClassNameW(window, className, 128);
        if (wcscmp(className, L"Chrome_WidgetWin_1") == 0 && IsWindowVisible(window) &&
            ProcessNameForWindow(window) == *context->name) {
            context->windows->push_back(window);
        }
        return TRUE;
    }, reinterpret_cast<LPARAM>(&context));
    auto result = std::move(*context.windows);
    delete context.windows;
    return result;
}

inline void SendVirtualKey(WORD key, bool down) {
    INPUT input{};
    input.type = INPUT_KEYBOARD;
    input.ki.wVk = key;
    input.ki.dwFlags = down ? 0 : KEYEVENTF_KEYUP;
    SendInput(1, &input, sizeof(input));
}

inline void SendChord(WORD modifier, WORD key) {
    SendVirtualKey(modifier, true);
    SendVirtualKey(key, true);
    SendVirtualKey(key, false);
    SendVirtualKey(modifier, false);
}

inline bool ForceForegroundWindow(HWND window) {
    if (!window) return false;
    ShowWindow(window, SW_MAXIMIZE);
    const DWORD currentThread = GetCurrentThreadId();
    DWORD targetProcess = 0;
    const DWORD targetThread = GetWindowThreadProcessId(window, &targetProcess);
    const HWND oldForeground = GetForegroundWindow();
    DWORD oldProcess = 0;
    const DWORD oldThread = oldForeground
        ? GetWindowThreadProcessId(oldForeground, &oldProcess) : 0;
    if (targetThread && targetThread != currentThread) AttachThreadInput(currentThread, targetThread, TRUE);
    if (oldThread && oldThread != currentThread && oldThread != targetThread) {
        AttachThreadInput(currentThread, oldThread, TRUE);
    }
    BringWindowToTop(window);
    SetForegroundWindow(window);
    SetFocus(window);
    if (oldThread && oldThread != currentThread && oldThread != targetThread) {
        AttachThreadInput(currentThread, oldThread, FALSE);
    }
    if (targetThread && targetThread != currentThread) AttachThreadInput(currentThread, targetThread, FALSE);
    std::this_thread::sleep_for(std::chrono::milliseconds(150));
    return GetForegroundWindow() == window;
}

class ClipboardGuard {
public:
    explicit ClipboardGuard(const std::wstring& value) {
        if (!OpenClipboard(nullptr)) throw Error(L"无法打开剪贴板");
        HANDLE old = GetClipboardData(CF_UNICODETEXT);
        if (old) {
            if (const auto* text = static_cast<const wchar_t*>(GlobalLock(old))) {
                previous_ = text;
                GlobalUnlock(old);
                hasPrevious_ = true;
            }
        }
        Set(value);
        CloseClipboard();
    }
    ~ClipboardGuard() {
        if (!hasPrevious_ || !OpenClipboard(nullptr)) return;
        Set(previous_);
        CloseClipboard();
    }
private:
    static void Set(const std::wstring& value) {
        EmptyClipboard();
        const size_t bytes = (value.size() + 1) * sizeof(wchar_t);
        HGLOBAL memory = GlobalAlloc(GMEM_MOVEABLE, bytes);
        if (!memory) return;
        void* target = GlobalLock(memory);
        memcpy(target, value.c_str(), bytes);
        GlobalUnlock(memory);
        if (!SetClipboardData(CF_UNICODETEXT, memory)) GlobalFree(memory);
    }
    bool hasPrevious_ = false;
    std::wstring previous_;
};

inline HWND OpenExtensionPage(const std::wstring& browser, const fs::path& executable) {
    const std::wstring executableName = browser == L"chrome" ? L"chrome.exe" : L"msedge.exe";
    std::set<HWND> previous;
    for (HWND window : BrowserWindows(executableName)) previous.insert(window);
    std::wstring command = L"\"" + executable.wstring() +
        L"\" --new-window about:blank --start-maximized --force-renderer-accessibility";
    STARTUPINFOW startup{sizeof(startup)};
    PROCESS_INFORMATION process{};
    Check(CreateProcessW(nullptr, command.data(), nullptr, nullptr, FALSE, 0, nullptr, nullptr,
                         &startup, &process), L"无法启动浏览器");
    CloseHandle(process.hThread);
    CloseHandle(process.hProcess);
    HWND selected = nullptr;
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(20);
    while (std::chrono::steady_clock::now() < deadline && !selected) {
        const auto windows = BrowserWindows(executableName);
        for (HWND window : windows) if (!previous.contains(window)) { selected = window; break; }
        if (!selected) {
            const HWND foreground = GetForegroundWindow();
            if (std::find(windows.begin(), windows.end(), foreground) != windows.end()) selected = foreground;
        }
        if (!selected) std::this_thread::sleep_for(std::chrono::milliseconds(250));
    }
    Check(selected != nullptr, L"浏览器窗口没有在规定时间内出现");
    Check(ForceForegroundWindow(selected), L"无法激活浏览器窗口");
    std::this_thread::sleep_for(std::chrono::milliseconds(800));
    SendChord(VK_CONTROL, 'L');
    std::this_thread::sleep_for(std::chrono::milliseconds(150));
    {
        ClipboardGuard clipboard(browser == L"chrome" ? L"chrome://extensions" : L"edge://extensions");
        SendChord(VK_CONTROL, 'V');
        std::this_thread::sleep_for(std::chrono::milliseconds(250));
        SendVirtualKey(VK_RETURN, true);
        SendVirtualKey(VK_RETURN, false);
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
    const auto navigationDeadline = std::chrono::steady_clock::now() + std::chrono::seconds(8);
    while (std::chrono::steady_clock::now() < navigationDeadline) {
        wchar_t title[1024]{};
        GetWindowTextW(selected, title, 1024);
        if (wcsstr(title, L"about:blank") == nullptr) break;
        std::this_thread::sleep_for(std::chrono::milliseconds(200));
    }
    wchar_t finalTitle[1024]{};
    GetWindowTextW(selected, finalTitle, 1024);
    Check(wcsstr(finalTitle, L"about:blank") == nullptr,
          L"浏览器仍停留在 about:blank，未进入扩展管理页面");
    std::this_thread::sleep_for(std::chrono::milliseconds(800));
    return selected;
}

inline ComPtr<IUIAutomationElement> FindNamedElement(
    IUIAutomation* automation, IUIAutomationElement* root,
    const std::vector<std::wstring>& names, DWORD timeoutMs) {
    const auto deadline = GetTickCount64() + timeoutMs;
    do {
        for (const auto& name : names) {
            VARIANT value{};
            value.vt = VT_BSTR;
            value.bstrVal = SysAllocString(name.c_str());
            ComPtr<IUIAutomationCondition> condition;
            const HRESULT conditionResult = automation->CreatePropertyCondition(
                UIA_NamePropertyId, value, &condition);
            VariantClear(&value);
            if (FAILED(conditionResult)) continue;
            ComPtr<IUIAutomationElement> found;
            if (SUCCEEDED(root->FindFirst(TreeScope_Subtree, condition.Get(), &found)) && found) {
                return found;
            }
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(200));
    } while (GetTickCount64() < deadline);
    return nullptr;
}

inline bool ActivateUiaElement(IUIAutomationElement* element, bool preferToggle) {
    if (!element) return false;
    if (preferToggle) {
        ComPtr<IUIAutomationTogglePattern> toggle;
        if (SUCCEEDED(element->GetCurrentPatternAs(UIA_TogglePatternId,
                __uuidof(IUIAutomationTogglePattern), &toggle)) && toggle &&
            SUCCEEDED(toggle->Toggle())) return true;
    }
    ComPtr<IUIAutomationInvokePattern> invoke;
    if (SUCCEEDED(element->GetCurrentPatternAs(UIA_InvokePatternId,
            __uuidof(IUIAutomationInvokePattern), &invoke)) && invoke &&
        SUCCEEDED(invoke->Invoke())) return true;
    if (!preferToggle) {
        ComPtr<IUIAutomationTogglePattern> toggle;
        if (SUCCEEDED(element->GetCurrentPatternAs(UIA_TogglePatternId,
                __uuidof(IUIAutomationTogglePattern), &toggle)) && toggle &&
            SUCCEEDED(toggle->Toggle())) return true;
    }
    ComPtr<IUIAutomationLegacyIAccessiblePattern> legacy;
    if (SUCCEEDED(element->GetCurrentPatternAs(UIA_LegacyIAccessiblePatternId,
            __uuidof(IUIAutomationLegacyIAccessiblePattern), &legacy)) && legacy &&
        SUCCEEDED(legacy->DoDefaultAction())) return true;
    return false;
}

inline bool ClickCalibrated(HWND window, double xRatio, double yRatio) {
    RECT rect{};
    if (!GetWindowRect(window, &rect) || rect.right <= rect.left || rect.bottom <= rect.top) return false;
    const int x = rect.left + static_cast<int>((rect.right - rect.left) * xRatio);
    const int y = rect.top + static_cast<int>((rect.bottom - rect.top) * yRatio);
    if (!SetCursorPos(x, y)) return false;
    INPUT input[2]{};
    input[0].type = input[1].type = INPUT_MOUSE;
    input[0].mi.dwFlags = MOUSEEVENTF_LEFTDOWN;
    input[1].mi.dwFlags = MOUSEEVENTF_LEFTUP;
    return SendInput(2, input, sizeof(INPUT)) == 2;
}

inline bool UseUiaOrCoordinates(IUIAutomation* automation, IUIAutomationElement* root,
                                HWND window, const std::vector<std::wstring>& names,
                                bool preferToggle, double xRatio, double yRatio,
                                const std::function<void(const std::wstring&)>& progress) {
    auto element = FindNamedElement(automation, root, names, 2500);
    if (element && ActivateUiaElement(element.Get(), preferToggle)) {
        progress(L"已通过 UI Automation 操作“" + names.front() + L"”");
        return true;
    }
    progress(L"UI Automation 接口不可用，正在使用作者校准坐标……");
    return ClickCalibrated(window, xRatio, yRatio);
}

inline HWND WaitFolderDialog(DWORD timeoutMs) {
    const auto deadline = GetTickCount64() + timeoutMs;
    while (GetTickCount64() < deadline) {
        HWND foreground = GetForegroundWindow();
        wchar_t className[64]{};
        if (foreground) GetClassNameW(foreground, className, 64);
        if (foreground && wcscmp(className, L"#32770") == 0) return foreground;
        struct Context { HWND result = nullptr; } context;
        EnumWindows([](HWND window, LPARAM raw) -> BOOL {
            auto* context = reinterpret_cast<Context*>(raw);
            wchar_t name[64]{};
            GetClassNameW(window, name, 64);
            if (IsWindowVisible(window) && wcscmp(name, L"#32770") == 0) {
                context->result = window;
                return FALSE;
            }
            return TRUE;
        }, reinterpret_cast<LPARAM>(&context));
        if (context.result) return context.result;
        std::this_thread::sleep_for(std::chrono::milliseconds(200));
    }
    return nullptr;
}

inline void ChooseExtensionFolder(IUIAutomation* automation, HWND dialog,
                                  const fs::path& extensionDir) {
    Check(dialog != nullptr, L"没有出现选择扩展文件夹的窗口");
    SetForegroundWindow(dialog);
    {
        ClipboardGuard clipboard(extensionDir.wstring());
        SendChord(VK_CONTROL, 'L');
        SendChord(VK_CONTROL, 'V');
        std::this_thread::sleep_for(std::chrono::milliseconds(250));
        SendVirtualKey(VK_RETURN, true);
        SendVirtualKey(VK_RETURN, false);
        std::this_thread::sleep_for(std::chrono::milliseconds(800));
    }
    ComPtr<IUIAutomationElement> root;
    automation->ElementFromHandle(dialog, &root);
    auto button = root ? FindNamedElement(automation, root.Get(),
        {L"选择文件夹", L"选择文件夹(&S)", L"Select Folder", L"Select Folder (&S)"}, 1500)
        : nullptr;
    if (!button || !ActivateUiaElement(button.Get(), false)) {
        SendVirtualKey(VK_RETURN, true);
        SendVirtualKey(VK_RETURN, false);
    }
    std::this_thread::sleep_for(std::chrono::milliseconds(1500));
    Check(!IsWindow(dialog), L"文件夹选择窗口仍未关闭，扩展可能尚未加载");
}

inline void InstallBrowserExtension(
    const std::wstring& browser, const fs::path& executable, const fs::path& extensionDir,
    const BrowserPoints& points, const std::function<void(const std::wstring&)>& progress) {
    Check(fs::is_directory(extensionDir), L"扩展目录不存在：" + extensionDir.wstring());
    progress(L"正在打开 " + browser + L" 扩展管理页面……");
    const HWND window = OpenExtensionPage(browser, executable);
    ComPtr<IUIAutomation> automation;
    Check(SUCCEEDED(CoCreateInstance(CLSID_CUIAutomation, nullptr, CLSCTX_INPROC_SERVER,
                                     IID_PPV_ARGS(&automation))),
          L"无法初始化 Windows UI Automation");
    ComPtr<IUIAutomationElement> root;
    Check(SUCCEEDED(automation->ElementFromHandle(window, &root)) && root,
          L"无法读取浏览器 UI Automation 树");
    const std::vector<std::wstring> developerNames{
        L"开发者模式", L"开发人员模式", L"Developer mode"};
    const std::vector<std::wstring> loadNames{
        L"加载未打包的扩展", L"加载未打包的扩展程序", L"加载已解压的扩展程序",
        L"加载解压缩的扩展", L"加载解压缩扩展", L"Load unpacked"};
    auto load = FindNamedElement(automation.Get(), root.Get(), loadNames, 1200);
    if (!load) {
        Check(UseUiaOrCoordinates(automation.Get(), root.Get(), window, developerNames, true,
                                  points.developer_x, points.developer_y, progress),
              L"UI Automation 和备用坐标均无法操作“开发人员模式”");
        std::this_thread::sleep_for(std::chrono::milliseconds(900));
    }
    Check(UseUiaOrCoordinates(automation.Get(), root.Get(), window, loadNames, false,
                              points.load_x, points.load_y, progress),
          L"UI Automation 和备用坐标均无法操作“加载未打包的扩展”");
    ChooseExtensionFolder(automation.Get(), WaitFolderDialog(10000), extensionDir);
}

inline void OpenTargetUrl(const fs::path& executable, const std::wstring& url) {
    std::wstring command = L"\"" + executable.wstring() + L"\" --new-window --start-maximized \"" + url + L"\"";
    STARTUPINFOW startup{sizeof(startup)};
    PROCESS_INFORMATION process{};
    if (CreateProcessW(nullptr, command.data(), nullptr, nullptr, FALSE, 0, nullptr, nullptr,
                       &startup, &process)) {
        CloseHandle(process.hThread);
        CloseHandle(process.hProcess);
    }
}

}  // namespace native
