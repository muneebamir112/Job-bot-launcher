import asyncio
import os
import sys
import re
import logging
from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

try:
    from patchright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
except ImportError:
    from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

if getattr(sys, 'frozen', False):
    CURRENT_DIR = os.path.dirname(sys.executable)
    BASE_DIR = CURRENT_DIR
else:
    CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
    BASE_DIR = os.path.dirname(CURRENT_DIR)

load_dotenv(os.path.join(CURRENT_DIR, ".env"))

# Scopes & Config
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive"
]

GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "1mAc6E1jyafvlBZ6pWnoEykKatuJJePV1YJwTzi87Qg8")
GOOGLE_DOC_ID = os.getenv("GOOGLE_DOC_ID", "1ho1Og5uYPHweKDe1ymSFdwS63tX95cr9CHWxsvemMZI")
SERVICE_ACCOUNT_FILE = os.path.join(CURRENT_DIR, "service_account.json")
if not os.path.exists(SERVICE_ACCOUNT_FILE):
    alt_sa = os.path.join(BASE_DIR, "service_account.json")
    if os.path.exists(alt_sa):
        SERVICE_ACCOUNT_FILE = alt_sa

LOGS_DIR = os.path.join(CURRENT_DIR, "logs")
os.makedirs(LOGS_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOGS_DIR, "scrape_questions.log")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("QuestionScraper")

# Import the Google Doc Writer
import google_doc_writer

# Standard identity keywords that are not custom employer questions
EXCLUDED_BASIC_KEYWORDS = [
    'first name', 'given name', 'last name', 'family name', 'middle name',
    'email', 'phone', 'phone number', 'mobile', 'address', 'street address',
    'postal code', 'zip code', 'city', 'state', 'resume', 'curriculum vitae',
    'attach resume', 'upload resume', 'cover letter', 'attach cover letter',
    'linkedin profile', 'linkedin url', 'github profile', 'github url',
    'portfolio website', 'personal website'
]

async def dismiss_popups(page):
    selectors = [
        "button:has-text('Accept All')", "button:has-text('Accept Cookies')",
        "button:has-text('I agree')", "button:has-text('Got it')",
        "button:has-text('Dismiss')", "button[aria-label='Close']",
        "button.close", ".close-modal"
    ]
    for s in selectors:
        try:
            for el in await page.locator(s).all():
                if await el.is_visible():
                    await el.click(timeout=1000)
                    await asyncio.sleep(0.3)
        except Exception:
            pass

async def click_apply_button_if_present(page, context):
    """
    If on a job landing / JD page, clicks 'Apply' or 'Apply Now' to reveal the application form.
    Handles multi-tab navigation if a new tab is opened.
    """
    apply_selectors = [
        "a[href*='apply']:not([href*='linkedin']):not([href*='indeed'])",
        "button:has-text('Apply Now')", "a:has-text('Apply Now')",
        "button:has-text('Apply for this job')", "a:has-text('Apply for this job')",
        "button:has-text('Apply Online')", "a:has-text('Apply Online')",
        "button:has-text('Start Application')", "a:has-text('Start Application')",
        "[data-automation-id='applyButton']", "[data-automation-id='jobFoundationalApplyButton']",
        "button:has-text('Apply')", "a:has-text('Apply')"
    ]
    
    target_page = page
    initial_pages = len(context.pages)
    
    for sel in apply_selectors:
        try:
            btns = await page.locator(sel).all()
            for btn in btns[:2]:
                if await btn.is_visible() and await btn.is_enabled():
                    btn_text = (await btn.inner_text()).strip()
                    logger.info(f"  -> Found apply trigger '{btn_text}', clicking...")
                    await btn.click(timeout=3000)
                    await asyncio.sleep(3)
                    
                    if len(context.pages) > initial_pages:
                        target_page = context.pages[-1]
                        await target_page.wait_for_load_state("domcontentloaded", timeout=10000)
                        logger.info("  -> Switched to newly opened application tab.")
                    return target_page
        except Exception:
            continue
            
    return target_page

async def extract_form_questions(page) -> list:
    """
    Scans the page and all embedded frames (ATS iframes like Greenhouse, Lever, Workday)
    for custom application questions, extracting both the question prompt and its available
    options (dropdown options, radio choices, checkbox options).
    """
    raw_items = []
    seen_prompts = set()

    for frame in page.frames:
        try:
            frame_results = await frame.evaluate('''() => {
                const results = [];
                const localSeen = new Set();

                // Helper: clean text
                const clean = (t) => (t || '').trim().replace(/\\s+/g, ' ');

                // 1. Group containers: Fieldsets, [role="radiogroup"], [role="group"], custom questions
                const groupContainers = document.querySelectorAll(
                    'fieldset, [role="radiogroup"], [role="group"], .application-question, [class*="question"], [class*="form-group"]'
                );
                for (const container of groupContainers) {
                    const headingEl = container.querySelector(
                        'legend, [role="heading"], [class*="legend"], [class*="label"], [class*="title"], [class*="prompt"], h3, h4, h5, p'
                    );
                    if (!headingEl) continue;
                    const qText = clean(headingEl.innerText || headingEl.textContent);
                    if (!qText || qText.length < 5 || qText.length > 350) continue;
                    if (localSeen.has(qText.toLowerCase())) continue;

                    // Check for radio or checkbox options inside this container
                    const optionEls = container.querySelectorAll(
                        'input[type="radio"], input[type="checkbox"], [role="radio"], [role="checkbox"]'
                    );
                    const options = [];
                    for (const optEl of optionEls) {
                        let optText = '';
                        if (optEl.id) {
                            const l = container.querySelector(`label[for="${optEl.id}"]`);
                            if (l) optText = l.innerText;
                        }
                        if (!optText && optEl.closest('label')) {
                            optText = optEl.closest('label').innerText;
                        }
                        if (!optText && optEl.value && optEl.value !== 'on') {
                            optText = optEl.value;
                        }
                        const cleanOpt = clean(optText);
                        if (cleanOpt && cleanOpt.toLowerCase() !== qText.toLowerCase() && !options.includes(cleanOpt) && cleanOpt.length < 150) {
                            options.push(cleanOpt);
                        }
                    }

                    if (options.length > 0) {
                        localSeen.add(qText.toLowerCase());
                        results.push({question: qText, options: options});
                    }
                }

                // Helper: check if a list of options contains phone country dial codes (e.g. Afghanistan+93)
                const isCountryCodeList = (list) => {
                    let matches = 0;
                    for (const item of list) {
                        if (/\\+\\d{1,4}$|^\\+\\d{1,4}/.test(item.trim())) {
                            matches++;
                            if (matches >= 2) return true;
                        }
                    }
                    return false;
                };

                // 2. Select dropdowns
                const selects = document.querySelectorAll('select');
                for (const sel of selects) {
                    let qText = '';
                    if (sel.id) {
                        const l = document.querySelector(`label[for="${sel.id}"]`);
                        if (l) qText = l.innerText;
                    }
                    if (!qText && sel.getAttribute('aria-label')) {
                        qText = sel.getAttribute('aria-label');
                    }
                    if (!qText) {
                        const p = sel.closest('label, .form-group, .field, [class*="field"], [class*="input"], [class*="row"]');
                        if (p) {
                            const l = p.querySelector('label, [class*="label"], [class*="title"], p, h4, h5');
                            if (l && !l.contains(sel)) qText = l.innerText;
                        }
                    }
                    qText = clean(qText);
                    if (!qText || qText.length < 5 || localSeen.has(qText.toLowerCase())) continue;

                    const opts = [];
                    for (const o of sel.options) {
                        const ot = clean(o.text || o.innerText);
                        if (ot && !['select', 'select...', 'please select', 'choose', 'none', ''].includes(ot.toLowerCase())) {
                            if (!opts.includes(ot)) opts.push(ot);
                        }
                    }

                    // If this dropdown is purely phone country codes, skip it
                    if (isCountryCodeList(opts)) {
                        continue;
                    }

                    localSeen.add(qText.toLowerCase());
                    results.push({question: qText, options: opts});
                }

                // 3. Text inputs, textareas, and comboboxes
                const textFields = document.querySelectorAll(
                    'input[type="text"], input[type="url"], input[type="number"], textarea, [role="combobox"]'
                );
                for (const tf of textFields) {
                    let qText = '';
                    if (tf.id) {
                        const l = document.querySelector(`label[for="${tf.id}"]`);
                        if (l) qText = l.innerText;
                    }
                    if (!qText && tf.getAttribute('aria-label')) {
                        qText = tf.getAttribute('aria-label');
                    }
                    if (!qText) {
                        const p = tf.closest('label, .form-group, .field, [class*="field"], [class*="input"], [class*="row"]');
                        if (p) {
                            const l = p.querySelector('label, [class*="label"], [class*="title"], p, h4, h5');
                            if (l && !l.contains(tf)) qText = l.innerText;
                        }
                    }
                    if (!qText && tf.getAttribute('placeholder')) {
                        qText = tf.getAttribute('placeholder');
                    }
                    qText = clean(qText);
                    if (!qText || qText.length < 5 || localSeen.has(qText.toLowerCase())) continue;

                    // Check if this combobox has visible options strictly associated with it
                    let opts = [];
                    if (tf.getAttribute('role') === 'combobox') {
                        let listbox = null;
                        const controlsId = tf.getAttribute('aria-controls') || tf.getAttribute('aria-owns');
                        if (controlsId) {
                            listbox = document.getElementById(controlsId);
                        }
                        if (!listbox) {
                            const parentContainer = tf.closest('.form-group, .field, [class*="field"], [class*="select"], [class*="form-item"]');
                            if (parentContainer) {
                                listbox = parentContainer.querySelector('[role="listbox"], .select__menu');
                            }
                        }
                        if (listbox) {
                            const optNodes = listbox.querySelectorAll('[role="option"], .select__option');
                            for (const on of optNodes) {
                                const ot = clean(on.innerText);
                                if (ot && !opts.includes(ot)) opts.push(ot);
                            }
                        }
                    }

                    // Never attach phone country dial codes as combobox options
                    if (isCountryCodeList(opts)) {
                        opts = [];
                    }

                    localSeen.add(qText.toLowerCase());
                    results.push({question: qText, options: opts});
                }

                return results;
            }''')

            for item in frame_results:
                raw_items.append(item)
        except Exception as e:
            logger.debug(f"Frame evaluation note: {e}")

    # Clean and filter questions
    cleaned = []
    
    for item in raw_items:
        q_raw = item.get("question", "")
        # Remove trailing asterisks and colons
        q_norm = re.sub(r'[\*\:\s]+$', '', " ".join(q_raw.split())).strip()
        q_lower = q_norm.lower()

        if not q_norm or len(q_norm) < 4:
            continue

        # Ignore UI placeholders, language pickers, cookie banners, and search inputs
        if q_lower in [
            'start typing...', 'start typing', 'type here...', 'type here',
            'type your answer here', 'enter your answer', 'select an option',
            'select one', 'please select', 'choose...', 'choose one', 'none',
            'search', 'cookie list search', 'select language', 'language',
            'country', 'select country', 'country/region', 'country / region',
            'country code', 'phone country code', 'dial code'
        ]:
            continue

        # Check if options are phone country dial codes (e.g. Afghanistan+93)
        opts = item.get("options", [])
        dial_matches = sum(1 for o in opts if re.search(r'\+\d{1,4}$|^\+\d{1,4}', str(o).strip()))
        if dial_matches >= 2:
            opts = []
            if q_lower in ['country', 'country code', 'phone country code', 'dial code', 'search']:
                continue

        # Filter standard basic contact fields if short
        if len(q_lower) < 45:
            is_basic = False
            for kw in EXCLUDED_BASIC_KEYWORDS:
                if kw in q_lower:
                    is_basic = True
                    break
            if is_basic:
                continue

        # Filter EEO / Legal disclosures that are not actual questions
        if any(term in q_lower for term in [
            'equal employment opportunity', 'eeoc', 'affirmative action',
            'veteran status', 'voluntary self-identification', 'privacy policy'
        ]) and len(q_lower) > 150:
            continue

        if q_lower not in seen_prompts:
            seen_prompts.add(q_lower)
            cleaned.append({
                "question": q_norm,
                "options": opts
            })

    return cleaned

async def run_scraper():
    logger.info("==========================================")
    logger.info(" Starting Standalone Form Questions Scraper")
    logger.info("==========================================")
    logger.info(f"Target Google Doc ID: {GOOGLE_DOC_ID}")
    logger.info(f"Source Google Sheet ID: {GOOGLE_SHEET_ID}")
    
    # 1. Fetch Existing Companies from Google Doc to skip duplicates
    existing_companies = set()
    try:
        existing_companies = google_doc_writer.get_existing_companies(SERVICE_ACCOUNT_FILE, GOOGLE_DOC_ID)
        logger.info(f"Google Doc currently contains {len(existing_companies)} scraped company record(s).")
    except Exception as e:
        logger.warning(f"Could not read existing companies from Doc (will proceed): {e}")

    # 2. Connect to Google Sheets and read "Jobs" tab
    try:
        creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
        client = gspread.authorize(creds)
        spreadsheet = client.open_by_key(GOOGLE_SHEET_ID)
        try:
            ws = spreadsheet.worksheet("Jobs")
        except Exception:
            ws = spreadsheet.sheet1
        rows = ws.get_all_values()
    except Exception as e:
        logger.error(f"Failed to connect to Google Sheet: {e}")
        return

    if len(rows) < 2:
        logger.warning("No jobs found in the 'Jobs' worksheet.")
        return

    headers = rows[0]
    data_rows = rows[1:]

    # Locate Company and Link columns
    company_col_idx = 1 # Column B (default)
    link_col_idx = 5    # Column F (default)

    for i, h in enumerate(headers):
        h_lower = str(h).strip().lower()
        if "company" in h_lower:
            company_col_idx = i
        elif "link" in h_lower or "url" in h_lower:
            link_col_idx = i

    jobs_to_process = []
    for r_idx, r in enumerate(data_rows, start=2):
        if len(r) <= max(company_col_idx, link_col_idx):
            continue
        comp = r[company_col_idx].strip()
        link = r[link_col_idx].strip()
        
        if comp and link and link.startswith("http"):
            jobs_to_process.append((comp, link, r_idx))

    logger.info(f"Found {len(jobs_to_process)} total job links in the Google Sheet.")
    
    # Filter out companies already scraped into the Google Doc
    pending_jobs = [
        (c, l, r) for c, l, r in jobs_to_process
        if c.lower() not in existing_companies
    ]
    logger.info(f"{len(pending_jobs)} job(s) pending question extraction (unscraped).\n")

    if not pending_jobs:
        logger.info("All companies from the sheet are already present in the Google Doc! Nothing to do.")
        return

    # 3. Launch stealth browser and process jobs
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )

        total_scraped = 0
        total_failed = 0

        for idx, (company, url, row_num) in enumerate(pending_jobs, start=1):
            logger.info(f"\n--- [{idx}/{len(pending_jobs)}] {company} (Row {row_num}) ---")
            logger.info(f"URL: {url}")
            
            page = await context.new_page()
            questions = []
            
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=40000)
                await asyncio.sleep(3)
                await dismiss_popups(page)

                # Attempt to reach the active application form if on a description page
                active_page = await click_apply_button_if_present(page, context)
                await asyncio.sleep(3)
                await dismiss_popups(active_page)

                # Extract questions from form and embedded iframes
                questions = await extract_form_questions(active_page)
                
                if questions:
                    logger.info(f"  [+] Found {len(questions)} question(s) for {company}:")
                    for q_i, q_item in enumerate(questions[:5], start=1):
                        prompt = q_item.get("question", "") if isinstance(q_item, dict) else str(q_item)
                        opts = q_item.get("options", []) if isinstance(q_item, dict) else []
                        opts_str = f" [Options: {', '.join(opts[:4])}{'...' if len(opts)>4 else ''}]" if opts else ""
                        logger.info(f"      Q{q_i}: {prompt[:80]}{opts_str}")
                    if len(questions) > 5:
                        logger.info(f"      ... and {len(questions) - 5} more questions.")

                    # Append to Google Doc in real time
                    success = google_doc_writer.append_company_questions(
                        SERVICE_ACCOUNT_FILE, GOOGLE_DOC_ID, company, questions
                    )
                    if success:
                        logger.info(f"  [✔] Successfully saved questions to Google Doc!")
                        existing_companies.add(company.lower())
                        total_scraped += 1
                    else:
                        logger.warning(f"  [!] Failed to append questions to Google Doc for {company}.")
                else:
                    logger.info(f"  [-] No custom questions found on form for {company}.")
                    total_failed += 1

            except Exception as e:
                logger.error(f"  [✖] Error extracting questions for {company}: {e}")
                total_failed += 1
            finally:
                # Close any extra tabs opened for this job
                for p_tab in list(context.pages):
                    try:
                        await p_tab.close()
                    except Exception:
                        pass

        await browser.close()
        logger.info("\n==========================================")
        logger.info(f"Done! {total_scraped} companies saved to Google Doc, {total_failed} skipped/failed.")
        logger.info("==========================================")

def main():
    asyncio.run(run_scraper())

if __name__ == "__main__":
    main()
