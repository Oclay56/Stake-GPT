from __future__ import annotations

import ctypes
import json
import math
import os
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

try:
    from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageTk
except Exception:  # pragma: no cover - fallback keeps the helper usable without Pillow.
    Image = None
    ImageChops = None
    ImageDraw = None
    ImageFilter = None
    ImageFont = None
    ImageTk = None


ROOT_DIR = Path(__file__).resolve().parents[1]
WPARAM = ctypes.c_size_t
LPARAM = ctypes.c_ssize_t
LRESULT = ctypes.c_ssize_t
APP_DISPLAY_NAME = "Stake-GPT"
STAKE_LOGO_TEXT = "Stake-GPT"
HELPER_BG = "#101418"
HELPER_FG = "#F4F6F8"
HELPER_MUTED_FG = "#B9C0C8"
HELPER_PANEL_BG = "#171C21"
HELPER_CARD_BG = "#151A1F"
HELPER_CARD_BORDER = "#3A434D"
HELPER_BUTTON_BG = "#1B232C"
HELPER_BUTTON_ACTIVE_BG = "#2B3A48"
HELPER_SUCCESS_FG = "#8CDC83"
HELPER_WARNING_FG = "#FFD166"
HELPER_DANGER_FG = "#FF736B"
LOGO_FONT_FAMILY = "Segoe Script"
LOGO_ANIMATION_INTERVAL_MS = 45
LOGO_ANIMATION_CYCLE_FRAMES = 96
LOGO_CANVAS_HEIGHT = 116
LOGO_REFERENCE_WIDTH_SCALE = 1.18
DEFAULT_COLOR_SETTINGS = {
    "outlineColor": HELPER_BUTTON_BG,
    "backgroundColor": HELPER_BG,
    "borderColor": HELPER_CARD_BORDER,
    "cmdColor": HELPER_FG,
}


def _rounded_polygon_points(
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    radius: int,
) -> list[int]:
    radius = max(1, min(radius, (x2 - x1) // 2, (y2 - y1) // 2))
    return [
        x1 + radius,
        y1,
        x2 - radius,
        y1,
        x2,
        y1,
        x2,
        y1 + radius,
        x2,
        y2 - radius,
        x2,
        y2,
        x2 - radius,
        y2,
        x1 + radius,
        y2,
        x1,
        y2,
        x1,
        y2 - radius,
        x1,
        y1 + radius,
        x1,
        y1,
    ]


def draw_rounded_rect(
    canvas: Canvas,
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    *,
    radius: int,
    fill: str,
    outline: str,
    width: int = 1,
    tags: str = "rounded",
) -> None:
    canvas.create_polygon(
        _rounded_polygon_points(x1, y1, x2, y2, radius),
        smooth=True,
        splinesteps=18,
        fill=fill,
        outline=outline,
        width=width,
        tags=tags,
    )


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


class RoundedCard(Frame):
    def __init__(
        self,
        parent,
        *,
        outer_bg: str,
        card_bg: str = HELPER_CARD_BG,
        border: str = HELPER_CARD_BORDER,
        radius: int = 14,
        content_pad: int = 12,
        height: int | None = None,
    ) -> None:
        super().__init__(parent, bg=outer_bg, bd=0, highlightthickness=0)
        self.outer_bg = outer_bg
        self.card_bg = card_bg
        self.border = border
        self.radius = radius
        self.content_pad = content_pad
        if height is not None:
            self.configure(height=height)
            self.pack_propagate(False)

        self.canvas = Canvas(
            self,
            bg=outer_bg,
            bd=0,
            highlightthickness=0,
            relief="flat",
        )
        self.canvas.pack(fill=BOTH, expand=True)
        self.body = Frame(self.canvas, bg=card_bg, bd=0, highlightthickness=0)
        self._body_window = self.canvas.create_window(
            content_pad,
            content_pad,
            anchor="nw",
            window=self.body,
        )
        self.canvas.bind("<Configure>", self._redraw)

    def configure(self, cnf=None, **kwargs):  # type: ignore[override]
        if cnf:
            kwargs.update(cnf)
        new_bg = kwargs.pop("bg", None)
        new_border = kwargs.pop("border", None)
        result = super().configure(**kwargs)
        if new_bg:
            self.outer_bg = str(new_bg)
            if hasattr(self, "canvas"):
                self.canvas.configure(bg=self.outer_bg)
                self._redraw()
        if new_border:
            self.border = str(new_border)
            if hasattr(self, "canvas"):
                self._redraw()
        return result

    config = configure

    def set_border_color(self, color: str) -> None:
        self.configure(border=color)

    def _redraw(self, _event=None) -> None:
        if not hasattr(self, "canvas"):
            return
        width = max(self.canvas.winfo_width(), 2)
        height = max(self.canvas.winfo_height(), 2)
        self.canvas.delete("card_bg")
        draw_rounded_rect(
            self.canvas,
            1,
            1,
            width - 1,
            height - 1,
            radius=self.radius,
            fill=self.card_bg,
            outline=self.border,
            tags="card_bg",
        )
        pad = self.content_pad
        self.canvas.coords(self._body_window, pad, pad)
        self.canvas.itemconfigure(
            self._body_window,
            width=max(1, width - (pad * 2)),
            height=max(1, height - (pad * 2)),
        )
        self.canvas.tag_lower("card_bg")


class RoundedButton(Canvas):
    def __init__(
        self,
        parent,
        *,
        text: str,
        command: Callable[[], None],
        width: int = 180,
        height: int = 46,
        bg: str = HELPER_BUTTON_BG,
        activebackground: str = HELPER_BUTTON_ACTIVE_BG,
        fg: str = HELPER_FG,
        activeforeground: str = HELPER_FG,
    ) -> None:
        self.command = command
        self.text = text
        self.button_bg = bg
        self.active_bg = activebackground
        self.button_fg = fg
        self.active_fg = activeforeground
        self.outline = HELPER_CARD_BORDER
        self.outer_bg = str(parent.cget("bg") or HELPER_BG)
        self.hovered = False
        super().__init__(
            parent,
            width=width,
            height=height,
            bg=self.outer_bg,
            bd=0,
            highlightthickness=0,
            relief="flat",
            cursor="hand2",
        )
        self.bind("<Configure>", lambda _event: self._draw())
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_click)
        self._draw()

    def configure(self, cnf=None, **kwargs):  # type: ignore[override]
        if cnf:
            kwargs.update(cnf)
        if "text" in kwargs:
            self.text = str(kwargs.pop("text"))
        if "bg" in kwargs:
            self.button_bg = str(kwargs.pop("bg"))
        if "activebackground" in kwargs:
            self.active_bg = str(kwargs.pop("activebackground"))
        if "fg" in kwargs:
            self.button_fg = str(kwargs.pop("fg"))
        if "activeforeground" in kwargs:
            self.active_fg = str(kwargs.pop("activeforeground"))
        if "highlightbackground" in kwargs:
            self.outline = str(kwargs.pop("highlightbackground"))
        if "highlightcolor" in kwargs:
            kwargs.pop("highlightcolor")
        if "outerbackground" in kwargs:
            self.outer_bg = str(kwargs.pop("outerbackground"))
            super().configure(bg=self.outer_bg)
        result = super().configure(**kwargs) if kwargs else None
        if hasattr(self, "button_bg"):
            self._draw()
        return result

    config = configure

    def set_outer_background(self, color: str) -> None:
        self.configure(outerbackground=color)

    def _on_enter(self, _event=None) -> None:
        self.hovered = True
        self._draw()

    def _on_leave(self, _event=None) -> None:
        self.hovered = False
        self._draw()

    def _on_click(self, _event=None) -> None:
        self.command()

    def _draw(self) -> None:
        self.delete("all")
        width = max(self.winfo_width(), int(self.cget("width")), 2)
        height = max(self.winfo_height(), int(self.cget("height")), 2)
        fill = self.active_bg if self.hovered else self.button_bg
        foreground = self.active_fg if self.hovered else self.button_fg
        draw_rounded_rect(
            self,
            1,
            1,
            width - 1,
            height - 1,
            radius=9,
            fill=fill,
            outline=self.outline,
            tags="button_bg",
        )
        self.create_text(
            width // 2,
            height // 2,
            text=self.text,
            fill=foreground,
            font=("Segoe UI", 10, "bold"),
            anchor="center",
        )


class AzpHelperGui:
    def __init__(self) -> None:
        self.root = Tk()
        color_settings = load_helper_color_settings()
        self.root.title(APP_DISPLAY_NAME)
        self.root.geometry("1180x760")
        self.root.minsize(980, 640)
        self.root.configure(bg=color_settings["backgroundColor"])
        self.process: subprocess.Popen[str] | None = None
        self.output_queue: queue.Queue[str] = queue.Queue()
        self._closing = False
        self._hidden_to_tray = False
        self.color_dialog: Toplevel | None = None
        self.helper_bg = color_settings["backgroundColor"]
        self.button_accent_bg = color_settings["outlineColor"]
        self.card_border_color = color_settings["borderColor"]
        self.cmd_text_color = color_settings["cmdColor"]
        self.button_accent_active_bg = HELPER_BUTTON_ACTIVE_BG
        self.control_buttons: list[Button] = []
        self.border_widgets: list = []
        self.cmd_widgets: list = []
        self.background_widgets: list = [self.root]
        self.button_accent_active_bg = active_color_for(self.button_accent_bg)
        self.tray_icon = WindowsTrayIcon(
            APP_DISPLAY_NAME,
            on_restore=lambda: self.root.after(0, self.restore_from_tray),
            on_exit=lambda: self.root.after(0, self.close),
        )

        self.shell = Frame(self.root, bg=self.helper_bg)
        self.shell.pack(fill=BOTH, expand=True, padx=24, pady=(18, 24))
        self.background_widgets.append(self.shell)

        self.logo = create_stake_logo_header(self.shell, background=self.helper_bg)
        self.logo.pack(fill="x", pady=(8, 0))
        self.background_widgets.append(self.logo)

        self.description_label = Label(
            self.shell,
            text=(
                "Review Mode reads Stake UI boards. Build Mode can click exact "
                "validated legs into the slip for review only."
            ),
            font=("Segoe UI", 12),
            wraplength=900,
            bg=self.helper_bg,
            fg=HELPER_MUTED_FG,
        )
        self.description_label.pack(pady=(0, 22))
        self.background_widgets.append(self.description_label)

        self.controls = Frame(self.shell, bg=self.helper_bg)
        self.controls.pack(fill="x", pady=(0, 18))
        self.background_widgets.append(self.controls)

        review_button = RoundedButton(
            self.controls,
            text=">  Start Review Mode",
            command=lambda: self.start_helper("review"),
            width=230,
            bg=self.button_accent_bg,
            activebackground=self.button_accent_active_bg,
        )
        review_button.pack(side=LEFT, fill="x", expand=True, padx=(0, 8))
        self.control_buttons.append(review_button)

        build_button = RoundedButton(
            self.controls,
            text="::  Start Build Slip Mode",
            command=lambda: self.start_helper("build"),
            width=240,
            bg=self.button_accent_bg,
            activebackground=self.button_accent_active_bg,
        )
        build_button.pack(side=LEFT, fill="x", expand=True, padx=(8, 8))
        self.control_buttons.append(build_button)

        setup_button = RoundedButton(
            self.controls,
            text="Setup Check",
            command=self.run_setup_check,
            width=170,
            bg=self.button_accent_bg,
            activebackground=self.button_accent_active_bg,
        )
        setup_button.pack(side=LEFT, fill="x", expand=True, padx=(8, 8))
        self.control_buttons.append(setup_button)

        cache_button = RoundedButton(
            self.controls,
            text="Clean Cache",
            command=self.run_cache_cleanup,
            width=150,
            bg=self.button_accent_bg,
            activebackground=self.button_accent_active_bg,
        )
        cache_button.pack(side=LEFT, fill="x", expand=True, padx=(8, 8))
        self.control_buttons.append(cache_button)

        color_button = RoundedButton(
            self.controls,
            text="Color",
            command=self.choose_button_color,
            width=110,
            bg=self.button_accent_bg,
            activebackground=self.button_accent_active_bg,
        )
        color_button.pack(side=LEFT, fill="x", expand=True, padx=(8, 8))
        self.control_buttons.append(color_button)

        stop_button = RoundedButton(
            self.controls,
            text="[]  Stop",
            command=self.stop_helper,
            width=110,
            bg=self.button_accent_bg,
            activebackground=self.button_accent_active_bg,
            fg=HELPER_DANGER_FG,
            activeforeground=HELPER_DANGER_FG,
        )
        stop_button.pack(side=LEFT, fill="x", expand=False, padx=(8, 0))
        self.control_buttons.append(stop_button)

        status_card = self._create_card(self.shell, height=74)
        status_card.pack(fill="x", pady=(0, 18))
        status_dot = Canvas(
            status_card.body,
            width=18,
            height=18,
            bg=HELPER_CARD_BG,
            highlightthickness=0,
            bd=0,
        )
        status_dot.pack(side=LEFT, padx=(12, 18), pady=16)
        self.status_dot = status_dot
        self.status_dot_item = status_dot.create_oval(
            2,
            2,
            16,
            16,
            fill=HELPER_SUCCESS_FG,
            outline=HELPER_SUCCESS_FG,
        )
        Label(
            status_card.body,
            text="Status:",
            anchor="w",
            font=("Segoe UI", 11, "bold"),
            bg=HELPER_CARD_BG,
            fg=HELPER_MUTED_FG,
        ).pack(side=LEFT, pady=14)
        self.status_label = Label(
            status_card.body,
            text="idle",
            anchor="w",
            font=("Segoe UI", 11),
            bg=HELPER_CARD_BG,
            fg=HELPER_FG,
        )
        self.status_label.pack(side=LEFT, fill="x", expand=True, padx=(20, 12), pady=14)

        log_card = self._create_card(self.shell)
        log_card.pack(fill=BOTH, expand=True)
        log_header = Frame(log_card.body, bg=HELPER_CARD_BG)
        log_header.pack(fill="x", padx=24, pady=(18, 8))
        self.log_icon_label = Label(
            log_header,
            text=">_",
            width=3,
            font=("Consolas", 14, "bold"),
            bg=HELPER_CARD_BG,
            fg=self.cmd_text_color,
        )
        self.log_icon_label.pack(side=LEFT)
        self.cmd_widgets.append(self.log_icon_label)
        self.log_title_label = Label(
            log_header,
            text="Stake-GPT setup check",
            anchor="w",
            font=("Segoe UI", 12, "bold"),
            bg=HELPER_CARD_BG,
            fg=self.cmd_text_color,
        )
        self.log_title_label.pack(side=LEFT, fill="x", expand=True, padx=(10, 0))
        self.cmd_widgets.append(self.log_title_label)
        self.log_divider = Frame(log_card.body, height=1, bg=self.card_border_color)
        self.log_divider.pack(
            fill="x",
            padx=24,
            pady=(0, 10),
        )
        self.border_widgets.append(self.log_divider)
        self.log = Text(
            log_card.body,
            height=16,
            wrap="word",
            font=("Consolas", 11),
            bg=self.helper_bg if self.helper_bg != HELPER_BG else HELPER_PANEL_BG,
            fg=self.cmd_text_color,
            insertbackground=self.cmd_text_color,
            selectbackground=self.button_accent_active_bg,
            selectforeground=self.cmd_text_color,
            relief="flat",
            borderwidth=0,
            highlightthickness=1,
            highlightbackground=self.button_accent_bg,
            highlightcolor=self.button_accent_active_bg,
        )
        self.log.pack(fill=BOTH, expand=True, padx=24, pady=(0, 24))
        self._write_log(format_setup_report(check_local_helper_setup(ROOT_DIR)) + "\n\n")

        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.bind("<Unmap>", self.on_unmap)
        self.root.after(100, self.drain_output)

    def _create_card(self, parent, *, height: int | None = None) -> RoundedCard:
        card = RoundedCard(
            parent,
            outer_bg=self.helper_bg,
            height=height,
        )
        self.background_widgets.append(card)
        self.border_widgets.append(card)
        return card

    def _set_status(self, text: str, *, stopped: bool = False) -> None:
        self.status_label.configure(text=text)
        color = HELPER_DANGER_FG if stopped else HELPER_SUCCESS_FG
        self.status_dot.itemconfigure(self.status_dot_item, fill=color, outline=color)

    def run(self) -> None:
        self.root.mainloop()

    def choose_button_color(self) -> None:
        if self.color_dialog is not None:
            try:
                if self.color_dialog.winfo_exists():
                    self._close_color_dialog()
                    return
            except Exception:
                pass
            self.color_dialog = None

        dialog = Toplevel(self.root)
        self.color_dialog = dialog
        dialog.title("Helper Color")
        dialog.configure(bg=self.helper_bg)
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.protocol("WM_DELETE_WINDOW", self._close_color_dialog)

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
        ).pack(side=LEFT, padx=(0, 8))
        Button(
            body,
            text="Border",
            width=12,
            command=lambda: self._choose_helper_color("border", dialog),
            **_button_style(self.button_accent_bg, self.button_accent_active_bg),
        ).pack(side=LEFT, padx=(0, 8))
        Button(
            body,
            text="CMD",
            width=12,
            command=lambda: self._choose_helper_color("cmd", dialog),
            **_button_style(self.button_accent_bg, self.button_accent_active_bg),
        ).pack(side=LEFT)

        dialog.focus_set()

    def _close_color_dialog(self) -> None:
        dialog = self.color_dialog
        self.color_dialog = None
        if dialog is None:
            return
        try:
            if dialog.winfo_exists():
                dialog.destroy()
        except Exception:
            return

    def _choose_helper_color(self, target: str, dialog: Toplevel | None = None) -> None:
        current_color = {
            "background": self.helper_bg,
            "border": self.card_border_color,
            "cmd": self.cmd_text_color,
            "outline": self.button_accent_bg,
        }.get(target, self.button_accent_bg)
        rgb_value, hex_value = colorchooser.askcolor(
            color=current_color,
            title=f"Helper {target.title()} Color",
        )
        accent = normalize_color_choice(rgb_value, fallback=str(hex_value or ""))
        if not accent:
            return

        if target == "background":
            self.helper_bg = apply_background_color(self.background_widgets, self.log, accent)
            for button in self.control_buttons:
                if hasattr(button, "set_outer_background"):
                    button.set_outer_background(self.helper_bg)
            apply_cmd_color(self.cmd_widgets, self.log, self.cmd_text_color)
            self._save_helper_color_settings()
            self._write_log(f"Helper background color set to {self.helper_bg}.\n")
        elif target == "border":
            self.card_border_color = apply_border_color(self.border_widgets, accent)
            self._save_helper_color_settings()
            self._write_log(f"Helper border color set to {self.card_border_color}.\n")
        elif target == "cmd":
            self.cmd_text_color = apply_cmd_color(self.cmd_widgets, self.log, accent)
            self._save_helper_color_settings()
            self._write_log(f"Helper CMD color set to {self.cmd_text_color}.\n")
        else:
            outline = apply_outline_color(self.control_buttons, self.log, accent)
            self.button_accent_bg = outline["accent"]
            self.button_accent_active_bg = outline["activeAccent"]
            self._save_helper_color_settings()
            self._write_log(f"Helper outline color set to {self.button_accent_bg}.\n")

        if dialog:
            try:
                if dialog.winfo_exists():
                    dialog.lift()
                    dialog.focus_set()
            except Exception:
                pass

    def _save_helper_color_settings(self) -> None:
        save_helper_color_settings(
            {
                "outlineColor": self.button_accent_bg,
                "backgroundColor": self.helper_bg,
                "borderColor": self.card_border_color,
                "cmdColor": self.cmd_text_color,
            }
        )

    def start_helper(self, mode: str) -> None:
        if self.process and self.process.poll() is None:
            messagebox.showinfo(APP_DISPLAY_NAME, "Helper is already running.")
            return

        python_exe = ROOT_DIR / ".venv" / "Scripts" / "python.exe"
        setup_report = check_local_helper_setup(ROOT_DIR)
        if not setup_report["ok"]:
            report_text = format_setup_report(setup_report)
            self._write_log(report_text + "\n\n")
            messagebox.showerror(
                APP_DISPLAY_NAME,
                report_text,
            )
            return

        self._set_status(f"starting {mode} mode...")
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
        self._set_status("setup ready" if "Ready." in report_text else "setup needs attention")

    def run_cache_cleanup(self) -> None:
        python_exe = ROOT_DIR / ".venv" / "Scripts" / "python.exe"
        if not python_exe.exists():
            messagebox.showerror(
                APP_DISPLAY_NAME,
                f"Could not find {python_exe}. Run the project setup first.",
            )
            return
        if not (ROOT_DIR / ".env").exists():
            messagebox.showerror(
                APP_DISPLAY_NAME,
                f"Could not find {ROOT_DIR / '.env'}. The cleanup needs Supabase settings.",
            )
            return

        self._set_status("cleaning Supabase cache...")
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
            self._set_status("idle", stopped=True)
            self._write_log("Helper is not running.\n")
            return

        self._write_log("Stopping helper...\n")
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
        self._set_status("stopped", stopped=True)
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
                self._set_status("waiting for GPT jobs")
            elif "completed job" in lower:
                self._set_status("completed job; waiting for next job")
            elif "supabase cleanup" in lower and "exited with code 0" in lower:
                self._set_status("cache cleaned")
            elif "supabase cleanup" in lower:
                self._set_status("cache cleanup updated")
            elif "helper poll error" in lower:
                self._set_status("connection issue; retrying")
            elif "failed job" in lower:
                self._set_status("job failed; waiting for next job")
            elif "error" in lower:
                self._set_status("helper error")
            elif "exited with code" in lower:
                self._set_status("stopped", stopped=True)
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


def helper_color_settings_path(
    env: dict[str, str] | None = None,
    *,
    home: Path | None = None,
) -> Path:
    source_env = os.environ if env is None else env
    base = (
        source_env.get("APPDATA")
        or source_env.get("LOCALAPPDATA")
        or str((home or Path.home()) / ".stake-gpt-helper")
    )
    return Path(base) / "Stake-GPT Helper" / "settings.json"


def load_helper_color_settings(path: Path | None = None) -> dict[str, str]:
    settings_path = path or helper_color_settings_path()
    settings = dict(DEFAULT_COLOR_SETTINGS)
    try:
        raw = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return settings
    if not isinstance(raw, dict):
        return settings

    for key, default in DEFAULT_COLOR_SETTINGS.items():
        settings[key] = normalize_color_choice(raw.get(key), fallback=default)
    return settings


def save_helper_color_settings(
    settings: dict[str, str],
    path: Path | None = None,
) -> Path:
    settings_path = path or helper_color_settings_path()
    cleaned = {
        key: normalize_color_choice(settings.get(key), fallback=default)
        for key, default in DEFAULT_COLOR_SETTINGS.items()
    }
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        json.dumps(cleaned, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return settings_path


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
        button.configure(
            bg=normalized,
            activebackground=active,
            highlightbackground=normalized,
            highlightcolor=active,
        )
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


def apply_border_color(widgets, border: str) -> str:
    normalized = normalize_color_choice(border, fallback=HELPER_CARD_BORDER)
    for widget in widgets:
        if hasattr(widget, "set_border_color"):
            widget.set_border_color(normalized)
        else:
            widget.configure(bg=normalized)
    return normalized


def apply_cmd_color(widgets, log, color: str) -> str:
    normalized = normalize_color_choice(color, fallback=HELPER_FG)
    for widget in widgets:
        widget.configure(fg=normalized)
    log.configure(
        fg=normalized,
        insertbackground=normalized,
        selectforeground=normalized,
    )
    return normalized


def logo_fill_fraction(frame: int, *, cycle_frames: int = LOGO_ANIMATION_CYCLE_FRAMES) -> float:
    cycle = max(2, int(cycle_frames))
    phase = (int(frame) % cycle) / (cycle / 2)
    if phase <= 1:
        return round(phase, 4)
    return round(max(0.0, 2 - phase), 4)


def logo_liquid_fill_polygon(
    width: int,
    height: int,
    fill_fraction: float,
    *,
    frame: int,
    amplitude: int = 7,
) -> list[tuple[int, int]]:
    width = max(1, int(width))
    height = max(1, int(height))
    fraction = max(0.0, min(1.0, float(fill_fraction)))
    base_y = height * (1 - fraction)
    step = max(8, width // 6)
    points: list[tuple[int, int]] = []
    for x in range(0, width + step, step):
        clipped_x = min(x, width)
        wave = math.sin((clipped_x / 34) + (frame * 0.22)) * amplitude
        y = round(max(0, min(height, base_y + wave)))
        points.append((clipped_x, y))
    points.extend([(width, height), (0, height)])
    return points


def complete_gpt_t_mask(mask: Any) -> None:
    """Fill out the left side of the script T in the fixed Stake-GPT wordmark."""
    if ImageDraw is None or STAKE_LOGO_TEXT != "Stake-GPT":
        return

    draw = ImageDraw.Draw(mask)
    width, height = mask.size
    stroke = max(5, round(height * 0.09))
    start = (round(width * 0.79), round(height * 0.30))
    end = (round(width * 0.965), round(height * 0.22))
    draw.line([start, end], fill=255, width=stroke)
    radius = stroke // 2
    for x, y in (start, end):
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=255)


def render_stake_logo_frame(
    *,
    width: int,
    height: int = LOGO_CANVAS_HEIGHT,
    background: str,
    frame: int,
) -> Any:
    if (
        Image is None
        or ImageChops is None
        or ImageDraw is None
        or ImageFilter is None
        or ImageFont is None
    ):
        raise RuntimeError("Pillow is required to render animated logo frames.")

    width = max(280, int(width))
    height = max(80, int(height))
    background = normalize_color_choice(background, fallback=HELPER_BG)
    image = Image.new("RGBA", (width, height), background)
    draw = ImageDraw.Draw(image)
    font = _stake_logo_font(width, height)
    bbox = draw.textbbox((0, 0), STAKE_LOGO_TEXT, font=font)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]

    text_mask = Image.new("L", (width, height), 0)
    raw_padding = 12
    raw_mask = Image.new(
        "L",
        (text_width + raw_padding * 2, text_height + raw_padding * 2),
        0,
    )
    ImageDraw.Draw(raw_mask).text(
        (raw_padding - bbox[0], raw_padding - bbox[1]),
        STAKE_LOGO_TEXT,
        font=font,
        fill=255,
    )
    target_width = min(width - 34, round(raw_mask.width * LOGO_REFERENCE_WIDTH_SCALE))
    target_height = raw_mask.height
    resampling = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
    shaped_mask = raw_mask.resize((target_width, target_height), resampling)
    complete_gpt_t_mask(shaped_mask)
    text_x = (width - target_width) // 2
    text_y = (height - target_height) // 2 - 6
    text_mask.paste(shaped_mask, (text_x, text_y))
    shadow_mask = Image.new("L", (width, height), 0)
    shadow_mask.paste(text_mask, (4, 7))
    shadow_mask = shadow_mask.filter(ImageFilter.GaussianBlur(1.25))
    shadow = Image.new("RGBA", (width, height), (0, 0, 0, 160))
    image.paste(shadow, (0, 0), shadow_mask)

    base = Image.new("RGBA", (width, height), (178, 182, 190, 255))
    image.paste(base, (0, 0), text_mask)

    liquid_mask = Image.new("L", (width, height), 0)
    ImageDraw.Draw(liquid_mask).polygon(
        logo_liquid_fill_polygon(
            width,
            height,
            logo_fill_fraction(frame),
            frame=frame,
        ),
        fill=255,
    )
    fill_mask = ImageChops.multiply(text_mask, liquid_mask)
    fill = Image.new("RGBA", (width, height), (248, 246, 255, 255))
    image.paste(fill, (0, 0), fill_mask)
    return image


def _stake_logo_font(width: int, height: int) -> Any:
    if Image is None or ImageDraw is None or ImageFont is None:
        raise RuntimeError("Pillow is required to load logo fonts.")

    font_paths = [
        Path(os.environ.get("WINDIR", "C:\\Windows")) / "Fonts" / "BRUSHSCI.TTF",
        Path(os.environ.get("WINDIR", "C:\\Windows")) / "Fonts" / "segoescb.ttf",
        Path(os.environ.get("WINDIR", "C:\\Windows")) / "Fonts" / "segoesc.ttf",
        Path(os.environ.get("WINDIR", "C:\\Windows")) / "Fonts" / "ariali.ttf",
    ]
    for size in range(min(98, height - 12), 34, -2):
        for font_path in font_paths:
            if not font_path.exists():
                continue
            try:
                font = ImageFont.truetype(str(font_path), size=size)
            except OSError:
                continue
            bbox = ImageDraw.Draw(Image.new("RGB", (1, 1))).textbbox(
                (0, 0),
                STAKE_LOGO_TEXT,
                font=font,
            )
            text_width = bbox[2] - bbox[0]
            text_height = bbox[3] - bbox[1]
            scaled_width = (text_width + 24) * LOGO_REFERENCE_WIDTH_SCALE
            if scaled_width <= width - 34 and text_height <= height - 10:
                return font
    return ImageFont.load_default()


def _button_style(
    bg: str = HELPER_BUTTON_BG,
    active_bg: str = HELPER_BUTTON_ACTIVE_BG,
) -> dict[str, object]:
    return {
        "bg": bg,
        "fg": HELPER_FG,
        "activebackground": active_bg,
        "activeforeground": HELPER_FG,
        "relief": "flat",
        "font": ("Segoe UI", 10, "bold"),
        "borderwidth": 0,
        "highlightthickness": 1,
        "highlightbackground": HELPER_CARD_BORDER,
        "highlightcolor": active_bg,
    }


def create_stake_logo_header(parent, *, background: str = HELPER_BG) -> Canvas:
    canvas = Canvas(
        parent,
        height=LOGO_CANVAS_HEIGHT,
        bg=background,
        bd=0,
        highlightthickness=0,
        relief="flat",
    )
    animation_state = {"frame": 0, "started": False}

    def redraw(_event=None, *, advance: bool = False) -> None:
        if advance:
            animation_state["frame"] += 1
        canvas.delete("all")
        width = max(canvas.winfo_width(), 1)
        background_color = str(canvas.cget("bg") or background)
        if ImageTk is not None:
            frame_image = render_stake_logo_frame(
                width=width,
                height=LOGO_CANVAS_HEIGHT,
                background=background_color,
                frame=animation_state["frame"],
            )
            photo = ImageTk.PhotoImage(frame_image)
            canvas._logo_photo = photo
            canvas.create_image(0, 0, image=photo, anchor="nw")
            return

        center_x = width // 2
        baseline_y = 54
        font = (LOGO_FONT_FAMILY, 58, "bold italic")

        canvas.create_text(
            center_x + 4,
            baseline_y + 6,
            text=STAKE_LOGO_TEXT,
            font=font,
            fill="#01020D",
            anchor="center",
        )
        canvas.create_text(
            center_x + 2,
            baseline_y + 3,
            text=STAKE_LOGO_TEXT,
            font=font,
            fill="#111324",
            anchor="center",
        )
        canvas.create_text(
            center_x,
            baseline_y,
            text=STAKE_LOGO_TEXT,
            font=font,
            fill="#F8F6FF",
            anchor="center",
        )

    def animate() -> None:
        try:
            redraw(advance=True)
            canvas.after(LOGO_ANIMATION_INTERVAL_MS, animate)
        except Exception:
            return

    canvas.bind("<Configure>", redraw)
    canvas.after(1, redraw)
    if not animation_state["started"]:
        animation_state["started"] = True
        canvas.after(LOGO_ANIMATION_INTERVAL_MS, animate)
    return canvas


def main() -> int:
    app = AzpHelperGui()
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
