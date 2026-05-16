"""
Keeppy.py  –  clipboard snippet saver (Cross-Platform Version)
Run (Windows): .\venv\Scripts\python Keeppy.py
Run (Linux):   sudo ./venv/bin/python Keeppy.py
Hotkey: Alt+Shift+C  → stash whatever text is currently selected
"""

import os
import sys
import time
import json
import queue
import platform
import threading
import subprocess
from datetime import datetime
import tkinter as tk
from tkinter import filedialog, messagebox
from PIL import Image, ImageDraw
import pystray
import keyboard

# ══════════════════════════════════════════════════════════════════════════════
#  STEP 0 — RESOLVE REAL USER  (Cross-Platform Support)
# ══════════════════════════════════════════════════════════════════════════════

current_os   = platform.system()           # "Linux" | "Windows"
ACTUAL_USER  = (
    os.environ.get("SUDO_USER")            # set by sudo automatically on Linux
    or os.environ.get("USER")
    or os.environ.get("USERNAME")          # Fallback for Windows
    or "nobody"
)

# Real home dir even when we are root on Linux
USER_HOME    = (
    os.path.expanduser(f"~{ACTUAL_USER}")
    if (ACTUAL_USER != "nobody" and current_os == "Linux")
    else os.path.expanduser("~")
)

APP_NAME     = "Keeppy"
SCRIPT_PATH  = os.path.abspath(__file__)

# Config lives in the user's home folder
CONFIG_FILE  = os.path.join(USER_HOME, ".keeppy_config.json")

# Shared state
target_file_path: str         = ""
popup_queue:      queue.Queue = queue.Queue()


# ══════════════════════════════════════════════════════════════════════════════
#  1.  X11 DISPLAY DETECTION  (Linux Only)
# ══════════════════════════════════════════════════════════════════════════════

def _resolve_x11_env() -> tuple:
    if current_os == "Windows":
        return "", ""

    display = os.environ.get("DISPLAY", ":0")
    xauth   = os.path.join(USER_HOME, ".Xauthority")

    for proc_name in ("gnome-shell", "gnome-session", "Xorg", "xorg"):
        try:
            r = subprocess.run(
                ["pgrep", "-u", ACTUAL_USER, proc_name],
                capture_output=True, text=True, timeout=2,
            )
            pid = r.stdout.strip().split("\n")[0]
            if not pid:
                continue
            raw = subprocess.run(
                ["cat", f"/proc/{pid}/environ"],
                capture_output=True, timeout=2,
            ).stdout
            for var in raw.split(b"\x00"):
                v = var.decode("utf-8", errors="ignore")
                if v.startswith("DISPLAY="):
                    display = v.split("=", 1)[1]
                elif v.startswith("XAUTHORITY="):
                    xauth = v.split("=", 1)[1]
            break
        except Exception:
            continue

    return display, xauth


DISPLAY, XAUTHORITY = _resolve_x11_env()

# Inject into environmental variables for Linux X11 systems
if current_os == "Linux":
    os.environ["DISPLAY"]    = DISPLAY
    os.environ["XAUTHORITY"] = XAUTHORITY

print(f"[{APP_NAME}] user={ACTUAL_USER}  home={USER_HOME}")
if current_os == "Linux":
    print(f"[{APP_NAME}] DISPLAY={DISPLAY}   XAUTHORITY={XAUTHORITY}")
print(f"[{APP_NAME}] config={CONFIG_FILE}")


# ══════════════════════════════════════════════════════════════════════════════
#  2.  CLIPBOARD — CROSS-PLATFORM SYSTEM
# ══════════════════════════════════════════════════════════════════════════════

def get_primary_selection() -> str:
    if current_os == "Windows":
        try:
            # Query standard system clipboard engine on Windows
            root = tk.Tk()
            root.withdraw()
            text = root.clipboard_get()
            root.destroy()
            return text
        except Exception as exc:
            print(f"[{APP_NAME}] Windows clipboard read error: {exc}")
            return ""
    else:
        # Use primary mouse selection wrapper via xclip on Linux
        try:
            env = {**os.environ, "DISPLAY": DISPLAY, "XAUTHORITY": XAUTHORITY}
            r = subprocess.run(
                ["sudo", "-u", ACTUAL_USER, "xclip", "-selection", "primary", "-o"],
                capture_output=True, text=True, timeout=3, env=env,
            )
            return r.stdout
        except Exception as exc:
            print(f"[{APP_NAME}] xclip error: {exc}")
            return ""


# ══════════════════════════════════════════════════════════════════════════════
#  3.  AUTO-START
# ══════════════════════════════════════════════════════════════════════════════

def _autostart_linux():
    import pwd
    autostart_dir = os.path.join(USER_HOME, ".config", "autostart")
    os.makedirs(autostart_dir, exist_ok=True)

    desktop_path = os.path.join(autostart_dir, "keeppy.desktop")
    content = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={APP_NAME}\n"
        f"Exec=sudo {sys.executable} \"{SCRIPT_PATH}\"\n"
        "Hidden=false\n"
        "NoDisplay=false\n"
        "X-GNOME-Autostart-enabled=true\n"
        f"Comment={APP_NAME} clipboard snippet saver\n"
    )
    with open(desktop_path, "w") as f:
        f.write(content)
    os.chmod(desktop_path, 0o755)

    try:
        pw = pwd.getpwnam(ACTUAL_USER)
        os.chown(autostart_dir, pw.pw_uid, pw.pw_gid)
        os.chown(desktop_path,  pw.pw_uid, pw.pw_gid)
    except Exception:
        pass


def _autostart_windows():
    import winreg
    if getattr(sys, "frozen", False):
        cmd = f'"{sys.executable}"'
    else:
        pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        if not os.path.exists(pythonw):
            pythonw = sys.executable
        cmd = f'"{pythonw}" "{SCRIPT_PATH}"'
    key = winreg.OpenKey(
        winreg.HKEY_CURRENT_USER,
        r"Software\Microsoft\Windows\CurrentVersion\Run",
        0, winreg.KEY_SET_VALUE,
    )
    winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, cmd)
    winreg.CloseKey(key)


def setup_autostart():
    try:
        if current_os == "Linux":
            _autostart_linux()
        elif current_os == "Windows":
            _autostart_windows()
        print(f"[{APP_NAME}] Auto-start registered.")
    except Exception as exc:
        print(f"[{APP_NAME}] Auto-start skipped: {exc}")


# ══════════════════════════════════════════════════════════════════════════════
#  4.  CONFIG LOAD / SAVE
# ══════════════════════════════════════════════════════════════════════════════

def load_settings():
    global target_file_path
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                target_file_path = json.load(f).get("target_file_path", "")
            print(f"[{APP_NAME}] Config loaded → {target_file_path}")
        except Exception as exc:
            print(f"[{APP_NAME}] Config read error: {exc}")
            target_file_path = ""
    else:
        print(f"[{APP_NAME}] No config found — running first-time setup.")

    if not target_file_path:
        initial_setup_gui()   # blocks until user saves
        setup_autostart()     # register once after first setup


def save_settings(path: str):
    global target_file_path
    target_file_path = path
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump({"target_file_path": path}, f)
        
        # Linux specific permissions corrections
        if current_os == "Linux":
            import pwd
            pw = pwd.getpwnam(ACTUAL_USER)
            os.chown(CONFIG_FILE, pw.pw_uid, pw.pw_gid)
            
        print(f"[{APP_NAME}] Config saved → {CONFIG_FILE}")
    except Exception as exc:
        print(f"[{APP_NAME}] Config save error: {exc}")


# ══════════════════════════════════════════════════════════════════════════════
#  5.  SETTINGS GUI
# ══════════════════════════════════════════════════════════════════════════════

def initial_setup_gui():
    def browse_file():
        fp = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[("Text Files", "*.txt"), ("All Files", "*.*")],
            title="Select or Create your Keeppy Storage File",
        )
        if fp:
            entry_path.delete(0, tk.END)
            entry_path.insert(0, fp)

    def save_and_close():
        path = entry_path.get().strip()
        if path:
            save_settings(path)
            root.destroy()
        else:
            messagebox.showerror("Error", "Please select a valid file path!")

    def clear_all_data():
        if target_file_path and os.path.exists(target_file_path):
            if messagebox.askyesno("Confirm Delete", "Delete ALL saved snippets?"):
                open(target_file_path, "w").close()
                messagebox.showinfo("Done", "All data cleared.")
        else:
            messagebox.showerror("Error", "No storage file found to clear.")

    root = tk.Tk()
    root.title(f"{APP_NAME} – Settings")
    root.geometry("470x265")
    root.resizable(False, False)
    root.attributes("-topmost", True)

    tk.Label(root, text=APP_NAME,
             font=("Arial", 13, "bold"), fg="#2ea44f").pack(pady=(14, 0))
    tk.Label(root, text="Hotkey:  Alt + Shift + C  →  stash selected text",
             font=("Arial", 9), fg="#555").pack(pady=(2, 10))
    tk.Label(root, text="Storage file:",
             font=("Arial", 9)).pack(anchor="w", padx=22)

    frame = tk.Frame(root)
    frame.pack(fill="x", padx=22, pady=4)
    entry_path = tk.Entry(frame, width=44, font=("Arial", 9))
    entry_path.pack(side="left", expand=True, fill="x")
    if target_file_path:
        entry_path.insert(0, target_file_path)
    tk.Button(frame, text="Browse", command=browse_file).pack(
        side="right", padx=(6, 0))

    tk.Button(root, text="✔  Save Settings", bg="#2ea44f", fg="white",
              font=("Arial", 10, "bold"), command=save_and_close,
              relief="flat", padx=10).pack(fill="x", padx=22, pady=(12, 4))
    tk.Button(root, text="⚠  Delete All Saved Data", bg="#da3637", fg="white",
              font=("Arial", 9), command=clear_all_data,
              relief="flat").pack(fill="x", padx=22, pady=(0, 8))

    root.mainloop()


# ══════════════════════════════════════════════════════════════════════════════
#  6.  NOTIFICATION POPUP
# ══════════════════════════════════════════════════════════════════════════════

def show_popup(text: str):
    if not target_file_path or not text.strip():
        return

    BG, FG, DIM = "#1e1e1e", "#f1f1f1", "#aaaaaa"
    GREEN, RED  = "#2ea44f", "#da3637"

    GRAB_RELEASED = [False]   # guard: only release grab once
    TIMER_ID      = [None]    # mutable so inner closures can update it

    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.configure(bg=BG)

    # ── preview ───────────────────────────────────────────────────────────────
    preview = text.strip().replace("\n", " ")
    if len(preview) > 50:
        preview = preview[:50] + "…"

    tk.Label(root, text="📋  Stash this snippet?",
             font=("Arial", 10, "bold"), bg=BG, fg=FG).pack(pady=(10, 2))
    tk.Label(root, text=f'"{preview}"',
             font=("Arial", 9, "italic"), bg=BG, fg=DIM,
             wraplength=270).pack(pady=(0, 10), padx=16)

    # ── shared cleanup helpers ────────────────────────────────────────────────
    def _release_grab():
        if not GRAB_RELEASED[0]:
            GRAB_RELEASED[0] = True
            try:
                root.grab_release()
            except Exception:
                pass

    def _cancel_timer():
        if TIMER_ID[0] is not None:
            try:
                root.after_cancel(TIMER_ID[0])
            except Exception:
                pass
            TIMER_ID[0] = None

    # ── button actions ────────────────────────────────────────────────────────
    def on_save():
        _cancel_timer()
        _release_grab()
        ts = datetime.now().strftime("[%b %d, %Y   %I:%M %p]")
        try:
            with open(target_file_path, "a", encoding="utf-8") as f:
                f.write(f"{ts}\n{text}\n\n")
            print(f"[{APP_NAME}] Saved at {ts}")
        except Exception as exc:
            print(f"[{APP_NAME}] Save error: {exc}")
        root.destroy()

    def on_close():
        _cancel_timer()
        _release_grab()
        root.destroy()

    # ── buttons ───────────────────────────────────────────────────────────────
    btn_frame = tk.Frame(root, bg=BG)
    btn_frame.pack(pady=(0, 10))

    save_btn = tk.Button(
        btn_frame, text="  ✓  Save  (Enter)  ",
        font=("Arial", 10, "bold"), bg=GREEN, fg="white",
        command=on_save, relief="flat", bd=0,
    )
    save_btn.pack(side="left", padx=8)

    tk.Button(
        btn_frame, text="  ✗  Close  ",
        font=("Arial", 10, "bold"), bg=RED, fg="white",
        command=on_close, relief="flat", bd=0,
    ).pack(side="right", padx=8)

    root.bind("<Return>", lambda _: on_save())
    root.bind("<Escape>", lambda _: on_close())

    # ── position: bottom-right corner ─────────────────────────────────────────
    root.update_idletasks()
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    ww, wh = root.winfo_width(),       root.winfo_height()
    root.geometry(f"+{sw - ww - 20}+{sh - wh - 60}")

    # ── seize focus cleanly without freezing Windows OS handles ───────────────
    def _seize_focus():
        try:
            root.lift()
            root.focus_force()
            save_btn.focus_force()
            if current_os == "Linux":
                root.grab_set_global()  # Necessary workaround for headless sudo contexts on X11
            else:
                root.grab_set()         # Safe local focus grab on Windows
        except Exception as exc:
            print(f"[{APP_NAME}] Focus grab error: {exc}")

    root.after(200, _seize_focus)

    # ── auto-dismiss ──────────────────────────────────────────────────────────
    TIMER_ID[0] = root.after(7000, on_close)

    root.mainloop()


# ══════════════════════════════════════════════════════════════════════════════
#  7.  HOTKEY HANDLER  (keyboard listener thread)
# ══════════════════════════════════════════════════════════════════════════════

def on_hotkey_pressed():
    time.sleep(0.15)   # Allow physical keystrokes to register release state
    
    if current_os == "Windows":
        # Force the highlighted text into standard clipboard on Windows OS
        keyboard.send("ctrl+c")
        time.sleep(0.12)  # Give Windows OS time to cache text values
        
    text = get_primary_selection()
    if text.strip():
        popup_queue.put(text)
    else:
        print(f"[{APP_NAME}] Nothing selected (Clipboard/PRIMARY was empty).")


def start_keyboard_listener():
    keyboard.add_hotkey("alt+shift+c", on_hotkey_pressed)
    keyboard.wait()


# ══════════════════════════════════════════════════════════════════════════════
#  8.  SYSTEM TRAY  (daemon thread)
# ══════════════════════════════════════════════════════════════════════════════

def _build_icon() -> Image.Image:
    img = Image.new("RGB", (64, 64), "#1e1e1e")
    d   = ImageDraw.Draw(img)
    d.rectangle([(8, 8), (56, 56)], fill="#2ea44f")
    d.text((14, 12), "Kp", fill="white", font_size=26)
    return img


def _quit(icon, _):
    icon.stop()
    os._exit(0)


def _open_settings(icon, _):
    popup_queue.put("__SETTINGS__")


def run_system_tray():
    icon        = pystray.Icon(APP_NAME)
    icon.icon   = _build_icon()
    icon.title  = f"{APP_NAME}  (Alt+Shift+C to stash)"
    icon.menu   = pystray.Menu(
        pystray.MenuItem("⚙  Settings & Manage", _open_settings),
        pystray.MenuItem("✖  Exit Keeppy",        _quit),
    )
    icon.run()


# ══════════════════════════════════════════════════════════════════════════════
#  9.  MAIN LOOP 
# ══════════════════════════════════════════════════════════════════════════════

def main_loop():
    while True:
        try:
            item = popup_queue.get(timeout=0.2)
            if item == "__SETTINGS__":
                initial_setup_gui()
            else:
                show_popup(item)
        except queue.Empty:
            continue
        except KeyboardInterrupt:
            break


# ══════════════════════════════════════════════════════════════════════════════
#  10. ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    load_settings()

    threading.Thread(target=start_keyboard_listener, daemon=True).start()
    threading.Thread(target=run_system_tray,         daemon=True).start()

    print(f"[{APP_NAME}] Running on {current_os}.  Hotkey: Alt+Shift+C")
    main_loop() 