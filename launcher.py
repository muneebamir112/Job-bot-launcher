"""
Job Bot Launcher

A small GUI with three panels:
  - "Job Scraper" ("Scrape All Jobs") runs, for every (job title, location)
    row in Sheet2: GlassD/glassdoor_scraper_final.py, then
    Hiring_cafe/scraper.py, then Jobgether/jobgether_scraper.py, in that
    order, before moving to the next row. Each scraper launches its own
    stealth (patchright) browser, filters to Remote-only / posted within the
    last 24 hours, skips links that land on a CAPTCHA/verification page, and
    pushes real job links into Sheet1 as clickable HYPERLINK() cells.
  - "Generate Resume" runs resume-bot/ollama_generate.py with a job description
    (auto-fetched from a pasted job posting URL) and a company name, which
    tailors and renders a .docx resume.
  - "Start Applying" runs Job-Bot/main.py, which reads pending job links from
    the Google Sheet and applies to them.

Each bot is launched as an independent subprocess and they can all run at the
same time. None of the bots' own source code is modified by this launcher.
"""

import os
import queue
import re
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import scrolledtext, ttk

import gspread
from google.oauth2.service_account import Credentials

GLASSD_DIR = r"C:\Users\Muneeb\Desktop\WebNcodes\scraper\GlassD"
HIRINGCAFE_DIR = r"C:\Users\Muneeb\Desktop\WebNcodes\scraper\Hiring_cafe"
JOBGETHER_DIR = r"C:\Users\Muneeb\Desktop\WebNcodes\scraper\Jobgether"
JOBBOT_DIR = r"C:\Users\Muneeb\Desktop\WebNcodes\Job-Bot"
RESUMEBOT_DIR = r"C:\Users\Muneeb\Desktop\WebNcodes\resume-bot"
LOGS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")

# Same spreadsheet the scrapers already sync job links into (Sheet1). Sheet2
# holds the job-title/location queue that "Scrape All Platforms" reads from.
GOOGLE_SHEET_ID = "1FsPR9t-BB1GZ6kWfANnrDfq4p9XobDuA1tVB4D2sJDg"
SHEET2_NAME = "Sheet2"
SERVICE_ACCOUNT_FILE = os.path.join(GLASSD_DIR, "service_account.json")

# Dark theme palette. Each BotPanel is assigned one accent color (in creation
# order) purely for visual variety between panels — no behavioral effect.
COLORS = {
    "bg": "#0f1115",
    "panel_bg": "#171a21",
    "input_bg": "#0d0f13",
    "text": "#e6e6e6",
    "muted": "#7d8390",
    "border": "#2a2f3a",
    "idle": "#7d8390",
    "running": "#3ddc84",
}
ACCENT_PALETTE = ["#4f8cff", "#c77dff", "#2dd4bf", "#f5a524", "#3ddc84"]


def configure_style(root):
    """One-time ttk theme setup so buttons/entries/scrollbars can be
    recolored (the default Windows ttk themes ignore most color options)."""
    style = ttk.Style(root)
    style.theme_use("clam")

    style.configure(
        "Secondary.TButton",
        background="#2a2f3a", foreground=COLORS["text"],
        borderwidth=0, focusthickness=0, padding=6, font=("Segoe UI", 9),
    )
    style.map("Secondary.TButton", background=[("active", "#3a4050"), ("disabled", "#20232b")])

    for i, accent in enumerate(ACCENT_PALETTE):
        name = f"Accent{i}.TButton"
        style.configure(
            name, background=accent, foreground="#0f1115",
            borderwidth=0, focusthickness=0, padding=6, font=("Segoe UI", 9, "bold"),
        )
        style.map(name, background=[("active", accent), ("disabled", "#2a2f3a")],
                  foreground=[("disabled", COLORS["muted"])])

    style.configure(
        "Dark.TEntry",
        fieldbackground=COLORS["input_bg"], foreground=COLORS["text"],
        insertcolor=COLORS["text"], borderwidth=1, padding=4,
        bordercolor=COLORS["border"], lightcolor=COLORS["border"], darkcolor=COLORS["border"],
    )
    style.map("Dark.TEntry", bordercolor=[("focus", ACCENT_PALETTE[0])])

    style.configure(
        "Horizontal.TScrollbar",
        background=COLORS["panel_bg"], troughcolor=COLORS["bg"],
        bordercolor=COLORS["bg"], arrowcolor=COLORS["text"],
    )
    return style


class BotPanel(tk.Frame):
    """One bot's controls (Start/Stop/Clear + status) and its own live log panel.
    Subclasses can override build_command() to gather/validate input before the
    subprocess is launched (return None to abort the launch)."""

    _accent_counter = 0

    def __init__(self, parent, title, button_text, script_name, cwd, log_file=None):
        self.accent = ACCENT_PALETTE[BotPanel._accent_counter % len(ACCENT_PALETTE)]
        self._button_style = f"Accent{BotPanel._accent_counter % len(ACCENT_PALETTE)}.TButton"
        BotPanel._accent_counter += 1

        super().__init__(
            parent, bg=COLORS["panel_bg"], bd=0,
            highlightthickness=1, highlightbackground=COLORS["border"],
        )
        self.cwd = cwd
        self.script_name = script_name
        self.process = None
        self.log_queue = queue.Queue()

        # Optional: persist this panel's log output to its own file on disk
        # (e.g. for the scraper panels) in addition to the on-screen log box.
        # Panels that don't pass log_file behave exactly as before.
        self._log_fh = None
        if log_file:
            os.makedirs(os.path.dirname(log_file), exist_ok=True)
            self._log_fh = open(log_file, "a", encoding="utf-8")

        tk.Frame(self, bg=self.accent, height=3).pack(fill="x", side="top")

        tk.Label(
            self, text=title, font=("Segoe UI", 12, "bold"),
            fg=self.accent, bg=COLORS["panel_bg"],
        ).pack(pady=(10, 4))

        self.extra_inputs_frame = tk.Frame(self, bg=COLORS["panel_bg"])
        self.extra_inputs_frame.pack(fill="x", padx=10)
        self.build_inputs(self.extra_inputs_frame)

        btn_frame = tk.Frame(self, bg=COLORS["panel_bg"])
        btn_frame.pack(pady=6)
        self.start_btn = ttk.Button(
            btn_frame, text=button_text, width=20, command=self.start, style=self._button_style,
        )
        self.start_btn.pack(side="left", padx=4)
        self.stop_btn = ttk.Button(
            btn_frame, text="Stop", width=8, command=self.stop, state="disabled", style="Secondary.TButton",
        )
        self.stop_btn.pack(side="left", padx=4)
        self.clear_btn = ttk.Button(
            btn_frame, text="Clear", width=8, command=self.clear_log, style="Secondary.TButton",
        )
        self.clear_btn.pack(side="left", padx=4)

        self.status_label = tk.Label(self, text="Idle", fg=COLORS["idle"], bg=COLORS["panel_bg"], font=("Segoe UI", 9))
        self.status_label.pack(pady=(0, 6))

        self.log_box = scrolledtext.ScrolledText(
            self, width=58, height=28, state="disabled", bg=COLORS["input_bg"], fg=COLORS["text"],
            insertbackground=COLORS["text"], font=("Consolas", 9), bd=0,
            highlightthickness=1, highlightbackground=COLORS["border"], highlightcolor=self.accent,
        )
        self.log_box.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        self.after(100, self._poll_queue)

    def build_inputs(self, parent):
        """Override in subclasses to add input widgets above the buttons."""
        pass

    def build_command(self):
        """Return the argv list to launch, or None to abort (e.g. validation
        failed - subclasses should log a message themselves before returning None)."""
        return [sys.executable, "-u", self.script_name]

    def _append_log(self, text):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", text)
        self.log_box.see("end")
        self.log_box.configure(state="disabled")
        if self._log_fh:
            self._log_fh.write(text)
            self._log_fh.flush()

    def start(self):
        if self.process is not None:
            return
        cmd = self.build_command()
        if cmd is None:
            return
        self._append_log(f"$ {' '.join(cmd)}\n\n")
        self.status_label.configure(text="Running...", fg=COLORS["running"])
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        try:
            self.process = subprocess.Popen(
                cmd,
                cwd=self.cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except Exception as e:
            self._append_log(f"Failed to launch: {e}\n")
            self.status_label.configure(text="Idle", fg=COLORS["idle"])
            self.start_btn.configure(state="normal")
            self.stop_btn.configure(state="disabled")
            return
        threading.Thread(target=self._read_output, daemon=True).start()

    def _read_output(self):
        for line in self.process.stdout:
            self.log_queue.put(line)
        self.log_queue.put(None)  # sentinel: process ended

    def _poll_queue(self):
        try:
            while True:
                line = self.log_queue.get_nowait()
                if line is None:
                    code = self.process.wait()
                    self._append_log(f"\n--- Finished (exit code {code}) ---\n")
                    self.status_label.configure(text="Idle", fg=COLORS["idle"])
                    self.start_btn.configure(state="normal")
                    self.stop_btn.configure(state="disabled")
                    self.process = None
                else:
                    self._append_log(line)
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def clear_log(self):
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

    def stop(self):
        if self.process is not None:
            self._append_log("\n--- Stopping... ---\n")
            self.process.terminate()

    def is_running(self):
        return self.process is not None


class ResumeBotPanel(BotPanel):
    """resume-bot needs a job description + company name before it can run.
    Both are auto-extracted from a pasted job posting URL via resume-bot's
    own fetch_jd.py, so this panel just takes the URL and builds its command
    from what fetch_jd.py reports instead of launching a fixed script with
    no arguments."""

    def build_inputs(self, parent):
        tk.Label(parent, text="Job Posting URL:", fg=COLORS["muted"], bg=COLORS["panel_bg"], font=("Segoe UI", 9)).pack(anchor="w")
        self.url_entry = ttk.Entry(parent, style="Dark.TEntry")
        self.url_entry.pack(fill="x", pady=(0, 6))

    def build_command(self):
        url = self.url_entry.get().strip()

        if not url:
            self._append_log("Please enter a job posting URL before generating.\n")
            return None

        self._append_log(f"Fetching job description from: {url}\n")
        fetch = subprocess.run(
            [sys.executable, "-u", "fetch_jd.py", url],
            cwd=self.cwd,
            capture_output=True,
            text=True,
        )
        self._append_log(fetch.stdout)
        if fetch.returncode != 0:
            self._append_log(fetch.stderr)
            self._append_log("Failed to fetch the job description from that URL.\n")
            return None

        company_match = re.search(r"^Company:\s*(.+)$", fetch.stdout, re.MULTILINE)
        saved_match = re.search(r"^Saved:\s*(.+)$", fetch.stdout, re.MULTILINE)
        if not company_match or not saved_match:
            self._append_log("Could not determine the company name or saved file from fetch_jd.py.\n")
            return None

        company = company_match.group(1).strip()
        jd_filename = os.path.basename(saved_match.group(1).strip())

        return [sys.executable, "-u", "ollama_generate.py", jd_filename, company]


class ScraperPanel(tk.Frame):
    """Single consolidated scraper control, replacing a separate manual panel
    per platform. One button runs Glassdoor, then Hiring Cafe, then Jobgether
    — once per (job title, location) row in Sheet2 — before moving to the
    next row. Each script already applies its own Remote-only, ~24h-old, and
    CAPTCHA-link-skip filtering internally; this panel only sequences the 3
    subprocesses and fans their output out to the shared log box, each
    platform's own log file, and a combined scrape_all.log.

    Platforms run strictly one at a time, even across different job titles:
    each launches its own dedicated stealth browser (patchright), and running
    them one at a time keeps behavior simple and predictable."""

    ACCENT = ACCENT_PALETTE[2]

    def __init__(self, parent):
        super().__init__(
            parent, bg=COLORS["panel_bg"], bd=0,
            highlightthickness=1, highlightbackground=COLORS["border"],
        )
        self._stop_requested = False
        self._thread = None
        self._current_proc = None

        tk.Frame(self, bg=self.ACCENT, height=3).pack(fill="x", side="top")

        tk.Label(
            self, text="Job Scraper", font=("Segoe UI", 12, "bold"),
            fg=self.ACCENT, bg=COLORS["panel_bg"],
        ).pack(pady=(10, 2))
        tk.Label(
            self,
            text="Glassdoor → Hiring Cafe → Jobgether, per Sheet2 title\nRemote · United States · posted within 24h",
            fg=COLORS["muted"], bg=COLORS["panel_bg"], font=("Segoe UI", 8), justify="center",
        ).pack(pady=(0, 8))

        btn_frame = tk.Frame(self, bg=COLORS["panel_bg"])
        btn_frame.pack(pady=6)
        self.start_btn = ttk.Button(
            btn_frame, text="Scrape All Jobs", width=20, command=self.start, style="Accent2.TButton",
        )
        self.start_btn.pack(side="left", padx=4)
        self.stop_btn = ttk.Button(
            btn_frame, text="Stop", width=8, command=self.stop, state="disabled", style="Secondary.TButton",
        )
        self.stop_btn.pack(side="left", padx=4)
        self.clear_btn = ttk.Button(
            btn_frame, text="Clear", width=8, command=self.clear_log, style="Secondary.TButton",
        )
        self.clear_btn.pack(side="left", padx=4)

        self.status_label = tk.Label(self, text="Idle", fg=COLORS["idle"], bg=COLORS["panel_bg"], font=("Segoe UI", 9))
        self.status_label.pack(pady=(0, 6))

        self.log_box = scrolledtext.ScrolledText(
            self, width=58, height=28, state="disabled", bg=COLORS["input_bg"], fg=COLORS["text"],
            insertbackground=COLORS["text"], font=("Consolas", 9), bd=0,
            highlightthickness=1, highlightbackground=COLORS["border"], highlightcolor=self.ACCENT,
        )
        self.log_box.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        os.makedirs(LOGS_DIR, exist_ok=True)
        self._log_fh = open(os.path.join(LOGS_DIR, "scrape_all.log"), "a", encoding="utf-8")

    def is_running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        if self.is_running():
            return
        self._stop_requested = False
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.status_label.configure(text="Running...", fg=COLORS["running"])
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_requested = True
        self._log("\n--- Stop requested: will halt after the current platform finishes ---\n")

    def terminate_now(self):
        """Hard stop used when the app window is closing — kill whatever
        subprocess is currently running instead of waiting for it to finish."""
        self._stop_requested = True
        if self._current_proc is not None:
            try:
                self._current_proc.terminate()
            except Exception:
                pass

    def clear_log(self):
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

    def _log(self, text):
        def _do():
            self.log_box.configure(state="normal")
            self.log_box.insert("end", text)
            self.log_box.see("end")
            self.log_box.configure(state="disabled")
        self.after(0, _do)
        self._log_fh.write(text)
        self._log_fh.flush()

    def _set_status(self, text, color):
        self.after(0, lambda: self.status_label.configure(text=text, fg=color))

    def _finish(self):
        def _do():
            self.status_label.configure(text="Idle", fg=COLORS["idle"])
            self.start_btn.configure(state="normal")
            self.stop_btn.configure(state="disabled")
        self.after(0, _do)

    def _fetch_titles(self):
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=scopes)
        client = gspread.authorize(creds)
        ws = client.open_by_key(GOOGLE_SHEET_ID).worksheet(SHEET2_NAME)
        rows = ws.get_all_values()[1:]  # skip header row
        return [(r[0].strip(), r[1].strip()) for r in rows if len(r) >= 2 and r[0].strip()]

    def _run_platform(self, label, cwd, cmd, log_path):
        """Run one platform's scraper as a subprocess, streaming its output
        into the shared log box, this platform's own log file, and the
        combined scrape_all.log — blocking until it finishes or Stop is hit."""
        self._log(f"\n$ {' '.join(cmd)}\n\n")
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as platform_fh:
            platform_fh.write(f"$ {' '.join(cmd)}\n\n")
            try:
                proc = subprocess.Popen(
                    cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1,
                )
            except Exception as e:
                msg = f"Failed to launch {label}: {e}\n"
                self._log(msg)
                platform_fh.write(msg)
                return
            self._current_proc = proc
            for line in proc.stdout:
                self._log(line)
                platform_fh.write(line)
                platform_fh.flush()
                if self._stop_requested:
                    proc.terminate()
            code = proc.wait()
            self._current_proc = None
            msg = f"\n--- {label} finished (exit code {code}) ---\n"
            self._log(msg)
            platform_fh.write(msg)

    def _run(self):
        self._log("\n=== Scrape All: starting ===\n")
        try:
            titles = self._fetch_titles()
        except Exception as e:
            self._log(f"Failed to read job titles from Sheet2: {e}\n")
            self._finish()
            return

        self._log(f"Loaded {len(titles)} job titles from Sheet2\n")

        for idx, (title, location) in enumerate(titles, start=1):
            if self._stop_requested:
                break
            self._log(f"\n--- [{idx}/{len(titles)}] {title} | {location} ---\n")

            self._set_status(f"[{idx}/{len(titles)}] {title} — Glassdoor", COLORS["running"])
            inputs_path = os.path.join(GLASSD_DIR, "inputs.txt")
            with open(inputs_path, "w", encoding="utf-8") as f:
                f.write(f"{title}\n{location}\n")
            self._run_platform(
                "Glassdoor", GLASSD_DIR,
                [sys.executable, "-u", "glassdoor_scraper_final.py"],
                os.path.join(LOGS_DIR, "glassdoor.log"),
            )
            if self._stop_requested:
                break

            self._set_status(f"[{idx}/{len(titles)}] {title} — Hiring Cafe", COLORS["running"])
            self._run_platform(
                "Hiring Cafe", HIRINGCAFE_DIR,
                [sys.executable, "-u", "scraper.py", title, location],
                os.path.join(LOGS_DIR, "hiring_cafe.log"),
            )
            if self._stop_requested:
                break

            self._set_status(f"[{idx}/{len(titles)}] {title} — Jobgether", COLORS["running"])
            self._run_platform(
                "Jobgether", JOBGETHER_DIR,
                [sys.executable, "-u", "jobgether_scraper.py", title, "15"],
                os.path.join(LOGS_DIR, "jobgether.log"),
            )

        self._log("\n=== Scrape All: finished ===\n" if not self._stop_requested else "\n=== Scrape All: stopped ===\n")
        self._finish()


def main():
    root = tk.Tk()
    root.title("Job Bot Launcher")
    root.configure(bg=COLORS["bg"])
    root.geometry("1900x780")
    configure_style(root)
    try:
        root.state("zoomed")  # maximize to the screen so panels don't get pushed off-window
    except tk.TclError:
        pass

    header = tk.Frame(root, bg=COLORS["bg"])
    header.pack(fill="x", padx=14, pady=(12, 4))
    tk.Label(
        header, text="Job Bot Launcher", font=("Segoe UI", 16, "bold"),
        fg=COLORS["text"], bg=COLORS["bg"],
    ).pack(side="left")
    tk.Label(
        header, text="  scrape  →  tailor  →  apply", font=("Segoe UI", 10),
        fg=COLORS["muted"], bg=COLORS["bg"],
    ).pack(side="left", pady=(4, 0))

    # 3 panels side by side can still be wider than a small screen, so the
    # panel row lives in a horizontally scrollable canvas instead of a plain
    # frame.
    canvas_frame = tk.Frame(root, bg=COLORS["bg"])
    canvas_frame.pack(fill="both", expand=True)

    panel_canvas = tk.Canvas(canvas_frame, highlightthickness=0, bg=COLORS["bg"])
    hscroll = ttk.Scrollbar(canvas_frame, orient="horizontal", command=panel_canvas.xview)
    panel_canvas.configure(xscrollcommand=hscroll.set)
    hscroll.pack(side="bottom", fill="x")
    panel_canvas.pack(side="top", fill="both", expand=True)

    container = tk.Frame(panel_canvas, bg=COLORS["bg"])
    container_window = panel_canvas.create_window((0, 0), window=container, anchor="nw")

    def _on_container_configure(event):
        panel_canvas.configure(scrollregion=panel_canvas.bbox("all"))

    def _on_canvas_configure(event):
        panel_canvas.itemconfigure(container_window, height=event.height)

    container.bind("<Configure>", _on_container_configure)
    panel_canvas.bind("<Configure>", _on_canvas_configure)

    scraper_panel = ScraperPanel(container)
    scraper_panel.pack(side="left", fill="both", expand=True, padx=6, pady=6)

    resume_panel = ResumeBotPanel(
        container, "Resume Bot", "Generate Resume",
        "ollama_generate.py", RESUMEBOT_DIR,
    )
    resume_panel.pack(side="left", fill="both", expand=True, padx=6, pady=6)

    apply_panel = BotPanel(
        container, "Application Bot", "Start Applying",
        "main.py", JOBBOT_DIR,
    )
    apply_panel.pack(side="left", fill="both", expand=True, padx=6, pady=6)

    def on_close():
        scraper_panel.terminate_now()
        for panel in (resume_panel, apply_panel):
            if panel.is_running():
                panel.process.terminate()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
