"""The app's icon for its windows and the taskbar (Windows and Linux).

The files live in am\\assets (bundled by AddonManager.spec, so the same path works from
source and from the built program). A missing or broken icon never stops the app: the
window then just has Tk's default icon.
"""

import logging
import os

log = logging.getLogger(__name__)

ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
ICO = os.path.join(ASSETS, "AddonManager.ico")
# Biggest first: Tk passes them all to the system, which picks the best size.
PNGS = [os.path.join(ASSETS, n) for n in
        ("AddonManager.png", "AddonManager_48.png", "AddonManager_32.png", "AddonManager_16.png")]
# Windows groups taskbar buttons by this ID and shows this app's own icon for them
# (otherwise a script run shows Python's). No game or company names (tos rule).
APP_ID = "GuildAddonManager.AddonManager"


def set_app_id():
    """Windows only, before the first window: the app's own taskbar identity."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
        return True
    except Exception as e:
        log.info("App ID not set: %s", e)
        return False


def apply(root):
    """Set the icon of root and every window opened later. Returns the loaded images,
    also kept on root (Tk drops an image whose Python object is freed). Never raises."""
    import tkinter as tk
    images = []
    try:
        if os.name == "nt" and os.path.isfile(ICO):
            root.iconbitmap(default=ICO)   # sharp small sizes in title bars, Alt+Tab
        images = [tk.PhotoImage(master=root, file=p) for p in PNGS if os.path.isfile(p)]
        if images:
            root.iconphoto(True, *images)
    except Exception as e:   # tk.TclError, a damaged file...
        log.warning("Window icon not set: %s", e)
    root._am_icons = images
    return images
