"""
AlphaHunt — One-Click Launcher  |  alphahunt.in
Double-click this file to start the dashboard.
Requires Python 3.10+  (python.org — tick "Add to PATH")
"""

import sys, os, subprocess, threading, time, webbrowser, socket, tkinter as tk
from tkinter import font as tkfont

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REQS     = os.path.join(BASE_DIR, "requirements.txt")
HOST     = "127.0.0.1"
PORT     = 8000
APP_URL  = f"http://{HOST}:{PORT}/app"
MAX_LOG  = 600

# ── colours (Deep Ocean) ─────────────────────────────────────────────────────
BG   = "#020b18"; SURF  = "#061220"; SURF2 = "#0b1d30"
ACC  = "#0ea5e9"; ACC2  = "#38bdf8"; RED   = "#f43f5e"
YEL  = "#f59e0b"; TXT   = "#f0f6ff"; MUTED = "#8dafc8"

# ── Windows flag: suppress console window ────────────────────────────────────
_CFLAGS = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _port_free(host, port):
    """Return True if nothing is listening on host:port."""
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return False
    except OSError:
        return True


def _kill_tree(proc):
    """Kill a subprocess and ALL its children (handles uvicorn reloader)."""
    if proc is None or proc.poll() is not None:
        return
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, creationflags=_CFLAGS
            )
        except Exception:
            pass
    else:
        import signal, os as _os
        try:
            _os.killpg(_os.getpgid(proc.pid), signal.SIGTERM)
        except Exception:
            try: proc.terminate()
            except Exception: pass
    try:
        proc.wait(timeout=5)
    except Exception:
        try: proc.kill()
        except Exception: pass


class Launcher(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("AlphaHunt")
        self.configure(bg=BG)
        self.resizable(False, False)
        self._center(580, 500)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._server    = None
        self._phase     = "idle"   # idle | installing | running | stopping
        self._ready_evt = threading.Event()

        self._build_ui()
        self._log("AlphaHunt ready.", ACC2)
        self._log(f"Dir: {BASE_DIR}", MUTED)
        self._log("Press  ▶ Start  to launch.", MUTED)

    # ── UI ───────────────────────────────────────────────────────────────────
    def _build_ui(self):
        hf = tkfont.Font(family="Segoe UI", size=16, weight="bold")
        sf = tkfont.Font(family="Segoe UI", size=10)
        lf = tkfont.Font(family="Consolas",  size=9)
        xf = tkfont.Font(family="Segoe UI", size=8)

        # header
        hdr = tk.Frame(self, bg=SURF, pady=14)
        hdr.pack(fill="x")
        tk.Label(hdr, text="Alpha",  font=hf, bg=SURF, fg=TXT).pack(side="left", padx=(20, 0))
        tk.Label(hdr, text="Hunt",   font=hf, bg=SURF, fg=ACC2).pack(side="left")
        tk.Label(hdr, text=" .in",   font=hf, bg=SURF, fg=MUTED).pack(side="left")
        self._badge = tk.Label(hdr, text="● OFFLINE", font=sf,
                               bg=SURF2, fg=RED, padx=10, pady=4)
        self._badge.pack(side="right", padx=20)

        # log
        lf_wrap = tk.Frame(self, bg=BG, padx=12, pady=8)
        lf_wrap.pack(fill="both", expand=True)
        self._log_box = tk.Text(lf_wrap, bg=SURF, fg=TXT, font=lf,
                                relief="flat", bd=0, state="disabled",
                                wrap="word", height=18, cursor="arrow",
                                insertbackground=TXT, selectbackground=SURF2)
        sb = tk.Scrollbar(lf_wrap, command=self._log_box.yview,
                          bg=SURF2, troughcolor=BG, relief="flat")
        self._log_box.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self._log_box.pack(fill="both", expand=True)
        for tag, fg in [("acc", ACC2), ("muted", MUTED), ("warn", YEL), ("err", RED), ("plain", TXT)]:
            self._log_box.tag_config(tag, foreground=fg)

        # buttons
        br = tk.Frame(self, bg=BG, pady=10)
        br.pack(fill="x", padx=12)
        self._btn_start = self._mk_btn(br, "▶  Start",           ACC,   "#fff", self._on_start)
        self._btn_open  = self._mk_btn(br, "🌐  Open Dashboard",  SURF2, ACC2,  self._on_open, state="disabled")
        self._btn_stop  = self._mk_btn(br, "■  Stop",             RED,   "#fff", self._on_stop,  state="disabled")
        for b in (self._btn_start, self._btn_open, self._btn_stop):
            b.pack(side="left", padx=(0, 8))

        tk.Label(self, text="Requires Python 3.10+  ·  Packages installed automatically on first run",
                 font=xf, bg=BG, fg=MUTED).pack(pady=(0, 10))

    def _mk_btn(self, p, text, bg, fg, cmd, state="normal"):
        return tk.Button(p, text=text, bg=bg, fg=fg, relief="flat",
                         padx=14, pady=8, cursor="hand2",
                         font=tkfont.Font(family="Segoe UI", size=10, weight="bold"),
                         activebackground=SURF2, activeforeground=fg,
                         command=cmd, bd=0, state=state)

    def _center(self, w, h):
        self.update_idletasks()
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        self.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}")

    # ── logging ──────────────────────────────────────────────────────────────
    def _log(self, msg, colour=TXT):
        tag = {ACC: "acc", ACC2: "acc", MUTED: "muted", YEL: "warn", RED: "err"}.get(colour, "plain")
        self._log_box.configure(state="normal")
        lines = int(self._log_box.index("end-1c").split(".")[0])
        if lines > MAX_LOG:
            self._log_box.delete("1.0", f"{lines - MAX_LOG}.0")
        ts = time.strftime("%H:%M:%S")
        self._log_box.insert("end", f"[{ts}]  {msg}\n", tag)
        self._log_box.see("end")
        self._log_box.configure(state="disabled")

    def _log_safe(self, msg, colour=TXT):
        self.after(0, self._log, msg, colour)

    # ── button handlers ──────────────────────────────────────────────────────
    def _on_start(self):
        if self._phase != "idle":
            return
        self._phase = "installing"
        self._btn_start.config(state="disabled", text="⏳ Starting…")
        self._ready_evt.clear()
        threading.Thread(target=self._launch, daemon=True).start()

    def _on_open(self):
        webbrowser.open(APP_URL)

    def _on_stop(self):
        if self._phase != "running":
            return
        self._phase = "stopping"
        self._log("Stopping server…", YEL)
        self._btn_stop.config(state="disabled", text="Stopping…")
        threading.Thread(target=self._do_stop, daemon=True).start()

    def _on_close(self):
        _kill_tree(self._server)
        self.destroy()

    # ── launch flow ──────────────────────────────────────────────────────────
    def _launch(self):
        # 1 — check port
        if not _port_free(HOST, PORT):
            self._log_safe(f"Port {PORT} is already in use — another server may be running.", YEL)
            self._log_safe(f"Opening {APP_URL} directly.", ACC2)
            self._phase = "running"
            self.after(0, self._set_running)
            webbrowser.open(APP_URL)
            return

        # 2 — install requirements
        self._log_safe("Installing / verifying packages…", MUTED)
        try:
            r = subprocess.run(
                [sys.executable, "-m", "pip", "install", "-q", "-r", REQS],
                cwd=BASE_DIR, capture_output=True, text=True,
                creationflags=_CFLAGS
            )
            if r.returncode != 0:
                err = (r.stderr or r.stdout or "").strip()
                for ln in err.splitlines()[:10]:
                    self._log_safe("  pip: " + ln, YEL)
            else:
                self._log_safe("Packages OK.", ACC2)
        except Exception as e:
            self._log_safe(f"pip error: {e}", RED)

        # 3 — start uvicorn (no --reload: avoids child-process pipe issues)
        self._log_safe(f"Starting server → {APP_URL}", ACC2)
        try:
            kwargs = dict(
                cwd=BASE_DIR,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                creationflags=_CFLAGS,
            )
            # On POSIX: start in a new process group so we can kill the whole tree
            if sys.platform != "win32":
                kwargs["start_new_session"] = True

            self._server = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "app:app",
                 "--host", HOST, "--port", str(PORT)],
                **kwargs
            )
        except FileNotFoundError:
            self._log_safe("uvicorn not found — pip install may have failed.", RED)
            self.after(0, self._set_idle)
            return
        except Exception as e:
            self._log_safe(f"Launch error: {e}", RED)
            self.after(0, self._set_idle)
            return

        # 4 — stream output; _stream_output sets _ready_evt on "Uvicorn running"
        threading.Thread(target=self._stream_output, daemon=True).start()

        # 5 — wait up to 60 s for ready signal
        self._log_safe("Waiting for server to be ready…", MUTED)
        ready = self._ready_evt.wait(timeout=60)

        # Check process is still alive
        if self._server.poll() is not None:
            self._log_safe("Server process exited — check errors above.", RED)
            self.after(0, self._set_idle)
            return

        if not ready:
            self._log_safe("Startup taking longer than expected — opening browser anyway.", YEL)

        # 6 — online
        self._phase = "running"
        self.after(0, self._set_running)
        time.sleep(0.3)
        webbrowser.open(APP_URL)
        self._log_safe("Dashboard is live — browser opened.", ACC2)

    def _stream_output(self):
        """Read uvicorn stdout/stderr line-by-line and push to the log widget."""
        if not self._server:
            return
        ready_phrases = ("uvicorn running", "application startup complete", "started server process")
        for line in self._server.stdout:
            line = line.rstrip()
            if not line:
                continue
            lo = line.lower()
            if any(w in lo for w in ("error", "traceback", "exception", "importerror", "modulenotfounderror")):
                colour = RED
            elif any(w in lo for w in ("warning", "warn", "deprecated")):
                colour = YEL
            elif any(p in lo for p in ready_phrases):
                colour = ACC2
                self._ready_evt.set()   # ← unblock launch thread
            else:
                colour = MUTED
            self._log_safe(line, colour)

        # stdout closed → server exited
        if self._phase == "running":
            self._log_safe("Server stopped unexpectedly.", YEL)
            self.after(0, self._set_idle)

    # ── server teardown ───────────────────────────────────────────────────────
    def _do_stop(self):
        _kill_tree(self._server)
        self._server = None
        self._log_safe("Server stopped.", MUTED)
        self.after(0, self._set_idle)

    # ── UI state helpers ──────────────────────────────────────────────────────
    def _set_running(self):
        self._badge.config(text="● LIVE", fg=ACC)
        self._btn_start.config(state="disabled", text="▶  Start")
        self._btn_open.config(state="normal")
        self._btn_stop.config(state="normal",  text="■  Stop")

    def _set_idle(self):
        self._phase = "idle"
        self._badge.config(text="● OFFLINE", fg=RED)
        self._btn_start.config(state="normal",   text="▶  Start")
        self._btn_open.config(state="disabled")
        self._btn_stop.config(state="disabled",  text="■  Stop")


if __name__ == "__main__":
    Launcher().mainloop()
