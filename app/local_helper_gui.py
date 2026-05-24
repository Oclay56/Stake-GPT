from __future__ import annotations

import ctypes
import queue
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from tkinter import (
    BOTH,
    END,
    LEFT,
    RIGHT,
    Button,
    Canvas,
    Frame,
    Label,
    Tk,
    Text,
    Toplevel,
    colorchooser,
    messagebox,
)
from ctypes import wintypes

from .local_helper_setup import check_local_helper_setup, format_setup_report


ROOT_DIR = Path(__file__).resolve().parents[1]
WPARAM = ctypes.c_size_t
LPARAM = ctypes.c_ssize_t
LRESULT = ctypes.c_ssize_t
HELPER_BG = "#03041D"
HELPER_FG = "#F4F0FF"
HELPER_MUTED_FG = "#C9C7D9"
HELPER_PANEL_BG = "#070923"
HELPER_BUTTON_BG = "#11143A"
HELPER_BUTTON_ACTIVE_BG = "#1A1E55"
LOGO_FONT_FAMILY = "Segoe Script"


def should_minimize_to_tray(window_state: str, *, tray_supported: bool) -> bool:
    return tray_supported and window_state == "iconic"


class WindowsTrayIcon:
    WM_TRAYICON = 0x0400 + 31
    WM_DESTROY = 0x0002
    WM_COMMAND = 0x0111
    WM_LBUTTONDBLCLK = 0x0203
    WM_RBUTTONUP = 0x0205

    NIF_MESSAGE = 0x00000001
    NIF_ICON = 0x00000002
    NIF_TIP = 0x00000004
    NIM_ADD = 0x00000000
    NIM_DELETE = 0x00000002
    TPM_RETURNCMD = 0x0100
    TPM_RIGHTBUTTON = 0x0002
    MF_STRING = 0x0000
    IDI_APPLICATION = 32512

    RESTORE_COMMAND = 1001
    EXIT_COMMAND = 1002

    def __init__(
        self,
        tooltip: str,
        *,
        on_restore: Callable[[], None],
        on_exit: Callable[[], None],
    ) -> None:
        self.tooltip = tooltip[:127]
        self.on_restore = on_restore
        self.on_exit = on_exit
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._hwnd: int | None = None
        self._hicon: int | None = None
        self._visible = False
        self._wndproc = None
        self._class_name = f"AZP_LOCAL_HELPER_TRAY_{id(self)}"

    @staticmethod
    def is_supported() -> bool:
        return sys.platform == "win32"

    def show(self) -> bool:
        if not self.is_supported():
            return False
        if self._visible:
            return True

        if not self._thread or not self._thread.is_alive():
            self._ready.clear()
            self._thread = threading.Thread(target=self._message_loop, daemon=True)
            self._thread.start()

        if not self._ready.wait(timeout=2) or not self._hwnd:
            return False

        data = self._notify_data()
        shell32 = ctypes.windll.shell32
        self._visible = bool(shell32.Shell_NotifyIconW(self.NIM_ADD, ctypes.byref(data)))
        return self._visible

    def hide(self) -> None:
        if not self._visible or not self._hwnd:
            return
        data = self._notify_data()
        ctypes.windll.shell32.Shell_NotifyIconW(self.NIM_DELETE, ctypes.byref(data))
        self._visible = False

    def close(self) -> None:
        if not self.is_supported():
            return
        self.hide()
        if self._hwnd:
            ctypes.windll.user32.PostMessageW.argtypes = [
                wintypes.HWND,
                wintypes.UINT,
                WPARAM,
                LPARAM,
            ]
            ctypes.windll.user32.PostMessageW(self._hwnd, self.WM_DESTROY, 0, 0)

    def _notify_data(self):
        class NotifyIconData(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("hWnd", wintypes.HWND),
                ("uID", wintypes.UINT),
                ("uFlags", wintypes.UINT),
                ("uCallbackMessage", wintypes.UINT),
                ("hIcon", wintypes.HANDLE),
                ("szTip", wintypes.WCHAR * 128),
                ("dwState", wintypes.DWORD),
                ("dwStateMask", wintypes.DWORD),
                ("szInfo", wintypes.WCHAR * 256),
                ("uTimeoutOrVersion", wintypes.UINT),
                ("szInfoTitle", wintypes.WCHAR * 64),
                ("dwInfoFlags", wintypes.DWORD),
                ("guidItem", ctypes.c_byte * 16),
                ("hBalloonIcon", wintypes.HANDLE),
            ]

        data = NotifyIconData()
        data.cbSize = ctypes.sizeof(NotifyIconData)
        data.hWnd = self._hwnd
        data.uID = 1
        data.uFlags = self.NIF_MESSAGE | self.NIF_ICON | self.NIF_TIP
        data.uCallbackMessage = self.WM_TRAYICON
        data.hIcon = self._hicon or ctypes.windll.user32.LoadIconW(None, self.IDI_APPLICATION)
        data.szTip = self.tooltip
        return data

    def _message_loop(self) -> None:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, WPARAM, LPARAM]
        user32.DefWindowProcW.restype = LRESULT

        WNDPROC = ctypes.WINFUNCTYPE(
            LRESULT,
            wintypes.HWND,
            wintypes.UINT,
            WPARAM,
            LPARAM,
        )

        class WndClass(ctypes.Structure):
            _fields_ = [
                ("style", wintypes.UINT),
                ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HANDLE),
                ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HANDLE),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR),
            ]

        def wndproc(hwnd, msg, wparam, lparam):
            if msg == self.WM_TRAYICON:
                if lparam == self.WM_LBUTTONDBLCLK:
                    self.on_restore()
                    return 0
                if lparam == self.WM_RBUTTONUP:
                    self._show_menu(hwnd)
                    return 0
            if msg == self.WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
            if msg == self.WM_COMMAND:
                command = wparam & 0xFFFF
                if command == self.RESTORE_COMMAND:
                    self.on_restore()
                elif command == self.EXIT_COMMAND:
                    self.on_exit()
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._wndproc = WNDPROC(wndproc)
        hinstance = kernel32.GetModuleHandleW(None)
        self._hicon = user32.LoadIconW(None, self.IDI_APPLICATION)

        wndclass = WndClass()
        wndclass.lpfnWndProc = self._wndproc
        wndclass.hInstance = hinstance
        wndclass.hIcon = self._hicon
        wndclass.lpszClassName = self._class_name
        user32.RegisterClassW(ctypes.byref(wndclass))

        self._hwnd = user32.CreateWindowExW(
            0,
            self._class_name,
            self._class_name,
            0,
            0,
            0,
            0,
            0,
            None,
            None,
            hinstance,
            None,
        )
        self._ready.set()

        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _show_menu(self, hwnd: int) -> None:
        user32 = ctypes.windll.user32
        menu = user32.CreatePopupMenu()
        user32.AppendMenuW(menu, self.MF_STRING, self.RESTORE_COMMAND, "Restore")
        user32.AppendMenuW(menu, self.MF_STRING, self.EXIT_COMMAND, "Exit")

        point = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(point))
        user32.SetForegroundWindow(hwnd)
        command = user32.TrackPopupMenu(
            menu,
            self.TPM_RETURNCMD | self.TPM_RIGHTBUTTON,
            point.x,
            point.y,
            0,
            hwnd,
            None,
        )
        user32.DestroyMenu(menu)
        if command == self.RESTORE_COMMAND:
            self.on_restore()
        elif command == self.EXIT_COMMAND:
            self.on_exit()


class AzpHelperGui:
    def __init__(self) -> None:
        self.root = Tk()
        self.root.title("Stake-GPT Helper")
        self.root.geometry("780x500")
        self.root.minsize(700, 400)
        self.root.configure(bg=HELPER_BG)
        self.process: subprocess.Popen[str] | None = None
        self.output_queue: queue.Queue[str] = queue.Queue()
        self._closing = False
        self._hidden_to_tray = False
        self.helper_bg = HELPER_BG
        self.button_accent_bg = HELPER_BUTTON_BG
        self.button_accent_active_bg = HELPER_BUTTON_ACTIVE_BG
        self.control_buttons: list[Button] = []
        self.background_widgets: list = [self.root]
        self.tray_icon = WindowsTrayIcon(
            "Stake-GPT Helper",
            on_restore=lambda: self.root.after(0, self.restore_from_tray),
            on_exit=lambda: self.root.after(0, self.close),
        )

        self.logo = create_stake_logo_header(self.root)
        self.logo.pack(fill="x", padx=16, pady=(14, 2))
        self.background_widgets.append(self.logo)

        self.description_label = Label(
            self.root,
            text=(
                "Review Mode reads Stake UI boards. Build Mode can click exact "
                "validated legs into the slip for review only."
            ),
            font=("Segoe UI", 10),
            wraplength=660,
            bg=HELPER_BG,
            fg=HELPER_MUTED_FG,
        )
        self.description_label.pack(pady=(0, 12))
        self.background_widgets.append(self.description_label)

        self.controls = Frame(self.root, bg=HELPER_BG)
        self.controls.pack(fill="x", padx=16, pady=(0, 10))
        self.background_widgets.append(self.controls)

        review_button = Button(
            self.controls,
            text="Start Review Mode",
            command=lambda: self.start_helper("review"),
            width=22,
            **_button_style(self.button_accent_bg, self.button_accent_active_bg),
        )
        review_button.pack(side=LEFT, padx=(0, 8))
        self.control_buttons.append(review_button)

        build_button = Button(
            self.controls,
            text="Start Build Slip Mode",
            command=lambda: self.start_helper("build"),
            width=22,
            **_button_style(self.button_accent_bg, self.button_accent_active_bg),
        )
        build_button.pack(side=LEFT, padx=(0, 8))
        self.control_buttons.append(build_button)

        setup_button = Button(
            self.controls,
            text="Setup Check",
            command=self.run_setup_check,
            width=15,
            **_button_style(self.button_accent_bg, self.button_accent_active_bg),
        )
        setup_button.pack(side=LEFT, padx=(0, 8))
        self.control_buttons.append(setup_button)

        cache_button = Button(
            self.controls,
            text="Clean Cache",
            command=self.run_cache_cleanup,
            width=14,
            **_button_style(self.button_accent_bg, self.button_accent_active_bg),
        )
        cache_button.pack(side=LEFT)
        self.control_buttons.append(cache_button)

        color_button = Button(
            self.controls,
            text="Color",
            command=self.choose_button_color,
            width=10,
            **_button_style(self.button_accent_bg, self.button_accent_active_bg),
        )
        color_button.pack(side=LEFT, padx=(8, 0))
        self.control_buttons.append(color_button)

        stop_button = Button(
            self.controls,
            text="Stop",
            command=self.stop_helper,
            width=10,
            **_button_style(self.button_accent_bg, self.button_accent_active_bg),
        )
        stop_button.pack(side=RIGHT)
        self.control_buttons.append(stop_button)

        self.status_label = Label(
            self.root,
            text="Status: idle",
            anchor="w",
            font=("Segoe UI", 10, "bold"),
            bg=HELPER_BG,
            fg=HELPER_FG,
        )
        self.status_label.pack(fill="x", padx=16)
        self.background_widgets.append(self.status_label)

        self.log = Text(
            self.root,
            height=16,
            wrap="word",
            font=("Consolas", 10),
            bg=HELPER_PANEL_BG,
            fg=HELPER_FG,
            insertbackground=HELPER_FG,
            selectbackground=HELPER_BUTTON_ACTIVE_BG,
            selectforeground=HELPER_FG,
            relief="flat",
            borderwidth=0,
            highlightthickness=1,
            highlightbackground=HELPER_BUTTON_BG,
            highlightcolor=HELPER_BUTTON_ACTIVE_BG,
        )
        self.log.pack(fill=BOTH, expand=True, padx=16, pady=(8, 16))
        self._write_log("Pick a mode. Close this window when you are done.\n")
        self._write_log("Build Mode never enters a stake amount and never clicks Place Bet.\n\n")
        self._write_log(format_setup_report(check_local_helper_setup(ROOT_DIR)) + "\n\n")

        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.bind("<Unmap>", self.on_unmap)
        self.root.after(100, self.drain_output)

    def run(self) -> None:
        self.root.mainloop()

    def choose_button_color(self) -> None:
        dialog = Toplevel(self.root)
        dialog.title("Helper Color")
        dialog.configure(bg=self.helper_bg)
        dialog.resizable(False, False)
        dialog.transient(self.root)

        Label(
            dialog,
            text="Color target",
            font=("Segoe UI", 10, "bold"),
            bg=self.helper_bg,
            fg=HELPER_FG,
        ).pack(padx=14, pady=(12, 8))

        body = Frame(dialog, bg=self.helper_bg)
        body.pack(padx=14, pady=(0, 14))

        Button(
            body,
            text="Outline",
            width=12,
            command=lambda: self._choose_helper_color("outline", dialog),
            **_button_style(self.button_accent_bg, self.button_accent_active_bg),
        ).pack(side=LEFT, padx=(0, 8))
        Button(
            body,
            text="Background",
            width=12,
            command=lambda: self._choose_helper_color("background", dialog),
            **_button_style(self.button_accent_bg, self.button_accent_active_bg),
        ).pack(side=LEFT)

        dialog.focus_set()

    def _choose_helper_color(self, target: str, dialog: Toplevel | None = None) -> None:
        current_color = self.helper_bg if target == "background" else self.button_accent_bg
        rgb_value, hex_value = colorchooser.askcolor(
            color=current_color,
            title=f"Helper {target.title()} Color",
        )
        accent = normalize_color_choice(rgb_value, fallback=str(hex_value or ""))
        if not accent:
            return

        if target == "background":
            self.helper_bg = apply_background_color(self.background_widgets, self.log, accent)
            self._write_log(f"Helper background color set to {self.helper_bg}.\n")
        else:
            outline = apply_outline_color(self.control_buttons, self.log, accent)
            self.button_accent_bg = outline["accent"]
            self.button_accent_active_bg = outline["activeAccent"]
            self._write_log(f"Helper outline color set to {self.button_accent_bg}.\n")

        if dialog:
            dialog.destroy()

    def start_helper(self, mode: str) -> None:
        if self.process and self.process.poll() is None:
            messagebox.showinfo("Stake-GPT Helper", "Helper is already running.")
            return

        python_exe = ROOT_DIR / ".venv" / "Scripts" / "python.exe"
        setup_report = check_local_helper_setup(ROOT_DIR)
        if not setup_report["ok"]:
            report_text = format_setup_report(setup_report)
            self._write_log(report_text + "\n\n")
            messagebox.showerror(
                "Stake-GPT Helper",
                report_text,
            )
            return

        self.status_label.configure(text=f"Status: starting {mode} mode...")
        self._write_log(f"Starting helper in {mode} mode...\n")

        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.process = subprocess.Popen(
            [str(python_exe), "-m", "app.local_stake_helper", "--mode", mode],
            cwd=ROOT_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            creationflags=creationflags,
        )
        threading.Thread(target=self.capture_output, daemon=True).start()

    def run_setup_check(self) -> None:
        report_text = format_setup_report(check_local_helper_setup(ROOT_DIR))
        self._write_log(report_text + "\n\n")
        self.status_label.configure(
            text="Status: setup ready"
            if "Ready." in report_text
            else "Status: setup needs attention"
        )

    def run_cache_cleanup(self) -> None:
        python_exe = ROOT_DIR / ".venv" / "Scripts" / "python.exe"
        if not python_exe.exists():
            messagebox.showerror(
                "Stake-GPT Helper",
                f"Could not find {python_exe}. Run the project setup first.",
            )
            return
        if not (ROOT_DIR / ".env").exists():
            messagebox.showerror(
                "Stake-GPT Helper",
                f"Could not find {ROOT_DIR / '.env'}. The cleanup needs Supabase settings.",
            )
            return

        self.status_label.configure(text="Status: cleaning Supabase cache...")
        self._write_log("Running Supabase cache cleanup...\n")
        threading.Thread(target=self._run_cache_cleanup_thread, daemon=True).start()

    def _run_cache_cleanup_thread(self) -> None:
        python_exe = ROOT_DIR / ".venv" / "Scripts" / "python.exe"
        completed = subprocess.run(
            [str(python_exe), "-m", "app.supabase_cache"],
            cwd=ROOT_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        output = completed.stdout or ""
        if output:
            self.output_queue.put(output)
        self.output_queue.put(f"Supabase cache cleanup exited with code {completed.returncode}.\n")

    def stop_helper(self) -> None:
        if not self.process or self.process.poll() is not None:
            self.status_label.configure(text="Status: idle")
            self._write_log("Helper is not running.\n")
            return

        self._write_log("Stopping helper...\n")
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
        self.status_label.configure(text="Status: stopped")
        self._write_log("Helper stopped.\n")

    def close(self) -> None:
        self._closing = True
        self.tray_icon.close()
        self.stop_helper()
        self.root.destroy()

    def on_unmap(self, _event=None) -> None:
        if self._closing or self._hidden_to_tray:
            return
        try:
            window_state = self.root.state()
        except Exception:
            return
        if should_minimize_to_tray(
            window_state,
            tray_supported=self.tray_icon.is_supported(),
        ):
            self.root.after(50, self.minimize_to_tray)

    def minimize_to_tray(self) -> None:
        if self._closing or self._hidden_to_tray:
            return
        if self.tray_icon.show():
            self._hidden_to_tray = True
            self.root.withdraw()
            self._write_log(
                "Helper minimized to system tray. Double-click the tray icon to restore.\n"
            )
        else:
            self._write_log("System tray unavailable; helper stayed minimized.\n")

    def restore_from_tray(self) -> None:
        self.tray_icon.hide()
        self._hidden_to_tray = False
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def capture_output(self) -> None:
        if not self.process or not self.process.stdout:
            return
        for line in self.process.stdout:
            self.output_queue.put(line)
        code = self.process.wait()
        self.output_queue.put(f"Helper exited with code {code}.\n")

    def drain_output(self) -> None:
        while True:
            try:
                line = self.output_queue.get_nowait()
            except queue.Empty:
                break
            self._write_log(line)
            lower = line.lower()
            if "waiting for stake ui jobs" in lower:
                self.status_label.configure(text="Status: waiting for GPT jobs")
            elif "completed job" in lower:
                self.status_label.configure(text="Status: completed job; waiting for next job")
            elif "supabase cleanup" in lower and "exited with code 0" in lower:
                self.status_label.configure(text="Status: cache cleaned")
            elif "supabase cleanup" in lower:
                self.status_label.configure(text="Status: cache cleanup updated")
            elif "helper poll error" in lower:
                self.status_label.configure(text="Status: connection issue; retrying")
            elif "failed job" in lower:
                self.status_label.configure(text="Status: job failed; waiting for next job")
            elif "error" in lower:
                self.status_label.configure(text="Status: helper error")
            elif "exited with code" in lower:
                self.status_label.configure(text="Status: stopped")
        self.root.after(100, self.drain_output)

    def _write_log(self, text: str) -> None:
        self.log.insert(END, text)
        self.log.see(END)


def normalize_color_choice(value, *, fallback: str) -> str:
    if isinstance(value, str):
        text = value.strip()
        if len(text) == 7 and text.startswith("#"):
            try:
                int(text[1:], 16)
            except ValueError:
                return fallback
            return text.upper()
        return fallback

    if not isinstance(value, (list, tuple)) or len(value) < 3:
        return fallback

    components: list[int] = []
    for raw_component in value[:3]:
        try:
            component = int(round(float(raw_component)))
        except (TypeError, ValueError):
            return fallback
        if component < 0 or component > 255:
            return fallback
        components.append(component)

    return "#{:02X}{:02X}{:02X}".format(*components)


def active_color_for(hex_color: str) -> str:
    normalized = normalize_color_choice(hex_color, fallback=HELPER_BUTTON_ACTIVE_BG)
    red = min(255, int(normalized[1:3], 16) + 25)
    green = min(255, int(normalized[3:5], 16) + 25)
    blue = min(255, int(normalized[5:7], 16) + 25)
    return f"#{red:02X}{green:02X}{blue:02X}"


def apply_outline_color(buttons, log, accent: str) -> dict[str, str]:
    normalized = normalize_color_choice(accent, fallback=HELPER_BUTTON_BG)
    active = active_color_for(normalized)
    for button in buttons:
        button.configure(bg=normalized, activebackground=active)
    log.configure(
        highlightbackground=normalized,
        highlightcolor=active,
        selectbackground=active,
    )
    return {"accent": normalized, "activeAccent": active}


def apply_background_color(widgets, log, background: str) -> str:
    normalized = normalize_color_choice(background, fallback=HELPER_BG)
    for widget in widgets:
        widget.configure(bg=normalized)
    log.configure(bg=normalized, insertbackground=HELPER_FG)
    return normalized


def _button_style(
    bg: str = HELPER_BUTTON_BG,
    active_bg: str = HELPER_BUTTON_ACTIVE_BG,
) -> dict[str, str | int]:
    return {
        "bg": bg,
        "fg": HELPER_FG,
        "activebackground": active_bg,
        "activeforeground": HELPER_FG,
        "relief": "flat",
        "borderwidth": 0,
        "highlightthickness": 0,
    }


def create_stake_logo_header(parent) -> Canvas:
    canvas = Canvas(
        parent,
        height=92,
        bg=HELPER_BG,
        bd=0,
        highlightthickness=0,
        relief="flat",
    )

    def redraw(_event=None) -> None:
        canvas.delete("all")
        width = max(canvas.winfo_width(), 1)
        center_x = width // 2
        baseline_y = 45
        font = (LOGO_FONT_FAMILY, 50, "bold italic")

        canvas.create_text(
            center_x + 4,
            baseline_y + 6,
            text="Stake",
            font=font,
            fill="#01020D",
            anchor="center",
        )
        canvas.create_text(
            center_x + 2,
            baseline_y + 3,
            text="Stake",
            font=font,
            fill="#111324",
            anchor="center",
        )
        canvas.create_text(
            center_x,
            baseline_y,
            text="Stake",
            font=font,
            fill="#FFFFFF",
            anchor="center",
        )
        canvas.create_text(
            center_x - 1,
            baseline_y - 1,
            text="Stake",
            font=font,
            fill="#F8F6FF",
            anchor="center",
        )

    canvas.bind("<Configure>", redraw)
    canvas.after(1, redraw)
    return canvas


def main() -> int:
    app = AzpHelperGui()
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
