"""Dark look for the window: ttk 'clam' styles + a dark Windows title bar."""

import ctypes
import logging
import sys
from tkinter import ttk

log = logging.getLogger(__name__)

BG = "#121212"          # window (near black)
SURFACE = "#1f1f1f"     # tooltip, progress trough
BORDER = "#3a3a3a"
TEXT = "#e8e8e8"
MUTED = "#9a9a9a"       # version, headers
BUTTON = "#2b2b2b"
BUTTON_HOVER = "#383838"
BUTTON_PRESSED = "#1a1a1a"
DISABLED = "#5e5e5e"
ACCENT = "#c62828"      # red: main button + progress bar (white text 5.6:1)
ACCENT_HOVER = "#d32f2f"
ACCENT_TEXT = "#ffffff"
BANNER_BG = "#3d0f0f"   # "WoW is running" note (dark red)
BANNER_TEXT = "#ffb3b3"
ERROR_BG = "#c62828"    # failed: white bold text on a red block
ERROR_TEXT = "#ffffff"
STATUS = {"update": "#ff5252",        # bright red: needs action
          "not_installed": "#d0d0d0", # light grey
          "up_to_date": "#7a7a7a"}    # dim grey: nothing to do


def apply(root):
    """Style every ttk widget dark. Call once, before building widgets."""
    root.configure(background=BG)
    s = ttk.Style(root)
    s.theme_use("clam")   # 'vista' ignores colours
    s.configure(".", background=BG, foreground=TEXT, fieldbackground=SURFACE,
                bordercolor=BORDER, lightcolor=BG, darkcolor=BG,
                troughcolor=SURFACE, focuscolor=ACCENT, font=("Segoe UI", 9))
    s.configure("TFrame", background=BG)
    s.configure("TLabel", background=BG, foreground=TEXT)
    s.configure("Muted.TLabel", foreground=MUTED)
    s.configure("Failed.TLabel", background=ERROR_BG, foreground=ERROR_TEXT,
                font=("Segoe UI", 9, "bold"), padding=(6, 1))
    s.configure("TLabelframe", background=BG, bordercolor=BORDER,
                lightcolor=BORDER, darkcolor=BORDER)
    s.configure("TLabelframe.Label", background=BG, foreground=MUTED)
    s.configure("TButton", background=BUTTON, foreground=TEXT, bordercolor=BORDER,
                lightcolor=BUTTON, darkcolor=BUTTON, padding=(10, 4))
    s.map("TButton",
          background=[("disabled", BG), ("pressed", BUTTON_PRESSED), ("active", BUTTON_HOVER)],
          foreground=[("disabled", DISABLED)],
          lightcolor=[("pressed", BUTTON_PRESSED), ("active", BUTTON_HOVER)],
          darkcolor=[("pressed", BUTTON_PRESSED), ("active", BUTTON_HOVER)])
    s.configure("Accent.TButton", background=ACCENT, foreground=ACCENT_TEXT,
                lightcolor=ACCENT, darkcolor=ACCENT, bordercolor=ACCENT)
    s.map("Accent.TButton",
          background=[("disabled", BG), ("pressed", ACCENT), ("active", ACCENT_HOVER)],
          foreground=[("disabled", DISABLED)],
          bordercolor=[("disabled", BORDER)],
          lightcolor=[("disabled", BG), ("active", ACCENT_HOVER)],
          darkcolor=[("disabled", BG), ("active", ACCENT_HOVER)])
    s.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor=SURFACE,
                bordercolor=BORDER, lightcolor=ACCENT, darkcolor=ACCENT)


def dark_title_bar(root):
    """Ask Windows 10 (20H1+) / 11 for a dark title bar. Harmless if it fails."""
    if sys.platform != "win32":
        return
    try:
        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        on = ctypes.c_int(1)
        for attr in (20, 19):   # DWMWA_USE_IMMERSIVE_DARK_MODE (new id, then old)
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attr, ctypes.byref(on), ctypes.sizeof(on)) == 0:
                break
        colour = ctypes.c_int(0x00121212)   # DWMWA_CAPTION_COLOR (Win11), BGR of BG
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(colour),
                                                   ctypes.sizeof(colour))
    except Exception as e:   # older Windows: stays light, app still works
        log.info("Dark title bar not available: %s", e)
