"""
Job Bot Launcher

A small GUI with three panels:
  - "Scrape All Jobs" runs GlassD/glassdoor_scraper_final.py,
    Hiring_cafe/scraper.py, and Jobgether/jobgether_scraper.py concurrently
    for each (job title, location) row in Sheet2 in turn, one row at a time
    (so up to 3 browsers open at once). Every click processes all Sheet2
    rows from the top; no keyword is skipped for having been scraped in an
    earlier run. Each scraper launches its own stealth (patchright) browser,
    applies that platform's own "posted within 24h" filter server-side
    before paginating (instead of paging through everything and discarding
    old listings afterward), filters to Remote-only, skips links that land
    on a CAPTCHA/verification page, and pushes each job into Sheet1 as a
    clickable HYPERLINK() cell as soon as it's found.
  - "Generate Resumes" reads Company Name + Job Link straight from Sheet1,
    then for each row without a resume yet (checked against CVS_DIR, not
    just Sheet1) runs resume-bot/fetch_jd.py (to fetch the job description)
    and ollama_generate.py (to tailor and render a PDF resume), one row
    after another, marking the row's Resume column "Generated" on success.
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
import time
import tkinter as tk
from datetime import datetime
from tkinter import scrolledtext, ttk

import gspread
from google.oauth2.service_account import Credentials

GLASSD_DIR = r"C:\Users\webNcodes\Desktop\webncodes\scraper\GlassD"
HIRINGCAFE_DIR = r"C:\Users\webNcodes\Desktop\webncodes\scraper\Hiring_cafe"
JOBGETHER_DIR = r"C:\Users\webNcodes\Desktop\webncodes\scraper\Jobgether"
JOBBOT_DIR = r"C:\Users\webNcodes\Desktop\webncodes\Job-Bot"
RESUMEBOT_DIR = r"C:\Users\webNcodes\Desktop\webncodes\resume-bot"
LOGS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
# Must match CVS_DIR in resume-bot/ollama_generate.py - that's where the
# actual rendered resumes end up; used here only to check whether one
# already exists for a company (see ResumeBotPanel._run).
CVS_DIR = r"C:\Users\webNcodes\Desktop\CVs"

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


def stamp_log_line(text):
    """Prefix a log line with its own [YYYY-MM-DD HH:MM:SS] timestamp, so
    every event in a run's log file shows exactly when it happened,
    including the date - important since a run can span past midnight.
    Leading blank lines are preserved as pure spacing ahead of the
    timestamp rather than having it land after them. Shared by every panel
    that logs to a per-run file (see BotPanel, ScraperPanel)."""
    stripped = text.lstrip("\n")
    leading_newlines = text[:len(text) - len(stripped)]
    if not stripped:
        return text
    return f"{leading_newlines}[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {stripped}"


def format_duration(td):
    """Render a timedelta as e.g. '1h 23m 45s' for end-of-run log lines."""
    total_seconds = int(td.total_seconds())
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts = []
    if hours:
        parts.append(f"{hours}h")
    if hours or minutes:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return " ".join(parts)


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

    style.configure(
        "TNotebook", background=COLORS["panel_bg"], borderwidth=0,
    )
    style.configure(
        "TNotebook.Tab", background=COLORS["input_bg"], foreground=COLORS["text"],
        padding=(10, 4), borderwidth=0, font=("Segoe UI", 9),
    )
    style.map(
        "TNotebook.Tab",
        background=[("selected", COLORS["border"])],
        foreground=[("selected", COLORS["text"])],
    )
    return style


class BotPanel(tk.Frame):
    """One bot's controls (Start/Stop/Clear + status) and its own live log panel.
    Subclasses can override build_command() to gather/validate input before the
    subprocess is launched (return None to abort the launch)."""

    _accent_counter = 0

    def __init__(self, parent, title, button_text, script_name, cwd, log_file=None, log_dir=None, log_prefix=None):
        self.accent = ACCENT_PALETTE[BotPanel._accent_counter % len(ACCENT_PALETTE)]
        self._button_style = f"Accent{BotPanel._accent_counter % len(ACCENT_PALETTE)}.TButton"
        BotPanel._accent_counter += 1

        super().__init__(
            parent, bg=COLORS["panel_bg"], bd=0,
            highlightthickness=1, highlightbackground=COLORS["border"],
        )
        self.title = title
        self.cwd = cwd
        self.script_name = script_name
        self.process = None
        self.log_queue = queue.Queue()
        self._run_start = None
        self.continuous_mode = False
        self._stop_requested = False

        # Two ways to persist this panel's log output to disk, in addition
        # to the on-screen log box:
        #   log_file            - a single file appended to across every run.
        #   log_dir/log_prefix  - a NEW timestamped file
        #                         ("<log_prefix>_<start timestamp>.log" in
        #                         log_dir) created each time Start is
        #                         clicked, matching ScraperPanel's per-run
        #                         log files - so each run's own file
        #                         unambiguously shows when that run started
        #                         (filename) and ended (its own last line).
        # Panels that pass neither behave exactly as before (no file logging).
        self._log_fh = None
        self._log_dir = log_dir
        self._log_prefix = log_prefix
        if log_file:
            os.makedirs(os.path.dirname(log_file), exist_ok=True)
            self._log_fh = open(log_file, "a", encoding="utf-8")

        tk.Frame(self, bg=self.accent, height=3).pack(fill="x", side="top")

        tk.Label(
            self, text=title, font=("Segoe UI", 12, "bold"),
            fg=self.accent, bg=COLORS["panel_bg"],
        ).pack(pady=(10, 4))

        extra_inputs_frame = tk.Frame(self, bg=COLORS["panel_bg"])
        extra_inputs_frame.pack(fill="x", padx=10)
        self.build_inputs(extra_inputs_frame)

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
        # Panels using the per-run timestamped-file mode stamp every line
        # (on-screen and in the file) so the file's own content shows
        # exactly when each event happened; the older single-appended-file
        # mode (log_file=) leaves text unstamped, matching its prior behavior.
        display_text = stamp_log_line(text) if self._log_dir else text
        self.log_box.configure(state="normal")
        self.log_box.insert("end", display_text)
        self.log_box.see("end")
        self.log_box.configure(state="disabled")
        if self._log_fh:
            self._log_fh.write(display_text)
            self._log_fh.flush()

    def start(self, continuous=False):
        if self.process is not None:
            return
        cmd = self.build_command()
        if cmd is None:
            return
        self.continuous_mode = continuous
        self._stop_requested = False
        
        # Only open a new log file if we're not just respawning in continuous mode
        if self._run_start is None or not self.continuous_mode:
            self._run_start = datetime.now()
            if self._log_dir and self._log_prefix:
                os.makedirs(self._log_dir, exist_ok=True)
                log_path = os.path.join(
                    self._log_dir,
                    f"{self._log_prefix}_{self._run_start.strftime('%Y-%m-%d_%H-%M-%S')}.log",
                )
                self._log_fh = open(log_path, "a", encoding="utf-8")
                self._append_log(f"=== {self.title}: starting ===\n")
        
        self._spawn_process(cmd)

    def _spawn_process(self, cmd):
        self._append_log(f"$ {' '.join(cmd)}\n\n")
        self.status_label.configure(text="Running...", fg=COLORS["running"])
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        try:
            self.process = subprocess.Popen(
                cmd,
                cwd=self.cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except Exception as e:
            self._append_log(f"Failed to launch: {e}\n")
            self._close_run_log("failed to launch")
            self.status_label.configure(text="Idle", fg=COLORS["idle"])
            self.start_btn.configure(state="normal")
            self.stop_btn.configure(state="disabled")
            return
        threading.Thread(target=self._read_output, daemon=True).start()

    def _read_output(self):
        for line in self.process.stdout:
            self.log_queue.put(line)
        self.log_queue.put(None)  # sentinel: process ended

    def _close_run_log(self, end_label):
        """Write this run's end-of-run marker (with total elapsed time) and
        close its log file - only relevant in the per-run timestamped-file
        mode (log_dir/log_prefix); a no-op otherwise."""
        if self._log_dir and self._log_fh is not None:
            elapsed = format_duration(datetime.now() - self._run_start) if self._run_start else "?"
            self._append_log(f"\n=== {self.title}: {end_label} (total time: {elapsed}) ===\n")
            self._log_fh.close()
            self._log_fh = None

    def _poll_queue(self):
        try:
            while True:
                line = self.log_queue.get_nowait()
                if line is None:
                    code = self.process.wait()
                    self._append_log(f"\n--- Finished (exit code {code}) ---\n")
                    self.process = None
                    if self.continuous_mode and not self._stop_requested:
                        self.status_label.configure(text="Waiting...", fg=COLORS["idle"])
                        self.after(30000, self._check_and_respawn)
                    else:
                        end_label = "stopped" if self._stop_requested else f"finished (exit code {code})"
                        self._close_run_log(end_label)
                        self._run_start = None
                        self.status_label.configure(text="Idle", fg=COLORS["idle"])
                        self.start_btn.configure(state="normal")
                        self.stop_btn.configure(state="disabled")
                else:
                    self._append_log(line)
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _check_and_respawn(self):
        if not self._stop_requested and self.continuous_mode:
            cmd = self.build_command()
            if cmd is not None:
                self._spawn_process(cmd)
            else:
                self._close_run_log("stopped")
                self._run_start = None
                self.status_label.configure(text="Idle", fg=COLORS["idle"])
                self.start_btn.configure(state="normal")
                self.stop_btn.configure(state="disabled")
        elif self._run_start is not None:
            self._close_run_log("stopped")
            self._run_start = None
            self.status_label.configure(text="Idle", fg=COLORS["idle"])
            self.start_btn.configure(state="normal")
            self.stop_btn.configure(state="disabled")

    def clear_log(self):
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

    def stop(self):
        self._stop_requested = True
        if self.process is not None:
            self._append_log("\n--- Stopping... ---\n")
            self.process.terminate()

    def is_running(self):
        return self.process is not None


class ResumeBotPanel(BotPanel):
    """Instead of pasting one job posting URL at a time, this panel reads
    every row's Company Name + Job Link straight from the same Google Sheet
    the scrapers fill in, and generates a tailored resume for each one in
    turn - same "pull the work queue from the sheet" pattern ScraperPanel
    already uses for job titles. A row is skipped if a resume for that
    company already exists (CVS_DIR/<company>/Jimmy Tran.pdf), so
    re-running this after new jobs are scraped only processes the new ones.
    Once a resume is generated, that row's Resume column (J) is set to
    "Generated" so it's visible directly in the sheet."""

    # Below this many characters the fetched page almost certainly isn't the
    # real job description (JS-rendered board, consent wall, 404 shell).
    MIN_JD_CHARS = 400

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._stop_requested = False
        self._thread = None
        self._current_proc = None

    def build_inputs(self, parent):
        tk.Label(
            parent,
            text="Reads Company Name + Job Link from the Google Sheet\nand generates a resume for each row not done yet.",
            fg=COLORS["muted"], bg=COLORS["panel_bg"], font=("Segoe UI", 8), justify="center",
        ).pack(anchor="w", pady=(0, 6))

        profile_frame = tk.Frame(parent, bg=COLORS["panel_bg"])
        profile_frame.pack(fill="x", pady=5)
        tk.Label(profile_frame, text="Number of Profiles:", font=("Segoe UI", 10), fg=COLORS["text"], bg=COLORS["panel_bg"]).pack(side="left", padx=5)
        self.num_profiles_var = tk.IntVar(value=1)
        self.profile_spinbox = tk.Spinbox(profile_frame, from_=1, to=10, textvariable=self.num_profiles_var, width=5, font=("Segoe UI", 10))
        self.profile_spinbox.pack(side="left", padx=5)

    def is_running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self, continuous=False):
        if self.is_running():
            return
        self.continuous_mode = continuous
        self._stop_requested = False
        self._append_log(f"\n=== Generate Resumes: starting at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===\n")
        self.status_label.configure(text="Running...", fg=COLORS["running"])
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_requested = True
        self._append_log("\n--- Stopping... ---\n")
        if self._current_proc is not None:
            try:
                self._current_proc.terminate()
            except Exception:
                pass

    def terminate_now(self):
        """Hard stop used when the app window is closing — same purpose as
        ScraperPanel.terminate_now(), needed here because this panel manages
        its own subprocess via _current_proc instead of BotPanel's
        single-shot self.process that on_close() otherwise expects."""
        self.stop()

    def _finish(self):
        def _do():
            self.status_label.configure(text="Idle", fg=COLORS["idle"])
            self.start_btn.configure(state="normal")
            self.stop_btn.configure(state="disabled")
        self.after(0, _do)

    def _append_log(self, text):
        """All of this panel's logging happens on the worker thread, but Tk
        widgets may only be touched from the main thread — so hand the widget
        update back to it via after() (same approach as ScraperPanel._log).
        Writing to the log file is safe here since only one worker runs."""
        file_only = False
        if "[FILE_ONLY]" in text:
            file_only = True
            text = text.replace("[FILE_ONLY]", "")
            
        if not file_only:
            def _do():
                self.log_box.configure(state="normal")
                self.log_box.insert("end", text)
                self.log_box.see("end")
                self.log_box.configure(state="disabled")
            self.after(0, _do)
            
        if self._log_fh:
            stamped_text = stamp_log_line(text)
            self._log_fh.write(stamped_text)
            self._log_fh.flush()

    def _set_status(self, text):
        self.after(0, lambda: self.status_label.configure(text=text, fg=COLORS["running"]))

    @staticmethod
    def _slugify(text):
        """Mirrors fetch_jd.py's own slugify() exactly, so the jd_<slug>.txt
        filename it writes when given an explicit output_name can be
        predicted here without parsing fetch_jd.py's stdout."""
        text = re.sub(r"[^a-zA-Z0-9]+", "_", text).strip("_").lower()
        return text[:60] or "job"

    # Column I - written "Generated" once a resume for that row's job exists.
    RESUME_COLUMN = 9

    def _connect_sheet(self):
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=scopes)
        client = gspread.authorize(creds)
        return client.open_by_key(GOOGLE_SHEET_ID).sheet1

    def _fetch_jobs(self, ws):
        rows = ws.get_all_values()[1:]  # skip header row
        jobs = []
        for i, r in enumerate(rows, start=2):  # row 2 is the first data row
            if len(r) < 6:
                continue
            company, link = r[1].strip(), r[5].strip()
            if company and link:
                jobs.append((company, link, i, r))
        return jobs

    def _sleep_unless_stopped(self, seconds):
        """Sleep in small increments so Stop still takes effect promptly.
        Returns True if Stop was hit during the wait."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self._stop_requested:
                return True
            time.sleep(min(0.5, end - time.monotonic()))
        return self._stop_requested

    def _run_subprocess(self, cmd):
        """Run one subprocess to completion, streaming its output into the
        log box, and return its exit code (or None if Stop was hit)."""
        try:
            proc = subprocess.Popen(
                cmd, cwd=self.cwd, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, encoding="utf-8", errors="replace"
            )
        except Exception as e:
            self._append_log(f"Failed to launch: {e}\n")
            return 1
        self._current_proc = proc
        for line in proc.stdout:
            self._append_log(line)
            if self._stop_requested:
                proc.terminate()
        code = proc.wait()
        self._current_proc = None
        return None if self._stop_requested else code

    def _run(self):
        while not self._stop_requested:
            try:
                sheet = self._connect_sheet()
                jobs = self._fetch_jobs(sheet)
                headers = sheet.row_values(1)
            except Exception as e:
                self._append_log(f"Failed to read jobs from the Google Sheet: {e}\n")
                if self.continuous_mode:
                    if self._sleep_unless_stopped(30):
                        break
                    continue
                else:
                    self._finish()
                    return

            num_profiles = self.num_profiles_var.get()
            # Profiles start at column index 8 (0-based) which is column I (1-based is 9)
            profile_names = headers[8:8+num_profiles]

            todo = []
            for company, link, row, row_data in jobs:
                for profile_name in profile_names:
                    try:
                        header_idx = headers.index(profile_name)
                        col_index = header_idx + 1
                        
                        sheet_status = ""
                        if header_idx < len(row_data):
                            sheet_status = row_data[header_idx].strip().lower()
                            
                        # Check if the Google Sheet already says it's done
                        if sheet_status in ("generated", "applied", "submitted", "human attention"):
                            continue
                            
                    except ValueError:
                        continue
                        
                    pdf_path = os.path.join(CVS_DIR, company, f"{profile_name}.pdf")
                    if not os.path.exists(pdf_path):
                        todo.append((company, link, row, profile_name, col_index))

            if todo:
                self._append_log(
                    f"Loaded {len(jobs)} job(s) with a link from the sheet — "
                    f"Queued {len(todo)} resume generation(s) across {num_profiles} profile(s)\n"
                )

            made = failed = 0
            for idx, (company, link, row, profile_name, col_index) in enumerate(todo, start=1):
                if self._stop_requested:
                    break

                self._append_log(f"\n--- [{idx}/{len(todo)}] {company} for {profile_name} ---\n")

                self._set_status(f"[{idx}/{len(todo)}] {company} ({profile_name}) — fetching JD")
                self._append_log(f"$ fetch_jd.py {link} \"{company}\"\n\n")
                code = self._run_subprocess([sys.executable, "-u", "fetch_jd.py", link, company])
                if code is None:
                    break
                if code != 0:
                    self._append_log(f"  Fetch failed for {company}, retrying once more in 15s...\n")
                    if self._sleep_unless_stopped(15):
                        break
                    self._append_log(f"$ fetch_jd.py {link} \"{company}\" (retry)\n\n")
                    code = self._run_subprocess([sys.executable, "-u", "fetch_jd.py", link, company])
                    if code is None:
                        break
                if code != 0:
                    self._append_log(f"  Failed to fetch the job description for {company}, skipping.\n")
                    failed += 1
                    continue

                jd_filename = f"jd_{self._slugify(company)}.txt"
                jd_path = os.path.join(self.cwd, jd_filename)
                try:
                    with open(jd_path, encoding="utf-8") as f:
                        jd_len = len(f.read().strip())
                except OSError as e:
                    self._append_log(f"  Could not read {jd_filename}: {e}, skipping.\n")
                    failed += 1
                    continue
                if jd_len < self.MIN_JD_CHARS:
                    self._append_log(
                        f"  Job description is only {jd_len} characters. This usually "
                        f"indicates an expired job link (404) or consent wall. "
                        f"Skipping resume generation to prevent garbage data.\n"
                    )
                    try:
                        if os.path.exists(jd_path):
                            os.remove(jd_path)
                    except OSError:
                        pass
                    failed += 1
                    continue

                self._set_status(f"[{idx}/{len(todo)}] {company} ({profile_name}) — generating resume")
                self._append_log(f"\n$ ollama_generate.py {jd_filename} \"{company}\" \"{profile_name}\"\n\n")
                code = self._run_subprocess([sys.executable, "-u", "ollama_generate.py", jd_filename, company, profile_name])
                if code is None:
                    break
                if code != 0:
                    self._append_log(f"  Failed to generate a resume for {company} ({profile_name}).\n")
                    failed += 1
                else:
                    made += 1
                    try:
                        sheet.update_cell(row, col_index, "Generated")
                    except Exception as e:
                        self._append_log(f"  Resume was generated but marking the sheet failed: {e}\n")
                    
                # Clean up the JD text file regardless of success or failure
                try:
                    if os.path.exists(jd_path):
                        os.remove(jd_path)
                except OSError as e:
                    self._append_log(f"  Could not clean up {jd_filename}: {e}\n")

            if todo:
                self._append_log(f"\n{made} resume(s) generated, {failed} skipped/failed\n")
            
            if not self.continuous_mode or self._stop_requested:
                break
                
            self._set_status("Waiting for new jobs...")
            if self._sleep_unless_stopped(30):
                break

        end_label = "stopped" if self._stop_requested else "finished"
        self._append_log(f"\n=== Generate Resumes: {end_label} at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===\n")
        self._finish()


class CoverLetterBotPanel(BotPanel):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._stop_requested = False
        self._thread = None
        self._current_proc = None

    def build_inputs(self, parent):
        tk.Label(
            parent,
            text="Reads Company Name + Job Link from the Google Sheet\nand generates a tailored cover letter for each row not done yet.",
            fg=COLORS["muted"], bg=COLORS["panel_bg"], font=("Segoe UI", 8), justify="center",
        ).pack(anchor="w", pady=(0, 6))

        profile_frame = tk.Frame(parent, bg=COLORS["panel_bg"])
        profile_frame.pack(fill="x", pady=5)
        tk.Label(profile_frame, text="Number of Profiles:", font=("Segoe UI", 10), fg=COLORS["text"], bg=COLORS["panel_bg"]).pack(side="left", padx=5)
        self.num_profiles_var = tk.IntVar(value=1)
        self.profile_spinbox = tk.Spinbox(profile_frame, from_=1, to=10, textvariable=self.num_profiles_var, width=5, font=("Segoe UI", 10))
        self.profile_spinbox.pack(side="left", padx=5)

    def is_running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self, continuous=False):
        if self.is_running():
            return
        self.continuous_mode = continuous
        self._stop_requested = False
        self._append_log(f"\n=== Generate Cover Letters: starting at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===\n")
        self.status_label.configure(text="Running...", fg=COLORS["running"])
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_requested = True
        self._append_log("\n--- Stopping... ---\n")
        if self._current_proc is not None:
            try:
                self._current_proc.terminate()
            except Exception:
                pass

    def terminate_now(self):
        self.stop()

    def _finish(self):
        def _do():
            self.status_label.configure(text="Idle", fg=COLORS["idle"])
            self.start_btn.configure(state="normal")
            self.stop_btn.configure(state="disabled")
        self.after(0, _do)

    def _append_log(self, text):
        file_only = False
        if "[FILE_ONLY]" in text:
            file_only = True
            text = text.replace("[FILE_ONLY]", "")
            
        if not file_only:
            def _do():
                self.log_box.configure(state="normal")
                self.log_box.insert("end", text)
                self.log_box.see("end")
                self.log_box.configure(state="disabled")
            self.after(0, _do)
            
        if self._log_fh:
            stamped_text = stamp_log_line(text)
            self._log_fh.write(stamped_text)
            self._log_fh.flush()

    def _set_status(self, text):
        self.after(0, lambda: self.status_label.configure(text=text, fg=COLORS["running"]))

    @staticmethod
    def _slugify(text):
        text = re.sub(r"[^a-zA-Z0-9]+", "_", text).strip("_").lower()
        return text[:60] or "job"

    # We will write 'Cover Letter Generated' instead of 'Generated' maybe?
    # Actually, the user asked if checking if PDF exists is enough, they didn't answer about the sheet column.
    # So we'll just check if PDF exists. We won't write to the sheet.

    def _connect_sheet(self):
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=scopes)
        client = gspread.authorize(creds)
        return client.open_by_key(GOOGLE_SHEET_ID).sheet1

    def _fetch_jobs(self, ws):
        rows = ws.get_all_values()[1:]  # skip header row
        jobs = []
        for i, r in enumerate(rows, start=2):  # row 2 is the first data row
            if len(r) < 6:
                continue
            company, link = r[1].strip(), r[5].strip()
            if company and link:
                jobs.append((company, link, i, r))
        return jobs

    def _sleep_unless_stopped(self, seconds):
        for _ in range(int(seconds * 10)):
            if self._stop_requested:
                return True
            time.sleep(0.1)
        return False

    def _run_subprocess(self, cmd):
        try:
            self._current_proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                creationflags=subprocess.CREATE_NO_WINDOW
            )
            for line in iter(self._current_proc.stdout.readline, ""):
                if self._stop_requested:
                    self._current_proc.terminate()
                    break
                self._append_log(line)
            
            self._current_proc.stdout.close()
            self._current_proc.wait()
            ret = self._current_proc.returncode
            self._current_proc = None
            return ret
        except Exception as e:
            self._append_log(f"  Error launching subprocess: {e}\n")
            return -1

    def _run(self):
        self._current_proc = None
        while True:
            if self._stop_requested:
                break

            self._set_status("Connecting to Google Sheets...")
            try:
                sheet = self._connect_sheet()
                headers = sheet.get_all_values()[0]
                jobs = self._fetch_jobs(sheet)
            except Exception as e:
                self._append_log(f"Failed to fetch job links: {e}\nRetrying in 15s...\n")
                if self._sleep_unless_stopped(15):
                    break
                continue

            num_profiles = self.num_profiles_var.get()
            profile_names = headers[8:8+num_profiles]

            todo = []
            COVERLETTER_DIR = r"C:\Users\webNcodes\Desktop\coverletter"
            for company, link, row, row_data in jobs:
                for profile_name in profile_names:
                    # Don't check the Google sheet for cover letter status, just check the file!
                    pdf_path = os.path.join(COVERLETTER_DIR, company, f"Cover Letter - {profile_name}.pdf")
                    if not os.path.exists(pdf_path):
                        todo.append((company, link, row, profile_name))

            if todo:
                self._append_log(
                    f"Loaded {len(jobs)} job(s) with a link from the sheet — "
                    f"Queued {len(todo)} cover letter generation(s) across {num_profiles} profile(s)\n"
                )

            made = failed = 0
            for idx, (company, link, row, profile_name) in enumerate(todo, start=1):
                if self._stop_requested:
                    break

                self._append_log(f"\n--- [{idx}/{len(todo)}] {company} for {profile_name} ---\n")

                jd_filename = f"jd_{self._slugify(company)}.txt"
                jd_path = os.path.join(RESUMEBOT_DIR, jd_filename)

                self._set_status(f"[{idx}/{len(todo)}] {company} ({profile_name}) — fetching JD")
                self._append_log(f"$ fetch_jd.py {link} \"{company}\"\n\n")
                
                # Run fetch_jd from ResumeBot dir
                cwd_resumebot = RESUMEBOT_DIR
                self._current_proc = subprocess.Popen(
                    [sys.executable, "-u", "fetch_jd.py", link, company],
                    cwd=cwd_resumebot,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                    creationflags=subprocess.CREATE_NO_WINDOW
                )
                for line in iter(self._current_proc.stdout.readline, ""):
                    if self._stop_requested:
                        self._current_proc.terminate()
                        break
                    self._append_log(line)
                self._current_proc.wait()
                code = self._current_proc.returncode

                if code != 0:
                    self._append_log(f"  Fetch failed for {company}, skipping cover letter generation.\n")
                    failed += 1
                    continue

                self._set_status(f"[{idx}/{len(todo)}] {company} ({profile_name}) — generating cover letter")
                self._append_log(f"\n$ batch_generate.py {jd_path} \"{company}\" \"{profile_name}\"\n\n")
                
                # Run batch_generate from cover_letter_bot dir
                cwd_clbot = r"C:\Users\webNcodes\Desktop\webncodes\cover_letter_bot"
                self._current_proc = subprocess.Popen(
                    [sys.executable, "-u", "batch_generate.py", jd_path, company, profile_name],
                    cwd=cwd_clbot,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                    creationflags=subprocess.CREATE_NO_WINDOW
                )
                for line in iter(self._current_proc.stdout.readline, ""):
                    if self._stop_requested:
                        self._current_proc.terminate()
                        break
                    self._append_log(line)
                self._current_proc.wait()
                code = self._current_proc.returncode

                if code != 0:
                    self._append_log(f"  Failed to generate a cover letter for {company} ({profile_name}).\n")
                    failed += 1
                else:
                    made += 1
                    
                # Clean up the JD text file regardless of success or failure
                try:
                    if os.path.exists(jd_path):
                        os.remove(jd_path)
                except OSError as e:
                    self._append_log(f"  Could not clean up {jd_filename}: {e}\n")

            if todo:
                self._append_log(f"\n{made} cover letter(s) generated, {failed} skipped/failed\n")
            
            if not self.continuous_mode or self._stop_requested:
                break
                
            self._set_status("Waiting for new jobs...")
            if self._sleep_unless_stopped(30):
                break

        end_label = "stopped" if self._stop_requested else "finished"
        self._append_log(f"\n=== Generate Cover Letters: {end_label} at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===\n")
        self._finish()



class ScraperPanel(tk.Frame):
    """Single consolidated scraper control, replacing a separate manual panel
    per platform. One button runs Glassdoor, Hiring Cafe, and Jobgether
    concurrently for a job title/location row from Sheet2 - so up to 3 real
    Chrome windows can be open at the same time. Rows are processed strictly
    one at a time (the next keyword doesn't start until the current one's
    3 platforms all finish), since each platform's browser is launched
    against one fixed, shared Chrome profile directory that a second
    concurrent launch of the same platform can't also open. Each script
    already applies its own Remote-only, ~24h-old, and CAPTCHA-link-skip
    filtering internally; this panel just sequences/parallelizes the
    subprocesses and fans their output out to the shared log box, each
    platform's own log file, and a combined scrape_all_<start timestamp>.log
    - a new one per run, not appended to across runs, so each run's own file
    unambiguously shows when that run started (filename) and ended (its own
    last line).

    Every "Scrape All Jobs" click processes all Sheet2 rows from the top,
    across all 3 platforms - no keyword is skipped for having been scraped
    in an earlier run."""

    ACCENT = ACCENT_PALETTE[2]
    # Folder name (under LOGS_DIR) and log-filename slug per platform.
    PLATFORM_SLUGS = {"Glassdoor": "glassdoor", "Hiring Cafe": "hiring_cafe", "Jobgether": "jobgether"}

    def __init__(self, parent):
        super().__init__(
            parent, bg=COLORS["panel_bg"], bd=0,
            highlightthickness=1, highlightbackground=COLORS["border"],
        )
        self._stop_requested = False
        self._thread = None
        self._current_procs = set()  # subprocess.Popen objects currently running, guarded by _procs_lock
        self._procs_lock = threading.Lock()
        self._log_lock = threading.Lock()  # guards writes to scrape_all.log AND each platform's own log file, since its 3 platform threads log concurrently
        self._platform_log_fhs = {}  # label -> open file handle, set up fresh per run in _run()

        tk.Frame(self, bg=self.ACCENT, height=3).pack(fill="x", side="top")

        tk.Label(
            self, text="Job Scraper", font=("Segoe UI", 12, "bold"),
            fg=self.ACCENT, bg=COLORS["panel_bg"],
        ).pack(pady=(10, 2))
        tk.Label(
            self,
            text="3 platforms in parallel, one keyword at a time\nRemote · United States · posted within 24h",
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

        # A separate log tab per platform instead of one shared box, so
        # concurrent Glassdoor/Hiring Cafe/Jobgether output doesn't interleave
        # into a single hard-to-read stream.
        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.log_boxes = {}
        for label in ("Glassdoor", "Hiring Cafe", "Jobgether"):
            tab = tk.Frame(notebook, bg=COLORS["input_bg"])
            box = scrolledtext.ScrolledText(
                tab, width=58, height=28, state="disabled", bg=COLORS["input_bg"], fg=COLORS["text"],
                insertbackground=COLORS["text"], font=("Consolas", 9), bd=0,
                highlightthickness=1, highlightbackground=COLORS["border"], highlightcolor=self.ACCENT,
            )
            box.pack(fill="both", expand=True)
            notebook.add(tab, text=label)
            self.log_boxes[label] = box

        # Opened fresh per run in _run() - a new timestamped file per click of
        # "Scrape All Jobs" instead of one file appended to forever - so None
        # here just means no run has started yet.
        self._log_fh = None

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
        self._log("\n--- Stop requested: will halt after the current platforms finish ---\n")

    def terminate_now(self):
        """Hard stop used when the app window is closing — kill whichever
        platform subprocesses are currently running instead of waiting for
        them to finish."""
        self._stop_requested = True
        with self._procs_lock:
            procs = list(self._current_procs)
        for proc in procs:
            try:
                proc.terminate()
            except Exception:
                pass

    def clear_log(self):
        def _do():
            for box in self.log_boxes.values():
                box.configure(state="normal")
                box.delete("1.0", "end")
                box.configure(state="disabled")
        self.after(0, _do)

    def _log(self, text):
        """Log an orchestration-level message (batch start/stop, keyword
        header) - mirrored into every platform's tab, since it's not
        specific to one platform, plus the combined scrape_all.log file."""
        text = stamp_log_line(text)

        def _do():
            for box in self.log_boxes.values():
                box.configure(state="normal")
                box.insert("end", text)
                box.see("end")
                box.configure(state="disabled")
        self.after(0, _do)
        # Multiple platform threads can log concurrently now, so the shared
        # file write needs its own lock - without it, interleaved write()
        # calls from different threads can garble lines in scrape_all.log.
        with self._log_lock:
            if self._log_fh is not None:
                self._log_fh.write(text)
                self._log_fh.flush()

    def _close_run_logs(self, end_label, elapsed):
        """Write each platform's own end-of-run marker, then close the
        combined log file and every platform's log file. Clears
        self._log_fh/_platform_log_fhs first (under the lock _log()/
        _log_platform() also use) so any straggler log call from a
        not-yet-wound-down daemon thread just no-ops instead of writing to a
        closed handle."""
        with self._log_lock:
            fh = self._log_fh
            self._log_fh = None
            platform_fhs = self._platform_log_fhs
            self._platform_log_fhs = {}
        if fh is not None:
            fh.close()
        for label, pfh in platform_fhs.items():
            try:
                pfh.write(stamp_log_line(f"=== {label}: {end_label} (total time: {elapsed}) ===\n"))
                pfh.flush()
            except Exception:
                pass
            pfh.close()

    def _log_platform(self, label, text):
        """Log a line that belongs to one specific platform's subprocess -
        goes only into that platform's own tab (not the others), plus the
        combined scrape_all.log file so the full interleaved history is
        still available there even though the on-screen view is now split."""
        text = stamp_log_line(text)
        box = self.log_boxes.get(label)
        if box is not None:
            def _do():
                box.configure(state="normal")
                box.insert("end", text)
                box.see("end")
                box.configure(state="disabled")
            self.after(0, _do)
        with self._log_lock:
            if self._log_fh is not None:
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

    # If a platform's subprocess produces no output at all for this long, it's
    # treated as stuck (e.g. its browser crashed and it's waiting forever on a
    # dead connection) and killed so the batch can move on to the next
    # platform/title instead of hanging indefinitely.
    STALL_TIMEOUT_SECONDS = 300

    def _run_platform(self, label, cwd, cmd):
        """Run one platform's scraper as a subprocess, streaming its output
        into that platform's own log tab/box, its own per-run log file (see
        _run()), and the combined scrape_all log — blocking until it
        finishes, Stop is hit, or it stalls for STALL_TIMEOUT_SECONDS with
        no new output. Returns True only if the subprocess actually exited
        cleanly (code 0, not stalled, not stopped) - callers use this to
        decide whether a keyword truly finished or should stay eligible for
        retry.

        Wrapped in try/finally: this keyword's thread joins all 3 of its
        platform threads before the batch can move on - if this raised
        without cleaning up, that join() would hang forever."""
        def _write_platform_log(text):
            stamped = stamp_log_line(text)
            with self._log_lock:
                fh = self._platform_log_fhs.get(label)
                if fh is not None:
                    fh.write(stamped)
                    fh.flush()

        self._log_platform(label, f"\n$ {' '.join(cmd)}\n\n")
        _write_platform_log(f"$ {' '.join(cmd)}\n\n")
        proc = None
        try:
            try:
                proc = subprocess.Popen(
                    cmd, cwd=cwd, stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1,
                )
            except Exception as e:
                msg = f"Failed to launch {label}: {e}\n"
                self._log_platform(label, msg)
                _write_platform_log(msg)
                return False
            with self._procs_lock:
                self._current_procs.add(proc)

            line_queue = queue.Queue()

            def _reader():
                for line in proc.stdout:
                    line_queue.put(line)
                line_queue.put(None)  # sentinel: process's stdout closed

            threading.Thread(target=_reader, daemon=True).start()

            stalled = False
            while True:
                if self._stop_requested:
                    proc.terminate()
                    break
                try:
                    line = line_queue.get(timeout=self.STALL_TIMEOUT_SECONDS)
                except queue.Empty:
                    stalled = True
                    msg = (
                        f"\n--- {label} produced no output for "
                        f"{self.STALL_TIMEOUT_SECONDS // 60} minutes, "
                        f"terminating (likely stuck) ---\n"
                    )
                    self._log_platform(label, msg)
                    _write_platform_log(msg)
                    proc.terminate()
                    break
                if line is None:
                    break
                self._log_platform(label, line)
                _write_platform_log(line)

            try:
                code = proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                code = proc.wait()
            if not stalled:
                msg = f"\n--- {label} finished (exit code {code}) ---\n"
                self._log_platform(label, msg)
                _write_platform_log(msg)
            return code == 0 and not stalled and not self._stop_requested
        finally:
            if proc is not None:
                with self._procs_lock:
                    self._current_procs.discard(proc)

    def _run(self):
        run_start = datetime.now()
        run_stamp = run_start.strftime('%Y-%m-%d_%H-%M-%S')
        os.makedirs(LOGS_DIR, exist_ok=True)

        # A fresh, separately-named log file per click of "Scrape All Jobs"
        # (instead of one file appended to across every run), so each run's
        # start/end timestamps are unambiguous - the filename itself records
        # when this run started, and the file's own last line records when
        # it ended (see the final _log() call below). Same per-run-file
        # treatment for each platform, each in its own subfolder under
        # LOGS_DIR so every platform's history is easy to find on its own.
        self._log_fh = open(os.path.join(LOGS_DIR, f"scrape_all_{run_stamp}.log"), "a", encoding="utf-8")
        for label, slug in self.PLATFORM_SLUGS.items():
            platform_dir = os.path.join(LOGS_DIR, label)
            os.makedirs(platform_dir, exist_ok=True)
            fh = open(os.path.join(platform_dir, f"{slug}_{run_stamp}.log"), "a", encoding="utf-8")
            fh.write(stamp_log_line(f"=== {label}: starting ===\n"))
            fh.flush()
            self._platform_log_fhs[label] = fh

        self._log("\n=== Scrape All: starting ===\n")
        try:
            titles = self._fetch_titles()
        except Exception as e:
            self._log(f"Failed to read job titles from Sheet2: {e}\n")
            self._finish()
            self._close_run_logs("stopped", format_duration(datetime.now() - run_start))
            return

        self._log(f"Loaded {len(titles)} job titles from Sheet2\n")

        for idx, (title, location) in enumerate(titles, start=1):
            if self._stop_requested:
                break

            self._log(f"\n--- [{idx}/{len(titles)}] {title} | {location} ---\n")
            self._set_status(f"[{idx}/{len(titles)}] {title}", COLORS["running"])

            platform_specs = [
                ("Glassdoor", GLASSD_DIR,
                 [sys.executable, "-u", "glassdoor_scraper_final.py", title, location]),
                ("Hiring Cafe", HIRINGCAFE_DIR,
                 [sys.executable, "-u", "scraper.py", title, location]),
                ("Jobgether", JOBGETHER_DIR,
                 [sys.executable, "-u", "jobgether_scraper.py", title, "15"]),
            ]
            # Run all 3 platforms for this keyword at once - independent
            # subprocesses with their own Chrome profiles, so nothing is
            # shared between them. The next keyword doesn't start until all
            # 3 of these finish.
            results = {}

            def _run_and_record(label, cwd, cmd):
                results[label] = self._run_platform(label, cwd, cmd)

            platform_threads = [
                threading.Thread(target=_run_and_record, args=spec, daemon=True)
                for spec in platform_specs
            ]
            for t in platform_threads:
                t.start()
            for t in platform_threads:
                t.join()

            failed = [label for label, *_ in platform_specs if not results.get(label)]
            if not self._stop_requested and failed:
                self._log(f"  {title} | {location}: {', '.join(failed)} did not finish cleanly\n")

        end_label = "stopped" if self._stop_requested else "finished"
        elapsed = format_duration(datetime.now() - run_start)
        self._log(f"\n=== Scrape All: {end_label} (total time: {elapsed}) ===\n")
        self._finish()
        self._close_run_logs(end_label, elapsed)


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
    
    title_frame = tk.Frame(header, bg=COLORS["bg"])
    title_frame.pack(side="left")
    tk.Label(
        title_frame, text="Job Bot Launcher", font=("Segoe UI", 16, "bold"),
        fg=COLORS["text"], bg=COLORS["bg"],
    ).pack(side="left")
    tk.Label(
        title_frame, text="  scrape  →  tailor  →  apply", font=("Segoe UI", 10),
        fg=COLORS["muted"], bg=COLORS["bg"],
    ).pack(side="left", pady=(4, 0))

    # Add Auto-Pilot controls
    autopilot_frame = tk.Frame(header, bg=COLORS["bg"])
    autopilot_frame.pack(side="right", padx=10)
    
    tk.Label(
        autopilot_frame, text="Auto-Pilot Pipeline:", font=("Segoe UI", 10, "bold"),
        fg=COLORS["text"], bg=COLORS["bg"],
    ).pack(side="left", padx=5)

    def _start_autopilot():
        autopilot_start_btn.configure(state="disabled")
        autopilot_stop_btn.configure(state="normal")
        if not scraper_panel.is_running():
            scraper_panel.start()
        resume_panel.start(continuous=True)
        cover_letter_panel.start(continuous=True)
        apply_panel.start(continuous=True)

    def _stop_autopilot():
        autopilot_start_btn.configure(state="normal")
        autopilot_stop_btn.configure(state="disabled")
        resume_panel.stop()
        cover_letter_panel.stop()
        apply_panel.stop()
        # Note: Scraper panel runs standard once-through Sheet2 then exits natively.
        # But we could stop it if desired.
        if scraper_panel.is_running():
            scraper_panel.stop()

    autopilot_start_btn = ttk.Button(
        autopilot_frame, text="▶ Start Auto-Pilot", width=18, command=_start_autopilot, style="Accent4.TButton",
    )
    autopilot_start_btn.pack(side="left", padx=4)
    autopilot_stop_btn = ttk.Button(
        autopilot_frame, text="⏹ Stop Auto-Pilot", width=16, command=_stop_autopilot, state="disabled", style="Secondary.TButton",
    )
    autopilot_stop_btn.pack(side="left", padx=4)

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
        container, "Resume Bot", "Generate Resumes",
        "ollama_generate.py", RESUMEBOT_DIR,
        log_file=os.path.join(LOGS_DIR, "resume_bot.log"),
    )
    resume_panel.pack(side="left", fill="both", expand=True, padx=6, pady=6)

    cover_letter_panel = CoverLetterBotPanel(
        container, "Cover Letter Bot", "Generate Cover Letters",
        "batch_generate.py", r"C:\Users\webNcodes\Desktop\webncodes\cover_letter_bot",
        log_file=os.path.join(LOGS_DIR, "cover_letter_bot.log"),
    )
    cover_letter_panel.pack(side="left", fill="both", expand=True, padx=6, pady=6)

    class JobBotPanel(BotPanel):
        def build_inputs(self, parent):
            tk.Label(
                parent,
                text="Reads pending job links from the Google Sheet\nand applies using the generated profiles.",
                fg=COLORS["muted"], bg=COLORS["panel_bg"], font=("Segoe UI", 8), justify="center",
            ).pack(anchor="w", pady=(0, 6))

            profile_frame = tk.Frame(parent, bg=COLORS["panel_bg"])
            profile_frame.pack(fill="x", pady=5)
            tk.Label(profile_frame, text="Number of Profiles (0=All):", font=("Segoe UI", 10), fg=COLORS["text"], bg=COLORS["panel_bg"]).pack(side="left", padx=5)
            self.num_profiles_var = tk.IntVar(value=0)
            self.profile_spinbox = tk.Spinbox(profile_frame, from_=0, to=10, textvariable=self.num_profiles_var, width=5, font=("Segoe UI", 10))
            self.profile_spinbox.pack(side="left", padx=5)

        def build_command(self):
            cmd = super().build_command()
            if not cmd:
                return None
            num = self.num_profiles_var.get()
            if num > 0:
                cmd.extend(["--num-profiles", str(num)])
            return cmd

    apply_panel = JobBotPanel(
        container, "Application Bot", "Start Applying",
        "main.py", JOBBOT_DIR,
        log_dir=os.path.join(LOGS_DIR, "Application Bot"), log_prefix="apply",
    )
    apply_panel.pack(side="left", fill="both", expand=True, padx=6, pady=6)

    def on_close():
        scraper_panel.terminate_now()
        resume_panel.terminate_now()
        cover_letter_panel.terminate_now()
        apply_panel.stop()
        if apply_panel.process is not None:
            apply_panel.process.terminate()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
