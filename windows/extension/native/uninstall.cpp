#include "native_support.hpp"
#include "resource.h"

#include <shellapi.h>

#include <sstream>

#pragma comment(lib, "advapi32.lib")
#pragma comment(lib, "ole32.lib")
#pragma comment(lib, "shell32.lib")

namespace {

namespace fs = std::filesystem;

constexpr wchar_t kDisplayName[] = L"拼图助手（Extension 版）";

bool HasArgument(const wchar_t* expected) {
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

fs::path CurrentExecutable() {
    std::wstring buffer(32768, L'\0');
    const DWORD length = GetModuleFileNameW(nullptr, buffer.data(), static_cast<DWORD>(buffer.size()));
    native::Check(length > 0 && length < buffer.size(), L"无法定位卸载程序");
    buffer.resize(length);
    return buffer;
}

std::wstring Normalized(const fs::path& path) {
    std::error_code error;
    fs::path absolute = fs::weakly_canonical(path, error);
    if (error) absolute = fs::absolute(path, error);
    std::wstring value = absolute.wstring();
    std::transform(value.begin(), value.end(), value.begin(), towlower);
    return value;
}

void TerminateInstalledProcesses(const fs::path& installDir) {
    const std::set<std::wstring> targets{
        Normalized(installDir / L"app/Fxxk_Puzzle.exe")};
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

void Uninstall(const fs::path& installDir) {
    TerminateInstalledProcesses(installDir);
    RegDeleteTreeW(HKEY_CURRENT_USER, L"Software\\Classes\\fxxk-puzzle");
    RemoveTreeWithRetry(installDir);
    RegDeleteTreeW(HKEY_CURRENT_USER,
        L"Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\FxxkPuzzleExtension");
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

void RelayToTemporaryCopy(bool quiet) {
    wchar_t tempRaw[MAX_PATH]{};
    native::Check(GetTempPathW(MAX_PATH, tempRaw) > 0, L"无法定位临时目录");
    const fs::path directory = fs::path(tempRaw) /
        (L"FxxkPuzzleExtension-uninstall-" + std::to_wstring(GetCurrentProcessId()));
    fs::create_directories(directory);
    const fs::path copy = directory / L"Uninstall.exe";
    fs::copy_file(CurrentExecutable(), copy, fs::copy_options::overwrite_existing);
    std::wstring command = L"\"" + copy.wstring() + L"\" --remove";
    if (quiet) command += L" --quiet";
    STARTUPINFOW startup{sizeof(startup)};
    PROCESS_INFORMATION process{};
    native::Check(CreateProcessW(nullptr, command.data(), nullptr, nullptr, FALSE,
                                 CREATE_NO_WINDOW, nullptr, directory.c_str(), &startup, &process),
                  L"无法启动临时卸载程序");
    CloseHandle(process.hThread);
    CloseHandle(process.hProcess);
}

}  // namespace

int WINAPI wWinMain(HINSTANCE, HINSTANCE, PWSTR, int) {
    if (HasArgument(L"--smoke-test")) return 0;
    const bool quiet = HasArgument(L"--quiet");
    const bool remove = HasArgument(L"--remove");
    try {
        if (!remove) {
            RelayToTemporaryCopy(quiet);
            return 0;
        }
        if (!quiet) {
            const int answer = MessageBoxW(nullptr,
                L"将停止拼图助手，删除程序与识别区域配置，并移除 fxxk-puzzle:// 启动协议。",
                L"卸载 拼图助手（Extension 版）", MB_OKCANCEL | MB_ICONWARNING);
            if (answer != IDOK) return 0;
        }
        const fs::path installDir = native::LocalAppData() / L"Programs/FxxkPuzzleExtension";
        Uninstall(installDir);
        if (!quiet) {
            MessageBoxW(nullptr, L"拼图助手（Extension 版）已从此电脑移除。",
                        L"卸载完成", MB_OK | MB_ICONINFORMATION);
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
