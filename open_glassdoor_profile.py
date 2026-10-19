import os
import sys

# Allow execution from launcher without security alert
os.environ["JOBBOT_LAUNCHER_AUTH"] = "1"

if getattr(sys, 'frozen', False):
    CURRENT_DIR = os.path.dirname(sys.executable)
    PROFILE_DIR = os.path.join(CURRENT_DIR, "profiles", "glassdoor_profile")
else:
    CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
    
BASE_DIR = os.path.dirname(CURRENT_DIR)
PROFILE_DIR = os.path.join(BASE_DIR, "scraper", "GlassD", "profiles", "glassdoor_profile")

def find_chrome():
    paths = [
        "/opt/google/chrome/chrome",
        "/usr/bin/google-chrome",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe")
    ]
    for p in paths:
        if os.path.exists(p):
            return p
    return None

def main():
    try:
        from patchright.sync_api import sync_playwright
    except ImportError:
        print("patchright is not installed.")
        sys.exit(1)
    print(f"Profile Directory: {PROFILE_DIR}")
    cmd = [
        chrome_path,
        f"--user-data-dir={PROFILE_DIR}",
        "--password-store=basic",
        "--no-first-run",
        "--no-default-browser-check",
        "https://www.glassdoor.com/Job/index.htm"
    ]
    
    print("Browser opened for manual login. Close the browser window when done.")
    try:
        # Wait for the user to close Chrome
        proc = subprocess.Popen(cmd)
        proc.wait()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"Error launching Chrome: {e}")

if __name__ == "__main__":
    main()
