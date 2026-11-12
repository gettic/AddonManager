"""The look: Dark Gold Classic (dark window, antique-gold lines and text, red main
buttons) with knotwork corners. ttk 'clam' styles, the window frame, a dark title bar.

Every colour lives here. Pixel sizes go through px(), so 150 % display scaling gets
1.5x paddings, wrap widths and art like it gets 1.5x fonts. All art is the app's own
(am\\assets\\frame: corner knots drawn by the project's own script; the logo is the
guild's crest). No fonts are shipped: headings use a serif font the system has."""

import ctypes
import logging
import math
import os
import sys
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

log = logging.getLogger(__name__)

# ---------- colours ----------
BG = "#100e0b"          # window
PANEL = "#0b0a08"       # inside the list box
SURFACE = "#1a1712"     # tooltip, menus, licence text
FIELD = "#151310"       # folder field, tick box, progress trough
BORDER = "#7a6230"      # button and box borders (gold)
LINE = "#5e4a22"        # thin gold rules: banner, header row, box labels
ROW_LINE = "#2a2416"    # between list rows
FRAME_OUTER = "#7a6230"  # the window's double line
FRAME_INNER = "#4a3a18"
TEXT = "#ede6d6"
NAME = "#f7efdc"        # addon names (bold)
MUTED = "#9a917e"       # notes, version (6.2:1 on BG)
TITLE = "#d8b86a"       # banner title, window headings
HEAD = "#c9a65a"        # box labels, list headers, "Other addons"
BUTTON = "#231f17"
BUTTON_TEXT = "#e2c98a"  # 10:1 on BUTTON
BUTTON_HOVER = "#2f2819"
BUTTON_PRESSED = "#17140f"
DISABLED = "#7a705e"    # greyed text (3.9:1: clearly greyed, still readable)
UNINSTALL_TEXT = "#cdbb8c"   # Uninstall: a bit dimmer than BUTTON_TEXT
ACCENT = "#96201a"      # red main buttons (ACCENT_TEXT 6.9:1)
ACCENT_HOVER = "#b3261e"
ACCENT_TEXT = "#ffe9a8"
ACCENT_BORDER = "#c9a65a"
FOCUS = "#f2c94c"       # keyboard focus: bright gold border / ring
LINK = "#f2c94c"
HIGHLIGHT = "#1f1a0f"   # list row with an update
UPDATE_BG = "#1c1810"   # the app's own update bar
BANNER_BG = "#3d0f0f"   # "WoW is running" note (dark red)
BANNER_TEXT = "#ffb3b3"
ERROR_BG = "#b3261e"    # failed: white bold text on a red block
ERROR_TEXT = "#ffffff"
STATUS = {"update": "#f2c94c",        # gold: needs action
          "not_installed": "#b8ae98", # light grey
          "up_to_date": "#7cc96b"}    # green (9.8:1 on PANEL)

# ---------- fonts ----------
# Body: Segoe UI 9 (Windows) / Noto Sans 10 (Linux; Tk falls back if missing). Buttons:
# Segoe UI Semibold on Windows. Headings: the first serif font the system has.
FONT_FAMILY = "Segoe UI" if sys.platform == "win32" else "Noto Sans"
FONT_SIZE = 9 if sys.platform == "win32" else 10
BUTTON_FAMILY = "Segoe UI Semibold" if sys.platform == "win32" else None   # None = body
HEAD_CHOICES = (("Georgia", "Cambria", "Constantia") if sys.platform == "win32"
                else ("Noto Serif", "DejaVu Serif", "Liberation Serif"))
HEAD_FAMILY = None   # picked by apply() from the installed families

# ---------- art ----------
ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
FRAME_ART = os.path.join(ASSETS, "frame")
CORNER_NAME = "k6b_tied_{corner}_{step}.png"
CORNERS = ("tl", "tr", "bl", "br")
KNOT_INK = 29        # at 1x the knot's drawn part is inside 29 x 29 px of the 48 px piece
CORNER_OFFSET = 3    # px from the window edge (1x)

# Same-kind buttons: at least as wide as the widest of these (row buttons, Change...,
# Add file..., and every dialog's buttons), measured in the button font.
BUTTON_MIN_TEXTS = ("Change...", "Add file...", "Install", "Update", "Reinstall", "Uninstall")

SCALE = 1.0          # display scaling (1.0 = 100 %), set by apply()


def font(*styles, bigger=0):
    """A font tuple in the app's family, e.g. font("bold"), font("bold", bigger=1)."""
    return (FONT_FAMILY, FONT_SIZE + bigger) + styles


def head_font(bigger=0):
    """The heading font (serif, bold)."""
    return (HEAD_FAMILY or FONT_FAMILY, FONT_SIZE + bigger, "bold")


def button_font():
    return (BUTTON_FAMILY or FONT_FAMILY, FONT_SIZE)


def scale_of(widget):
    """Display scaling from Tk's pixels per point (96 dpi = 1.0); never below 1."""
    try:
        return max(1.0, float(widget.tk.call("tk", "scaling")) / (96 / 72))
    except (tk.TclError, ValueError):
        return 1.0


def px(n):
    """n pixels at 100 % -> pixels at the current display scaling."""
    return int(round(n * SCALE))


def art_step(scale=None):
    """Which corner art fits the scaling: (factor, file tag)."""
    s = SCALE if scale is None else scale
    return (1, "1x") if s < 1.25 else (1.5, "1_5x") if s < 1.75 else (2, "2x")


def chars(widget, texts, style="TButton"):
    """ttk width (in characters) that fits the widest text in the style's real font.
    Buttons of one kind get the same value, so they come out the same pixel width."""
    name = ttk.Style(widget).lookup(style, "font") or "TkDefaultFont"
    try:
        f = tkfont.Font(root=widget, font=name)
    except tk.TclError:
        f = tkfont.nametofont("TkDefaultFont")
    widest = max((f.measure(str(t)) for t in texts), default=0)
    return -(-widest // max(1, f.measure("0")))


def button_chars(widget, style="TButton"):
    """The width of the list's row buttons (and the smallest dialog button)."""
    return chars(widget, BUTTON_MIN_TEXTS, style)


def even_buttons(container, extra=()):
    """Every button directly in container (a dialog's button row) gets one width: the
    widest of their texts (and extra), at least a row button's width."""
    buttons = [w for w in container.winfo_children() if isinstance(w, ttk.Button)]
    if not buttons:
        return 0
    texts = [str(b.cget("text")) for b in buttons] + list(extra)
    width = max(button_chars(container), chars(container, texts))
    for b in buttons:
        b.configure(width=width)
    return width


def apply(root):
    """Style every ttk widget. Call once, before building widgets."""
    global HEAD_FAMILY, SCALE
    SCALE = scale_of(root)
    try:
        families = set(tkfont.families(root))
    except tk.TclError:
        families = set()
    HEAD_FAMILY = next((f for f in HEAD_CHOICES if f in families), FONT_FAMILY)
    root.configure(background=BG)
    s = ttk.Style(root)
    s.theme_use("clam")   # 'vista' ignores colours
    s.configure(".", background=BG, foreground=TEXT, fieldbackground=FIELD,
                bordercolor=BORDER, lightcolor=BG, darkcolor=BG,
                troughcolor=FIELD, focuscolor=FOCUS, font=font())
    s.configure("TFrame", background=BG)
    s.configure("List.TFrame", background=PANEL)
    s.configure("TLabel", background=BG, foreground=TEXT)
    s.configure("Muted.TLabel", foreground=MUTED)
    s.configure("Head.TLabel", foreground=HEAD, font=head_font())
    s.configure("Title.TLabel", foreground=TITLE, font=head_font(bigger=3))
    s.configure("Subtitle.TLabel", foreground=MUTED, font=font())
    s.configure("Failed.TLabel", background=ERROR_BG, foreground=ERROR_TEXT,
                font=font("bold"), padding=(6, 1))
    s.configure("TLabelframe", background=BG, bordercolor=LINE,
                lightcolor=LINE, darkcolor=LINE)
    s.configure("TLabelframe.Label", background=BG, foreground=HEAD, font=head_font())
    s.configure("TButton", background=BUTTON, foreground=BUTTON_TEXT, bordercolor=BORDER,
                lightcolor=BUTTON, darkcolor=BUTTON, padding=(px(10), px(3)),
                font=button_font(), anchor="center", focuscolor=BUTTON)
    s.map("TButton",
          background=[("disabled", BG), ("pressed", BUTTON_PRESSED), ("active", BUTTON_HOVER)],
          foreground=[("disabled", DISABLED)],
          bordercolor=[("disabled", LINE), ("focus", FOCUS)],
          lightcolor=[("pressed", BUTTON_PRESSED), ("active", BUTTON_HOVER)],
          darkcolor=[("pressed", BUTTON_PRESSED), ("active", BUTTON_HOVER)])
    # Row buttons (Install / Update / Uninstall in the list): a bit slimmer, so rows have
    # a gap between their buttons and the row lines run in that gap.
    s.configure("Item.TButton", padding=(px(10), px(1)))
    # Uninstall: the normal button with slightly dimmer text (its own disabled colour,
    # since a style's own foreground wins over the parent's state map).
    for name in ("Uninstall.TButton", "Uninstall.Item.TButton"):
        s.configure(name, foreground=UNINSTALL_TEXT)
        s.map(name, foreground=[("disabled", DISABLED)])
    for name in ("Accent.TButton", "Accent.Item.TButton"):
        s.configure(name, background=ACCENT, foreground=ACCENT_TEXT,
                    lightcolor=ACCENT, darkcolor=ACCENT, bordercolor=BORDER)   # focus: FOCUS
        s.map(name,
              background=[("disabled", BG), ("pressed", ACCENT), ("active", ACCENT_HOVER)],
              foreground=[("disabled", DISABLED)],
              bordercolor=[("disabled", LINE), ("focus", FOCUS)],
              lightcolor=[("disabled", BG), ("active", ACCENT_HOVER)],
              darkcolor=[("disabled", BG), ("active", ACCENT_HOVER)])
    for name in ("Item.TButton", "Uninstall.Item.TButton", "Accent.Item.TButton"):
        s.configure(name, padding=(px(10), px(1)))
    for name, fg in (("Path.TEntry", TEXT), ("Muted.Path.TEntry", MUTED)):
        s.configure(name, background=BG, fieldbackground=FIELD, foreground=fg,
                    bordercolor=LINE, focuscolor=LINE,
                    lightcolor=FIELD, darkcolor=FIELD, insertcolor=TEXT,
                    selectbackground=ACCENT, selectforeground=ACCENT_TEXT,
                    padding=(px(5), px(3)))
        s.map(name,
              fieldbackground=[("readonly", FIELD)],
              background=[("readonly", BG)],   # clam's light 'frame' colour showed at the corners
              foreground=[("readonly", fg)],
              bordercolor=[("focus", LINE)],
              lightcolor=[("focus", FIELD)],
              selectbackground=[("!focus", BUTTON_HOVER)],
              selectforeground=[("!focus", TEXT)])
    s.configure("TEntry", fieldbackground=FIELD, foreground=TEXT, insertcolor=TEXT,
                bordercolor=BORDER, lightcolor=FIELD, darkcolor=FIELD)
    s.map("TEntry", bordercolor=[("focus", FOCUS)])
    # Tick boxes and pick-one choices: a gold tick in a dark box, sized with the display
    # scaling (clam honours indicatorsize); the focus ring is bright gold.
    for name in ("TCheckbutton", "TRadiobutton"):
        s.configure(name, background=BG, foreground=TEXT, indicatorbackground=FIELD,
                    indicatorforeground=LINK, bordercolor=BORDER, focuscolor=FOCUS,
                    upperbordercolor=BORDER, lowerbordercolor=BORDER,
                    indicatorsize=px(12), indicatormargin=(0, 0, px(5), 0),
                    padding=(px(2), px(2)))
        s.map(name,
              background=[("active", BG)],
              foreground=[("disabled", DISABLED)],
              # greyed: the tick greys with its text (a bright gold tick looked clickable)
              indicatorforeground=[("disabled", DISABLED)],
              indicatorbackground=[("disabled", BG), ("pressed", BUTTON_HOVER)])
    # A pick-one button with a dark drop-down menu (Restore: which account; send later hours).
    s.configure("TMenubutton", background=BUTTON, foreground=BUTTON_TEXT, bordercolor=BORDER,
                lightcolor=BUTTON, darkcolor=BUTTON, arrowcolor=BUTTON_TEXT,
                padding=(px(8), px(3)), font=button_font())
    s.map("TMenubutton",
          background=[("disabled", BG), ("active", BUTTON_HOVER)],
          foreground=[("disabled", DISABLED)],
          bordercolor=[("focus", FOCUS)])
    s.configure("Horizontal.TProgressbar", background=ACCENT_HOVER, troughcolor=FIELD,
                bordercolor=LINE, lightcolor=ACCENT_HOVER, darkcolor=ACCENT,
                thickness=px(10))
    # Scroll bars: dark trough, gold thumb. The main list's is thin and has no arrows.
    for orient in ("Vertical", "Horizontal"):
        s.configure(f"{orient}.TScrollbar", background=BUTTON, troughcolor=PANEL,
                    bordercolor=LINE, lightcolor=BUTTON, darkcolor=BUTTON, arrowcolor=HEAD,
                    gripcount=0)
        s.map(f"{orient}.TScrollbar", background=[("active", BUTTON_HOVER)])
    s.layout("Gold.Vertical.TScrollbar", [("Vertical.Scrollbar.trough", {
        "sticky": "ns", "children": [("Vertical.Scrollbar.thumb",
                                      {"expand": "1", "sticky": "nswe"})]})])
    s.configure("Gold.Vertical.TScrollbar", background=BORDER, troughcolor=PANEL,
                bordercolor=PANEL, lightcolor=BORDER, darkcolor=BORDER,
                arrowsize=px(6), gripcount=0)
    s.map("Gold.Vertical.TScrollbar", background=[("active", HEAD)])
    s.layout("Gold.Horizontal.TScrollbar", [("Horizontal.Scrollbar.trough", {
        "sticky": "ew", "children": [("Horizontal.Scrollbar.thumb",
                                      {"expand": "1", "sticky": "nswe"})]})])
    s.configure("Gold.Horizontal.TScrollbar", background=BORDER, troughcolor=PANEL,
                bordercolor=PANEL, lightcolor=BORDER, darkcolor=BORDER,
                arrowsize=px(6), gripcount=0)
    s.map("Gold.Horizontal.TScrollbar", background=[("active", HEAD)])


def menu_options():
    """Colours and font for a tk.Menu."""
    return dict(background=SURFACE, foreground=TEXT, activebackground=ACCENT,
                activeforeground=ACCENT_TEXT, borderwidth=1, font=font())


# ---------- the window frame ----------
class Frame:
    """The gold double line round a window and (corners=True) the knot corners.

    A Canvas placed under everything draws the lines and the full corner pieces; the
    knots' drawn parts also sit in four small Labels above the content (Tk widgets can't
    be see-through). `clear` says how far the knots reach in from the window's edge, so
    layouts keep text and buttons out of those squares. The content stays packed (the
    window's requested size keeps coming from it)."""

    def __init__(self, win, content, margin=None, corners=False):
        self.win = win
        self.margin = px(6) if margin is None else margin
        self.canvas = tk.Canvas(win, background=BG, highlightthickness=0, borderwidth=0)
        self.canvas.place(x=0, y=0, relwidth=1, relheight=1)
        tk.Misc.lower(self.canvas)
        content.pack_configure(padx=self.margin, pady=self.margin)
        self.line = max(1, int(SCALE))
        self.inset = px(4)
        self.images, self.labels = {}, {}
        self.clear = 0
        self.off = px(CORNER_OFFSET)
        if corners:
            factor, step = art_step()
            ink = int(math.ceil(KNOT_INK * factor))
            for corner in CORNERS:
                path = os.path.join(FRAME_ART, CORNER_NAME.format(corner=corner, step=step))
                try:
                    full = tk.PhotoImage(master=win, file=path)
                except tk.TclError:
                    log.info("Corner art missing: %s", path)
                    continue
                n = full.width()
                x0 = 0 if corner[1] == "l" else n - ink
                y0 = 0 if corner[0] == "t" else n - ink
                crop = tk.PhotoImage(master=win)
                crop.tk.call(crop, "copy", full, "-from", x0, y0, x0 + ink, y0 + ink)
                self.images[corner] = (full, crop)
                self.labels[corner] = tk.Label(win, image=crop, background=BG, borderwidth=0,
                                               highlightthickness=0)
            if self.images:
                self.clear = self.off + ink
        self.canvas.bind("<Configure>", self.draw)
        self.canvas.bind("<Destroy>", lambda _e: release_images(
            [i for pair in self.images.values() for i in pair]), add="+")

    def draw(self, _event=None):
        c = self.canvas
        try:
            w, h = c.winfo_width(), c.winfo_height()
            c.delete("all")
        except tk.TclError:
            return
        lw, d = self.line, self.inset
        c.create_rectangle(0, 0, w - 1, h - 1, outline=FRAME_OUTER, width=lw)
        c.create_rectangle(d, d, w - 1 - d, h - 1 - d, outline=FRAME_INNER, width=lw)
        anchors = {"tl": "nw", "tr": "ne", "bl": "sw", "br": "se"}
        for corner, (full, _crop) in self.images.items():
            x = self.off if corner[1] == "l" else w - self.off
            y = self.off if corner[0] == "t" else h - self.off
            c.create_image(x, y, image=full, anchor=anchors[corner])
            label = self.labels[corner]
            label.place(x=x, y=y, anchor=anchors[corner])
            label.lift()


def release_images(images):
    """Delete Tk images now, on the main thread (a closed window's images would otherwise
    be freed by Python's garbage collector later, maybe on a worker thread, which Tk
    refuses)."""
    for img in images:
        name = getattr(img, "name", None)
        if not name:
            continue
        try:
            img.tk.call("image", "delete", name)
        except (tk.TclError, RuntimeError):
            pass
        img.name = None


def frame_window(win, content, corners=False):
    """Frame a Toplevel whose content frame is already packed; kept on the window."""
    win._am_frame = Frame(win, content, corners=corners)
    return win._am_frame


def heading(parent, text, bigger=3):
    """A window's heading: gold serif text with a thin gold rule under it."""
    box = ttk.Frame(parent)
    box.label = ttk.Label(box, text=text, style="Title.TLabel", font=head_font(bigger))
    box.label.pack(anchor="w")
    tk.Frame(box, height=1, background=LINE).pack(fill="x", pady=(px(3), 0))
    return box


def dark_title_bar(root):
    """Ask Windows 10 (20H1+) / 11 for a dark title bar (and a gold border on 11).
    Harmless if it fails."""
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
        for attr, bgr in ((35, 0x000b0e10),    # DWMWA_CAPTION_COLOR: BG
                          (34, 0x0030627a)):   # DWMWA_BORDER_COLOR: gold
            value = ctypes.c_int(bgr)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(value),
                                                       ctypes.sizeof(value))
    except Exception as e:   # older Windows: stays light, app still works
        log.info("Dark title bar not available: %s", e)
