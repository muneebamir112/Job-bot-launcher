import sys

path = r'd:\webncodes\Job-bot-launcher\launcher.py'
with open(path, 'r', encoding='utf-8') as f:
    content = f.read()

# 1. Update _add_profiles
bad_add = '''            try:
                sheet = self._connect_sheet()
                headers = sheet.row_values(1)
            except Exception as e:
                messagebox.showerror("Sheet Error", f"Could not connect to Google Sheet:\\n{e}")
                return'''
good_add = '''            try:
                sheet = self._connect_sheet()
                headers = sheet.row_values(1)
                
                # Check for jobs
                jobs = self._fetch_jobs(sheet)
                if not jobs:
                    messagebox.showerror("Empty Sheet", "The Google Sheet is empty or contains no job links!\\n\\nPlease run the Scraper to add at least one job link before uploading profiles.")
                    return
            except Exception as e:
                messagebox.showerror("Sheet Error", f"Could not connect to Google Sheet:\\n{e}")
                return'''

# 2. Update ResumeBotPanel _run
bad_run_res = '''            try:
                sheet = self._connect_sheet()
                jobs = self._fetch_jobs(sheet)
                headers = sheet.row_values(1)
            except Exception as e:
                self._append_log(f"Failed to read jobs from the Google Sheet: {e}\\n")
                if self.continuous_mode:
                    if self._sleep_unless_stopped(30):
                        break
                    continue
                else:
                    self._finish()
                    return'''
good_run_res = '''            try:
                sheet = self._connect_sheet()
                jobs = self._fetch_jobs(sheet)
                if not jobs:
                    self._append_log("The Google Sheet is empty or contains no job links! Please run the scraper first.\\n")
                    if self.continuous_mode:
                        if self._sleep_unless_stopped(30): break
                        continue
                    else:
                        self._finish()
                        return
                headers = sheet.row_values(1)
            except Exception as e:
                self._append_log(f"Failed to read jobs from the Google Sheet: {e}\\n")
                if self.continuous_mode:
                    if self._sleep_unless_stopped(30):
                        break
                    continue
                else:
                    self._finish()
                    return'''

# 3. Update CoverLetterBotPanel _run
bad_run_cl = '''                try:
                    sheet = self._connect_sheet()
                    headers = sheet.get_all_values()[0]
                    jobs = self._fetch_jobs(sheet)
                except Exception as e:
                    self._append_log(f"Failed to fetch job links: {e}\\nRetrying in 15s...\\n")
                    if self._sleep_unless_stopped(15):
                        break
                    continue'''
good_run_cl = '''                try:
                    sheet = self._connect_sheet()
                    headers = sheet.get_all_values()[0]
                    jobs = self._fetch_jobs(sheet)
                    if not jobs:
                        self._append_log("The Google Sheet is empty or contains no job links! Please run the scraper first.\\n")
                        self._finish()
                        return
                except Exception as e:
                    self._append_log(f"Failed to fetch job links: {e}\\nRetrying in 15s...\\n")
                    if self._sleep_unless_stopped(15):
                        break
                    continue'''

if bad_add in content:
    content = content.replace(bad_add, good_add)
    print("Replaced _add_profiles")
if bad_run_res in content:
    content = content.replace(bad_run_res, good_run_res)
    print("Replaced Resume _run")
if bad_run_cl in content:
    content = content.replace(bad_run_cl, good_run_cl)
    print("Replaced Cover Letter _run")

with open(path, 'w', encoding='utf-8') as f:
    f.write(content)
print("Done")
