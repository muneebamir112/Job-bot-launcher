import os
import sys
import subprocess

# Allow execution from launcher without security alert
os.environ["JOBBOT_LAUNCHER_AUTH"] = "1"

if getattr(sys, 'frozen', False):
    CURRENT_DIR = os.path.dirname(sys.executable)
else:
    CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
    
BASE_DIR = os.path.dirname(CURRENT_DIR)
PROFILE_DIR = os.path.join(BASE_DIR, "scraper", "GlassD", "profiles", "glassdoor_profile")

def find_chrome():
    paths = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe")
    ]
    for p in paths:
        if os.path.exists(p):
            return p
    return None

def main():
    os.makedirs(PROFILE_DIR, exist_ok=True)
    print("Opening Glassdoor profile for manual login natively...")
    
    chrome_path = find_chrome()
    if not chrome_path:
        print("Could not find Google Chrome installed on this system.")
        sys.exit(1)
        
    cmd = [
        chrome_path,
        f"--user-data-dir={PROFILE_DIR}",
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
