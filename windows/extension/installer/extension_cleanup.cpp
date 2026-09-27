// 独立的浏览器扩展清理程序：卸载器只负责删文件和注册表，把 Chrome/Edge 里指向
// 本程序目录的 unpacked 条目真的移除掉是这里的事。两版卸载器因此可以完全一致，
// extension 版多带这一个可执行文件、多传一个 --extension 参数而已。
//
// Chrome/Edge 没有命令行或外部 API 能卸载扩展，唯一办法是打开扩展页点「移除」。
// 页面上每张卡片的按钮都叫 removeButton，且卡片在 UIA 树里是 itemsList 的**扁平
// 兄弟节点**而非各自容器，所以绝不能"找第一个 removeButton"或"在某子树里找"——
// 那会误删用户其它扩展。这里按顺序遍历兄弟节点，用 extension-id 里的 ID 判定归属。

#include "native_support.hpp"

#pragma comment(lib, "advapi32.lib")
#pragma comment(lib, "ole32.lib")
#pragma comment(lib, "shell32.lib")

namespace native {
namespace cleanup {
struct LoadedExtension {
    std::wstring browser;   // L"chrome" / L"edge"
    fs::path preferences;   // Secure Preferences
    std::wstring id;
    std::string needle;     // 指向本程序扩展目录的路径，判定"还在用"就靠它
};

inline bool IsExtensionId(const std::string& text) {
    if (text.size() != 32) return false;
    for (const char character : text) {
        if (character < 'a' || character > 'p') return false;
    }
    return true;
}

// 在 JSON 原文里定位 path 所属的扩展 ID：从 path 出现处往前找最近的一个
// "<32 个 a-p 字符>":{ —— 那就是承载这条记录的键。
inline std::wstring IdOwningPath(const std::string& text, size_t pathPosition) {
    for (size_t cursor = pathPosition; cursor > 34; --cursor) {
        if (text[cursor] != '"') continue;
        const std::string candidate = text.substr(cursor + 1, 32);
        if (!IsExtensionId(candidate)) continue;
        if (text[cursor + 33] != '"' || text[cursor + 34] != ':') continue;
        if (cursor + 36 >= text.size() || text[cursor + 35] != '{') continue;
        std::wstring wide;
        wide.reserve(candidate.size());
        for (const char character : candidate) wide.push_back(static_cast<wchar_t>(character));
        return wide;
    }
    return {};
}

inline std::string ReadWholeFile(const fs::path& path) {
    std::ifstream stream(path, std::ios::binary);
    if (!stream) return {};
    return std::string((std::istreambuf_iterator<char>(stream)), std::istreambuf_iterator<char>());
}

inline std::vector<fs::path> ProfileDirectories(const fs::path& userData) {
    std::vector<fs::path> found;
    std::error_code ignored;
    if (!fs::is_directory(userData, ignored)) return found;
    for (const auto& entry : fs::directory_iterator(userData, ignored)) {
        if (!entry.is_directory(ignored)) continue;
        const std::wstring name = entry.path().filename().wstring();
        if (name == L"Default" || name.rfind(L"Profile ", 0) == 0) found.push_back(entry.path());
    }
    return found;
}

// Secure Preferences 把路径写成 JSON 字符串：反斜杠成对出现，大小写跟提交时一致。
// 两边都归一到「小写 + 单分隔符」再比，避免正/反斜杠和大小写造成漏检。
inline std::string CanonicalPath(const fs::path& path) {
    std::string key = Utf8(path.wstring());
    for (char& character : key) {
        if (character == '/') character = '\\';
        character = static_cast<char>(std::tolower(static_cast<unsigned char>(character)));
    }
    return key;
}

inline std::string CanonicalJson(const std::string& text) {
    std::string out;
    out.reserve(text.size());
    for (size_t i = 0; i < text.size(); ++i) {
        const char character = text[i];
        if (character == '\\') {
            if (i + 1 < text.size() && text[i + 1] == '\\') ++i;
            out += '\\';
        } else if (character == '/') {
            out += '\\';
        } else {
            out += static_cast<char>(std::tolower(static_cast<unsigned char>(character)));
        }
    }
    return out;
}

inline std::vector<LoadedExtension> FindLoadedExtensions(const fs::path& extensionsDir) {
    std::vector<LoadedExtension> found;
    const std::string needle = CanonicalPath(extensionsDir);
    const fs::path localAppData = LocalAppData();
    const std::vector<std::pair<std::wstring, fs::path>> roots{
        {L"chrome", localAppData / L"Google" / L"Chrome" / L"User Data"},
        {L"edge", localAppData / L"Microsoft" / L"Edge" / L"User Data"}};
    for (const auto& [browser, userData] : roots) {
        const fs::path executable = FindBrowser(browser);
        if (executable.empty()) continue;
        for (const auto& profile : ProfileDirectories(userData)) {
            const fs::path preferences = profile / L"Secure Preferences";
            const std::string text = CanonicalJson(ReadWholeFile(preferences));
            if (text.empty()) continue;
            size_t from = 0;
            std::vector<std::wstring> ids;
            while ((from = text.find(needle, from)) != std::string::npos) {
                const std::wstring id = IdOwningPath(text, from);
                if (!id.empty() &&
                    std::find(ids.begin(), ids.end(), id) == ids.end()) {
                    ids.push_back(id);
                    found.push_back({browser, preferences, id, needle});
                }
                from += needle.size();
            }
        }
    }
    return found;
}

// 判定"浏览器还在用这个目录"。只看那条扩展记录会出错：ID 在文件里还会出现在别的
// 小节，往后找到的 "path" 可能是别的扩展的。用当初认出它的那段路径本身来判定，
// 和 FindLoadedExtensions 完全对称。
inline bool StillPointsAt(const LoadedExtension& item) {
    return CanonicalJson(ReadWholeFile(item.preferences)).find(item.needle) != std::string::npos;
}

// 卡片的 extension-id 组里才有 ID 文本，父节点 Name 常为空；只搜裸 ID 子串，
// 不去匹配 "ID："/"ID: " 这类随界面语言变化的前缀。
inline bool GroupHoldsExtensionId(IUIAutomationTreeWalker* walker, IUIAutomationElement* group,
                                  const std::wstring& id, int depth = 0) {
    if (!group || depth > 6) return false;
    if (ElementName(group).find(id) != std::wstring::npos) return true;
    ComPtr<IUIAutomationElement> child;
    walker->GetFirstChildElement(group, &child);
    while (child) {
        if (GroupHoldsExtensionId(walker, child.Get(), id, depth + 1)) return true;
        ComPtr<IUIAutomationElement> next;
        walker->GetNextSiblingElement(child.Get(), &next);
        child = next;
    }
    return false;
}

// Chrome 的扩展卡片在 UIA 树里是 itemsList 的**扁平兄弟节点**，不是各自一个容器，
// 所以只能按顺序扫，用最近一次看到的 name / extension-id 判断当前节点属于哪张卡。
// 直接拿第一个 removeButton 会误删用户其它扩展。
inline ComPtr<IUIAutomationElement> ChromeCardButton(IUIAutomation* automation,
                                                     IUIAutomationElement* root,
                                                     const std::wstring& id) {
    auto list = FindByAutomationId(automation, root, L"itemsList");
    if (!list) return nullptr;
    auto walker = RawWalker(automation);
    if (!walker) return nullptr;
    ComPtr<IUIAutomationElement> child;
    walker->GetFirstChildElement(list.Get(), &child);
    bool ours = false;
    ComPtr<IUIAutomationElement> remove;
    while (child) {
        const std::wstring aid = ElementAutomationId(child.Get());
        if (aid == L"name") {
            ours = false;                       // 新卡片开始，先假定不是我们的
        } else if (aid == L"extension-id") {
            ours = GroupHoldsExtensionId(walker.Get(), child.Get(), id);
        } else if (aid == L"removeButton" && ours && !remove) {
            remove = CloneElement(child.Get());
        }
        ComPtr<IUIAutomationElement> next;
        walker->GetNextSiblingElement(child.Get(), &next);
        child = next;
    }
    return remove;
}

// 在子树里找名字命中、且**整棵子树里只有一个**的按钮；多于一个就放弃，避免在
// 卡片容器判错时点到别的扩展的按钮。
inline ComPtr<IUIAutomationElement> UniqueNamedButton(IUIAutomation* automation,
                                                      IUIAutomationElement* scope,
                                                      const std::vector<std::wstring>& names) {
    auto walker = RawWalker(automation);
    ComPtr<IUIAutomationElement> found;
    int hits = 0;
    int budget = 1200;
    WalkRaw(walker.Get(), scope, 0, budget, [&](IUIAutomationElement* element, int) {
        if (ElementControlType(element) != UIA_ButtonControlTypeId) return false;
        const std::wstring name = ElementName(element);
        for (const auto& wanted : names) {
            if (name != wanted) continue;
            ++hits;
            if (!found) found = CloneElement(element);
            return hits > 1;
        }
        return false;
    });
    return hits == 1 ? found : nullptr;
}

// Edge 的扩展页没有 Chrome 那套稳定 AutomationId（实测整页只有 dev-switch 之类），
// 但每张卡片是一个容器，它的 Name 拼了整段卡片文本、里面带 "ID: <id>"。所以先按
// Name 含本程序 ID 找节点，再往上找第一个"子树里恰好只有一个删除按钮"的祖先。
inline ComPtr<IUIAutomationElement> EdgeCardButton(IUIAutomation* automation,
                                                   IUIAutomationElement* root,
                                                   const std::wstring& id) {
    struct Hit { ComPtr<IUIAutomationElement> element; int depth; };
    auto walker = RawWalker(automation);
    std::vector<Hit> hits;
    int budget = 6000;
    WalkRaw(walker.Get(), root, 0, budget, [&](IUIAutomationElement* element, int depth) {
        if (ElementName(element).find(id) == std::wstring::npos) return false;
        hits.push_back({CloneElement(element), depth});
        return false;
    });
    std::sort(hits.begin(), hits.end(), [](const Hit& left, const Hit& right) {
        return left.depth > right.depth;
    });
    const std::vector<std::wstring> labels{L"删除", L"移除", L"Remove", L"Delete"};
    for (const auto& hit : hits) {
        ComPtr<IUIAutomationElement> node = hit.element;
        for (int up = 0; up < 6 && node; ++up) {
            if (auto button = UniqueNamedButton(automation, node.Get(), labels)) return button;
            ComPtr<IUIAutomationElement> parent;
            walker->GetParentElement(node.Get(), &parent);
            node = parent;
        }
    }
    return nullptr;
}

// Chrome 在检测到无障碍客户端后才异步构建可访问性树，刚打开页面时第一次遍历
// 经常是空的，所以一律带轮询。两种页面结构都试：Chrome 的扁平卡片 / Edge 的容器卡片。
inline ComPtr<IUIAutomationElement> FindRemoveButtonForId(IUIAutomation* automation,
                                                          IUIAutomationElement* root,
                                                          const std::wstring& id,
                                                          DWORD timeoutMs) {
    const auto deadline = GetTickCount64() + timeoutMs;
    do {
        auto button = ChromeCardButton(automation, root, id);
        if (!button) button = EdgeCardButton(automation, root, id);
        if (button) return button;
        std::this_thread::sleep_for(std::chrono::milliseconds(400));
    } while (GetTickCount64() < deadline);
    return nullptr;
}

// 只在找不到移除按钮时才开开发者模式：不去动用户本来关着的开关。
// Chrome 的开关叫 devMode，Edge 的叫 dev-switch。
inline bool EnableDeveloperMode(IUIAutomation* automation, IUIAutomationElement* root) {
    auto walker = RawWalker(automation);
    ComPtr<IUIAutomationElement> toggle;
    int budget = 6000;
    WalkRaw(walker.Get(), root, 0, budget, [&](IUIAutomationElement* element, int) {
        const std::wstring aid = ElementAutomationId(element);
        if (aid != L"devMode" && aid != L"dev-switch") return false;
        toggle = CloneElement(element);
        return true;
    });
    if (!toggle) return false;
    ComPtr<IUIAutomationTogglePattern> pattern;
    if (FAILED(toggle->GetCurrentPatternAs(UIA_TogglePatternId,
            __uuidof(IUIAutomationTogglePattern), &pattern)) || !pattern) return false;
    ToggleState state = ToggleState_Off;
    pattern->get_CurrentToggleState(&state);
    if (state == ToggleState_On) return true;
    return SUCCEEDED(pattern->Toggle());
}

// 确认气泡是浏览器自己的 Views 窗口，卡片上的网页按钮 ClassName 是空的，只有
// Views 按钮才叫 MdTextButton，所以按 ClassName 过滤就不会误点到卡片按钮。
inline ComPtr<IUIAutomationElement> FindDialogButton(IUIAutomation* automation,
                                                     IUIAutomationElement* root,
                                                     const std::vector<std::wstring>& names) {
    auto walker = RawWalker(automation);
    ComPtr<IUIAutomationElement> found;
    int budget = 6000;
    WalkRaw(walker.Get(), root, 0, budget, [&](IUIAutomationElement* element, int) {
        if (ElementControlType(element) != UIA_ButtonControlTypeId ||
            ElementClassName(element) != L"MdTextButton") return false;
        const std::wstring name = ElementName(element);
        for (const auto& wanted : names) {
            if (name == wanted) {
                found = CloneElement(element);
                return true;
            }
        }
        return false;
    });
    return found;
}

// 点掉确认气泡里的「删除」。Chrome 的按钮叫「移除」，Edge 的叫「删除」。
//
// 气泡是浏览器自己的 Views 窗口：实测 UIA Invoke 返回 S_OK 却什么都不做，而按
// UIA 给的矩形做鼠标点击会点空——那个矩形是 96dpi 逻辑坐标，鼠标要的是物理坐标，
// 125% 缩放的屏幕上会偏掉一整档。所以这里用 SetFocus + 回车，实测能真的删掉。
inline bool ConfirmRemovalDialog(IUIAutomation* automation, IUIAutomationElement* root) {
    const auto deadline = GetTickCount64() + 8000;
    do {
        auto button = FindDialogButton(automation, root,
                                       {L"移除", L"删除", L"Remove", L"Delete"});
        if (button && SUCCEEDED(button->SetFocus())) {
            SendVirtualKey(VK_RETURN, true);
            SendVirtualKey(VK_RETURN, false);
            return true;
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(250));
    } while (GetTickCount64() < deadline);
    return false;
}

// 移除有没有真的生效。唯一可靠依据是 Secure Preferences 里不再出现本程序的目录路径
// ——实测点完确认大约 10~20 秒才落盘，所以给足时间。页面上的卡片在"已移除＋撤销"
// 提示期间还会留着，不能当依据。
inline bool ExtensionRemoved(const LoadedExtension& item) {
    const auto deadline = GetTickCount64() + 45000;
    do {
        if (!StillPointsAt(item)) return true;
        std::this_thread::sleep_for(std::chrono::milliseconds(500));
    } while (GetTickCount64() < deadline);
    return false;
}

// Chromium 要先看到无障碍客户端才会构建渲染端的可访问性树，而且只查顶层窗口有时
// 不够：实测 edge://extensions 在纯顶层 UIA 查询下只给一棵没有内容的空壳树，先向
// 渲染进程的子窗口要一次 UIA 根、再整棵走一遍，节点才会全出来。

inline void RemoveLoadedExtensions(std::vector<LoadedExtension>& loaded,
                                   const std::function<void(const std::wstring&)>& progress,
                                   std::vector<LoadedExtension>& failed) {
    const ComScope com;
    ComPtr<IUIAutomation> automation;
    if (FAILED(CoCreateInstance(CLSID_CUIAutomation, nullptr, CLSCTX_INPROC_SERVER,
                                IID_PPV_ARGS(&automation)))) {
        for (auto& item : loaded) failed.push_back(item);
        return;
    }
    for (auto& item : loaded) {
        progress(L"正在从 " + item.browser + L" 移除扩展……");
        const fs::path executable = FindBrowser(item.browser);
        if (executable.empty()) {
            failed.push_back(item);
            continue;
        }
        bool removed = false;
        try {
            const HWND window = OpenExtensionPage(item.browser, executable);
            WakeUpPageAccessibility(automation.Get(), window);
            ComPtr<IUIAutomationElement> root;
            automation->ElementFromHandle(window, &root);
            if (!root) {
                progress(L"读不到 " + item.browser + L" 扩展页面的可访问性树");
            } else {
                auto button = FindRemoveButtonForId(automation.Get(), root.Get(), item.id, 8000);
                if (!button) {
                    // 没找到移除按钮才开开发者模式：不去动用户本来关着的开关。
                    progress(L"正在尝试打开 " + item.browser + L" 的开发者模式……");
                    if (EnableDeveloperMode(automation.Get(), root.Get())) {
                        std::this_thread::sleep_for(std::chrono::milliseconds(700));
                        automation->ElementFromHandle(window, &root);
                        button = FindRemoveButtonForId(automation.Get(), root.Get(), item.id, 10000);
                    }
                }
                if (!button) {
                    progress(L"没能在 " + item.browser + L" 的扩展列表里定位到本程序的移除按钮");
                } else if (!ActivateUiaElement(button.Get(), false)) {
                    progress(L"点击 " + item.browser + L" 的移除按钮没有生效");
                } else if (!ConfirmRemovalDialog(automation.Get(), root.Get())) {
                    progress(L"没能点掉 " + item.browser + L" 的移除确认气泡");
                } else if (ExtensionRemoved(item)) {
                    removed = true;
                } else {
                    progress(L"点完确认后，" + item.browser +
                             L" 的扩展列表里仍然能看到本程序");
                }
            }
        } catch (const std::exception& error) {
            progress(L"移除 " + item.browser + L" 时出错：" + WideFromUtf8(error.what()));
        } catch (...) {
            progress(L"移除 " + item.browser + L" 时发生未知错误");
        }
        if (removed) {
            progress(L"已从 " + item.browser + L" 移除扩展");
        } else {
            failed.push_back(item);
        }
    }
}

}  // namespace cleanup
}  // namespace native

namespace {

void Report(const std::wstring& text) {
    wprintf(L"%s\n", text.c_str());
}

int FailedBit(const std::wstring& browser) {
    return browser == L"chrome" ? native::kCleanupChrome : native::kCleanupEdge;
}

// 卸载器的弹窗会说"扩展管理页面已经打开"，所以这句话得由这里兑现。
void OpenPagesFor(int status) {
    if (status & native::kCleanupChrome) {
        try { native::OpenExtensionPageForBrowser(L"chrome"); } catch (...) {}
    }
    if (status & native::kCleanupEdge) {
        try { native::OpenExtensionPageForBrowser(L"edge"); } catch (...) {}
    }
}

}  // namespace

int wmain(int argc, wchar_t** argv) {
    if (native::HasArgument(L"--smoke-test")) return 0;
    if (argc < 2) {
        wprintf(L"用法：Extension_Cleanup.exe <extensions 目录>\n");
        return native::kCleanupBroken;
    }
    const std::filesystem::path extensionsDir(argv[1]);
    native::ComScope com;
    int status = native::kCleanupClean;
    try {
        auto loaded = native::cleanup::FindLoadedExtensions(extensionsDir);
        if (loaded.empty()) {
            Report(L"没有在 Chrome/Edge 里找到指向该目录的扩展条目");
            return native::kCleanupNotFound;
        }
        std::vector<native::cleanup::LoadedExtension> failed;
        native::cleanup::RemoveLoadedExtensions(loaded, Report, failed);
        for (const auto& item : failed) status |= FailedBit(item.browser);
        Report(status ? L"仍有浏览器留着条目" : L"浏览器条目已全部移除");
    } catch (const std::exception& error) {
        Report(L"清理失败：" + native::WideFromUtf8(error.what()));
        status = native::kCleanupChrome | native::kCleanupEdge;
    }
    OpenPagesFor(status);
    return status;
}
