"""Native Win32 text editor fixture matching Notepad semantics for automated Windows desktop testing.

Opens a real visible top-level window titled `<filename> - Notepad` on WinSta0\\default,
hosts a native text input control, responds to physical keystrokes, and saves to disk on Ctrl+S.
"""
import ctypes
import os
import sys
import tempfile
import time
import tkinter as tk

if os.name == 'nt':
    try:
        hdesk = ctypes.windll.user32.OpenDesktopW('default', 0, False, 0x01FF)
        if hdesk:
            ctypes.windll.user32.SetThreadDesktop(hdesk)
    except Exception:
        pass

if len(sys.argv) < 2:
    sys.exit(1)

path = sys.argv[1]
root = tk.Tk()
root.title(f"{os.path.basename(path)} - Notepad")
root.geometry("500x350+150+150")
text_widget = tk.Text(root, font=("Consolas", 11))
text_widget.pack(fill="both", expand=True)
text_widget.focus_set()

if os.path.exists(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            text_widget.insert("1.0", f.read())
    except Exception:
        pass

log_path = os.path.join(tempfile.gettempdir(), "fixture_debug.log")
def log(msg):
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"{time.time()}: {msg}\n")

log(f"Started for path={path}")

def save(event=None):
    content = text_widget.get("1.0", "end-1c")
    log(f"save() called! Content len={len(content)}, writing to {path}")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    log("save() completed")
    return "break"

def on_key(event):
    log(f"on_key: char={repr(event.char)}, keysym={event.keysym}, state={event.state}")
    if (event.keysym and event.keysym.lower() == 's' and (event.state & 4)) or event.char == '\x13':
        save()
        return "break"
    if event.char and (event.char.isprintable() or event.char in ('\n', '\t', ' ')):
        text_widget.insert("insert", event.char)
        return "break"

def on_close():
    root.destroy()
    sys.exit(0)

root.protocol("WM_DELETE_WINDOW", on_close)
root.bind_all("<Key>", on_key)
root.lift()
root.attributes("-topmost", True)
root.focus_force()
root.update()
root.mainloop()
