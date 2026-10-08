import os
import sys

# Allow execution from launcher without security alert
os.environ["JOBBOT_LAUNCHER_AUTH"] = "1"

if getattr(sys, 'frozen', False):
    CURRENT_DIR = os.path.dirname(sys.executable)
else:
    CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
    
BASE_DIR = os.path.dirname(CURRENT_DIR)
PROFILE_DIR = os.path.join(BASE_DIR, "scraper", "GlassD", "profiles", "glassdoor_profile")

def main():
    try:
        from patchright.sync_api import sync_playwright
    except ImportError:
        print("patchright is not installed.")
        sys.exit(1)
        
    os.makedirs(PROFILE_DIR, exist_ok=True)
    print("Opening Glassdoor profile for manual login...")
    with sync_playwright() as p:
        try:
            context = p.chromium.launch_persistent_context(
                PROFILE_DIR,
                channel="chrome",
                headless=False,
                no_viewport=True,
            )
        except Exception as e:
            print(f"Launch failed: {e}")
            return

        page = context.pages[0] if context.pages else context.new_page()
        page.goto("https://www.glassdoor.com/Job/index.htm")
        print("Browser opened for manual login. Close the browser window when done.")
        
        try:
            context.wait_for_event("close", timeout=0)
        except Exception:
            pass

if __name__ == "__main__":
    main()
