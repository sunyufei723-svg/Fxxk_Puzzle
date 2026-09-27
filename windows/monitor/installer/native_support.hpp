#pragma once

#ifndef UNICODE
#define UNICODE
#endif
#ifndef _UNICODE
#define _UNICODE
#endif

#include <windows.h>
#include <shlobj.h>
#include <shobjidl.h>
#include <shellapi.h>
#include <tlhelp32.h>

#include <algorithm>
#include <chrono>
#include <filesystem>
#include <fstream>
#include <functional>
#include <set>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>


#pragma comment(lib, "user32.lib")
#pragma comment(lib, "advapi32.lib")
#pragma comment(lib, "ole32.lib")
#pragma comment(lib, "shell32.lib")

namespace native {

namespace fs = std::filesystem;

// Extension_Cleanup.exe 的退出码，卸载器按位解读。monitor 版没有这个程序，但两版
// 共用同一份 uninstall.cpp，所以常量两边都得有。
enum CleanupStatus : int {
    kCleanupClean = 0,
    kCleanupChrome = 1,   // Chrome 里还留着条目
    kCleanupEdge = 2,     // Edge 里还留着条目
    kCleanupNotFound = 4, // 两个浏览器都没有指向该目录的条目
    kCleanupBroken = 8,   // 参数或环境不对，什么都没做成
};

inline std::string Utf8(const std::wstring& value) {
    if (value.empty()) return {};
    const int size = WideCharToMultiByte(CP_UTF8, 0, value.data(), static_cast<int>(value.size()),
                                         nullptr, 0, nullptr, nullptr);
    std::string result(static_cast<size_t>(size), '\0');
    WideCharToMultiByte(CP_UTF8, 0, value.data(), static_cast<int>(value.size()),
                        result.data(), size, nullptr, nullptr);
    return result;
}

inline std::runtime_error Error(const std::wstring& message) {
    return std::runtime_error(Utf8(message));
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

inline bool HasArgument(const wchar_t* expected) {
    int count = 0;
    LPWSTR* arguments = CommandLineToArgvW(GetCommandLineW(), &count);
    if (!arguments) return false;
    bool found = false;
    for (int index = 1; index < count; ++index) {
        if (_wcsicmp(arguments[index], expected) == 0) found = true;
    }
    LocalFree(arguments);
    return found;
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


inline uint64_t DirectorySize(const fs::path& root) {
    uint64_t total = 0;
    std::error_code error;
    for (fs::recursive_directory_iterator it(root, error), end; !error && it != end; it.increment(error)) {
        if (it->is_regular_file(error)) total += it->file_size(error);
    }
    return total;
}

inline void RegisterUninstall(const fs::path& installDir, const fs::path& executable) {
    constexpr auto path = L"Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\FxxkPuzzleMonitor";
    HKEY key = nullptr;
    Check(RegCreateKeyExW(HKEY_CURRENT_USER, path, 0, nullptr, 0, KEY_WRITE, nullptr,
                          &key, nullptr) == ERROR_SUCCESS,
          L"无法登记 Windows 卸载信息");
    const auto uninstaller = installDir / L"Uninstall.exe";
    SetRegistryString(key, L"DisplayName", L"拼图助手（Monitor 版）");
    SetRegistryString(key, L"DisplayVersion", L"2.2.0");
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



// launcher.exe 用 host 精确匹配 + 路径前缀判断当前页面是否要拉起业务进程，
// 所以安装时必须把用户填的完整网址拆成这两项写进 config.json。
inline void SplitTarget(const std::wstring& url, std::wstring& host, std::wstring& path) {
    const size_t scheme = url.find(L"://");
    std::wstring rest = scheme == std::wstring::npos ? url : url.substr(scheme + 3);
    const size_t slash = rest.find(L'/');
    host = slash == std::wstring::npos ? rest : rest.substr(0, slash);
    path = slash == std::wstring::npos ? std::wstring(L"/") : rest.substr(slash);
    const size_t query = path.find_first_of(L"?#");
    if (query != std::wstring::npos) path = path.substr(0, query);
    if (path.empty()) path = std::wstring(L"/");
    const size_t colon = host.find(L':');
    if (colon != std::wstring::npos) host = host.substr(0, colon);
    for (auto& character : host) {
        if (character >= L'A' && character <= L'Z') character += 32;
    }
}

// 热键写成 JSON 对象字符串，供 config.json 与 settings.json 直接嵌入（不要再过 JsonEscape）。
inline std::string HotkeyJson(const std::wstring& selectKey, const std::wstring& executeKey,
                              const std::wstring& cancelKey) {
    return "{\n    \"select\": \"" + JsonEscape(selectKey) + "\",\n"
           "    \"execute\": \"" + JsonEscape(executeKey) + "\",\n"
           "    \"cancel\": \"" + JsonEscape(cancelKey) + "\"\n  }";
}

// launcher.exe 与 Fxxk_Puzzle.exe 必须同目录：process.py 用 sys.executable 旁边的 Fxxk_Puzzle.exe 起子进程。
inline void CopyPayload(const fs::path& source, const fs::path& destination,
                        const std::wstring& url, const std::wstring& selectKey,
                        const std::wstring& executeKey, const std::wstring& cancelKey,
                        const std::vector<std::wstring>& browsers) {
    Check(fs::is_regular_file(source / L"launcher.exe"), L"安装包缺少 launcher.exe");
    Check(fs::is_regular_file(source / L"Fxxk_Puzzle.exe"), L"安装包缺少 Fxxk_Puzzle.exe");
    Check(fs::is_directory(source / L"_internal"), L"安装包缺少 _internal 运行库目录");
    Check(fs::is_regular_file(source / L"Uninstall.exe"), L"安装包缺少 Uninstall.exe");
    fs::create_directories(destination);
    for (const auto& name : {L"launcher.exe", L"Fxxk_Puzzle.exe", L"Uninstall.exe"}) {
        fs::copy_file(source / name, destination / name, fs::copy_options::overwrite_existing);
    }
    fs::copy(source / L"_internal", destination / L"_internal",
             fs::copy_options::recursive | fs::copy_options::overwrite_existing);
    const auto icon = source / L"icon16.png";
    if (fs::is_regular_file(icon)) {
        fs::copy_file(icon, destination / L"icon16.png", fs::copy_options::overwrite_existing);
    }

    std::wstring host;
    std::wstring path;
    SplitTarget(url, host, path);
    std::string browserList;
    for (size_t index = 0; index < browsers.size(); ++index) {
        if (index) browserList += ", ";
        browserList += "\"" + JsonEscape(browsers[index]) + "\"";
    }
    const std::string hotkeys = HotkeyJson(selectKey, executeKey, cancelKey);
    WriteUtf8(destination / L"config.json",
              "{\n  \"target_url\": \"" + JsonEscape(url) + "\",\n"
              "  \"target_host\": \"" + JsonEscape(host) + "\",\n"
              "  \"target_path_prefix\": \"" + JsonEscape(path) + "\",\n"
              "  \"browsers\": [" + browserList + "],\n"
              "  \"hotkeys\": " + hotkeys + ",\n"
              "  \"check_interval_seconds\": 1.0\n}\n");
    WriteUtf8(destination / L"settings.json",
              "{\n  \"drag_speed\": 600,\n  \"hotkeys\": " + hotkeys + "\n}\n");
}

inline fs::path StartupFolder() {
    PWSTR buffer = nullptr;
    Check(SHGetKnownFolderPath(FOLDERID_Startup, 0, nullptr, &buffer) == S_OK,
          L"无法定位 Windows 启动目录");
    std::wstring path(buffer);
    CoTaskMemFree(buffer);
    return fs::path(path);
}

// 常驻监视必须随登录启动；.lnk 只能走 Shell Link COM。
// 这里用 __uuidof / IID_PPV_ARGS 而不是手写 GUID：手写的 {000213F6,...} 在实测中
// 让 CoCreateInstance 返回 0x80040154 REGDB_E_CLASSNOTREG，而 SDK 头里的
// DECLSPEC_UUID 声明不会错。
inline void CreateStartupShortcut(const fs::path& target) {
    IShellLinkW* link = nullptr;
    const HRESULT created = CoCreateInstance(__uuidof(ShellLink), nullptr, CLSCTX_INPROC_SERVER,
                                             IID_PPV_ARGS(&link));
    if (FAILED(created) || !link) {
        wchar_t code[32]{};
        swprintf_s(code, L"0x%08lX", (unsigned long)created);
        throw Error(L"无法创建登录启动快捷方式（" + std::wstring(code) + L"）");
    }
    IPersistFile* persist = nullptr;
    const HRESULT queried = link->QueryInterface(IID_PPV_ARGS(&persist));
    link->SetPath(target.wstring().c_str());
    link->SetWorkingDirectory(target.parent_path().wstring().c_str());
    link->Release();
    Check(SUCCEEDED(queried) && persist, L"无法保存登录启动快捷方式");
    const fs::path path = StartupFolder() / L"拼图助手 Monitor.lnk";
    const HRESULT saved = persist->Save(path.wstring().c_str(), TRUE);
    persist->Release();
    Check(SUCCEEDED(saved), L"无法写入登录启动快捷方式");
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
