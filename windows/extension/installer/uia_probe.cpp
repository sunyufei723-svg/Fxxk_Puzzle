#include "native_support.hpp"

#include <iostream>

#pragma comment(lib, "ole32.lib")
#pragma comment(lib, "oleaut32.lib")
#pragma comment(lib, "shell32.lib")
#pragma comment(lib, "uiautomationcore.lib")

namespace {

void DumpNamedElements(IUIAutomation* automation, IUIAutomationElement* root) {
    Microsoft::WRL::ComPtr<IUIAutomationCondition> condition;
    if (FAILED(automation->CreateTrueCondition(&condition))) return;
    Microsoft::WRL::ComPtr<IUIAutomationElementArray> elements;
    if (FAILED(root->FindAll(TreeScope_Subtree, condition.Get(), &elements)) || !elements) return;
    int length = 0;
    elements->get_Length(&length);
    std::wcerr << L"UIA named elements: " << length << L"\n";
    for (int index = 0; index < length; ++index) {
        Microsoft::WRL::ComPtr<IUIAutomationElement> element;
        if (FAILED(elements->GetElement(index, &element)) || !element) continue;
        BSTR name = nullptr;
        BSTR automationId = nullptr;
        BSTR controlType = nullptr;
        element->get_CurrentName(&name);
        element->get_CurrentAutomationId(&automationId);
        element->get_CurrentLocalizedControlType(&controlType);
        if ((name && *name) || (automationId && *automationId)) {
            std::wcerr << L"[" << index << L"] name=" << (name ? name : L"")
                       << L" | id=" << (automationId ? automationId : L"")
                       << L" | type=" << (controlType ? controlType : L"") << L"\n";
        }
        SysFreeString(name);
        SysFreeString(automationId);
        SysFreeString(controlType);
    }
}

}  // namespace

int wmain(int argc, wchar_t** argv) {
    if (argc != 2 || (wcscmp(argv[1], L"chrome") != 0 && wcscmp(argv[1], L"edge") != 0)) {
        std::wcerr << L"usage: UiaProbe.exe chrome|edge\n";
        return 2;
    }
    const std::wstring browser = argv[1];
    const auto executable = native::FindBrowser(browser);
    if (executable.empty()) {
        std::wcerr << L"browser not found: " << browser << L"\n";
        return 3;
    }
    const HRESULT initialized = CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED);
    try {
        const HWND window = native::OpenExtensionPage(browser, executable);
        Microsoft::WRL::ComPtr<IUIAutomation> automation;
        native::Check(SUCCEEDED(CoCreateInstance(CLSID_CUIAutomation, nullptr,
            CLSCTX_INPROC_SERVER, IID_PPV_ARGS(&automation))), L"cannot initialize UI Automation");
        Microsoft::WRL::ComPtr<IUIAutomationElement> root;
        native::Check(SUCCEEDED(automation->ElementFromHandle(window, &root)) && root,
                      L"cannot read browser UI Automation tree");
        const std::vector<std::wstring> developerNames{
            L"开发者模式", L"开发人员模式", L"Developer mode"};
        const std::vector<std::wstring> loadNames{
            L"加载未打包的扩展", L"加载未打包的扩展程序", L"加载已解压的扩展程序",
            L"加载解压缩的扩展", L"加载解压缩扩展", L"Load unpacked"};
        auto developer = native::FindNamedElement(automation.Get(), root.Get(), developerNames, 5000);
        if (!developer) DumpNamedElements(automation.Get(), root.Get());
        native::Check(developer != nullptr, L"developer mode element not found");
        auto load = native::FindNamedElement(automation.Get(), root.Get(), loadNames, 1200);
        const bool changed = !load;
        if (changed) {
            native::Check(native::ActivateUiaElement(developer.Get(), true),
                          L"developer mode has no actionable UIA pattern");
            std::this_thread::sleep_for(std::chrono::milliseconds(1000));
            load = native::FindNamedElement(automation.Get(), root.Get(), loadNames, 5000);
        }
        Microsoft::WRL::ComPtr<IUIAutomationInvokePattern> invoke;
        Microsoft::WRL::ComPtr<IUIAutomationLegacyIAccessiblePattern> legacy;
        const bool hasInvoke = load && SUCCEEDED(load->GetCurrentPatternAs(UIA_InvokePatternId,
            __uuidof(IUIAutomationInvokePattern), &invoke)) && invoke;
        const bool hasLegacy = load && SUCCEEDED(load->GetCurrentPatternAs(
            UIA_LegacyIAccessiblePatternId, __uuidof(IUIAutomationLegacyIAccessiblePattern),
            &legacy)) && legacy;
        if (changed) native::ActivateUiaElement(developer.Get(), true);
        native::Check(load != nullptr, L"load unpacked element not found");
        native::Check(hasInvoke || hasLegacy, L"load unpacked element has no actionable UIA pattern");
        std::wcout << browser << L": developer UIA action=ok, load UIA action=ok ("
                   << (hasInvoke ? L"InvokePattern" : L"LegacyIAccessiblePattern") << L")\n";
        if (SUCCEEDED(initialized)) CoUninitialize();
        return 0;
    } catch (const std::exception& error) {
        std::wcerr << browser << L": " << native::WideFromUtf8(error.what()) << L"\n";
        if (SUCCEEDED(initialized)) CoUninitialize();
        return 1;
    }
}
