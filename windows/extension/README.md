# Windows Extension 版分发流程

Extension 版通过浏览器扩展检测目标网址，再用 `fxxk-puzzle://` 协议启动本地拼图程序。`Setup.exe` 完成整个安装流程，不要求用户手工注册协议、配置网址或加载扩展。

## 用户安装

1. 解压完整的发布包并运行 `Setup.exe`。
2. 阅读安装说明并明确同意，然后选择 Chrome、Edge 或两者。
3. 在目标网址页面勾选“上财”，文本框将填入 `https://login.sufe.edu.cn/`；也可以手工修改。
4. 为“框选识别区域”“识别并执行”“取消操作”选择互不重复的 F1–F12 快捷键。
5. 点击安装。原生安装器会把应用放到 `%LOCALAPPDATA%\Programs\FxxkPuzzleExtension`，注册当前用户的 `fxxk-puzzle://` 协议，然后依次打开所选浏览器并加载安装目录中的 `extensions\`。浏览器操作优先使用 Windows UI Automation 的语义接口；接口不可用时才使用作者校准坐标。
6. 自动加载期间请保持桌面解锁，不要操作鼠标或键盘。如果浏览器界面与作者校准环境不一致，安装器会停止并提示联系作者，不会继续盲点。
7. 安装完成后，安装器会先在前台说明识别区域的配置方法；确认后才自动打开目标网址。网页出现拼图验证码时按自定义的框选快捷键，框选完整验证码图片和底部滑轨。

安装完成后可运行安装目录中的 `Uninstall.exe`，也可以在 Windows 设置的“已安装的应用”中卸载。卸载程序会删除 Extension 版应用、扩展文件和 `fxxk-puzzle://` 协议注册。

## 作者构建

```powershell
cd windows/extension
python -m pip install -r requirements.txt
python build.py
```

构建机还需安装 Visual Studio 2022 Build Tools 的 C++ 桌面工具集。产物为 `dist/Fxxk_Puzzle-extension-portable.zip`。`build.py` 会生成 Extension 版应用、原生安装向导、原生卸载程序、`extensions\` 和使用说明。`calibrate.py` 只供作者在构建前运行；先后执行 `python calibrate.py chrome` 和 `python calibrate.py edge`，所得最大化窗口坐标会编译进 `Setup.exe`，校准脚本、UIA 探针和坐标 JSON 都不会进入分发目录。
