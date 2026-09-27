#include "native_support.hpp"
#include "resource.h"

#pragma comment(lib, "advapi32.lib")
#pragma comment(lib, "ole32.lib")
#pragma comment(lib, "shell32.lib")

namespace {

namespace fs = std::filesystem;

fs::path CurrentExecutable() {
    std::wstring buffer(32768, L'\0');
    const DWORD length = GetModuleFileNameW(nullptr, buffer.data(), static_cast<DWORD>(buffer.size()));
    native::Check(length > 0 && length < buffer.size(), L"无法定位卸载程序");
    buffer.resize(length);
    return buffer;
}

// 两版共用这一个卸载器：版本差异全部收在 Edition 里。判断哪一版优先看"我自己
// 躺在哪个目录里"，其次才看命令行参数（见 ResolvePlan）。
// extension 版只多一件事——让独立的 Extension_Cleanup.exe 去移除浏览器里的条目；
// UIA 那套代码不在卸载器里，所以卸载器本身可以是同一个源文件。
struct Edition {
    const wchar_t* folder;         // 安装目录名，同时也是卸载注册表键名
    const wchar_t* label;          // 弹窗里给用户看的版本名
    const wchar_t* startupLink;    // 登录启动快捷方式，没有就留空
    std::vector<const wchar_t*> processes;
};

constexpr wchar_t kExtensionFolder[] = L"FxxkPuzzleExtension";
constexpr wchar_t kMonitorFolder[] = L"FxxkPuzzleMonitor";

Edition SelectEdition(bool withExtension) {
    if (withExtension) {
        return {kExtensionFolder, L"Extension 版", L"", {L"app/Fxxk_Puzzle.exe"}};
    }
    // launcher.exe 是常驻监视器，Fxxk_Puzzle.exe 是它拉起的操作界面，两个都要停。
    return {kMonitorFolder, L"Monitor 版", L"拼图助手 Monitor.lnk",
            {L"launcher.exe", L"Fxxk_Puzzle.exe"}};
}

bool IsExtensionLayout(const fs::path& dir) {
    return fs::exists(dir / L"extensions" / L"manifest.json") ||
           fs::exists(dir / L"Extension_Cleanup.exe") ||
           fs::exists(dir / L"app" / L"Fxxk_Puzzle.exe");
}

bool IsMonitorLayout(const fs::path& dir) {
    return fs::exists(dir / L"launcher.exe");
}

// 版本先按"这个卸载器自己躺在哪个目录里"判断，命令行参数只是补充。
// 只认 --extension 会出大事：从资源管理器双击 Uninstall.exe 时没有任何参数，
// 卸载器会以为自己是 Monitor 版，于是既不去清浏览器扩展，还会删错安装目录
// （实测把同一台机器上的 FxxkPuzzleMonitor 删掉了）。
struct Plan {
    fs::path installDir;
    bool withExtension;
};

Plan ResolvePlan(bool withExtension, bool withMonitor) {
    const fs::path here = CurrentExecutable().parent_path();
    if (IsMonitorLayout(here)) return {here, false};
    if (IsExtensionLayout(here)) return {here, true};
    const fs::path programs = native::LocalAppData() / L"Programs";
    const fs::path extension = programs / kExtensionFolder;
    const fs::path monitor = programs / kMonitorFolder;
    if (withExtension) return {extension, true};
    // 中继到临时目录后就没法再靠"自己在哪"判断了，所以第一道进程会把结论
    // 用这两个参数原样传过来。
    if (withMonitor) return {monitor, false};
    const bool extensionInstalled = IsExtensionLayout(extension);
    if (extensionInstalled && !IsMonitorLayout(monitor)) return {extension, true};
    if (!extensionInstalled && IsMonitorLayout(monitor)) return {monitor, false};
    // 两种都对不上（或被删了一半）：宁可停在原地报错，也不要猜一个目录去删。
    return {{}, withExtension};
}

std::wstring Normalized(const fs::path& path) {
    std::error_code error;
    fs::path absolute = fs::weakly_canonical(path, error);
    if (error) absolute = fs::absolute(path, error);
    std::wstring value = absolute.wstring();
    std::transform(value.begin(), value.end(), value.begin(), towlower);
    return value;
}

void TerminateInstalledProcesses(const fs::path& installDir, const Edition& edition) {
    std::set<std::wstring> targets;
    for (const auto* relative : edition.processes) {
        targets.insert(Normalized(installDir / relative));
    }
    HANDLE snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
    if (snapshot == INVALID_HANDLE_VALUE) return;
    PROCESSENTRY32W entry{sizeof(entry)};
    if (!Process32FirstW(snapshot, &entry)) {
        CloseHandle(snapshot);
        return;
    }
    do {
        HANDLE process = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_TERMINATE |
                                     SYNCHRONIZE, FALSE, entry.th32ProcessID);
        if (!process) continue;
        std::wstring path(32768, L'\0');
        DWORD length = static_cast<DWORD>(path.size());
        if (QueryFullProcessImageNameW(process, 0, path.data(), &length)) {
            path.resize(length);
            if (targets.contains(Normalized(path))) {
                TerminateProcess(process, 0);
                WaitForSingleObject(process, 3000);
            }
        }
        CloseHandle(process);
    } while (Process32NextW(snapshot, &entry));
    CloseHandle(snapshot);
}

void RemoveTreeWithRetry(const fs::path& path) {
    if (!fs::exists(path)) return;
    std::error_code last;
    for (int attempt = 0; attempt < 15; ++attempt) {
        last.clear();
        fs::remove_all(path, last);
        if (!fs::exists(path)) return;
        std::this_thread::sleep_for(std::chrono::milliseconds(200));
    }
    throw native::Error(L"无法删除安装目录：" + path.wstring() + L"\r\n" +
                        native::WideFromUtf8(last.message().c_str()));
}

// 卸载结果分成三种，必须区分开：浏览器里压根没找到条目 / 找到了并且删掉了 /
// 找到了但没删掉。v2.1.0 的问题就是把"没检测到"当成了"已清理"。
struct UninstallResult {
    std::vector<std::wstring> failedBrowsers;
    bool hadExtensionFiles = false;
    bool matchedAnyBrowser = false;
};

// 清理程序用退出码回报，哪几个浏览器还留着条目按位给出（见 native::CleanupStatus）；
// 需要用户接手时它自己会打开扩展管理页面，卸载器不碰浏览器。
int RunExtensionCleanup(const fs::path& installDir, const fs::path& extensionsDir) {
    const fs::path tool = installDir / L"Extension_Cleanup.exe";
    if (!fs::is_regular_file(tool)) return native::kCleanupBroken;
    std::wstring command = L"\"" + tool.wstring() + L"\" \"" + extensionsDir.wstring() + L"\"";
    STARTUPINFOW startup{sizeof(startup)};
    startup.dwFlags = STARTF_USESHOWWINDOW;
    startup.wShowWindow = SW_HIDE;
    PROCESS_INFORMATION process{};
    if (!CreateProcessW(nullptr, command.data(), nullptr, nullptr, FALSE, CREATE_NO_WINDOW,
                        nullptr, installDir.c_str(), &startup, &process)) {
        return native::kCleanupBroken;
    }
    CloseHandle(process.hThread);
    // 两个浏览器各要开一次扩展页，慢的时候一轮就要几十秒，别提前判它失败。
    const DWORD waited = WaitForSingleObject(process.hProcess, 5 * 60 * 1000);
    DWORD status = native::kCleanupBroken;
    if (waited == WAIT_OBJECT_0) GetExitCodeProcess(process.hProcess, &status);
    CloseHandle(process.hProcess);
    return waited == WAIT_OBJECT_0 ? static_cast<int>(status) : native::kCleanupBroken;
}

UninstallResult Uninstall(const fs::path& installDir, const Edition& edition, bool withExtension) {
    UninstallResult result;
    const fs::path extensionsDir = installDir / L"extensions";
    result.hadExtensionFiles = fs::exists(extensionsDir / L"manifest.json");
    TerminateInstalledProcesses(installDir, edition);
    if (withExtension) {
        const int status = RunExtensionCleanup(installDir, extensionsDir);
        if (status & native::kCleanupChrome) result.failedBrowsers.push_back(L"chrome");
        if (status & native::kCleanupEdge) result.failedBrowsers.push_back(L"edge");
        result.matchedAnyBrowser = (status & native::kCleanupNotFound) == 0;
        if (status & native::kCleanupBroken) {
            // 清理程序压根没跑起来：说不清浏览器里还剩什么，按"可能还留着"处理。
            result.matchedAnyBrowser = true;
            result.failedBrowsers = {L"chrome", L"edge"};
        }
    } else if (*edition.startupLink) {
        PWSTR startup = nullptr;
        if (SUCCEEDED(SHGetKnownFolderPath(FOLDERID_Startup, 0, nullptr, &startup))) {
            std::error_code ignored;
            fs::remove(fs::path(startup) / edition.startupLink, ignored);
        }
        CoTaskMemFree(startup);
    }
    RegDeleteTreeW(HKEY_CURRENT_USER, L"Software\\Classes\\fxxk-puzzle");
    RemoveTreeWithRetry(installDir);
    RegDeleteTreeW(HKEY_CURRENT_USER,
        (L"Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\" +
         std::wstring(edition.folder)).c_str());
    return result;
}

void ScheduleTemporaryCleanup(const fs::path& executable) {
    const fs::path directory = executable.parent_path();
    std::wstring command = L"cmd.exe /d /c \"ping 127.0.0.1 -n 3 >nul & del /f /q \"\"" +
        executable.wstring() + L"\"\" >nul 2>&1 & rmdir /s /q \"\"" +
        directory.wstring() + L"\"\" >nul 2>&1\"";
    STARTUPINFOW startup{sizeof(startup)};
    startup.dwFlags = STARTF_USESHOWWINDOW;
    startup.wShowWindow = SW_HIDE;
    PROCESS_INFORMATION process{};
    if (CreateProcessW(nullptr, command.data(), nullptr, nullptr, FALSE,
                       CREATE_NO_WINDOW, nullptr, nullptr, &startup, &process)) {
        CloseHandle(process.hThread);
        CloseHandle(process.hProcess);
    }
}

void RelayToTemporaryCopy(const std::wstring& tempPrefix, bool quiet, bool withExtension) {
    wchar_t tempRaw[MAX_PATH]{};
    native::Check(GetTempPathW(MAX_PATH, tempRaw) > 0, L"无法定位临时目录");
    const fs::path directory = fs::path(tempRaw) /
        (tempPrefix + L"-uninstall-" + std::to_wstring(GetCurrentProcessId()));
    fs::create_directories(directory);
    const fs::path copy = directory / L"Uninstall.exe";
    fs::copy_file(CurrentExecutable(), copy, fs::copy_options::overwrite_existing);
    std::wstring command = L"\"" + copy.wstring() + L"\" --remove";
    if (quiet) command += L" --quiet";
    // 搬去临时目录是为了能删掉自己；版本结论必须原样传过去，副本已经不在
    // 安装目录里，没法再靠自己判断。
    command += withExtension ? L" --extension" : L" --monitor";
    STARTUPINFOW startup{sizeof(startup)};
    PROCESS_INFORMATION process{};
    native::Check(CreateProcessW(nullptr, command.data(), nullptr, nullptr, FALSE,
                                 CREATE_NO_WINDOW, nullptr, directory.c_str(), &startup, &process),
                  L"无法启动临时卸载程序");
    CloseHandle(process.hThread);
    CloseHandle(process.hProcess);
}

void Show(const std::wstring& text, const std::wstring& title, UINT icon) {
    MessageBoxW(nullptr, text.c_str(), title.c_str(), MB_OK | icon);
}

std::wstring BrowserLabels(const std::vector<std::wstring>& browsers) {
    std::wstring labels;
    for (const auto& browser : browsers) {
        const std::wstring label = browser == L"chrome" ? L"Chrome" : L"Edge";
        if (labels.find(label) == std::wstring::npos) {
            if (!labels.empty()) labels += L"、";
            labels += label;
        }
    }
    return labels;
}

}  // namespace

int WINAPI wWinMain(HINSTANCE, HINSTANCE, PWSTR, int) {
    if (native::HasArgument(L"--smoke-test")) return 0;
    const bool quiet = native::HasArgument(L"--quiet");
    const bool remove = native::HasArgument(L"--remove");
    const bool withExtension = native::HasArgument(L"--extension");
    const Plan plan = ResolvePlan(withExtension, native::HasArgument(L"--monitor"));
    const Edition edition = SelectEdition(plan.withExtension);
    const std::wstring name = L"拼图助手（" + std::wstring(edition.label) + L"）";
    try {
        if (!remove) {
            RelayToTemporaryCopy(edition.folder, quiet, plan.withExtension);
            return 0;
        }
        native::Check(!plan.installDir.empty(),
            L"没找到本程序的安装目录，已停止卸载。\n"
            L"请从「设置 → 应用」里卸载，或直接运行安装目录里的 Uninstall.exe。");
        if (!quiet) {
            const std::wstring prompt = plan.withExtension
                ? L"将停止拼图助手，删除程序与识别区域配置，移除 fxxk-puzzle:// 启动协议，"
                  L"并尝试自动移除浏览器中已加载的扩展。"
                : L"将停止拼图助手，删除程序与识别区域配置，移除登录启动项和 "
                  L"fxxk-puzzle:// 启动协议。";
            if (MessageBoxW(nullptr, prompt.c_str(), (L"卸载 " + name).c_str(),
                            MB_OKCANCEL | MB_ICONWARNING) != IDOK) {
                return 0;
            }
        }
        const auto result = Uninstall(plan.installDir, edition, plan.withExtension);
        if (quiet) return 0;
        if (!result.failedBrowsers.empty()) {
            Show(L"程序文件已删除，但没能自动从 " + BrowserLabels(result.failedBrowsers) +
                 L" 移除扩展条目。\n\n"
                 L"请在地址栏打开 chrome://extensions 或 edge://extensions，找到「拼图助手」，"
                 L"点它卡片上的「移除」。\n"
                 L"该条目指向的目录已不存在，浏览器可能把它标为错误状态，移除即可清除。",
                 L"请手动移除浏览器扩展", MB_ICONWARNING);
        } else if (result.matchedAnyBrowser) {
            Show(name + L"已从此电脑移除，浏览器中的扩展也已一并移除。",
                 L"卸载完成", MB_ICONINFORMATION);
        } else if (result.hadExtensionFiles) {
            Show(L"程序文件已删除，但没能确认浏览器里的扩展条目是否还在。\n\n"
                 L"请在地址栏打开 chrome://extensions 或 edge://extensions，"
                 L"如果列表里还有「拼图助手」，请点它卡片上的「移除」。",
                 L"请检查浏览器扩展", MB_ICONWARNING);
        } else {
            Show(name + L"已从此电脑移除。", L"卸载完成", MB_ICONINFORMATION);
        }
        ScheduleTemporaryCleanup(CurrentExecutable());
        return 0;
    } catch (const std::exception& error) {
        if (!quiet) {
            MessageBoxW(nullptr, native::WideFromUtf8(error.what()).c_str(), L"卸载未完成",
                        MB_OK | MB_ICONERROR);
        }
        return 1;
    }
}
