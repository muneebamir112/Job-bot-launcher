import asyncio
import os
import sys
import re
import time
import logging
import threading
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

GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "").strip()
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

# Import the Google Doc / Drive Writer
import google_doc_writer

# Standard basic contact identity keywords (not custom employer questions)
EXCLUDED_BASIC_KEYWORDS = [
    'first name', 'given name', 'last name', 'family name', 'middle name',
    'email', 'phone', 'phone number', 'mobile', 'address', 'street address',
    'postal code', 'zip code', 'city', 'state', 'resume', 'curriculum vitae',
    'attach resume', 'upload resume', 'cover letter', 'attach cover letter',
    'linkedin profile', 'linkedin url', 'github profile', 'github url',
    'portfolio website', 'personal website'
]

async def dismiss_popups(page):
    dismiss_js = """() => {
        const selectors = [
            '#onetrust-accept-btn-handler', 'button#onetrust-accept-btn-handler',
            '#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll',
            '#CybotCookiebotDialogBodyButtonAccept', '#didomi-notice-agree-button',
            '.osano-cm-accept-all', '#truste-consent-button', '[data-testid="uc-accept-all-button"]',
            'button[id*="cookie-accept" i]', 'button[class*="cookie-accept" i]'
        ];
        for (const s of selectors) {
            try {
                const el = document.querySelector(s);
                if (el && (el.offsetParent !== null || el.getBoundingClientRect().height > 0)) {
                    el.click();
                    return true;
                }
            } catch (e) {}
        }
        return false;
    }"""
    try:
        await page.evaluate(dismiss_js)
    except Exception:
        pass

    selectors = [
        "button:has-text('Accept all cookies')", "button:has-text('Accept All Cookies')",
        "button:has-text('Accept All')", "button:has-text('Accept all')",
        "button:has-text('Accept Cookies')", "button:has-text('Allow all cookies')",
        "button:has-text('Allow All')", "button:has-text('I agree')", "button:has-text('I Agree')",
        "button:has-text('Got it')", "button:has-text('Dismiss')", "button[aria-label='Close']",
        "button.close", ".close-modal"
    ]
    for s in selectors:
        try:
            for el in await page.locator(s).all():
                if await el.is_visible():
                    await el.click(timeout=1000)
                    await asyncio.sleep(0.3)
                    break
        except Exception:
            pass

async def click_apply_button_if_present(page, context):
    """
    If on a job landing / JD page, clicks 'Apply' or 'Apply Now' to reveal the application form.
    Handles multi-tab navigation if a new tab is opened.
    """
    # 1. If the current page already contains an active application form, do not click away!
    try:
        active_form_count = await page.locator(
            "form input[type='file'], input[name*='resume' i], input[id*='resume' i], #resumator-form, .application-form, form[action*='apply']"
        ).count()
        if active_form_count > 0:
            logger.info("  -> Application form already active on page. No need to click apply trigger.")
            return page
    except Exception:
        pass

    apply_selectors = [
        "button:has-text('Apply for this job')", "a:has-text('Apply for this job')",
        "button:has-text('Apply for this Job')", "a:has-text('Apply for this Job')",
        "button:has-text('Apply Now')", "a:has-text('Apply Now')",
        "button:has-text('Apply Online')", "a:has-text('Apply Online')",
        "button:has-text('Start Application')", "a:has-text('Start Application')",
        "[data-automation-id='applyButton']", "[data-automation-id='jobFoundationalApplyButton']",
        "a[href*='apply']:not([href*='linkedin']):not([href*='indeed'])",
        "button:has-text('Apply')", "a:has-text('Apply')"
    ]
    
    target_page = page
    initial_pages = len(context.pages)
    forbidden_terms = ['all jobs', 'other jobs', 'view all', 'search', 'browse', 'share', 'back', 'career', 'return', 'login', 'sign in']
    
    for sel in apply_selectors:
        try:
            btns = await page.locator(sel).all()
            for btn in btns[:2]:
                if await btn.is_visible() and await btn.is_enabled():
                    btn_text = (await btn.inner_text()).strip()
                    btn_lower = btn_text.lower()
                    if any(term in btn_lower for term in forbidden_terms):
                        continue
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
    Scans the page and all embedded frames (ATS iframes like Ashby, Greenhouse, Lever, Workday)
    for custom application questions, extracting both the question prompt and its available
    options (dropdown options, radio choices, checkbox options, Yes/No pills).
    """
    raw_items = []
    seen_prompts = set()

    for frame in page.frames:
        try:
            frame_results = await frame.evaluate('''() => {
                const results = [];
                const localSeen = new Set();
                const clean = (t) => (t || '').trim().replace(/\\s+/g, ' ');

                // 1. Group containers: Fieldsets, [role="radiogroup"], [role="group"], custom questions, Ashby field entries
                const groupContainers = document.querySelectorAll(
                    'fieldset, [role="radiogroup"], [role="group"], .application-question, [class*="question"], [class*="form-group"], [class*="field-entry"], [class*="fieldEntry"]'
                );
                for (const container of groupContainers) {
                    const headingEl = container.querySelector(
                        'legend, [role="heading"], [class*="legend"], [class*="label"], [class*="title"], [class*="prompt"], [class*="question-title"], h3, h4, h5, p, label'
                    );
                    if (!headingEl) continue;
                    const qText = clean(headingEl.innerText || headingEl.textContent);
                    if (!qText || qText.length < 5 || qText.length > 350) continue;
                    if (localSeen.has(qText.toLowerCase())) continue;

                    // Check for radio, checkbox, or pill/button options inside this container
                    const optionEls = container.querySelectorAll(
                        'input[type="radio"], input[type="checkbox"], [role="radio"], [role="checkbox"], button[data-option], [class*="yesno-option"], [class*="pill-option"], [class*="option-button"]'
                    );
                    const options = [];
                    for (const optEl of optionEls) {
                        let optText = '';
                        if (optEl.getAttribute && optEl.getAttribute('data-option')) {
                            optText = optEl.getAttribute('data-option');
                        }
                        if (!optText && optEl.id) {
                            const l = container.querySelector(`label[for="${optEl.id}"]`);
                            if (l) optText = l.innerText;
                        }
                        if (!optText && optEl.closest && optEl.closest('label')) {
                            optText = optEl.closest('label').innerText;
                        }
                        if (!optText) {
                            optText = optEl.innerText || optEl.textContent;
                        }
                        if (!optText && optEl.value && optEl.value !== 'on') {
                            optText = optEl.value;
                        }
                        const cleanOpt = clean(optText);
                        if (cleanOpt && cleanOpt.toLowerCase() !== qText.toLowerCase() && !options.includes(cleanOpt) && cleanOpt.length < 150) {
                            options.push(cleanOpt);
                        }
                    }

                    // Check if container has input/textarea/select controls
                    const hasControls = container.querySelector('input, textarea, select, [contenteditable="true"]');
                    if (options.length > 0 || hasControls) {
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

async def find_next_page_button(page):
    """
    Scans the page and all frames for a 'Next', 'Continue', or 'Proceed' button
    to advance to the next step of a multi-page application form.
    Avoids 'Submit', 'Apply', or 'Back' buttons.
    """
    next_selectors = [
        "[data-automation-id='nextButton']",
        "[data-automation-id='bottom-navigation-next-button']",
        "button[aria-label*='Next' i]",
        "button:has-text('Next Step')",
        "button:has-text('Next Page')",
        "button:has-text('Save & Continue')",
        "button:has-text('Save and Continue')",
        "button:has-text('Save and continue')",
        "button:has-text('Next')",
        "button:has-text('Continue')",
        "button:has-text('Proceed')",
        "input[type='submit'][value*='Next' i]",
        "input[type='button'][value*='Next' i]",
        "input[type='button'][value*='Continue' i]",
        "a:has-text('Next Step')",
        "a:has-text('Save & Continue')",
        "a:has-text('Save and Continue')",
        "a:has-text('Continue')"
    ]
    
    forbidden_terms = ['submit', 'finish', 'complete', 'review and submit', 'back', 'previous', 'cancel', 'discard', 'slide', 'carousel', 'prev']

    for frame in page.frames:
        for sel in next_selectors:
            try:
                elements = await frame.locator(sel).all()
                for el in elements:
                    if await el.is_visible() and await el.is_enabled():
                        text = (await el.inner_text() or await el.get_attribute("value") or "").strip().lower()
                        aria_label = (await el.get_attribute("aria-label") or "").strip().lower()
                        class_name = (await el.get_attribute("class") or "").strip().lower()

                        if any(term in text or term in aria_label for term in forbidden_terms):
                            continue
                        if any(c in class_name for c in ['slick', 'swiper', 'carousel', 'slider', 'arrow']):
                            continue

                        if 'next' in text or 'continue' in text or 'proceed' in text or 'next' in aria_label or 'next' in sel.lower():
                            return {"element": el, "text": text or aria_label or sel}
            except Exception:
                continue
    return None

async def fill_required_fields_if_needed(page):
    """
    If a multi-step form has required basic fields on early pages that prevent advancing,
    fills standard safe placeholder values so the form can transition to subsequent question pages.
    """
    for frame in page.frames:
        try:
            inputs = await frame.locator("input[required], input[aria-required='true'], textarea[required]").all()
            for inp in inputs:
                try:
                    if not await inp.is_visible():
                        continue
                    val = (await inp.input_value() or "").strip()
                    if val:
                        continue
                    
                    inp_type = (await inp.get_attribute("type") or "text").lower()
                    name = (await inp.get_attribute("name") or "").lower()
                    inp_id = (await inp.get_attribute("id") or "").lower()
                    
                    if inp_type == "email" or "email" in name or "email" in inp_id:
                        await inp.fill("applicant.jobbot@gmail.com")
                    elif inp_type == "tel" or "phone" in name or "phone" in inp_id:
                        await inp.fill("5551234567")
                    elif "first" in name or "first" in inp_id:
                        await inp.fill("Applicant")
                    elif "last" in name or "last" in inp_id:
                        await inp.fill("Candidate")
                    elif "zip" in name or "postal" in name:
                        await inp.fill("90210")
                    elif "city" in name:
                        await inp.fill("New York")
                    else:
                        await inp.fill("N/A")
                    await asyncio.sleep(0.1)
                except Exception:
                    continue
        except Exception:
            pass

async def scrape_all_pages_questions(active_page, context, max_pages=7) -> list:
    """
    Crawls multi-page job application forms, scraping questions across each page.
    Advances using 'Next', 'Continue', or 'Save & Continue' until the application finishes.
    """
    all_questions = []
    seen_prompts = set()
    current_page = active_page

    for page_num in range(1, max_pages + 1):
        await dismiss_popups(current_page)
        await asyncio.sleep(1.5)

        # 1. Extract questions on the current page
        page_questions = await extract_form_questions(current_page)
        new_count = 0
        for q in page_questions:
            prompt_key = q["question"].strip().lower()
            if prompt_key not in seen_prompts:
                seen_prompts.add(prompt_key)
                all_questions.append(q)
                new_count += 1

        logger.info(f"    Page {page_num}: Found {len(page_questions)} question(s) ({new_count} new).")

        # If on page 2+ and no new questions were found, the form either didn't advance or reached the end
        if page_num > 1 and new_count == 0:
            logger.info(f"    -> No new questions discovered on Page {page_num}. Ending multi-page crawl.")
            break

        # 2. Check for next page trigger
        next_btn = await find_next_page_button(current_page)
        if not next_btn:
            if page_num > 1:
                logger.info(f"    -> Reached final page ({page_num}).")
            break

        # 3. Fill required fields if needed so browser allows clicking next
        await fill_required_fields_if_needed(current_page)
        await asyncio.sleep(0.5)

        # 4. Advance to next page
        btn_label = next_btn['text']
        logger.info(f"    -> Multi-page form detected. Clicking '{btn_label}' to reach Page {page_num + 1}...")
        
        initial_pages = len(context.pages)
        try:
            await next_btn["element"].click(timeout=5000)
            await asyncio.sleep(3)
            
            # Check if a new tab opened
            if len(context.pages) > initial_pages:
                current_page = context.pages[-1]
                await current_page.wait_for_load_state("domcontentloaded", timeout=10000)
                logger.info(f"    -> Switched to newly opened page tab.")
            else:
                await current_page.wait_for_load_state("domcontentloaded", timeout=10000)
        except Exception as click_err:
            logger.info(f"    -> Could not advance past Page {page_num}: {click_err}")
            break

    return all_questions


def get_column_indices(ws):
    """
    Dynamically maps header columns from the Google Sheet and guarantees
    the presence of 'Questions Doc Link' and 'Questions Count'.
    """
    rows = ws.get_all_values()
    headers = [str(h).strip() for h in (rows[0] if rows else [])]
    h_lowers = [h.lower() for h in headers]

    company_col_idx = 1
    title_col_idx = 2
    job_link_col_idx = 5
    doc_link_col_idx = None
    count_col_idx = None

    for i, h in enumerate(h_lowers):
        if "company" in h:
            company_col_idx = i
        elif "job title" in h or ("title" in h and "question" not in h):
            title_col_idx = i
        elif "questions doc" in h or "doc link" in h or ("question" in h and "link" in h):
            doc_link_col_idx = i
        elif "questions count" in h or "question count" in h or ("question" in h and "count" in h):
            count_col_idx = i
        elif ("job link" in h or "link" in h or "url" in h) and "doc" not in h and "question" not in h:
            job_link_col_idx = i

    # If question columns are missing from the sheet, add them
    updated_headers = False
    if doc_link_col_idx is None:
        headers.append("Questions Doc Link")
        doc_link_col_idx = len(headers) - 1
        updated_headers = True
    if count_col_idx is None:
        headers.append("Questions Count")
        count_col_idx = len(headers) - 1
        updated_headers = True

    if updated_headers:
        try:
            ws.update(range_name=f"A1:{gspread.utils.rowcol_to_a1(1, len(headers))}", values=[headers], value_input_option="USER_ENTERED")
            logger.info(f"[Sheet] Added Question columns: Doc Link (Col {doc_link_col_idx + 1}), Count (Col {count_col_idx + 1})")
        except Exception as e:
            logger.warning(f"[Sheet] Warning appending question headers: {e}")

    return company_col_idx, title_col_idx, job_link_col_idx, doc_link_col_idx, count_col_idx


def update_sheet_row(ws, row_num: int, doc_link_col_idx: int, count_col_idx: int, doc_url: str, count_str: str):
    """
    Updates the Google Sheet for a given job row with its Google Doc Link and Question Count.
    Uses batch update to make a single atomic API call per job.
    """
    try:
        if doc_url and doc_url.startswith("http"):
            doc_cell = f'=HYPERLINK("{doc_url}", "{doc_url}")'
        else:
            doc_cell = doc_url or "-"

        # If columns are adjacent (e.g., Col 10 and Col 11)
        if count_col_idx == doc_link_col_idx + 1:
            start_a1 = gspread.utils.rowcol_to_a1(row_num, doc_link_col_idx + 1)
            end_a1 = gspread.utils.rowcol_to_a1(row_num, count_col_idx + 1)
            ws.update(range_name=f"{start_a1}:{end_a1}", values=[[doc_cell, count_str]], value_input_option="USER_ENTERED")
        else:
            ws.update_cell(row_num, doc_link_col_idx + 1, doc_cell)
            ws.update_cell(row_num, count_col_idx + 1, count_str)
        return True
    except Exception as e:
        logger.error(f"[Sheet] Error updating Row {row_num} in Google Sheet: {e}")
        return False


async def run_scraper():
    logger.info("==========================================")
    logger.info("  Form Questions Scraper (Per-Job Docs)   ")
    logger.info("==========================================")
    logger.info(f"Source Google Sheet ID: {GOOGLE_SHEET_ID}")

    continuous_mode = ("--continuous" in sys.argv)
    drain_mode = not continuous_mode  # in single-shot mode, drain immediately

    # Background stdin listener for DRAIN command from launcher
    def _stdin_listener():
        nonlocal drain_mode
        try:
            for line in sys.stdin:
                line_str = line.strip().upper()
                if "DRAIN" in line_str:
                    logger.info("[Control] Received DRAIN signal. Finishing remaining jobs before exiting.")
                    drain_mode = True
                    break
        except Exception:
            pass
        drain_mode = True

    if continuous_mode:
        t = threading.Thread(target=_stdin_listener, daemon=True)
        t.start()
        logger.info("[Mode] Running in CONTINUOUS mode alongside active job scrapers.")
    else:
        logger.info("[Mode] Running in SINGLE-PASS mode.")

    # 1. Initialize Google Drive & fetch existing question docs to prevent duplicate creations
    drive_service = None
    existing_docs = {}
    try:
        drive_service = google_doc_writer.get_drive_service()
        folder_id = google_doc_writer.get_or_create_questions_folder(drive_service)
        existing_docs = google_doc_writer.get_existing_question_docs(drive_service, folder_id)
        logger.info(f"Found {len(existing_docs)} existing per-job Google Doc(s) in Drive folder.")
    except Exception as e:
        logger.warning(f"Could not connect to Google Drive ({e}). Will proceed with direct creation.")

    # 2. Connect to Google Sheets and read "Jobs" tab
    try:
        creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
        client = gspread.authorize(creds)
        spreadsheet = client.open_by_key(GOOGLE_SHEET_ID)
        try:
            ws = spreadsheet.worksheet("Jobs")
        except Exception:
            ws = spreadsheet.sheet1
    except Exception as e:
        logger.error(f"Failed to connect to Google Sheet: {e}")
        return

    company_col_idx, title_col_idx, job_link_col_idx, doc_link_col_idx, count_col_idx = get_column_indices(ws)

    keyword_filter = None
    for arg in sys.argv[1:]:
        if not arg.startswith("--"):
            keyword_filter = arg.strip().lower()
            break
    if keyword_filter:
        logger.info(f"Filtering pending jobs for keyword: '{keyword_filter}'")

    headless = os.getenv("HEADLESS", "false").lower() in ("true", "1")

    # 3. Launch browser (uses system Chrome for maximum ATS stealth and speed)
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(
                channel="chrome",
                headless=headless,
                args=["--disable-blink-features=AutomationControlled"]
            )
        except Exception:
            browser = await p.chromium.launch(headless=headless)

        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )

        total_scraped = 0
        total_skipped = 0

        # Main processing loop
        while True:
            # Refresh data from sheet
            try:
                rows = ws.get_all_values()
            except Exception as read_err:
                logger.warning(f"Error fetching sheet rows: {read_err}. Retrying in 5s...")
                await asyncio.sleep(5)
                continue

            data_rows = rows[1:] if len(rows) > 1 else []
            
            # Find all pending jobs:
            # A row is pending if:
            # 1. Company and Job Link are present
            # 2. Questions Doc Link is empty AND Questions Count is not already evaluated ("0 Questions", "None", or "X Questions")
            pending_jobs = []
            for r_idx, r in enumerate(data_rows, start=2):
                if len(r) <= max(company_col_idx, job_link_col_idx):
                    continue
                comp = r[company_col_idx].strip()
                title = r[title_col_idx].strip() if title_col_idx < len(r) and r[title_col_idx].strip() else "Job"
                link = r[job_link_col_idx].strip()
                
                doc_link = r[doc_link_col_idx].strip() if doc_link_col_idx < len(r) else ""
                count_val = r[count_col_idx].strip() if count_col_idx < len(r) else ""

                if not comp or not link or not link.startswith("http"):
                    continue

                if keyword_filter and (keyword_filter not in title.lower() and keyword_filter not in comp.lower()):
                    continue

                # Check if this row is already complete
                is_evaluated = bool(doc_link and doc_link.startswith("http")) or (count_val in ("0 Questions", "N/A", "-") and doc_link in ("-", "N/A"))
                if not is_evaluated:
                    pending_jobs.append((comp, title, link, doc_link, count_val, r_idx))

            if pending_jobs:
                logger.info(f"\n--- Found {len(pending_jobs)} job(s) pending form question extraction ---")

                for idx, (company, title, url, existing_doc_link, count_val, row_num) in enumerate(pending_jobs, start=1):
                    doc_target_name = f"{company} - {title}".strip()
                    target_key = doc_target_name.lower()

                    logger.info(f"\n[{idx}/{len(pending_jobs)}] {doc_target_name} (Row {row_num})")
                    logger.info(f"URL: {url}")

                    # Case A: Check if a dedicated Google Doc already exists in Drive for this job
                    if target_key in existing_docs:
                        existing_entry = existing_docs[target_key]
                        doc_url = existing_entry.get("url") if isinstance(existing_entry, dict) else f"https://docs.google.com/document/d/{existing_entry}/edit"
                        logger.info(f"  [✔] Found existing Google Doc on Drive: {doc_url}")
                        q_cnt = count_val if count_val and count_val != "None" else "Questions"
                        update_sheet_row(ws, row_num, doc_link_col_idx, count_col_idx, doc_url, q_cnt)
                        total_scraped += 1
                        continue

                    # Case B: Scrape the application page for questions
                    page = await context.new_page()
                    questions = []
                    
                    try:
                        await page.goto(url, wait_until="domcontentloaded", timeout=40000)
                        await asyncio.sleep(2.5)
                        await dismiss_popups(page)

                        # Attempt to reach active application form if on landing / description page
                        active_page = await click_apply_button_if_present(page, context)
                        await asyncio.sleep(2.5)
                        await dismiss_popups(active_page)

                        # Scrape questions across all pages of the application form
                        questions = await scrape_all_pages_questions(active_page, context)

                        if questions:
                            logger.info(f"  [+] Extracted {len(questions)} question(s) for {doc_target_name}:")
                            for q_i, q_item in enumerate(questions[:4], start=1):
                                prompt = q_item.get("question", "") if isinstance(q_item, dict) else str(q_item)
                                opts = q_item.get("options", []) if isinstance(q_item, dict) else []
                                opts_str = f" [Options: {', '.join(opts[:3])}{'...' if len(opts)>3 else ''}]" if opts else ""
                                logger.info(f"      Q{q_i}: {prompt[:80]}{opts_str}")
                            if len(questions) > 4:
                                logger.info(f"      ... and {len(questions) - 4} more questions.")

                            # Create dedicated Google Doc named '{company} - {job_title}'
                            doc_res = google_doc_writer.create_job_question_doc(
                                company, title, url, questions, drive_service=drive_service
                            )

                            if doc_res and doc_res.get("url"):
                                doc_url = doc_res["url"]
                                count_str = f"{len(questions)} Questions"
                                logger.info(f"  [✔] Google Doc Created: {doc_res['name']}")
                                logger.info(f"      Link: {doc_url}")
                                existing_docs[target_key] = doc_res
                                update_sheet_row(ws, row_num, doc_link_col_idx, count_col_idx, doc_url, count_str)
                                total_scraped += 1
                            else:
                                logger.warning(f"  [!] Failed to create Google Doc for {doc_target_name}.")
                                update_sheet_row(ws, row_num, doc_link_col_idx, count_col_idx, "-", f"{len(questions)} Questions")
                                total_skipped += 1
                        else:
                            logger.info(f"  [-] No custom employer questions found on application form.")
                            update_sheet_row(ws, row_num, doc_link_col_idx, count_col_idx, "-", "0 Questions")
                            total_skipped += 1

                    except Exception as e:
                        logger.error(f"  [✖] Error extracting questions for {doc_target_name}: {e}")
                        total_skipped += 1
                    finally:
                        # Close any tabs opened during this job's extraction
                        for p_tab in list(context.pages):
                            try:
                                await p_tab.close()
                            except Exception:
                                pass

                    await asyncio.sleep(1)

            else:
                # No pending jobs currently in the sheet
                if drain_mode:
                    logger.info("\n[Done] All jobs in the Google Sheet have been processed for questions!")
                    break
                else:
                    logger.info("All current jobs processed. Waiting for new jobs from scrapers (polling sheet in 5s)...")
                    await asyncio.sleep(5)

        await browser.close()
        logger.info("\n==========================================")
        logger.info(f"Question Scraper finished! Docs created/linked: {total_scraped}, No questions/skipped: {total_skipped}.")
        logger.info("==========================================")


def main():
    asyncio.run(run_scraper())

if __name__ == "__main__":
    main()
