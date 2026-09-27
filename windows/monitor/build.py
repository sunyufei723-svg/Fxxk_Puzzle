"""Build the Monitor runtime bundle, the native installers, and the portable ZIP."""
import shutil
import subprocess
import sys
from pathlib import Path


HERE = Path(__file__).resolve().parent
BUILD = HERE / "build"
RAW = HERE / "dist" / "_monitor_raw"
RELEASE = HERE / "dist" / "Fxxk_Puzzle-monitor"
NATIVE = HERE / "installer"
NATIVE_BUILD = NATIVE / "build"


def run(*args):
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", *args],
                   check=True, cwd=HERE)


def build_native_installers():
    # build_native.cmd uses relative paths (resource.rc refers to ..\assets\icon.ico),
    # so it must run with installer/ as the working directory.
    subprocess.run(["cmd.exe", "/d", "/c", str(NATIVE / "build_native.cmd")],
                   check=True, cwd=NATIVE)


def main():
    for path in (BUILD, RAW, RELEASE, NATIVE_BUILD):
        if path.exists():
            shutil.rmtree(path)
    RAW.mkdir(parents=True)

    run("--clean", "--distpath", str(RAW), "--workpath", str(BUILD / "bundle"),
        str(HERE / "Fxxk_Puzzle_monitor.spec"))
    build_native_installers()

    RELEASE.mkdir(parents=True)
    # launcher.exe, Fxxk_Puzzle.exe and the shared _internal/ must stay side by side:
    # process.py launches sys.executable's sibling Fxxk_Puzzle.exe.
    for item in (RAW / "Fxxk_Puzzle-monitor").iterdir():
        target = RELEASE / item.name
        if item.is_dir():
            shutil.copytree(item, target)
        else:
            shutil.copy2(item, target)
    shutil.copy2(NATIVE_BUILD / "Setup.exe", RELEASE / "Setup.exe")
    shutil.copy2(NATIVE_BUILD / "Uninstall.exe", RELEASE / "Uninstall.exe")
    shutil.copy2(HERE / "assets" / "icon16.png", RELEASE / "icon16.png")
    shutil.copy2(HERE / "README.md", RELEASE / "README.md")

    shutil.make_archive(str(HERE / "dist" / "Fxxk_Puzzle-monitor-portable"), "zip",
                        root_dir=RELEASE.parent, base_dir=RELEASE.name)
    shutil.rmtree(RAW)
    shutil.rmtree(BUILD)
    shutil.rmtree(NATIVE_BUILD)
    print(f"Built {RELEASE}")


if __name__ == "__main__":
    main()
