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

import sys
if getattr(sys, 'frozen', False):
    CURRENT_DIR = os.path.dirname(sys.executable)
else:
    CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox

from datetime import datetime
from tkinter import scrolledtext, ttk

import gspread
from google.oauth2.service_account import Credentials

import os
if getattr(sys, 'frozen', False):
    # If compiled with PyInstaller in monolithic mode, sys.executable is JobBot_Pro_Release\jobbot.exe
    # So BASE_DIR is just the directory containing the exe.
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(CURRENT_DIR)
GLASSD_DIR = os.path.join(BASE_DIR, "scraper", "GlassD")
HIRINGCAFE_DIR = os.path.join(BASE_DIR, "scraper", "Hiring_cafe")
JOBGETHER_DIR = os.path.join(BASE_DIR, "scraper", "Jobgether")
JOBBOT_DIR = os.path.join(BASE_DIR, "Job-Bot")
RESUMEBOT_DIR = os.path.join(BASE_DIR, "resume-bot")
if getattr(sys, 'frozen', False):
    LOGS_DIR = os.path.join(os.path.dirname(sys.executable), "logs")
else:
    LOGS_DIR = os.path.join(CURRENT_DIR, "logs")
# Must match CVS_DIR in resume-bot/ollama_generate.py - that's where the
# actual rendered resumes end up; used here only to check whether one
# already exists for a company (see ResumeBotPanel._run).
CVS_DIR = os.getenv("RESUMES_SAVE_PATH", os.path.join(BASE_DIR, "CVs"))

from dotenv import load_dotenv
load_dotenv(os.path.join(CURRENT_DIR, ".env"))

# Same spreadsheet the scrapers already sync job links into (Sheet1). Sheet2
# holds the job-title/location queue that "Scrape All Platforms" reads from.
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "1Kva2y5-54LXBWMTzNk_xp524ZE7N-CWiqL3VGATUZVM")
SHEET2_NAME = "Sheet2"
SERVICE_ACCOUNT_CANDIDATES = [
    os.path.join(CURRENT_DIR, "service_account.json"),
    os.path.join(CURRENT_DIR, "service_account.json"),
    os.path.join(GLASSD_DIR, "service_account.json"),
]
SERVICE_ACCOUNT_FILE = next((p for p in SERVICE_ACCOUNT_CANDIDATES if os.path.exists(p)), SERVICE_ACCOUNT_CANDIDATES[0])

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
        
        self.edit_kw_btn = ttk.Button(
            btn_frame, text="Edit Keywords", width=14, command=self.edit_keywords, style="Secondary.TButton",
        )
        self.edit_kw_btn.pack(side="left", padx=4)

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
                creationflags=0x08000000,
                env=os.environ.copy()
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
            log_path = self._log_fh.name
            self._log_fh.close()
            self._log_fh = None
            try:
                import log_to_html
                log_to_html.convert_to_html(log_path)
            except Exception as e:
                print(f"Failed to generate HTML log: {e}")

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

    def edit_keywords(self):
        try:
            scopes = ["https://www.googleapis.com/auth/spreadsheets"]
            from google.oauth2.service_account import Credentials
            import gspread
            creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=scopes)
            client = gspread.authorize(creds)
            ws = client.open_by_key(os.getenv("GOOGLE_SHEET_ID", GOOGLE_SHEET_ID)).worksheet(SHEET2_NAME)
            rows = ws.get_all_values()
        except Exception as e:
            from tkinter import messagebox
            messagebox.showerror("Error", f"Failed to load keywords from sheet:\n{e}")
            return
            
        top = tk.Toplevel(self)
        top.title("Edit Scraper Keywords")
        top.geometry("500x400")
        top.configure(bg=COLORS["panel_bg"])
        top.grab_set()
        
        tk.Label(top, text="Format: Job Title | Location\n(One per line)", bg=COLORS["panel_bg"], fg=COLORS["muted"], font=("Segoe UI", 9)).pack(pady=(10, 5))
        
        text_box = scrolledtext.ScrolledText(top, width=50, height=15, bg=COLORS["input_bg"], fg=COLORS["text"], insertbackground=COLORS["text"], font=("Consolas", 10))
        text_box.pack(padx=20, pady=5, fill="both", expand=True)
        
        header = rows[0] if rows else ["Job Title", "Location"]
        content = ""
        for r in rows[1:]:
            title = r[0] if len(r) > 0 else ""
            loc = r[1] if len(r) > 1 else ""
            if title or loc:
                content += f"{title} | {loc}\n"
        text_box.insert("1.0", content)
        
        def save():
            try:
                self.edit_kw_btn.configure(text="Saving...", state="disabled")
                top.update()
                
                new_text = text_box.get("1.0", "end-1c").strip()
                lines = new_text.split('\n')
                new_rows = [header]
                for line in lines:
                    if not line.strip(): continue
                    parts = line.split('|', 1)
                    t = parts[0].strip()
                    l = parts[1].strip() if len(parts) > 1 else "United States"
                    new_rows.append([t, l])
                    
                ws.clear()
                ws.update('A1', new_rows)
                top.destroy()
                from tkinter import messagebox
                messagebox.showinfo("Success", "Keywords synced to Google Sheet2 successfully!")
            except Exception as e:
                from tkinter import messagebox
                messagebox.showerror("Error", f"Failed to save:\n{e}")
            finally:
                self.edit_kw_btn.configure(text="Edit Keywords", state="normal")
                
        ttk.Button(top, text="Save to Google Sheets", command=save, style="Accent2.TButton").pack(pady=(5, 15))

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



def get_available_profiles():
    profiles = ["All Profiles"]
    profiles_dir = os.path.join(BASE_DIR, "profiles") if getattr(sys, 'frozen', False) else os.path.join(BASE_DIR, "Job-Bot", "profiles")
    if os.path.exists(profiles_dir):
        for f in os.listdir(profiles_dir):
            if f.endswith(".json"):
                profiles.append(f[:-5])
                
    if not getattr(sys, 'frozen', False):
        # Optional: also check resume-bot profiles if different
        resume_profiles_dir = os.path.join(BASE_DIR, "resume-bot", "profiles")
        if os.path.exists(resume_profiles_dir):
            for f in os.listdir(resume_profiles_dir):
                if f.endswith(".json"):
                    name = f[:-5]
                    if name not in profiles:
                        profiles.append(name)
                        
    return profiles

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
        self.edit_kw_btn.pack_forget()
        self._stop_requested = False
        self._thread = None
        self._current_proc = None
        self.after(500, self._check_button_state)

    def build_inputs(self, parent):
        tk.Label(
            parent,
            text="Reads Company Name + Job Link from the Google Sheet\nand generates a resume for each row not done yet.",
            fg=COLORS["muted"], bg=COLORS["panel_bg"], font=("Segoe UI", 8), justify="center",
        ).pack(anchor="w", pady=(0, 6))

        profile_frame = tk.Frame(parent, bg=COLORS["panel_bg"])
        profile_frame.pack(fill="x", pady=5)
        
        tk.Label(profile_frame, text="Number of Profiles to Process:", font=("Segoe UI", 10, "bold"), fg=COLORS["text"], bg=COLORS["panel_bg"]).pack(side="left", padx=5)
        self.num_profiles_var = tk.StringVar(value="1")
        vcmd = (self.register(lambda P: P.isdigit() or P == ""), '%P')
        self.profile_spinbox = ttk.Spinbox(profile_frame, from_=1, to=9999, textvariable=self.num_profiles_var, width=5, font=("Segoe UI", 10, "bold"), validate="key", validatecommand=vcmd)
        self.profile_spinbox.pack(side="left", padx=5)
        
        self.active_profiles = []
        
        # Add a shiny button to upload new profile JSONs dynamically!
        def _add_profiles():
            try:
                req_count = int(self.num_profiles_var.get())
            except ValueError:
                messagebox.showerror("Invalid Input", "Please enter a valid number.")
                return
            filepaths = filedialog.askopenfilenames(
                title=f"Select {req_count} Profile JSON(s)",
                filetypes=[("JSON Files", "*.json")]
            )
            if not filepaths:
                return
                
            if len(filepaths) != req_count:
                messagebox.showerror("Count Mismatch", f"You set the Count to {req_count}, but selected {len(filepaths)} files!\n\nPlease select exactly {req_count} file(s).")
                return
                
            try:
                sheet = self._connect_sheet()
                headers = sheet.row_values(1)
                
                # Check for jobs
                jobs = self._fetch_jobs(sheet)
                if not jobs:
                    messagebox.showerror("Empty Sheet", "The Google Sheet is empty or contains no job links!\n\nPlease run the Scraper to add at least one job link before uploading profiles.")
                    return
            except Exception as e:
                messagebox.showerror("Sheet Error", f"Could not connect to Google Sheet:\n{e}")
                return
                
            added_count = 0
            self.active_profiles = []
            
            for fp in filepaths:
                try:
                    import json
                    with open(fp, 'r', encoding='utf-8') as jsf:
                        data = json.load(jsf)
                    
                    # Determine profile name
                    prof_name = data.get("name", "").strip()
                    if not prof_name:
                        # fallback to filename
                        prof_name = os.path.basename(fp).replace(".json", "")
                        
                    # Title case the name for aesthetics
                    prof_name = prof_name.title()
                        
                    # Save to profiles directories
                    import shutil
                    if getattr(sys, 'frozen', False):
                        dest1 = os.path.join(BASE_DIR, "profiles", f"{prof_name}.json")
                        os.makedirs(os.path.dirname(dest1), exist_ok=True)
                        if os.path.abspath(fp) != os.path.abspath(dest1):
                            shutil.copy(fp, dest1)
                    else:
                        dest1 = os.path.join("..", "Job-Bot", "profiles", f"{prof_name}.json")
                        dest2 = os.path.join("..", "resume-bot", "profiles", f"{prof_name}.json")
                        
                        os.makedirs(os.path.dirname(dest1), exist_ok=True)
                        os.makedirs(os.path.dirname(dest2), exist_ok=True)
                        
                        if os.path.abspath(fp) != os.path.abspath(dest1):
                            shutil.copy(fp, dest1)
                        if os.path.abspath(fp) != os.path.abspath(dest2):
                            shutil.copy(fp, dest2)
                    
                    self.active_profiles.append(prof_name)
                    
                    # Check sheet headers
                    headers_lower = [str(h).strip().lower() for h in headers]
                    if prof_name.lower() not in headers_lower:
                        # Append to sheet
                        new_col_idx = len(headers) + 1
                        sheet.update_cell(1, new_col_idx, prof_name)
                        headers.append(prof_name)  # update local cache for next iteration
                        self._append_log(f"\n[+] Added new profile column '{prof_name}' to Google Sheet!\n")
                        added_count += 1
                        
                except Exception as e:
                    self._append_log(f"\n[!] Failed to process profile file '{os.path.basename(fp)}': {e}\n")
                    
            # Update UI label
            if self.active_profiles:
                profiles_str = ", ".join(self.active_profiles)
                self.target_profiles_lbl.configure(text=f"Target Profiles: {profiles_str}", fg="#4CAF50")
            
            messagebox.showinfo("Success", f"Successfully imported {len(self.active_profiles)} profile(s)!")

        tk.Button(
            profile_frame, 
            text="➕ Add Profiles", 
            command=_add_profiles, 
            bg="#2B579A", 
            fg="white", 
            font=("Segoe UI", 9, "bold"), 
            cursor="hand2",
            relief="flat",
            padx=8
        ).pack(side="left", padx=(15, 5))
        
        # Label to show the targeted profiles below it
        self.target_profiles_lbl = tk.Label(parent, text="Target Profiles: Default (First N in Sheet)", font=("Segoe UI", 8, "italic"), fg=COLORS["muted"], bg=COLORS["panel_bg"])
        self.target_profiles_lbl.pack(anchor="w", padx=5, pady=(0, 5))

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


    def _check_button_state(self):
        try:
            val_str = str(self.num_profiles_var.get())
            num = int(val_str)
            
            # Disable if uploaded profiles don't match the required count
            if len(self.active_profiles) != num:
                valid = False
            else:
                valid = True
        except Exception:
            valid = False
            
        if valid and not self.is_running():
            self.start_btn.configure(state="normal")
        else:
            self.start_btn.configure(state="disabled")
            
        self.after(200, self._check_button_state)

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

    def _get_cmd(self, script_name, *args):
        # Monolithic PyInstaller Entrypoint Logic
        # We strip the .py extension to get the command name (e.g. fetch_jd, batch_generate)
        command_name = script_name.replace(".py", "")
        if getattr(sys, 'frozen', False):
            # In compiled mode, sys.executable is jobbot.exe
            cmd = [sys.executable, command_name]
        else:
            # In local source mode, run the script directly with python
            cmd = [sys.executable, "-u", script_name]
        cmd.extend(args)
        return cmd

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
        spreadsheet = client.open_by_key(GOOGLE_SHEET_ID)
        try:
            return spreadsheet.worksheet("Jobs")
        except:
            return spreadsheet.sheet1

    def _fetch_jobs(self, ws):
        rows = ws.get_all_values()[1:]  # skip header row
        jobs = []
        for i, r in enumerate(rows, start=2):  # row 2 is the first data row
            if len(r) < 6:
                continue
            company, link = r[1].strip(), r[5].strip()
            
            if not company and link:
                try:
                    from urllib.parse import urlparse
                    import re
                    host = urlparse(link).netloc
                    host = re.sub(r"^www\.", "", host)
                    parts = host.split(".")
                    if len(parts) >= 2:
                        company = parts[-2].capitalize()
                    else:
                        company = host.capitalize()
                except Exception:
                    company = "Unknown"
                    
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
        self._last_output = ""
        try:
            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"
            proc = subprocess.Popen(
                    cmd, cwd=self.cwd, stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1, encoding='utf-8', errors='replace',
                    creationflags=0x08000000,
                    env=env
                )
        except Exception as e:
            self._append_log(f"Failed to launch: {e}\n")
            return 1
        self._current_proc = proc
        out_lines = []
        try:
            for line in iter(proc.stdout.readline, ""):
                self._append_log(line)
                out_lines.append(line)
                if self._stop_requested:
                    proc.terminate()
        except Exception as e:
            self._append_log(f"\n[Error reading stream: {e}]\n")
        self._last_output = "".join(out_lines)
        code = proc.wait()
        self._current_proc = None
        return None if self._stop_requested else code

    def _run(self):
        while not self._stop_requested:
            try:
                sheet = self._connect_sheet()
                jobs = self._fetch_jobs(sheet)
                if not jobs:
                    self._append_log("The Google Sheet is empty or contains no job links! Please run the scraper first.\n")
                    if self.continuous_mode:
                        if self._sleep_unless_stopped(30): break
                        continue
                    else:
                        self._finish()
                        return
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

            try:
                num_profiles = int(self.num_profiles_var.get())
            except ValueError:
                num_profiles = 1
            if hasattr(self, 'active_profiles') and self.active_profiles:
                profile_names = self.active_profiles
            else:
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
                        
                    pdf_path = os.path.join(CVS_DIR, f"{profile_name} - {company}.pdf")
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
                code = self._run_subprocess(self._get_cmd("fetch_jd.py", link, company))
                if code is None:
                    break
                if code != 0:
                    self._append_log(f"  Fetch failed for {company}, retrying once more in 15s...\n")
                    if self._sleep_unless_stopped(15):
                        break
                    self._append_log(f"$ fetch_jd.py {link} \"{company}\" (retry)\n\n")
                    code = self._run_subprocess(self._get_cmd("fetch_jd.py", link, company))
                    if code is None:
                        break
                if code != 0:
                    self._append_log(f"  Failed to fetch the job description for {company}, skipping.\n")
                    error_msg = "Fetch Failed"
                    if hasattr(self, '_last_output'):
                        out = self._last_output.lower()
                        if "403" in out or "forbidden" in out:
                            error_msg = "Blocked (403)"
                        elif "404" in out or "410" in out or "expired" in out:
                            error_msg = "Expired (404)"
                        elif "invalidschema" in out or "chrome-error" in out:
                            error_msg = "Invalid/Broken Link"
                    try:
                        sheet.update_cell(row, col_index, error_msg)
                    except Exception:
                        pass
                    failed += 1
                    continue

                jd_filename = f"jd_{self._slugify(company)}.txt"
                jd_path = os.path.join(self.cwd, jd_filename)
                try:
                    with open(jd_path, encoding="utf-8") as f:
                        jd_len = len(f.read().strip())
                except OSError as e:
                    self._append_log(f"  Could not read {jd_filename}: {e}, skipping.\n")
                    try:
                        sheet.update_cell(row, col_index, "Failed to read JD")
                    except Exception:
                        pass
                    failed += 1
                    continue
                if jd_len < self.MIN_JD_CHARS:
                    self._append_log(
                        f"  Job description is only {jd_len} characters. This usually "
                        f"indicates an expired job link (404) or consent wall. "
                        f"Skipping resume generation to prevent garbage data.\n"
                    )
                    try:
                        sheet.update_cell(row, col_index, "Invalid JD (Too short)")
                    except Exception:
                        pass
                    try:
                        if os.path.exists(jd_path):
                            os.remove(jd_path)
                    except OSError:
                        pass
                    failed += 1
                    continue

                model_name = os.getenv("OLLAMA_MODEL", "qwen2.5-coder:7b")
                self._set_status(f"[{idx}/{len(todo)}] {company} ({profile_name}) — generating resume")
                self._append_log(f"\n$ ollama_generate.py {jd_filename} \"{company}\" \"{profile_name}\" \"{model_name}\"\n\n")
                code = self._run_subprocess(self._get_cmd("ollama_generate.py", jd_filename, company, profile_name, model_name))
                if code is None:
                    break
                if code != 0:
                    self._append_log(f"  Failed to generate a resume for {company} ({profile_name}).\n")
                    try:
                        sheet.update_cell(row, col_index, "Generation Failed")
                    except Exception:
                        pass
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


class CoverLetterBotPanel(ResumeBotPanel):
    def build_inputs(self, parent):
        tk.Label(
            parent,
            text="Reads Company Name + Job Link from the Google Sheet\nand generates a tailored cover letter for each row not done yet.",
            fg=COLORS["muted"], bg=COLORS["panel_bg"], font=("Segoe UI", 8), justify="center",
        ).pack(anchor="w", pady=(0, 6))

        profile_frame = tk.Frame(parent, bg=COLORS["panel_bg"])
        profile_frame.pack(fill="x", pady=5)
        
        tk.Label(profile_frame, text="Number of Profiles to Process:", font=("Segoe UI", 10, "bold"), fg=COLORS["text"], bg=COLORS["panel_bg"]).pack(side="left", padx=5)
        self.num_profiles_var = tk.StringVar(value="1")
        vcmd = (self.register(lambda P: P.isdigit() or P == ""), '%P')
        self.profile_spinbox = ttk.Spinbox(profile_frame, from_=1, to=9999, textvariable=self.num_profiles_var, width=5, font=("Segoe UI", 10, "bold"), validate="key", validatecommand=vcmd)
        self.profile_spinbox.pack(side="left", padx=5)
        
        self.active_profiles = []
        
        # Add a shiny button to upload new profile JSONs dynamically!
        def _add_profiles():
            try:
                req_count = int(self.num_profiles_var.get())
            except ValueError:
                messagebox.showerror("Invalid Input", "Please enter a valid number.")
                return
            filepaths = filedialog.askopenfilenames(
                title=f"Select {req_count} Profile JSON(s)",
                filetypes=[("JSON Files", "*.json")]
            )
            if not filepaths:
                return
                
            if len(filepaths) != req_count:
                messagebox.showerror("Count Mismatch", f"You set the Count to {req_count}, but selected {len(filepaths)} files!\n\nPlease select exactly {req_count} file(s).")
                return
                
            try:
                sheet = self._connect_sheet()
                headers = sheet.row_values(1)
                
                # Check for jobs
                jobs = self._fetch_jobs(sheet)
                if not jobs:
                    messagebox.showerror("Empty Sheet", "The Google Sheet is empty or contains no job links!\n\nPlease run the Scraper to add at least one job link before uploading profiles.")
                    return
            except Exception as e:
                messagebox.showerror("Sheet Error", f"Could not connect to Google Sheet:\n{e}")
                return
                
            added_count = 0
            self.active_profiles = []
            
            for fp in filepaths:
                try:
                    import json
                    with open(fp, 'r', encoding='utf-8') as jsf:
                        data = json.load(jsf)
                    
                    # Determine profile name
                    prof_name = data.get("name", "").strip()
                    if not prof_name:
                        # fallback to filename
                        prof_name = os.path.basename(fp).replace(".json", "")
                        
                    # Title case the name for aesthetics
                    prof_name = prof_name.title()
                        
                    # Save to profiles directories
                    import shutil
                    if getattr(sys, 'frozen', False):
                        dest1 = os.path.join(BASE_DIR, "profiles", f"{prof_name}.json")
                        os.makedirs(os.path.dirname(dest1), exist_ok=True)
                        if os.path.abspath(fp) != os.path.abspath(dest1):
                            shutil.copy(fp, dest1)
                    else:
                        dest1 = os.path.join("..", "Job-Bot", "profiles", f"{prof_name}.json")
                        dest2 = os.path.join("..", "resume-bot", "profiles", f"{prof_name}.json")
                        
                        os.makedirs(os.path.dirname(dest1), exist_ok=True)
                        os.makedirs(os.path.dirname(dest2), exist_ok=True)
                        
                        if os.path.abspath(fp) != os.path.abspath(dest1):
                            shutil.copy(fp, dest1)
                        if os.path.abspath(fp) != os.path.abspath(dest2):
                            shutil.copy(fp, dest2)
                    
                    self.active_profiles.append(prof_name)
                    
                    # Check sheet headers
                    headers_lower = [str(h).strip().lower() for h in headers]
                    if prof_name.lower() not in headers_lower:
                        # Append to sheet
                        new_col_idx = len(headers) + 1
                        sheet.update_cell(1, new_col_idx, prof_name)
                        headers.append(prof_name)  # update local cache for next iteration
                        self._append_log(f"\n[+] Added new profile column '{prof_name}' to Google Sheet!\n")
                        added_count += 1
                        
                except Exception as e:
                    self._append_log(f"\n[!] Failed to process profile file '{os.path.basename(fp)}': {e}\n")
                    
            # Update UI label
            if self.active_profiles:
                profiles_str = ", ".join(self.active_profiles)
                self.target_profiles_lbl.configure(text=f"Target Profiles: {profiles_str}", fg="#4CAF50")
            
            messagebox.showinfo("Success", f"Successfully imported {len(self.active_profiles)} profile(s)!")

        tk.Button(
            profile_frame, 
            text="➕ Add Profiles", 
            command=_add_profiles, 
            bg="#2B579A", 
            fg="white", 
            font=("Segoe UI", 9, "bold"), 
            cursor="hand2",
            relief="flat",
            padx=8
        ).pack(side="left", padx=(15, 5))
        
        # Label to show the targeted profiles below it
        self.target_profiles_lbl = tk.Label(parent, text="Target Profiles: Default (First N in Sheet)", font=("Segoe UI", 8, "italic"), fg=COLORS["muted"], bg=COLORS["panel_bg"])
        self.target_profiles_lbl.pack(anchor="w", padx=5, pady=(0, 5))

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

    def _run(self):
        self._current_proc = None
        try:
            while True:
                if self._stop_requested:
                    break

                self._set_status("Connecting to Google Sheets...")
                try:
                    sheet = self._connect_sheet()
                    headers = sheet.get_all_values()[0]
                    jobs = self._fetch_jobs(sheet)
                    if not jobs:
                        self._append_log("The Google Sheet is empty or contains no job links! Please run the scraper first.\n")
                        self._finish()
                        return
                except Exception as e:
                    self._append_log(f"Failed to fetch job links: {e}\nRetrying in 15s...\n")
                    if self._sleep_unless_stopped(15):
                        break
                    continue

                selected_prof = getattr(self, "selected_profile_var", None)
                chosen = selected_prof.get().strip() if selected_prof else "All Profiles"
                if chosen and chosen != "All Profiles":
                    profile_names = [chosen]
                    num_profiles = 1
                else:
                    try:
                        num_profiles = int(self.num_profiles_var.get())
                    except ValueError:
                        num_profiles = 1
                    profile_names = headers[8:8+num_profiles]

                todo = []
                COVERLETTER_DIR = os.getenv("COVER_LETTERS_SAVE_PATH", os.path.join(BASE_DIR, "CVs"))
                for company, link, row, row_data in jobs:
                    for profile_name in profile_names:
                        # Don't check the Google sheet for cover letter status, just check the file!
                        pdf_path = os.path.join(COVERLETTER_DIR, f"Cover Letter - {profile_name} - {company}.pdf")
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
                    cwd_resumebot = BASE_DIR if getattr(sys, 'frozen', False) else RESUMEBOT_DIR
                    cmd_fetch = self._get_cmd("fetch_jd.py", link, company)
                
                    # temporarily override cwd for fetch
                    old_cwd = self.cwd
                    self.cwd = cwd_resumebot
                    code = self._run_subprocess(cmd_fetch)
                    self.cwd = old_cwd

                    if code is None:
                        break
                    if code != 0:
                        self._append_log(f"  Fetch failed for {company}, skipping cover letter generation.\n")
                        failed += 1
                        continue

                    model_name = os.getenv("OLLAMA_MODEL", "qwen2.5-coder:7b")
                    self._set_status(f"[{idx}/{len(todo)}] {company} ({profile_name}) — generating cover letter")
                    self._append_log(f"\n$ batch_generate.py {jd_path} \"{company}\" \"{profile_name}\" \"{model_name}\"\n\n")
                
                    # Run batch_generate from cover_letter_bot dir
                    cwd_clbot = BASE_DIR if getattr(sys, 'frozen', False) else os.path.join(BASE_DIR, "cover_letter_bot")
                    cmd_batch = self._get_cmd("batch_generate.py", jd_path, company, profile_name, model_name)
                
                    old_cwd = self.cwd
                    self.cwd = cwd_clbot
                    code = self._run_subprocess(cmd_batch)
                    self.cwd = old_cwd

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

        finally:
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
        
        self.edit_kw_btn = ttk.Button(
            btn_frame, text="Edit Keywords", width=14, command=self.edit_keywords, style="Secondary.TButton",
        )
        self.edit_kw_btn.pack(side="left", padx=4)

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
        self._log("\n--- Stop requested: halting current scrapers immediately ---\n")
        self.terminate_now()

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

    def edit_keywords(self):
        try:
            scopes = ["https://www.googleapis.com/auth/spreadsheets"]
            from google.oauth2.service_account import Credentials
            import gspread
            creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=scopes)
            client = gspread.authorize(creds)
            ws = client.open_by_key(os.getenv("GOOGLE_SHEET_ID", GOOGLE_SHEET_ID)).worksheet(SHEET2_NAME)
            rows = ws.get_all_values()
        except Exception as e:
            from tkinter import messagebox
            messagebox.showerror("Error", f"Failed to load keywords from sheet:\n{e}")
            return
            
        top = tk.Toplevel(self)
        top.title("Edit Scraper Keywords")
        top.geometry("500x400")
        top.configure(bg=COLORS["panel_bg"])
        top.grab_set()
        
        tk.Label(top, text="Format: Job Title | Location\n(One per line)", bg=COLORS["panel_bg"], fg=COLORS["muted"], font=("Segoe UI", 9)).pack(pady=(10, 5))
        
        text_box = scrolledtext.ScrolledText(top, width=50, height=15, bg=COLORS["input_bg"], fg=COLORS["text"], insertbackground=COLORS["text"], font=("Consolas", 10))
        text_box.pack(padx=20, pady=5, fill="both", expand=True)
        
        header = rows[0] if rows else ["Job Title", "Location"]
        content = ""
        for r in rows[1:]:
            title = r[0] if len(r) > 0 else ""
            loc = r[1] if len(r) > 1 else ""
            if title or loc:
                content += f"{title} | {loc}\n"
        text_box.insert("1.0", content)
        
        def save():
            try:
                self.edit_kw_btn.configure(text="Saving...", state="disabled")
                top.update()
                
                new_text = text_box.get("1.0", "end-1c").strip()
                lines = new_text.split('\n')
                new_rows = [header]
                for line in lines:
                    if not line.strip(): continue
                    parts = line.split('|', 1)
                    t = parts[0].strip()
                    l = parts[1].strip() if len(parts) > 1 else "United States"
                    new_rows.append([t, l])
                    
                ws.clear()
                ws.update('A1', new_rows)
                top.destroy()
                from tkinter import messagebox
                messagebox.showinfo("Success", "Keywords synced to Google Sheet2 successfully!")
            except Exception as e:
                from tkinter import messagebox
                messagebox.showerror("Error", f"Failed to save:\n{e}")
            finally:
                self.edit_kw_btn.configure(text="Edit Keywords", state="normal")
                
        ttk.Button(top, text="Save to Google Sheets", command=save, style="Accent2.TButton").pack(pady=(5, 15))

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
            
        try:
            import log_to_html
        except Exception:
            log_to_html = None

        if fh is not None:
            log_path = fh.name
            fh.close()
            if log_to_html:
                try:
                    log_to_html.convert_to_html(log_path)
                except Exception:
                    pass

        for label, pfh in platform_fhs.items():
            try:
                pfh.write(stamp_log_line(f"=== {label}: {end_label} (total time: {elapsed}) ===\n"))
                pfh.flush()
            except Exception:
                pass
            p_log_path = pfh.name
            pfh.close()
            if log_to_html:
                try:
                    log_to_html.convert_to_html(p_log_path)
                except Exception:
                    pass

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


    def _check_button_state(self):
        try:
            val_str = str(self.num_profiles_var.get())
            num = int(val_str)
            
            # Disable if uploaded profiles don't match the required count
            if len(self.active_profiles) != num:
                valid = False
            else:
                valid = True
        except Exception:
            valid = False
            
        if valid and not self.is_running():
            self.start_btn.configure(state="normal")
        else:
            self.start_btn.configure(state="disabled")
            
        self.after(200, self._check_button_state)

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
        ws = client.open_by_key(os.getenv("GOOGLE_SHEET_ID", GOOGLE_SHEET_ID)).worksheet(SHEET2_NAME)
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
                    creationflags=0x08000000,
                    env=os.environ.copy()
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
                ("Glassdoor", BASE_DIR if getattr(sys, 'frozen', False) else GLASSD_DIR,
                 [sys.executable, "glassdoor", title, location] if getattr(sys, 'frozen', False) else [sys.executable, "-u", "glassdoor_scraper_final.py", title, location]),
                ("Hiring Cafe", BASE_DIR if getattr(sys, 'frozen', False) else HIRINGCAFE_DIR,
                 [sys.executable, "hiring_cafe", title, location] if getattr(sys, 'frozen', False) else [sys.executable, "-u", "scraper.py", title, location]),
                ("Jobgether", BASE_DIR if getattr(sys, 'frozen', False) else JOBGETHER_DIR,
                 [sys.executable, "jobgether", title, "15"] if getattr(sys, 'frozen', False) else [sys.executable, "-u", "jobgether_scraper.py", title, "15"]),
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



def check_prerequisites(root):
    import os
    import sys
    import subprocess
    import shutil
    import tkinter as tk
    from tkinter import filedialog, messagebox, messagebox, ttk
    import webbrowser
    
    # 1. Check Service Account
    sa_paths = [
        os.path.join(CURRENT_DIR, "service_account.json"),
        os.path.join(JOBBOT_DIR, "service_account.json"),
    ]
    sa_found = any(os.path.exists(p) for p in sa_paths)
        
    # 2. Check .env and vars
    env_path = os.path.join(CURRENT_DIR, ".env")
    env_dict = {
        "OLLAMA_MODEL": "qwen2.5-coder:7b",
        "OLLAMA_HOST": "http://localhost:11434",
        "GOOGLE_SHEET_ID": ""
    }
    
    if os.path.exists(env_path):
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    if "=" in line and not line.strip().startswith("#"):
                        k, v = line.split("=", 1)
                        env_dict[k.strip()] = v.strip()
        except:
            pass
            
    sheet_id = env_dict.get("GOOGLE_SHEET_ID", "")
            
    # 3. Check Ollama
    ollama_ok = False
    try:
        if subprocess.run("ollama --version", shell=True, capture_output=True, timeout=5).returncode == 0:
            ollama_ok = True
    except:
        pass
        
    # SHOW GUI!
    setup_win = tk.Toplevel(root)
    setup_win.title("Job Bot Configuration Wizard")
    setup_win.geometry("900x820")
    setup_win.grab_set()
    def on_window_close():
        import sys
        root.destroy()
        import os; os._exit(0)
    setup_win.protocol('WM_DELETE_WINDOW', on_window_close)

    
    bg_color = "#121212"
    panel_bg = "#1E1E1E"
    fg_color = "#E0E0E0"
    accent = "#0078D4"
    help_color = "#999999"
    success_color = "#107C10"
    error_color = "#E81123"
    
    setup_win.configure(bg=bg_color)
    
    # Header
    header_frame = tk.Frame(setup_win, bg=bg_color)
    header_frame.pack(fill="x", pady=(25, 10))
    tk.Label(header_frame, text="Job Bot Configuration Wizard", font=("Segoe UI", 20, "bold"), bg=bg_color, fg="#FFFFFF").pack()
    tk.Label(header_frame, text="Complete the setup below to get your Job Bot running smoothly.", fg=help_color, bg=bg_color, font=("Segoe UI", 11)).pack(pady=5)
    
    # Main scrollable area
    canvas_frame = tk.Frame(setup_win, bg=bg_color)
    canvas_frame.pack(fill="both", expand=True, padx=20, pady=10)
    
    canvas = tk.Canvas(canvas_frame, bg=bg_color, highlightthickness=0)
    scrollbar = ttk.Scrollbar(canvas_frame, orient="vertical", command=canvas.yview)
    scrollable_frame = tk.Frame(canvas, bg=bg_color)
    
    scrollable_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
    frame_id = canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
    canvas.bind("<Configure>", lambda e: canvas.itemconfig(frame_id, width=e.width))
    canvas.configure(yscrollcommand=scrollbar.set)
    
    # Mousewheel scrolling
    def _on_mousewheel(event):
        try:
            canvas.yview_scroll(int(-1*(event.delta/120)), "units")
        except Exception:
            pass
        
    canvas.bind_all("<MouseWheel>", _on_mousewheel)
    
    canvas.pack(side="left", fill="both", expand=True)
    scrollbar.pack(side="right", fill="y")
    
    frame = scrollable_frame
    
    # Section 1
    f1 = tk.Frame(frame, bg=panel_bg, highlightbackground="#333333", highlightthickness=1)
    f1.pack(fill="x", pady=10, padx=5)
    f1.columnconfigure(0, weight=1)
    f1.columnconfigure(1, weight=0)
    f1.columnconfigure(2, weight=0)
    
    tk.Label(f1, text="1. Google Cloud Service Account", font=("Segoe UI", 13, "bold"), bg=panel_bg, fg="#FFFFFF").grid(row=0, column=0, columnspan=3, sticky="w", padx=20, pady=(20, 5))
    tk.Label(f1, text="This is the .json key file that grants the bot access to your Google Sheet.", bg=panel_bg, fg=help_color, font=("Segoe UI", 10)).grid(row=1, column=0, columnspan=3, sticky="w", padx=20, pady=(0, 15))
    
    sa_var = tk.StringVar(value="Status: Found ?" if sa_found else "Status: Missing ?")
    tk.Label(f1, textvariable=sa_var, bg=panel_bg, fg=success_color if sa_found else error_color, font=("Segoe UI", 11, "bold")).grid(row=2, column=0, sticky="w", padx=20, pady=(0, 5))
    
    sa_text = tk.Text(f1, height=12, width=65, bg="#121212", fg=fg_color, insertbackground=fg_color, font=("Consolas", 9), relief="flat")
    sa_text.grid(row=3, column=0, columnspan=3, sticky="ew", padx=20, pady=5)
    
    if sa_found:
        try:
            with open(os.path.join(CURRENT_DIR, "service_account.json"), "r", encoding="utf-8") as _f:
                sa_text.insert("1.0", _f.read())
        except: pass

    def upload_sa():
        filepath = filedialog.askopenfilename(filetypes=[("JSON Files", "*.json")])
        if filepath:
            try:
                with open(filepath, "r", encoding="utf-8") as _f:
                    content = _f.read()
                sa_text.delete("1.0", tk.END)
                sa_text.insert("1.0", content)
                sa_var.set("Status: Loaded into editor ?")
            except Exception as e:
                from tkinter import messagebox
                messagebox.showerror("Error", f"Failed to read file: {e}")
            
    tk.Button(f1, text="Browse & Upload .json", command=upload_sa, bg="#333333", fg=fg_color, font=("Segoe UI", 10), cursor="hand2", relief="flat", padx=15, pady=4).grid(row=2, column=1, sticky="w", padx=10)
    
    tk.Label(f1, text="?? How to get it: Google Cloud Console > IAM & Admin > Service Accounts > Create Key (JSON)", bg=panel_bg, fg="#AAAAAA", font=("Segoe UI", 9, "italic")).grid(row=4, column=0, columnspan=3, sticky="w", padx=20, pady=(15, 20))
    err_sa_lbl = tk.Label(f1, text="", fg=error_color, bg=panel_bg, font=("Segoe UI", 10, "bold"))
    err_sa_lbl.grid(row=5, column=0, columnspan=3, sticky="w", padx=20, pady=0)
    err_sa_lbl.grid_remove()
    
    # Section 2
    f2 = tk.Frame(frame, bg=panel_bg, highlightbackground="#333333", highlightthickness=1)
    f2.pack(fill="x", pady=10, padx=5)
    
    tk.Label(f2, text="2. Google Sheet ID", font=("Segoe UI", 13, "bold"), bg=panel_bg, fg="#FFFFFF").grid(row=0, column=0, columnspan=2, sticky="w", padx=20, pady=(20, 5))
    tk.Label(f2, text="The unique ID of the Google Sheet where jobs are stored.", bg=panel_bg, fg=help_color, font=("Segoe UI", 10)).grid(row=1, column=0, columnspan=2, sticky="w", padx=20, pady=(0, 15))
    
    sheet_entry = tk.Entry(f2, width=65, bg="#121212", fg=fg_color, insertbackground=fg_color, font=("Consolas", 11), relief="flat")
    sheet_entry.grid(row=2, column=0, columnspan=2, sticky="ew", padx=20, pady=5, ipady=5)
    f2.columnconfigure(0, weight=1)
    sheet_entry.insert(0, sheet_id)
    
    # 3. Resume Save Folder
    f3 = tk.Frame(frame, bg=panel_bg, highlightbackground="#333333", highlightthickness=1)
    f3.pack(fill="x", pady=10, padx=5)
    
    tk.Label(f3, text="3. Output Folder for Resumes", font=("Segoe UI", 13, "bold"), bg=panel_bg, fg="#FFFFFF").grid(row=0, column=0, columnspan=2, sticky="w", padx=20, pady=(20, 5))
    tk.Label(f3, text="Where should your customized PDF resumes be saved?", bg=panel_bg, fg=help_color, font=("Segoe UI", 10)).grid(row=1, column=0, columnspan=2, sticky="w", padx=20, pady=(0, 15))
    
    resume_path_var = tk.StringVar(value=os.getenv("RESUMES_SAVE_PATH", os.path.join(BASE_DIR, "CVs")))
    
    path_entry = tk.Entry(f3, textvariable=resume_path_var, width=50, bg="#121212", fg=fg_color, insertbackground=fg_color, font=("Consolas", 11), relief="flat")
    path_entry.grid(row=2, column=0, sticky="ew", padx=(20, 5), pady=5, ipady=5)
    
    from tkinter import filedialog
    def browse_path():
        folder = filedialog.askdirectory(title="Select Output Folder")
        if folder:
            resume_path_var.set(folder)
            
    tk.Button(f3, text="Browse...", command=browse_path, font=("Segoe UI", 10, "bold"), bg="#555", fg="white", relief="flat", cursor="hand2").grid(row=2, column=1, padx=(0, 20), pady=5, sticky="ew")
    
    # 3b. Cover Letter Save Folder
    tk.Label(f3, text="Output Folder for Cover Letters", font=("Segoe UI", 13, "bold"), bg=panel_bg, fg="#FFFFFF").grid(row=3, column=0, columnspan=2, sticky="w", padx=20, pady=(20, 5))
    tk.Label(f3, text="Where should your customized PDF cover letters be saved?", bg=panel_bg, fg="#AAAAAA", font=("Segoe UI", 10)).grid(row=4, column=0, columnspan=2, sticky="w", padx=20, pady=(0, 15))
    
    cl_path_var = tk.StringVar(value=os.getenv("COVER_LETTERS_SAVE_PATH", os.path.join(BASE_DIR, "CVs")))
    
    cl_path_entry = tk.Entry(f3, textvariable=cl_path_var, width=50, bg="#121212", fg="#FFFFFF", insertbackground="#FFFFFF", font=("Consolas", 11), relief="flat")
    cl_path_entry.grid(row=5, column=0, sticky="ew", padx=(20, 5), pady=5, ipady=5)
    
    def browse_cl_path():
        folder = filedialog.askdirectory(title="Select Output Folder for Cover Letters")
        if folder:
            cl_path_var.set(folder)
            
    tk.Button(f3, text="Browse...", command=browse_cl_path, font=("Segoe UI", 10, "bold"), bg="#555", fg="white", relief="flat", cursor="hand2").grid(row=5, column=1, padx=(0, 20), pady=5, sticky="ew")
    
    f3.columnconfigure(0, weight=1)

    
    tk.Label(f2, text="ℹ️ Look at your sheet URL: docs.google.com/spreadsheets/d/[THIS_IS_THE_ID]/edit", bg=panel_bg, fg="#AAAAAA", font=("Segoe UI", 9, "italic")).grid(row=3, column=0, columnspan=2, sticky="w", padx=20, pady=(5, 5))
    tk.Label(f2, text="⚠️ CRITICAL: You must share your sheet with the Service Account email as an Editor!", bg=panel_bg, fg="#FFB900", font=("Segoe UI", 9, "bold")).grid(row=4, column=0, columnspan=2, sticky="w", padx=20, pady=(0, 20))
    err_sheet_lbl = tk.Label(f2, text="", fg=error_color, bg=panel_bg, font=("Segoe UI", 10, "bold"))
    err_sheet_lbl.grid(row=5, column=0, columnspan=2, sticky="w", padx=20, pady=0)
    err_sheet_lbl.grid_remove()
    

    # Section 2.5: Email Verification


    # Section 3
    f3 = tk.Frame(frame, bg=panel_bg, highlightbackground="#333333", highlightthickness=1)
    f3.pack(fill="x", pady=10, padx=5)
    
    tk.Label(f3, text="3. Ollama AI Engine", font=("Segoe UI", 13, "bold"), bg=panel_bg, fg="#FFFFFF").grid(row=0, column=0, columnspan=2, sticky="w", padx=20, pady=(20, 5))
    tk.Label(f3, text="Ollama powers the dynamic resume generation and cover letter writing.", bg=panel_bg, fg=help_color, font=("Segoe UI", 10)).grid(row=1, column=0, columnspan=2, sticky="w", padx=20, pady=(0, 15))
    
    status_frame = tk.Frame(f3, bg=panel_bg)
    status_frame.grid(row=2, column=0, columnspan=2, sticky="w", padx=20, pady=(0, 15))
    tk.Label(status_frame, text="Status:", font=("Segoe UI", 11), bg=panel_bg, fg=fg_color).pack(side="left")
    tk.Label(status_frame, text=" Installed & Running ✔ " if ollama_ok else " Not Installed / Not Running ✖ ", bg=success_color if ollama_ok else error_color, fg="#FFFFFF", font=("Segoe UI", 10, "bold")).pack(side="left", padx=10)
    
    if not ollama_ok:
        tk.Label(f3, text="To run Ollama: Open a terminal and type 'ollama serve' or open the Ollama Desktop App.", bg=panel_bg, fg="#FFB900", font=("Segoe UI", 10, "bold")).grid(row=3, column=0, columnspan=2, sticky="w", padx=20, pady=(0, 5))
        def open_ollama(): webbrowser.open("https://ollama.com/download")
        tk.Button(f3, text="Download Ollama", command=open_ollama, bg="#333333", fg=fg_color, relief="flat", cursor="hand2", padx=10).grid(row=4, column=0, sticky="w", padx=20, pady=(0, 15))
    
    input_frame = tk.Frame(f3, bg=panel_bg)
    input_frame.grid(row=5, column=0, columnspan=2, sticky="w", padx=20, pady=(0, 20))
    
    tk.Label(input_frame, text="Host URL:", font=("Segoe UI", 10), bg=panel_bg, fg=fg_color).grid(row=1, column=0, sticky="w", pady=5)
    host_entry = tk.Entry(input_frame, width=35, bg="#121212", fg=fg_color, insertbackground=fg_color, font=("Consolas", 11), relief="flat")
    host_entry.grid(row=1, column=1, columnspan=2, sticky="w", padx=10, pady=5, ipady=4)
    host_entry.insert(0, env_dict.get("OLLAMA_HOST", "http://localhost:11434"))

    tk.Label(input_frame, text="Model Name:", font=("Segoe UI", 10), bg=panel_bg, fg=fg_color).grid(row=0, column=0, sticky="w", pady=5)
    model_entry = ttk.Combobox(input_frame, width=23, font=("Consolas", 11))
    model_entry.grid(row=0, column=1, sticky="w", padx=10, pady=5, ipady=4)
    
    def refresh_models():
        try:
            import urllib.request, json
            host_url = host_entry.get().strip().rstrip('/')
            req = urllib.request.Request(f"{host_url}/api/tags")
            with urllib.request.urlopen(req, timeout=2) as response:
                data = json.loads(response.read().decode())
                models = [m['name'] for m in data.get('models', [])]
                if models:
                    model_entry['values'] = models
                    if model_entry.get() not in models:
                        model_entry.set(models[0])
        except Exception:
            pass
            
    refresh_btn = tk.Button(input_frame, text="🔄 Fetch", command=refresh_models, bg="#555", fg="white", relief="flat", font=("Segoe UI", 9))
    refresh_btn.grid(row=0, column=2, padx=5)
    
    default_model = env_dict.get("OLLAMA_MODEL", "qwen2.5-coder:7b")
    model_entry.set(default_model)
    model_entry['values'] = [default_model]
    host_entry.after(500, refresh_models)
    
    tk.Label(input_frame, text="ℹ️ By default, Ollama runs on http://localhost:11434. If running remotely, use its IP address.", bg=panel_bg, fg="#AAAAAA", font=("Segoe UI", 9, "italic")).grid(row=2, column=0, columnspan=2, sticky="w", pady=(5, 0))
    err_ollama_lbl = tk.Label(input_frame, text="", fg=error_color, bg=panel_bg, font=("Segoe UI", 10, "bold"))
    err_ollama_lbl.grid(row=3, column=0, columnspan=2, sticky="w", pady=(5, 0))
    err_ollama_lbl.grid_remove()
    
    # Section 4 - Verification
    f5 = tk.Frame(frame, bg=bg_color)
    f5.pack(fill="x", pady=(20, 10), padx=5)
    
    def run_verify(btn_widget):
        btn_widget.configure(text="Verifying... Please wait...", state="disabled")
        setup_win.update()
        try:
            c = "# Ollama Configuration\n"
            c += "OLLAMA_MODEL=" + model_entry.get().strip() + "\n"
            c += "OLLAMA_HOST=" + host_entry.get().strip() + "\n\n"
            c += "# Google Sheet Configuration\n"
            c += "GOOGLE_SHEET_ID=" + sheet_entry.get().strip() + "\n"
            c += "RESUMES_SAVE_PATH=" + resume_path_var.get().strip() + "\n"
            c += "COVER_LETTERS_SAVE_PATH=" + cl_path_var.get().strip() + "\n"
            c += "SERVICE_ACCOUNT_JSON=service_account.json\n"
            
            c += "# Automation Behavior\n"
            c += "AUTO_SUBMIT=True\n"
            c += "HEADLESS=False\n"
            c += "STEALTH_MODE=True\n"
            c += "MAX_RETRIES=3\n"
            c += "SCREENSHOT_ON_SUCCESS=True\n"
    
            email = os.environ.get('JOBBOT_LAUNCHER_AUTH', '')
            if email: c += "AUTHORIZED_EMAIL=" + email + "\n"
            
            err_sa_lbl.config(fg=error_color)
            err_sheet_lbl.config(fg=error_color)
            
            err_sa_lbl.grid_remove()
            err_sheet_lbl.grid_remove()
            err_ollama_lbl.grid_remove()
            has_errors = False
            
            # Check Ollama
            import urllib.request
            ollama_url = host_entry.get().strip()
            try:
                req = urllib.request.Request(f"{ollama_url}/api/version")
                with urllib.request.urlopen(req, timeout=3) as resp:
                    if resp.status != 200:
                        raise Exception("Status not 200")
            except Exception as e:
                err_ollama_lbl.config(text=f"Ollama is not running or accessible at {ollama_url}.")
                err_ollama_lbl.grid()
                has_errors = True
            
            sa_content = sa_text.get("1.0", tk.END).strip()
            if sa_content:
                try:
                    with open(os.path.join(CURRENT_DIR, "service_account.json"), "w", encoding="utf-8") as _f:
                        _f.write(sa_content)
                except Exception as e:
                    err_sa_lbl.config(text=f"Could not save service_account.json: {e}")
                    err_sa_lbl.grid()
                    has_errors = True
                    
            if has_errors:
                return
                
            with open(env_path, "w", encoding="utf-8") as f:
                f.write(c)
                
            setup_win.update()
            
            try:
                import gspread
                from google.oauth2.service_account import Credentials
                sa_file = os.path.join(CURRENT_DIR, "service_account.json")
                if not os.path.exists(sa_file):
                    err_sa_lbl.config(text="service_account.json not found! Please upload it.")
                    err_sa_lbl.grid()
                    return
                    
                creds = Credentials.from_service_account_file(sa_file, scopes=["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"])
                client = gspread.authorize(creds)
                
                sid = sheet_entry.get().strip()
                if not sid:
                    err_sheet_lbl.config(text="Sheet ID is empty!")
                    err_sheet_lbl.grid()
                    return
                    
                try:
                    spreadsheet = client.open_by_key(sid)
                    
                    # Auto-create worksheets if they don't exist
                    existing_titles = [ws.title for ws in spreadsheet.worksheets()]
                    
                    if "Sheet1" not in existing_titles:
                        spreadsheet.add_worksheet(title="Sheet1", rows=1000, cols=20)
                    if "Sheet2" not in existing_titles:
                        ws2 = spreadsheet.add_worksheet(title="Sheet2", rows=1000, cols=10)
                        ws2.append_row(["Job Title", "Location"])
                    sheet = spreadsheet.worksheet("Sheet1")
                except Exception as e:
                    err_msg = str(e)
                    if "403" in err_msg or "permission" in err_msg.lower():
                        err_msg = "The Service Account does not have Editor access to this Google Sheet."
                    err_sheet_lbl.config(text=f"Could not open sheet. {err_msg}")
                    err_sheet_lbl.grid()
                    return
                    
                # Check write access
                try:
                    val = sheet.acell('A1').value
                    sheet.update_acell('A1', val or "")
                    
                    btn_done.configure(state="normal", bg=success_color)
                    err_sheet_lbl.config(text="Verification successful! Everything looks great.", fg=success_color)
                    err_sheet_lbl.grid()
                except Exception as e:
                    err_msg = str(e)
                    if "403" in err_msg or "permission" in err_msg.lower():
                        err_msg = "The Service Account does not have Editor access to this Google Sheet."
                    err_sheet_lbl.config(text=f"Cannot write to sheet (read-only?): {err_msg}")
                    err_sheet_lbl.grid()
                        
            except Exception as e:
                err_sheet_lbl.config(text=str(e))
                err_sheet_lbl.grid()
        finally:
            btn_widget.configure(text="⟳ Save & Verify Settings", state="normal")
            
    btn_verify = tk.Button(f5, text="⟳ Save & Verify Settings", bg=accent, fg="white", font=("Segoe UI", 12, "bold"), cursor="hand2", relief="flat", padx=20, pady=8)
    btn_verify.configure(command=lambda: run_verify(btn_verify))
    btn_verify.pack()
    
    # Footer (Packed at the absolute bottom)
    footer_frame = tk.Frame(setup_win, bg="#121212")
    footer_frame.pack(side="bottom", fill="x", pady=20)
    
    def on_done():
        from dotenv import load_dotenv
        load_dotenv(env_path, override=True)
        setup_win.destroy()
        
    btn_done = tk.Button(footer_frame, text="✔ I'm Done, Launch App", command=on_done, font=("Segoe UI", 14, "bold"), bg="#555555", fg="white", cursor="hand2", relief="flat", padx=30, pady=12, state="disabled")
    btn_done.pack()
    
    root.wait_window(setup_win)
    
    # Re-check silently
    sa_paths = [os.path.join(CURRENT_DIR, "service_account.json")]
    if not any(os.path.exists(p) for p in sa_paths): return False
    
    has_sid = False
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                if "GOOGLE_SHEET_ID=" in line and len(line.split("=")[1].strip()) > 5:
                    has_sid = True
    if not has_sid: return False
    return True

def run_auth_check(root):
    import urllib.request, urllib.parse, json, os, shutil
    from tkinter import simpledialog, messagebox
    import tkinter as tk
    
    env_path = os.path.join(CURRENT_DIR, '.env')
    email = ''
    if os.path.exists(env_path):
        with open(env_path, 'r', encoding='utf-8') as f:
            for line in f:
                if line.startswith('AUTHORIZED_EMAIL='):
                    email = line.split('=', 1)[1].strip()
                    
    def verify_email(e):
        try:
            url = 'https://job-bot.webncodes.site/api/verify-email'
            data = urllib.parse.urlencode({'email': e}).encode('utf-8')
            req = urllib.request.Request(url, data=data)
            with urllib.request.urlopen(req) as response:
                result = json.loads(response.read().decode('utf-8'))
                return result.get('valid'), result.get('message', 'Failed')
        except Exception as ex:
            return False, f'API Error: {ex}'
            
    if email:
        valid, msg = verify_email(email)
        if valid:
            os.environ['JOBBOT_LAUNCHER_AUTH'] = email
            return True
            
    # Need to prompt user
    while True:
        dialog = tk.Toplevel(root)
        dialog.title('Authentication Required')
        dialog.geometry('400x150')
        dialog.grab_set()

        def on_auth_close():
            import sys
            root.destroy()
            import os; os._exit(0)
        dialog.protocol('WM_DELETE_WINDOW', on_auth_close)

        
        tk.Label(dialog, text='Enter your Authorized Email:', font=('Segoe UI', 11)).pack(pady=10)
        entry = tk.Entry(dialog, width=40, font=('Segoe UI', 11))
        entry.pack(pady=5)
        if email: entry.insert(0, email)
        
        result_email = [None]
        def submit():
            result_email[0] = entry.get().strip()
            dialog.destroy()
            
        tk.Button(dialog, text='Verify', command=submit, bg='#0078D4', fg='white', width=15).pack(pady=10)
        
        root.wait_window(dialog)
        
        if not result_email[0]:
            return False # Cancelled
            
        valid, msg = verify_email(result_email[0])
        if valid:
            # Append to .env
            env_content = ''
            if os.path.exists(env_path):
                with open(env_path, 'r', encoding='utf-8') as f:
                    env_content = f.read()
                    
            import re
            if 'AUTHORIZED_EMAIL=' in env_content:
                env_content = re.sub(r'AUTHORIZED_EMAIL=.*', f'AUTHORIZED_EMAIL={result_email[0]}', env_content)
            else:
                env_content += f'\nAUTHORIZED_EMAIL={result_email[0]}\n'
                
            with open(env_path, 'w', encoding='utf-8') as f:
                f.write(env_content)
                
            # Copy to subdirs so scrapers get it
            for d in ['GlassD', 'Hiring_cafe', 'Jobgether', 'resume-bot']:
                dpath = os.path.join(os.path.dirname(CURRENT_DIR), d)
                if not os.path.exists(dpath): dpath = os.path.join(CURRENT_DIR, '..', 'scraper', d)
                if os.path.exists(dpath):
                    shutil.copy2(env_path, os.path.join(dpath, '.env'))
                    
            os.environ['JOBBOT_LAUNCHER_AUTH'] = result_email[0]
            messagebox.showinfo('Success', 'Authentication successful!')
            return True
        else:
            messagebox.showerror('Error', f'Verification failed: {msg}')
            email = result_email[0] # keep it for next loop



def sync_configs():
    import shutil
    current_dir = os.path.dirname(os.path.abspath(__file__))
    parent_dir = os.path.dirname(current_dir)
    env_src = os.path.join(current_dir, ".env")
    sa_src = os.path.join(current_dir, "service_account.json")
    
    targets = [
        os.path.join(parent_dir, "scraper", "GlassD"),
        os.path.join(parent_dir, "scraper", "Hiring_cafe"),
        os.path.join(parent_dir, "scraper", "Jobgether"),
        os.path.join(parent_dir, "resume-bot")
    ]
    
    for t in targets:
        if os.path.exists(t):
            if os.path.exists(env_src):
                shutil.copy2(env_src, os.path.join(t, ".env"))
            if os.path.exists(sa_src):
                shutil.copy2(sa_src, os.path.join(t, "service_account.json"))

def main():
    root = tk.Tk()
    root.withdraw() # Hide window during check
    
    if not run_auth_check(root):
        root.destroy()
        sys.exit(1)
        
    sync_configs()

    if not check_prerequisites(root):
        root.destroy()
        sys.exit(1)
        
    root.deiconify() # Show window after check

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
    # autopilot_frame.pack(side="right", padx=10)
    
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
        "ollama_generate.py", BASE_DIR if getattr(sys, 'frozen', False) else RESUMEBOT_DIR,
        log_file=os.path.join(LOGS_DIR, "resume_bot.log"),
    )
    resume_panel.pack(side="left", fill="both", expand=True, padx=6, pady=6)

    cover_letter_panel = CoverLetterBotPanel(
        container, "Cover Letter Bot", "Generate Cover Letters",
        "batch_generate.py", BASE_DIR if getattr(sys, 'frozen', False) else r"C:\Users\webNcodes\Desktop\webncodes\cover_letter_bot",
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

            req_frame = tk.Frame(parent, bg=COLORS["panel_bg"])
            req_frame.pack(fill="x", pady=5)
            self.fill_required_only_var = tk.BooleanVar(value=False)
            tk.Checkbutton(req_frame, text="Only fill compulsory fields (ignore optional)", variable=self.fill_required_only_var, fg=COLORS["text"], bg=COLORS["panel_bg"], selectcolor=COLORS["input_bg"], activebackground=COLORS["panel_bg"], activeforeground=COLORS["text"], font=("Segoe UI", 10)).pack(side="left", padx=5)

        def build_command(self):
            cmd = super().build_command()
            if not cmd:
                return None
            num = self.num_profiles_var.get()
            if num > 0:
                cmd.extend(["--num-profiles", str(num)])
            if self.fill_required_only_var.get():
                cmd.extend(["--fill-required-only"])
            return cmd

    apply_panel = JobBotPanel(
        container, "Application Bot", "Start Applying",
        "main.py", JOBBOT_DIR,
        log_dir=os.path.join(LOGS_DIR, "Application Bot"), log_prefix="apply",
    )
    # apply_panel.pack(side="left", fill="both", expand=True, padx=6, pady=6) # Hidden for now

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
