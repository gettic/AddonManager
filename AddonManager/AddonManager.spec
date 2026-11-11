# PyInstaller build config: one windowed AddonManager.exe. Run through build.ps1.
# On Linux (the GitHub Actions workflow linux-build.yml) the same spec makes the one-folder
# program dist/AddonManager/AddonManager, which the workflow packs into an AppImage (no
# Windows file properties or file icon there; the window icon comes from the bundled
# am/assets PNGs on both systems).
# The version comes from am\__init__.py (APP_VERSION), so it is kept in one place.
# tos rule: no "World of Warcraft", "WoW", "Warcraft" or "Blizzard" in the names below.
import os
import re
import sys

HERE = os.path.abspath(SPECPATH)
with open(os.path.join(HERE, "am", "__init__.py"), encoding="utf-8") as f:
    src = f.read()
APP_VERSION = re.search(r'APP_VERSION\s*=\s*"([^"]+)"', src).group(1)
APP_NAME = re.search(r'APP_NAME\s*=\s*"([^"]+)"', src).group(1)
nums = tuple(int(n) for n in (APP_VERSION.split(".") + ["0", "0", "0", "0"])[:4])

WINDOWS = sys.platform == "win32"
version_info = None
icon = None
if WINDOWS:
    from PyInstaller.utils.win32.versioninfo import (FixedFileInfo, StringFileInfo,
                                                     StringStruct, StringTable, VarFileInfo,
                                                     VarStruct, VSVersionInfo)

    version_info = VSVersionInfo(
        ffi=FixedFileInfo(filevers=nums, prodvers=nums),
        kids=[
            StringFileInfo([StringTable("040904B0", [
                StringStruct("CompanyName", APP_NAME),
                StringStruct("FileDescription", APP_NAME),
                StringStruct("FileVersion", APP_VERSION),
                StringStruct("InternalName", "AddonManager"),
                StringStruct("OriginalFilename", "AddonManager.exe"),
                StringStruct("ProductName", APP_NAME),
                StringStruct("ProductVersion", APP_VERSION),
            ])]),
            VarFileInfo([VarStruct("Translation", [1033, 1200])]),
        ],
    )
    icon = os.path.join(HERE, "am", "assets", "AddonManager.ico")   # the .exe file's icon

# The window/taskbar icons (am/icon.py), inside the program on both systems.
ASSETS = os.path.join(HERE, "am", "assets")
assets = [(os.path.join(ASSETS, n), os.path.join("am", "assets"))
          for n in ("AddonManager.ico", "AddonManager.png", "AddonManager_48.png",
                    "AddonManager_32.png", "AddonManager_16.png")]
# The window's knotwork corners (am/theme.py): 4 corners x 3 sizes, the app's own art.
assets += [(os.path.join(ASSETS, "frame", f"k6b_tied_{c}_{s}.png"),
            os.path.join("am", "assets", "frame"))
           for c in ("tl", "tr", "bl", "br") for s in ("1x", "1_5x", "2x")]
# The licences of everything bundled (shown by Settings > Licences, am/backup_ui.py).
assets.append((os.path.join(HERE, "THIRD-PARTY-NOTICES.txt"), "."))
for src, _dest in assets + ([(icon, "")] if icon else []):
    if not os.path.isfile(src):
        raise SystemExit(f"Missing file: {src}")

a = Analysis(
    [os.path.join(HERE, "main.py")],
    pathex=[HERE],
    datas=assets,
    excludes=["tests", "unittest", "pydoc", "doctest", "pdb"],
)
# Faster start: the one-file program unpacks every bundled file on each start (antivirus
# checks each one). Tcl's time zone files (600+) are never used, and the Tcl/Tk
# translations only affect Tk's own dialogs (English text there is fine).
_drop = re.compile(r"^(_tcl_data[\\/]tzdata|_tcl_data[\\/]msgs|_tk_data[\\/]msgs)[\\/]")
a.datas = [d for d in a.datas if not _drop.match(d[0])]
pyz = PYZ(a.pure)
if WINDOWS:
    # One file: AddonManager.exe.
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        name="AddonManager",
        console=False,
        upx=False,
        strip=False,
        icon=icon,
        version=version_info,
    )
else:
    # Linux: one folder, dist/AddonManager/ (program AddonManager + _internal/). The
    # workflow packs it into AddonManager-x86_64.AppImage, which is already one file, so
    # nothing is unpacked to /tmp at each start.
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="AddonManager",
        console=False,
        upx=False,
        strip=False,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        name="AddonManager",
        upx=False,
        strip=False,
    )
