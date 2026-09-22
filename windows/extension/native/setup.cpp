#include "native_support.hpp"
#include "resource.h"

#include <commctrl.h>
#include <shellapi.h>
#include <uxtheme.h>

#include <memory>
#include <sstream>

#pragma comment(lib, "advapi32.lib")
#pragma comment(lib, "comctl32.lib")
#pragma comment(lib, "gdi32.lib")
#pragma comment(lib, "ole32.lib")
#pragma comment(lib, "oleaut32.lib")
#pragma comment(lib, "shell32.lib")
#pragma comment(lib, "uiautomationcore.lib")
#pragma comment(lib, "uxtheme.lib")

namespace {

namespace fs = std::filesystem;

constexpr wchar_t kWindowClass[] = L"FxxkPuzzleExtensionSetup";
constexpr COLORREF kBackgroundColor = RGB(248, 250, 252);
constexpr COLORREF kTextColor = RGB(31, 41, 55);
constexpr COLORREF kMutedTextColor = RGB(75, 85, 99);
constexpr COLORREF kPrimaryColor = RGB(37, 99, 235);
constexpr COLORREF kPrimaryPressedColor = RGB(29, 78, 216);
constexpr COLORREF kBorderColor = RGB(203, 213, 225);
constexpr UINT WM_INSTALL_PROGRESS = WM_APP + 1;
constexpr UINT WM_INSTALL_FINISHED = WM_APP + 2;

enum ControlId {
    ID_NEXT = 1001,
    ID_BACK,
    ID_CANCEL,
    ID_CHROME,
    ID_EDGE,
    ID_SUFE,
    ID_URL,
    ID_SELECT_KEY,
    ID_EXECUTE_KEY,
    ID_CANCEL_KEY,
    ID_INSTALL,
    ID_FINISH,
};

enum class Page { Consent, Browser, Target, Hotkeys, Summary, Installing, Complete };

struct InstallResult {
    bool success = false;
    std::wstring message;
    std::vector<std::wstring> installed;
};

std::wstring WideFromUtf8(const char* value) {
    if (!value || !*value) return L"未知错误";
    const int size = MultiByteToWideChar(CP_UTF8, 0, value, -1, nullptr, 0);
    std::wstring result(static_cast<size_t>(size > 0 ? size : 1), L'\0');
    MultiByteToWideChar(CP_UTF8, 0, value, -1, result.data(), size);
    if (!result.empty() && result.back() == L'\0') result.pop_back();
    return result;
}

class SetupWindow {
public:
    explicit SetupWindow(HINSTANCE instance) : instance_(instance) {
        backgroundBrush_ = CreateSolidBrush(kBackgroundColor);
    }

    ~SetupWindow() {
        if (backgroundBrush_) DeleteObject(backgroundBrush_);
    }

    bool Create() {
        WNDCLASSEXW windowClass{sizeof(windowClass)};
        windowClass.lpfnWndProc = WindowProc;
        windowClass.hInstance = instance_;
        windowClass.hIcon = LoadIconW(instance_, MAKEINTRESOURCEW(IDI_APP_ICON));
        windowClass.hIconSm = windowClass.hIcon;
        windowClass.hCursor = LoadCursorW(nullptr, IDC_ARROW);
        windowClass.hbrBackground = backgroundBrush_;
        windowClass.lpszClassName = kWindowClass;
        if (!RegisterClassExW(&windowClass) && GetLastError() != ERROR_CLASS_ALREADY_EXISTS) {
            return false;
        }
        window_ = CreateWindowExW(
            0, kWindowClass, L"拼图助手安装向导（Extension 版）",
            WS_OVERLAPPED | WS_CAPTION | WS_SYSMENU | WS_MINIMIZEBOX,
            CW_USEDEFAULT, CW_USEDEFAULT, 700, 540, nullptr, nullptr, instance_, this);
        if (!window_) return false;
        ShowWindow(window_, SW_SHOW);
        UpdateWindow(window_);
        return true;
    }

    HWND Handle() const { return window_; }

private:
    static LRESULT CALLBACK WindowProc(HWND window, UINT message, WPARAM wParam, LPARAM lParam) {
        SetupWindow* self = reinterpret_cast<SetupWindow*>(GetWindowLongPtrW(window, GWLP_USERDATA));
        if (message == WM_NCCREATE) {
            const auto* create = reinterpret_cast<CREATESTRUCTW*>(lParam);
            self = static_cast<SetupWindow*>(create->lpCreateParams);
            self->window_ = window;
            SetWindowLongPtrW(window, GWLP_USERDATA, reinterpret_cast<LONG_PTR>(self));
        }
        return self ? self->HandleMessage(message, wParam, lParam)
                    : DefWindowProcW(window, message, wParam, lParam);
    }

    LRESULT HandleMessage(UINT message, WPARAM wParam, LPARAM lParam) {
        switch (message) {
            case WM_CREATE:
                font_ = CreateFontW(-16, 0, 0, 0, FW_NORMAL, FALSE, FALSE, FALSE,
                                    DEFAULT_CHARSET, OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS,
                                    CLEARTYPE_QUALITY, DEFAULT_PITCH, L"Microsoft YaHei UI");
                titleFont_ = CreateFontW(-24, 0, 0, 0, FW_SEMIBOLD, FALSE, FALSE, FALSE,
                                         DEFAULT_CHARSET, OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS,
                                         CLEARTYPE_QUALITY, DEFAULT_PITCH, L"Microsoft YaHei UI");
                ShowConsent();
                return 0;
            case WM_COMMAND:
                HandleCommand(LOWORD(wParam), HIWORD(wParam));
                return 0;
            case WM_CTLCOLORSTATIC: {
                HDC dc = reinterpret_cast<HDC>(wParam);
                SetBkMode(dc, TRANSPARENT);
                SetTextColor(dc, reinterpret_cast<HWND>(lParam) == instruction_
                                     ? kMutedTextColor : kTextColor);
                return reinterpret_cast<LRESULT>(backgroundBrush_);
            }
            case WM_CTLCOLORBTN: {
                HDC dc = reinterpret_cast<HDC>(wParam);
                SetBkMode(dc, TRANSPARENT);
                SetTextColor(dc, kTextColor);
                return reinterpret_cast<LRESULT>(backgroundBrush_);
            }
            case WM_DRAWITEM:
                return DrawButton(*reinterpret_cast<DRAWITEMSTRUCT*>(lParam)) ? TRUE : FALSE;
            case WM_INSTALL_PROGRESS: {
                std::unique_ptr<std::wstring> text(reinterpret_cast<std::wstring*>(lParam));
                if (status_) SetWindowTextW(status_, text->c_str());
                return 0;
            }
            case WM_INSTALL_FINISHED: {
                std::unique_ptr<InstallResult> result(reinterpret_cast<InstallResult*>(lParam));
                busy_ = false;
                if (!result->success) {
                    MessageBoxW(window_, result->message.c_str(), L"安装未完成", MB_OK | MB_ICONERROR);
                    ShowSummary();
                } else {
                    installed_ = result->installed;
                    ShowComplete();
                }
                return 0;
            }
            case WM_TIMER:
                DestroyWindow(window_);
                return 0;
            case WM_CLOSE:
                if (busy_) {
                    MessageBoxW(window_, L"请等待当前安装步骤完成。", L"正在安装",
                                MB_OK | MB_ICONINFORMATION);
                } else {
                    DestroyWindow(window_);
                }
                return 0;
            case WM_DESTROY:
                if (font_) DeleteObject(font_);
                if (titleFont_) DeleteObject(titleFont_);
                PostQuitMessage(0);
                return 0;
            default:
                return DefWindowProcW(window_, message, wParam, lParam);
        }
    }

    HWND AddControl(const wchar_t* className, const std::wstring& text, DWORD style,
                    int x, int y, int width, int height, int id = 0) {
        HWND control = CreateWindowExW(
            0, className, text.c_str(), WS_CHILD | WS_VISIBLE | style,
            x, y, width, height, window_, reinterpret_cast<HMENU>(static_cast<INT_PTR>(id)),
            instance_, nullptr);
        SendMessageW(control, WM_SETFONT, reinterpret_cast<WPARAM>(font_), TRUE);
        SetWindowTheme(control, L"Explorer", nullptr);
        controls_.push_back(control);
        return control;
    }

    HWND AddLabel(const std::wstring& text, int x, int y, int width, int height,
                  DWORD style = SS_LEFT) {
        return AddControl(L"STATIC", text, style | SS_NOPREFIX, x, y, width, height);
    }

    HWND AddButton(const std::wstring& text, int x, int y, int width, int id,
                   DWORD extraStyle = 0) {
        HWND button = AddControl(L"BUTTON", text, WS_TABSTOP | BS_OWNERDRAW,
                                 x, y, width, 36, id);
        if ((extraStyle & BS_DEFPUSHBUTTON) != 0) primaryButtons_.push_back(button);
        return button;
    }

    bool DrawButton(const DRAWITEMSTRUCT& item) const {
        if (item.CtlType != ODT_BUTTON) return false;
        const bool primary = std::find(primaryButtons_.begin(), primaryButtons_.end(),
                                       item.hwndItem) != primaryButtons_.end();
        const bool pressed = (item.itemState & ODS_SELECTED) != 0;
        const bool disabled = (item.itemState & ODS_DISABLED) != 0;
        RECT rect = item.rcItem;
        HBRUSH fill = CreateSolidBrush(
            disabled ? RGB(226, 232, 240)
                     : primary ? (pressed ? kPrimaryPressedColor : kPrimaryColor)
                               : (pressed ? RGB(241, 245, 249) : RGB(255, 255, 255)));
        HPEN border = CreatePen(PS_SOLID, 1,
            disabled ? RGB(203, 213, 225) : primary ? kPrimaryColor : kBorderColor);
        HGDIOBJ oldBrush = SelectObject(item.hDC, fill);
        HGDIOBJ oldPen = SelectObject(item.hDC, border);
        RoundRect(item.hDC, rect.left, rect.top, rect.right, rect.bottom, 8, 8);
        SelectObject(item.hDC, oldBrush);
        SelectObject(item.hDC, oldPen);
        DeleteObject(fill);
        DeleteObject(border);

        wchar_t text[128]{};
        GetWindowTextW(item.hwndItem, text, static_cast<int>(std::size(text)));
        SetBkMode(item.hDC, TRANSPARENT);
        SetTextColor(item.hDC, disabled ? RGB(148, 163, 184)
                                       : primary ? RGB(255, 255, 255) : kTextColor);
        HGDIOBJ oldFont = SelectObject(item.hDC, font_);
        DrawTextW(item.hDC, text, -1, &rect, DT_CENTER | DT_VCENTER | DT_SINGLELINE);
        SelectObject(item.hDC, oldFont);
        if ((item.itemState & ODS_FOCUS) != 0) {
            InflateRect(&rect, -4, -4);
            DrawFocusRect(item.hDC, &rect);
        }
        return true;
    }

    void BeginPage(Page page, const std::wstring& title, const std::wstring& instruction) {
        for (HWND control : controls_) DestroyWindow(control);
        controls_.clear();
        primaryButtons_.clear();
        status_ = nullptr;
        instruction_ = nullptr;
        page_ = page;
        HWND heading = AddLabel(title, 32, 26, 620, 42);
        SendMessageW(heading, WM_SETFONT, reinterpret_cast<WPARAM>(titleFont_), TRUE);
        instruction_ = AddLabel(instruction, 32, 82, 620, 100);
    }

    void ShowConsent() {
        BeginPage(Page::Consent, L"安装前确认",
                  L"本程序会安装拼图助手、注册 fxxk-puzzle:// 启动协议，并自动操作所选浏览器的扩展管理页面。安装期间请保持桌面解锁。\r\n\r\n只有同意后才会继续，不同意不会复制文件或修改注册表。");
        AddButton(L"不同意，退出安装", 32, 435, 170, ID_CANCEL);
        AddButton(L"同意并继续", 510, 435, 142, ID_NEXT, BS_DEFPUSHBUTTON);
    }

    void ShowBrowser() {
        BeginPage(Page::Browser, L"选择浏览器", L"请选择需要安装拼图助手扩展的浏览器，可多选。");
        const auto chrome = native::FindBrowser(L"chrome");
        const auto edge = native::FindBrowser(L"edge");
        chromeBox_ = AddControl(L"BUTTON",
            chrome.empty() ? L"Google Chrome（未检测到）" : L"Google Chrome（已检测）",
            BS_AUTOCHECKBOX | WS_TABSTOP, 48, 185, 330, 32, ID_CHROME);
        edgeBox_ = AddControl(L"BUTTON",
            edge.empty() ? L"Microsoft Edge（未检测到）" : L"Microsoft Edge（已检测）",
            BS_AUTOCHECKBOX | WS_TABSTOP, 48, 232, 330, 32, ID_EDGE);
        SendMessageW(chromeBox_, BM_SETCHECK, chromeSelected_ ? BST_CHECKED : BST_UNCHECKED, 0);
        SendMessageW(edgeBox_, BM_SETCHECK, edgeSelected_ ? BST_CHECKED : BST_UNCHECKED, 0);
        AddButton(L"下一步", 532, 435, 120, ID_NEXT, BS_DEFPUSHBUTTON);
    }

    void ShowTarget() {
        BeginPage(Page::Target, L"选择目标网址", L"选择预设，或直接输入需要监测的网址。");
        sufeBox_ = AddControl(L"BUTTON", L"上财", BS_AUTOCHECKBOX | WS_TABSTOP,
                              48, 176, 180, 32, ID_SUFE);
        SendMessageW(sufeBox_, BM_SETCHECK, sufeSelected_ ? BST_CHECKED : BST_UNCHECKED, 0);
        AddLabel(L"目标网址", 48, 224, 180, 28);
        urlEdit_ = AddControl(L"EDIT", targetUrl_, WS_TABSTOP | WS_BORDER | ES_AUTOHSCROLL,
                              48, 256, 604, 34, ID_URL);
        AddButton(L"上一步", 32, 435, 110, ID_BACK);
        AddButton(L"下一步", 532, 435, 120, ID_NEXT, BS_DEFPUSHBUTTON);
        SetFocus(urlEdit_);
    }

    void ShowHotkeys() {
        BeginPage(Page::Hotkeys, L"设置快捷键",
                  L"为三个操作选择互不重复的快捷键。安装后仍可在应用设置中修改。");
        AddLabel(L"框选识别区域", 64, 176, 170, 30);
        AddLabel(L"识别并执行", 64, 228, 170, 30);
        AddLabel(L"取消操作", 64, 280, 170, 30);
        selectCombo_ = AddCombo(242, 172, ID_SELECT_KEY, selectKey_);
        executeCombo_ = AddCombo(242, 224, ID_EXECUTE_KEY, executeKey_);
        cancelCombo_ = AddCombo(242, 276, ID_CANCEL_KEY, cancelKey_);
        AddButton(L"上一步", 32, 435, 110, ID_BACK);
        AddButton(L"下一步", 532, 435, 120, ID_NEXT, BS_DEFPUSHBUTTON);
    }

    HWND AddCombo(int x, int y, int id, const std::wstring& selected) {
        HWND combo = AddControl(L"COMBOBOX", L"", WS_TABSTOP | CBS_DROPDOWNLIST | WS_VSCROLL,
                                x, y, 120, 250, id);
        int selectedIndex = 0;
        for (int number = 1; number <= 12; ++number) {
            const std::wstring key = L"F" + std::to_wstring(number);
            SendMessageW(combo, CB_ADDSTRING, 0, reinterpret_cast<LPARAM>(key.c_str()));
            if (key == selected) selectedIndex = number - 1;
        }
        SendMessageW(combo, CB_SETCURSEL, selectedIndex, 0);
        return combo;
    }

    void ShowSummary() {
        const auto destination = native::LocalAppData() / L"Programs/FxxkPuzzleExtension";
        std::wstring browsers;
        if (chromeSelected_) browsers = L"Chrome";
        if (edgeSelected_) browsers += browsers.empty() ? L"Edge" : L"、Edge";
        std::wstringstream summary;
        summary << L"安装位置：" << destination.wstring() << L"\r\n"
                << L"浏览器：" << browsers << L"\r\n"
                << L"目标网址：" << targetUrl_ << L"\r\n"
                << L"快捷键：框选 " << selectKey_ << L"，执行 " << executeKey_
                << L"，取消 " << cancelKey_ << L"\r\n\r\n"
                << L"安装后将依次打开浏览器并加载 extensions 文件夹。";
        BeginPage(Page::Summary, L"准备安装", L"");
        AddLabel(summary.str(), 32, 82, 620, 300);
        AddButton(L"上一步", 32, 435, 110, ID_BACK);
        AddButton(L"安装", 532, 435, 120, ID_INSTALL, BS_DEFPUSHBUTTON);
    }

    void ShowInstalling() {
        BeginPage(Page::Installing, L"正在安装",
                  L"请不要操作鼠标或键盘，浏览器扩展安装完成前请保持桌面解锁。");
        status_ = AddLabel(L"正在复制应用文件……", 48, 192, 604, 80);
    }

    void ShowComplete() {
        BeginPage(Page::Complete, L"安装完成",
                  L"应用和扩展已经安装完成。\r\n\r\n目标网页打开并出现拼图验证码后，请按 " +
                  selectKey_ + L"，框选完整验证码图片和底部滑轨。");
        AddButton(L"完成", 532, 435, 120, ID_FINISH, BS_DEFPUSHBUTTON);
        SetWindowPos(window_, HWND_TOPMOST, 0, 0, 0, 0,
                     SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW);
        SetForegroundWindow(window_);
        MessageBoxW(window_,
            (L"点击“确定”后将自动打开目标网址，浏览器会覆盖本安装程序。\r\n\r\n"
             L"请记住：网页出现拼图验证码后，按 " + selectKey_ +
             L" 框选完整验证码图片和底部滑轨。").c_str(),
            L"接下来配置识别区域", MB_OK | MB_ICONINFORMATION);
        SetWindowPos(window_, HWND_NOTOPMOST, 0, 0, 0, 0,
                     SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW);
        const std::wstring first = chromeSelected_ ? L"chrome" : L"edge";
        const auto executable = native::FindBrowser(first);
        if (!executable.empty()) native::OpenTargetUrl(executable, targetUrl_);
    }

    static std::wstring Text(HWND control) {
        const int length = GetWindowTextLengthW(control);
        std::wstring value(static_cast<size_t>(length + 1), L'\0');
        GetWindowTextW(control, value.data(), length + 1);
        value.resize(static_cast<size_t>(length));
        return value;
    }

    void HandleCommand(int id, int notification) {
        if (id == ID_CANCEL || id == ID_FINISH) {
            SendMessageW(window_, WM_CLOSE, 0, 0);
            return;
        }
        if (id == ID_SUFE && notification == BN_CLICKED) {
            sufeSelected_ = SendMessageW(sufeBox_, BM_GETCHECK, 0, 0) == BST_CHECKED;
            if (sufeSelected_) SetWindowTextW(urlEdit_, L"https://login.sufe.edu.cn/");
            else if (Text(urlEdit_) == L"https://login.sufe.edu.cn/") SetWindowTextW(urlEdit_, L"");
            return;
        }
        if (id == ID_BACK) {
            if (page_ == Page::Target) ShowBrowser();
            else if (page_ == Page::Hotkeys) ShowTarget();
            else if (page_ == Page::Summary) ShowHotkeys();
            return;
        }
        if (id == ID_INSTALL) {
            StartInstall();
            return;
        }
        if (id != ID_NEXT) return;
        if (page_ == Page::Consent) {
            ShowBrowser();
        } else if (page_ == Page::Browser) {
            chromeSelected_ = SendMessageW(chromeBox_, BM_GETCHECK, 0, 0) == BST_CHECKED;
            edgeSelected_ = SendMessageW(edgeBox_, BM_GETCHECK, 0, 0) == BST_CHECKED;
            if (!chromeSelected_ && !edgeSelected_) {
                MessageBoxW(window_, L"Chrome 和 Edge 至少选择一个。", L"请选择浏览器",
                            MB_OK | MB_ICONWARNING);
                return;
            }
            if ((chromeSelected_ && native::FindBrowser(L"chrome").empty()) ||
                (edgeSelected_ && native::FindBrowser(L"edge").empty())) {
                MessageBoxW(window_, L"未找到已选择的浏览器。", L"浏览器未找到",
                            MB_OK | MB_ICONERROR);
                return;
            }
            ShowTarget();
        } else if (page_ == Page::Target) {
            targetUrl_ = Text(urlEdit_);
            sufeSelected_ = SendMessageW(sufeBox_, BM_GETCHECK, 0, 0) == BST_CHECKED;
            if (!(targetUrl_.starts_with(L"http://") || targetUrl_.starts_with(L"https://")) ||
                targetUrl_.find(L'.') == std::wstring::npos) {
                MessageBoxW(window_, L"请输入以 http:// 或 https:// 开头的完整网址。",
                            L"网址无效", MB_OK | MB_ICONWARNING);
                return;
            }
            ShowHotkeys();
        } else if (page_ == Page::Hotkeys) {
            selectKey_ = Text(selectCombo_);
            executeKey_ = Text(executeCombo_);
            cancelKey_ = Text(cancelCombo_);
            if (selectKey_ == executeKey_ || selectKey_ == cancelKey_ || executeKey_ == cancelKey_) {
                MessageBoxW(window_, L"三个操作必须使用不同的快捷键。", L"快捷键重复",
                            MB_OK | MB_ICONWARNING);
                return;
            }
            ShowSummary();
        }
    }

    void PostProgress(const std::wstring& value) const {
        PostMessageW(window_, WM_INSTALL_PROGRESS, 0,
                     reinterpret_cast<LPARAM>(new std::wstring(value)));
    }

    void StartInstall() {
        if (busy_) return;
        busy_ = true;
        ShowInstalling();
        const bool chrome = chromeSelected_;
        const bool edge = edgeSelected_;
        const std::wstring url = targetUrl_;
        const std::wstring select = selectKey_;
        const std::wstring execute = executeKey_;
        const std::wstring cancel = cancelKey_;
        std::thread([this, chrome, edge, url, select, execute, cancel] {
            auto result = std::make_unique<InstallResult>();
            const HRESULT com = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
            try {
                const fs::path source(native::ModuleDirectory());
                const fs::path destination = native::LocalAppData() / L"Programs/FxxkPuzzleExtension";
                PostProgress(L"正在复制应用文件……");
                native::CopyPayload(source, destination, url, select, execute, cancel);
                PostProgress(L"正在注册 fxxk-puzzle:// 启动协议……");
                native::RegisterProtocol(destination / L"app/Fxxk_Puzzle.exe");
                PostProgress(L"正在注册 Windows 卸载程序……");
                native::RegisterUninstall(destination, destination / L"app/Fxxk_Puzzle.exe");
                auto progress = [this](const std::wstring& text) { PostProgress(text); };
                if (chrome) {
                    native::InstallBrowserExtension(L"chrome", native::FindBrowser(L"chrome"),
                        destination / L"extensions", kChromePoints, progress);
                    result->installed.push_back(L"chrome");
                }
                if (edge) {
                    native::InstallBrowserExtension(L"edge", native::FindBrowser(L"edge"),
                        destination / L"extensions", kEdgePoints, progress);
                    result->installed.push_back(L"edge");
                }
                result->success = true;
            } catch (const std::exception& error) {
                result->message = WideFromUtf8(error.what());
            } catch (...) {
                result->message = L"发生未知安装错误";
            }
            if (SUCCEEDED(com)) CoUninitialize();
            PostMessageW(window_, WM_INSTALL_FINISHED, 0,
                         reinterpret_cast<LPARAM>(result.release()));
        }).detach();
    }

    HINSTANCE instance_ = nullptr;
    HWND window_ = nullptr;
    HFONT font_ = nullptr;
    HFONT titleFont_ = nullptr;
    HBRUSH backgroundBrush_ = nullptr;
    std::vector<HWND> controls_;
    std::vector<HWND> primaryButtons_;
    Page page_ = Page::Consent;
    bool busy_ = false;
    bool chromeSelected_ = false;
    bool edgeSelected_ = false;
    bool sufeSelected_ = false;
    std::wstring targetUrl_;
    std::wstring selectKey_ = L"F8";
    std::wstring executeKey_ = L"F9";
    std::wstring cancelKey_ = L"F2";
    std::vector<std::wstring> installed_;
    HWND chromeBox_ = nullptr;
    HWND edgeBox_ = nullptr;
    HWND sufeBox_ = nullptr;
    HWND urlEdit_ = nullptr;
    HWND selectCombo_ = nullptr;
    HWND executeCombo_ = nullptr;
    HWND cancelCombo_ = nullptr;
    HWND status_ = nullptr;
    HWND instruction_ = nullptr;
};

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

}  // namespace

int WINAPI wWinMain(HINSTANCE instance, HINSTANCE, PWSTR, int) {
    SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);
    INITCOMMONCONTROLSEX controls{sizeof(controls), ICC_STANDARD_CLASSES};
    InitCommonControlsEx(&controls);
    SetupWindow window(instance);
    if (!window.Create()) return 1;
    if (HasArgument(L"--smoke-test")) SetTimer(window.Handle(), 1, 500, nullptr);
    MSG message{};
    while (GetMessageW(&message, nullptr, 0, 0) > 0) {
        TranslateMessage(&message);
        DispatchMessageW(&message);
    }
    return static_cast<int>(message.wParam);
}
