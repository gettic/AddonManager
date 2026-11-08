# PyInstaller build config: one windowed AddonManager.exe. Run through build.ps1.
# The version comes from am\__init__.py (APP_VERSION), so it is kept in one place.
# tos rule: no "World of Warcraft", "WoW", "Warcraft" or "Blizzard" in the names below.
import os
import re

from PyInstaller.utils.win32.versioninfo import (FixedFileInfo, StringFileInfo, StringStruct,
                                                 StringTable, VarFileInfo, VarStruct,
                                                 VSVersionInfo)

HERE = os.path.abspath(SPECPATH)
with open(os.path.join(HERE, "am", "__init__.py"), encoding="utf-8") as f:
    src = f.read()
APP_VERSION = re.search(r'APP_VERSION\s*=\s*"([^"]+)"', src).group(1)
APP_NAME = re.search(r'APP_NAME\s*=\s*"([^"]+)"', src).group(1)
nums = tuple(int(n) for n in (APP_VERSION.split(".") + ["0", "0", "0", "0"])[:4])

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

icon = os.path.join(HERE, "assets", "icon.ico")

a = Analysis(
    [os.path.join(HERE, "main.py")],
    pathex=[HERE],
    excludes=["tests", "unittest", "pydoc", "doctest", "pdb"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    name="AddonManager",
    console=False,
    upx=False,
    icon=icon if os.path.isfile(icon) else None,
    version=version_info,
)
