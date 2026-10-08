import asyncio
import gspread
import logging
import os
from google.oauth2.service_account import Credentials
from playwright.async_api import async_playwright

# --- Setup Logging ---
os.makedirs('logs', exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler("logs/scrape_questions.log"),
        logging.StreamHandler()
    ]
)

# --- Configuration ---
SHEET_ID = "1mAc6E1jyafvlBZ6pWnoEykKatuJJePV1YJwTzi87Qg8"
SERVICE_ACCOUNT_FILE = 'service_account.json'
INPUT_TAB = "Jobs"
OUTPUT_TAB = "questions"

async def scrape_questions_from_page(context, url):
    """
    Attempts to find all questions on common job board pages, excluding standard profile fields.
    """
    questions = []
    page = await context.new_page()
    
    # Common words associated with standard profile fields that we want to EXCLUDE
    excluded_keywords = [
        'name', 'first name', 'last name', 'email', 'phone', 'address', 'location',
        'city', 'state', 'zip', 'resume', 'cv', 'cover letter', 'linkedin', 'github',
        'website', 'portfolio', 'school', 'degree', 'university', 'company', 
        'current employer', 'experience', 'country', 'gender', 'race', 'veteran', 'disability'
    ]

    try:
        await page.goto(url, timeout=30000, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)

        # Attempt to find and click "Apply" or "Open Form" buttons
        try:
            apply_selectors = [
                "button:has-text('Apply')", "a:has-text('Apply')",
                "button:has-text('Apply Now')", "a:has-text('Apply Now')",
                "button:has-text('Apply for this job')", "a:has-text('Apply for this job')",
                "button:has-text('Easy Apply')", "button:has-text('Apply to Job')",
                "a:has-text('Open Form')", "button:has-text('Open Form')"
            ]
            
            clicked = False
            for selector in apply_selectors:
                elements = await page.query_selector_all(selector)
                for el in elements:
                    if await el.is_visible():
                        logging.info("  -> Found apply button, clicking...")
                        initial_pages = len(context.pages)
                        await el.click(timeout=5000)
                        await page.wait_for_timeout(4000)
                        
                        # Check if a new tab was opened
                        if len(context.pages) > initial_pages:
                            # Switch to the new page
                            page = context.pages[-1]
                            try:
                                await page.wait_for_load_state("domcontentloaded", timeout=10000)
                            except Exception:
                                pass
                            logging.info("  -> Switched to new tab.")
                            
                        clicked = True
                        break
                if clicked:
                    break
        except Exception as e:
            pass # Ignore errors if we can't click an apply button

        # We will collect from all frames on the page (in case the form is embedded in an iframe)
        for frame in page.context.pages[-1].frames:
            try:
                # Find all labels, question headings, and textareas inside this frame
                elements = await frame.query_selector_all('label, legend, [role="heading"], [class*="question"], [class*="label"]')
                for el in elements:
                    text = await el.inner_text()
                    text = text.strip()
                    if not text or len(text) < 3:
                        continue
                        
                    text_lower = text.lower()
                    
                    is_excluded = False
                    if len(text_lower) < 40:
                        for kw in excluded_keywords:
                            if kw in text_lower:
                                is_excluded = True
                                break
                    
                    if 'demographic' in text_lower or 'eeo' in text_lower or 'equal opportunity' in text_lower:
                        is_excluded = True

                    if not is_excluded and text not in questions:
                         questions.append(text)
                             
                inputs = await frame.query_selector_all('textarea, input[type="text"]')
                for ta in inputs:
                    aria_label = await ta.get_attribute('aria-label')
                    placeholder = await ta.get_attribute('placeholder')
                    
                    for attr in (aria_label, placeholder):
                        if attr:
                            attr_clean = attr.strip()
                            attr_lower = attr_clean.lower()
                            is_excluded = False
                            if len(attr_lower) < 40:
                                for kw in excluded_keywords:
                                    if kw in attr_lower:
                                        is_excluded = True
                                        break
                            if not is_excluded and attr_clean not in questions:
                                questions.append(attr_clean)
            except Exception:
                pass # Ignore if we can't access a cross-origin frame directly

    except Exception as e:
        logging.error(f"Error scraping {url}: {e}")
        
    # Close all open pages to prevent memory leaks from new tabs
    for p in context.pages:
        try:
            await p.close()
        except Exception:
            pass
            
    return list(set([q.strip() for q in questions if q and len(q.strip()) > 3]))

async def main():
    logging.info("Authenticating with Google Sheets...")
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    credentials = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=scopes)
    gc = gspread.authorize(credentials)

    logging.info(f"Opening Spreadsheet ID: {SHEET_ID}")
    try:
        sh = gc.open_by_key(SHEET_ID)
        jobs_sheet = sh.worksheet(INPUT_TAB)
    except Exception as e:
        logging.error(f"Failed to open sheet or tab '{INPUT_TAB}'. Error: {e}")
        return

    try:
        # Fetch all worksheets and find a case-insensitive match
        worksheets = sh.worksheets()
        questions_sheet = next((ws for ws in worksheets if ws.title.strip().lower() == OUTPUT_TAB.strip().lower()), None)
        
        
        if not questions_sheet:
            logging.info(f"Tab '{OUTPUT_TAB}' not found. Creating it...")
            questions_sheet = sh.add_worksheet(title=OUTPUT_TAB, rows="1000", cols="2")
    except Exception as e:
        logging.error(f"Error accessing or creating the questions tab: {e}")
        return

    logging.info("Fetching jobs data...")
    records = jobs_sheet.get_all_records()
    
    if not records:
        logging.warning(f"No records found in {INPUT_TAB} tab.")
        return

    # Check headers dynamically
    first_record = records[0]
    company_key = None
    url_key = None
    
    for key in first_record.keys():
        if "company" in key.lower():
            company_key = key
        elif "url" in key.lower() or "link" in key.lower():
            url_key = key
            
            
    if not url_key or not company_key:
        logging.error(f"Could not automatically detect Company or URL columns. Found columns: {list(first_record.keys())}")
        return

    logging.info(f"Preparing '{OUTPUT_TAB}' tab...")
    questions_sheet.clear()
    questions_sheet.update('A1:B1', [["Company Name", "Questions"]])

    results = []

    logging.info("Starting browser for scraping...")
    async with async_playwright() as p:
        # Set headless=False so the browser window opens visibly
        browser = await p.chromium.launch(headless=False) 
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        for idx, row in enumerate(records):
            company_name = row.get(company_key, "Unknown Company")
            url = row.get(url_key, "")
            
            if not url or "http" not in url:
                continue
                
            logging.info(f"Scraping {company_name} ({url})...")
            questions = await scrape_questions_from_page(context, url)
            
            if questions:
                # Prepare a list of rows to append, leaving company name blank after the first row
                rows_to_append = []
                for i, q in enumerate(questions):
                    if i == 0:
                        rows_to_append.append([company_name, q])
                    else:
                        rows_to_append.append(["", q])
                        
                logging.info(f"  -> Found {len(questions)} potential questions.")
                try:
                    # append_rows adds multiple rows in one API call
                    questions_sheet.append_rows(rows_to_append)
                except Exception as e:
                    logging.error(f"  -> Failed to save to sheet: {e}")
            else:
                results.append([company_name, "No questions found."])
                logging.info("  -> No questions found.")
                try:
                    questions_sheet.append_row([company_name, "No questions found."])
                except Exception as e:
                    logging.error(f"  -> Failed to save to sheet: {e}")

        try:
            await browser.close()
        except Exception:
            pass

    logging.info("Scraping finished!")

if __name__ == "__main__":
    asyncio.run(main())
