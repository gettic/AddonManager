"""Right-click menus that also open with press-and-hold on Linux (Steam Deck touch
screen: a finger has no right button). Windows keeps plain right-click only."""
import os

HOLD_MS = 600     # how long a press must last
MOVE_PX = 10      # moving the finger further than this cancels (it was a drag/scroll)
LONG_PRESS = os.name != "nt"


class _Hold:
    def __init__(self, widget, popup):
        self.widget, self.popup = widget, popup
        self.job = None
        self.start = None

    def press(self, event):
        self.cancel()
        self.start = (event.x_root, event.y_root)
        self.job = self.widget.after(HOLD_MS, self.fire)

    def move(self, event):
        if self.start is None:
            return
        if (abs(event.x_root - self.start[0]) > MOVE_PX
                or abs(event.y_root - self.start[1]) > MOVE_PX):
            self.cancel()

    def cancel(self, _event=None):
        if self.job is not None:
            try:
                self.widget.after_cancel(self.job)
            except Exception:
                pass
        self.job = None

    def fire(self):
        self.job = None
        if self.start is None:
            return
        x, y = self.start
        self.start = None
        self.popup(_Spot(self.widget, x, y))


class _Spot:
    """The bits of a Tk event the popups use (where to open the menu)."""

    def __init__(self, widget, x_root, y_root):
        self.widget, self.x_root, self.y_root = widget, x_root, y_root
        self.x = x_root - widget.winfo_rootx()
        self.y = y_root - widget.winfo_rooty()


def bind_menu(widget, popup, long_press=None):
    """popup(event) on right-click; on Linux also on a 600 ms press-and-hold. Use it on
    labels and fields only, never on buttons (letting go would also click them)."""
    widget.bind("<Button-3>", popup, add="+")
    if LONG_PRESS if long_press is None else long_press:
        hold = _Hold(widget, popup)
        widget.bind("<ButtonPress-1>", hold.press, add="+")
        widget.bind("<B1-Motion>", hold.move, add="+")
        widget.bind("<ButtonRelease-1>", hold.cancel, add="+")
        widget._am_hold = hold   # tests reach it here
        return hold
    return None
