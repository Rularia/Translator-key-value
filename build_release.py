from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent
APP_FILE = ROOT / "app.py"
ICON_FILE = ROOT / "app.ico"
DIST_DIR = ROOT / "dist"
BUILD_DIR = ROOT / "build"
SPEC_FILE = ROOT / "app.spec"


def main() -> int:
    if not APP_FILE.exists():
        print(f"Missing app entry: {APP_FILE}")
        return 1

    pyinstaller_cmd = shutil.which("pyinstaller")
    if pyinstaller_cmd is None:
        print("PyInstaller is not installed in the current environment.")
        print("Install it first with: pip install pyinstaller")
        return 1

    command = [
        pyinstaller_cmd,
        "--noconfirm",
        "--clean",
        "--windowed",
        "--name",
        "TranslatorJsonTool",
        "--paths",
        str(ROOT / "src"),
        "--add-data",
        f"{ROOT / 'api_profiles.example.json'};.",
        str(APP_FILE),
    ]

    if ICON_FILE.exists():
        command.extend(["--icon", str(ICON_FILE)])
    else:
        print(f"Icon not found, building without icon: {ICON_FILE}")

    print("Running:", " ".join(f'"{part}"' if " " in part else part for part in command))
    result = subprocess.run(command, cwd=ROOT)
    if result.returncode != 0:
        return result.returncode

    print()
    print("Build complete.")
    print(f"Output folder: {DIST_DIR}")
    print(f"Build folder: {BUILD_DIR}")
    if SPEC_FILE.exists():
        print(f"Spec file: {SPEC_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
