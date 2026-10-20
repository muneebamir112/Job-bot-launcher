import os
import sys
import re
import time
import logging
import html

# Ensure path to google_doc_writer
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

try:
    import google_doc_writer
except ImportError:
    google_doc_writer = None

logger = logging.getLogger("QuestionExtractor")

EXCLUDED_BASIC_KEYWORDS = [
    'first name', 'given name', 'last name', 'family name', 'middle name',
    'email', 'phone', 'phone number', 'mobile', 'address', 'street address',
    'postal code', 'zip code', 'city', 'state', 'resume', 'curriculum vitae',
    'attach resume', 'upload resume', 'cover letter', 'attach cover letter',
    'linkedin profile', 'linkedin url', 'github profile', 'github url',
    'portfolio website', 'personal website'
]

JS_EXTRACT_QUESTIONS = """() => {
    const results = [];
    const localSeen = new Set();
    const clean = (t) => (t || '').trim().replace(/\\s+/g, ' ');

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

        if (isCountryCodeList(opts)) continue;

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

        if (isCountryCodeList(opts)) opts = [];

        localSeen.add(qText.toLowerCase());
        results.push({question: qText, options: opts});
    }

    return results;
}"""

def filter_cleaned_questions(raw_items):
    cleaned = []
    seen = set()
    for item in raw_items:
        q_raw = item.get("question", "")
        q_norm = re.sub(r'[\*\:\s]+$', '', " ".join(q_raw.split())).strip()
        q_lower = q_norm.lower()

        if not q_norm or len(q_norm) < 4:
            continue

        if q_lower in [
            'start typing...', 'start typing', 'type here...', 'type here',
            'type your answer here', 'enter your answer', 'select an option',
            'select one', 'please select', 'choose...', 'choose one', 'none',
            'search', 'cookie list search', 'select language', 'language',
            'country', 'select country', 'country/region', 'country / region',
            'country code', 'phone country code', 'dial code'
        ]:
            continue

        opts = item.get("options", [])
        dial_matches = sum(1 for o in opts if re.search(r'\+\d{1,4}$|^\+\d{1,4}', str(o).strip()))
        if dial_matches >= 2:
            opts = []
            if q_lower in ['country', 'country code', 'phone country code', 'dial code', 'search']:
                continue

        if len(q_lower) < 45:
            if any(kw in q_lower for kw in EXCLUDED_BASIC_KEYWORDS):
                continue

        if any(term in q_lower for term in [
            'equal employment opportunity', 'eeoc', 'affirmative action',
            'veteran status', 'voluntary self-identification', 'privacy policy'
        ]) and len(q_lower) > 150:
            continue

        if q_lower not in seen:
            seen.add(q_lower)
            cleaned.append({"question": q_norm, "options": opts})
    return cleaned


# ==============================================================================
# ASYNC IMPLEMENTATION (Used by Hiring Cafe, Jobgether)
# ==============================================================================

async def dismiss_popups_async(page):
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
        except Exception:
            pass

async def click_apply_if_landing_async(page, context):
    apply_selectors = [
        "button:has-text('Apply Now')", "a:has-text('Apply Now')",
        "button:has-text('Apply for this job')", "a:has-text('Apply for this job')",
        "button:has-text('Apply Online')", "a:has-text('Apply Online')",
        "button:has-text('Start Application')", "a:has-text('Start Application')",
        "[data-automation-id='applyButton']", "[data-automation-id='jobFoundationalApplyButton']",
        "button:has-text('Apply')", "a:has-text('Apply')"
    ]
    target_page = page
    initial_count = len(context.pages)
    for sel in apply_selectors:
        try:
            btns = await page.locator(sel).all()
            for btn in btns[:2]:
                if await btn.is_visible() and await btn.is_enabled():
                    await btn.click(timeout=2500)
                    import asyncio
                    await asyncio.sleep(2)
                    if len(context.pages) > initial_count:
                        target_page = context.pages[-1]
                        await target_page.wait_for_load_state("domcontentloaded", timeout=8000)
                    return target_page
        except Exception:
            continue
    return target_page

async def find_next_button_async(page):
    next_selectors = [
        "[data-automation-id='nextButton']",
        "[data-automation-id='bottom-navigation-next-button']",
        "button[aria-label*='Next' i]",
        "button:has-text('Next Step')",
        "button:has-text('Next Page')",
        "button:has-text('Save & Continue')",
        "button:has-text('Save and Continue')",
        "button:has-text('Next')",
        "button:has-text('Continue')",
        "button:has-text('Proceed')",
        "input[type='submit'][value*='Next' i]",
        "input[type='button'][value*='Next' i]",
        "input[type='button'][value*='Continue' i]",
        "a:has-text('Next Step')",
        "a:has-text('Save & Continue')",
        "a:has-text('Continue')"
    ]
    forbidden = ['submit', 'finish', 'complete', 'review and submit', 'back', 'previous', 'cancel', 'discard', 'slide', 'carousel', 'prev']
    for frame in page.frames:
        for sel in next_selectors:
            try:
                elements = await frame.locator(sel).all()
                for el in elements:
                    if await el.is_visible() and await el.is_enabled():
                        text = (await el.inner_text() or await el.get_attribute("value") or "").strip().lower()
                        aria_label = (await el.get_attribute("aria-label") or "").strip().lower()
                        class_name = (await el.get_attribute("class") or "").strip().lower()
                        if any(term in text or term in aria_label for term in forbidden):
                            continue
                        if any(c in class_name for c in ['slick', 'swiper', 'carousel', 'slider', 'arrow']):
                            continue
                        if 'next' in text or 'continue' in text or 'proceed' in text or 'next' in aria_label or 'next' in sel.lower():
                            return el
            except Exception:
                continue
    return None

async def fill_placeholders_async(page):
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
                    else:
                        await inp.fill("N/A")
                except Exception:
                    continue
        except Exception:
            pass

async def extract_questions_and_create_doc_async(page, context, company, title, job_url, drive_service=None, max_pages=6):
    """
    Extracts application questions from page in the same visit (multi-page crawl included),
    creates a Google Doc '{company} - {title}', and returns (doc_url, q_count_str, questions).
    """
    import asyncio
    try:
        active_page = await click_apply_if_landing_async(page, context)
        all_questions = []
        seen_prompts = set()
        current_page = active_page

        for page_num in range(1, max_pages + 1):
            await dismiss_popups_async(current_page)
            await asyncio.sleep(1.2)

            raw_items = []
            for frame in current_page.frames:
                try:
                    frame_results = await frame.evaluate(JS_EXTRACT_QUESTIONS)
                    raw_items.extend(frame_results)
                except Exception:
                    pass

            page_questions = filter_cleaned_questions(raw_items)
            new_count = 0
            for q in page_questions:
                prompt_key = q["question"].strip().lower()
                if prompt_key not in seen_prompts:
                    seen_prompts.add(prompt_key)
                    all_questions.append(q)
                    new_count += 1

            if page_num > 1 and new_count == 0:
                break

            next_btn = await find_next_button_async(current_page)
            if not next_btn:
                break

            await fill_placeholders_async(current_page)
            try:
                await next_btn.click(timeout=4000)
                await asyncio.sleep(2)
            except Exception:
                break

        if all_questions:
            doc_url = ""
            if google_doc_writer:
                try:
                    doc_res = google_doc_writer.create_job_question_doc(
                        company, title, job_url, all_questions, drive_service=drive_service
                    )
                    if doc_res:
                        doc_url = doc_res.get("url", "")
                except Exception as e:
                    print(f"[{company}] Failed creating question Google Doc: {e}", flush=True)

            q_count_str = f"{len(all_questions)} Questions"
            return doc_url, q_count_str, all_questions
    except Exception as e:
        print(f"[{company}] Question extraction error: {e}", flush=True)

    return "", "None", []


# ==============================================================================
# SYNC IMPLEMENTATION (Used by Glassdoor)
# ==============================================================================

def dismiss_popups_sync(page):
    selectors = [
        "button:has-text('Accept All')", "button:has-text('Accept Cookies')",
        "button:has-text('I agree')", "button:has-text('Got it')",
        "button:has-text('Dismiss')", "button[aria-label='Close']",
        "button.close", ".close-modal"
    ]
    for s in selectors:
        try:
            for el in page.locator(s).all():
                if el.is_visible():
                    el.click(timeout=1000)
        except Exception:
            pass

def click_apply_if_landing_sync(page, context):
    apply_selectors = [
        "button:has-text('Apply Now')", "a:has-text('Apply Now')",
        "button:has-text('Apply for this job')", "a:has-text('Apply for this job')",
        "button:has-text('Apply Online')", "a:has-text('Apply Online')",
        "button:has-text('Start Application')", "a:has-text('Start Application')",
        "[data-automation-id='applyButton']", "[data-automation-id='jobFoundationalApplyButton']",
        "button:has-text('Apply')", "a:has-text('Apply')"
    ]
    target_page = page
    initial_count = len(context.pages)
    for sel in apply_selectors:
        try:
            btns = page.locator(sel).all()
            for btn in btns[:2]:
                if btn.is_visible() and btn.is_enabled():
                    btn.click(timeout=2500)
                    time.sleep(2)
                    if len(context.pages) > initial_count:
                        target_page = context.pages[-1]
                        target_page.wait_for_load_state("domcontentloaded", timeout=8000)
                    return target_page
        except Exception:
            continue
    return target_page

def find_next_button_sync(page):
    next_selectors = [
        "[data-automation-id='nextButton']",
        "[data-automation-id='bottom-navigation-next-button']",
        "button[aria-label*='Next' i]",
        "button:has-text('Next Step')",
        "button:has-text('Next Page')",
        "button:has-text('Save & Continue')",
        "button:has-text('Save and Continue')",
        "button:has-text('Next')",
        "button:has-text('Continue')",
        "button:has-text('Proceed')",
        "input[type='submit'][value*='Next' i]",
        "input[type='button'][value*='Next' i]",
        "input[type='button'][value*='Continue' i]",
        "a:has-text('Next Step')",
        "a:has-text('Save & Continue')",
        "a:has-text('Continue')"
    ]
    forbidden = ['submit', 'finish', 'complete', 'review and submit', 'back', 'previous', 'cancel', 'discard', 'slide', 'carousel', 'prev']
    for frame in page.frames:
        for sel in next_selectors:
            try:
                elements = frame.locator(sel).all()
                for el in elements:
                    if el.is_visible() and el.is_enabled():
                        text = (el.inner_text() or el.get_attribute("value") or "").strip().lower()
                        aria_label = (el.get_attribute("aria-label") or "").strip().lower()
                        class_name = (el.get_attribute("class") or "").strip().lower()
                        if any(term in text or term in aria_label for term in forbidden):
                            continue
                        if any(c in class_name for c in ['slick', 'swiper', 'carousel', 'slider', 'arrow']):
                            continue
                        if 'next' in text or 'continue' in text or 'proceed' in text or 'next' in aria_label or 'next' in sel.lower():
                            return el
            except Exception:
                continue
    return None

def fill_placeholders_sync(page):
    for frame in page.frames:
        try:
            inputs = frame.locator("input[required], input[aria-required='true'], textarea[required]").all()
            for inp in inputs:
                try:
                    if not inp.is_visible():
                        continue
                    val = (inp.input_value() or "").strip()
                    if val:
                        continue
                    inp_type = (inp.get_attribute("type") or "text").lower()
                    name = (inp.get_attribute("name") or "").lower()
                    inp_id = (inp.get_attribute("id") or "").lower()
                    if inp_type == "email" or "email" in name or "email" in inp_id:
                        inp.fill("applicant.jobbot@gmail.com")
                    elif inp_type == "tel" or "phone" in name or "phone" in inp_id:
                        inp.fill("5551234567")
                    elif "first" in name or "first" in inp_id:
                        inp.fill("Applicant")
                    elif "last" in name or "last" in inp_id:
                        inp.fill("Candidate")
                    else:
                        inp.fill("N/A")
                except Exception:
                    continue
        except Exception:
            pass

def extract_questions_and_create_doc_sync(page, context, company, title, job_url, drive_service=None, max_pages=6):
    """
    Extracts application questions from page synchronously in the same visit,
    creates a Google Doc '{company} - {title}', and returns (doc_url, q_count_str, questions).
    """
    try:
        active_page = click_apply_if_landing_sync(page, context)
        all_questions = []
        seen_prompts = set()
        current_page = active_page

        for page_num in range(1, max_pages + 1):
            dismiss_popups_sync(current_page)
            time.sleep(1.2)

            raw_items = []
            for frame in current_page.frames:
                try:
                    frame_results = frame.evaluate(JS_EXTRACT_QUESTIONS)
                    raw_items.extend(frame_results)
                except Exception:
                    pass

            page_questions = filter_cleaned_questions(raw_items)
            new_count = 0
            for q in page_questions:
                prompt_key = q["question"].strip().lower()
                if prompt_key not in seen_prompts:
                    seen_prompts.add(prompt_key)
                    all_questions.append(q)
                    new_count += 1

            if page_num > 1 and new_count == 0:
                break

            next_btn = find_next_button_sync(current_page)
            if not next_btn:
                break

            fill_placeholders_sync(current_page)
            try:
                next_btn.click(timeout=4000)
                time.sleep(2)
            except Exception:
                break

        if all_questions:
            doc_url = ""
            if google_doc_writer:
                try:
                    doc_res = google_doc_writer.create_job_question_doc(
                        company, title, job_url, all_questions, drive_service=drive_service
                    )
                    if doc_res:
                        doc_url = doc_res.get("url", "")
                except Exception as e:
                    print(f"[{company}] Failed creating question Google Doc: {e}", flush=True)

            q_count_str = f"{len(all_questions)} Questions"
            return doc_url, q_count_str, all_questions
    except Exception as e:
        print(f"[{company}] Question extraction error: {e}", flush=True)

    return "", "None", []
