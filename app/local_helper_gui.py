from __future__ import annotations

import queue
import subprocess
import sys
import threading
from pathlib import Path
from tkinter import BOTH, END, LEFT, RIGHT, Button, Frame, Label, Tk, Text, messagebox


ROOT_DIR = Path(__file__).resolve().parents[1]


class AzpHelperGui:
    def __init__(self) -> None:
        self.root = Tk()
        self.root.title("AZP Local Helper")
        self.root.geometry("720x440")
        self.root.minsize(620, 360)
        self.process: subprocess.Popen[str] | None = None
        self.output_queue: queue.Queue[str] = queue.Queue()

        Label(
            self.root,
            text="AZP Local Helper",
            font=("Segoe UI", 16, "bold"),
        ).pack(pady=(14, 4))
        Label(
            self.root,
            text=(
                "Review Mode reads Stake UI boards. Build Mode can click exact "
                "validated legs into the slip for review only."
            ),
            font=("Segoe UI", 10),
            wraplength=660,
        ).pack(pady=(0, 12))

        controls = Frame(self.root)
        controls.pack(fill="x", padx=16, pady=(0, 10))

        Button(
            controls,
            text="Start Review Mode",
            command=lambda: self.start_helper("review"),
            width=22,
        ).pack(side=LEFT, padx=(0, 8))
        Button(
            controls,
            text="Start Build Slip Mode",
            command=lambda: self.start_helper("build"),
            width=22,
        ).pack(side=LEFT, padx=(0, 8))
        Button(
            controls,
            text="Stop Helper",
            command=self.stop_helper,
            width=16,
        ).pack(side=RIGHT)

        self.status_label = Label(
            self.root,
            text="Status: idle",
            anchor="w",
            font=("Segoe UI", 10, "bold"),
        )
        self.status_label.pack(fill="x", padx=16)

        self.log = Text(self.root, height=16, wrap="word", font=("Consolas", 10))
        self.log.pack(fill=BOTH, expand=True, padx=16, pady=(8, 16))
        self._write_log("Pick a mode. Close this window when you are done.\n")
        self._write_log("Build Mode never enters a stake amount and never clicks Place Bet.\n\n")

        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(100, self.drain_output)

    def run(self) -> None:
        self.root.mainloop()

    def start_helper(self, mode: str) -> None:
        if self.process and self.process.poll() is None:
            messagebox.showinfo("AZP Local Helper", "Helper is already running.")
            return

        python_exe = ROOT_DIR / ".venv" / "Scripts" / "python.exe"
        if not python_exe.exists():
            messagebox.showerror(
                "AZP Local Helper",
                f"Could not find {python_exe}. Run the project setup first.",
            )
            return
        if not (ROOT_DIR / ".env").exists():
            messagebox.showerror(
                "AZP Local Helper",
                f"Could not find {ROOT_DIR / '.env'}. The helper needs Supabase settings.",
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
        self.stop_helper()
        self.root.destroy()

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
            elif "failed job" in lower or "error" in lower:
                self.status_label.configure(text="Status: helper error")
            elif "exited with code" in lower:
                self.status_label.configure(text="Status: stopped")
        self.root.after(100, self.drain_output)

    def _write_log(self, text: str) -> None:
        self.log.insert(END, text)
        self.log.see(END)


def main() -> int:
    app = AzpHelperGui()
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
