"""Auto-apply bot.

Opens the posting in Chromium (Playwright), fills the standard fields from your
profile, attaches your latest resume and submits. It is deliberately cautious:
if a required field is still empty after filling (custom questions, EEO
dropdowns, essays...), it does NOT submit and moves the internship to
"Needs attention" with the list of fields it couldn't answer plus a screenshot.

Works best on Greenhouse, Lever and Ashby postings; other sites use the same
label-matching heuristics and may or may not work. Systems that need an account
(Workday and the like) are handed straight to you.

A listing that links to a job site you must be signed in to (Handshake), or has
no link at all, is first looked up on the company's own Greenhouse, Lever or
Ashby board; the bot applies there, never on the listing site.
"""
import asyncio
import os
import re
import traceback
from pathlib import Path
from urllib.parse import urlsplit

from . import db, jobboards

# Application systems where you must sign up before applying. The bot doesn't try these.
ACCOUNT_SITES = {
    "myworkdayjobs.com": "Workday",
    "myworkdaysite.com": "Workday",
    "oraclecloud.com": "Oracle Recruiting",
    "icims.com": "iCIMS",
    "taleo.net": "Taleo",
    "successfactors.com": "SuccessFactors",
}

# Job sites that list postings but need you signed in. The bot never opens these; it
# looks for the same job on the company's own board instead.
LISTING_SITES = {
    "joinhandshake.com": "Handshake",
}

RESUME_TYPES = {
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}

CONFIRM_RE = re.compile(
    r"thank(s| you) for (applying|your (application|interest))|application (has been |was )?"
    r"(submitted|received)|we('ve| have) received your application|successfully submitted|"
    r"you('ve| have) (already )?applied",
    re.I,
)

# Order matters: first match wins for each input.
FIELD_PATTERNS = [
    ("first_name", re.compile(r"first[\s_-]?name|given[\s_-]?name|\bfname\b", re.I)),
    ("last_name", re.compile(r"last[\s_-]?name|family[\s_-]?name|surname|\blname\b", re.I)),
    ("email", re.compile(r"e-?mail", re.I)),
    ("phone", re.compile(r"phone|mobile|\btel\b", re.I)),
    ("linkedin_url", re.compile(r"linkedin", re.I)),
    ("github_url", re.compile(r"github", re.I)),
    ("website", re.compile(r"website|portfolio|personal (site|url)|other url", re.I)),
    ("school", re.compile(r"school|university|college", re.I)),
    ("location", re.compile(r"location|current city|\bcity\b", re.I)),
    ("full_name", re.compile(r"(^|[^a-z])(full[\s_-]?)?name([^a-z]|$)", re.I)),
]
NOT_FULL_NAME = re.compile(r"company|employer|school|university|preferred|middle|user|nick|reference", re.I)

DESCRIBE_JS = """
el => {
  const parts = [el.name, el.id, el.placeholder, el.getAttribute('aria-label'),
                 el.getAttribute('autocomplete')];
  if (el.labels) for (const l of el.labels) parts.push(l.innerText);
  const lb = el.getAttribute('aria-labelledby');
  if (lb) for (const id of lb.split(/\\s+/)) {
    const n = document.getElementById(id); if (n) parts.push(n.innerText);
  }
  return parts.filter(Boolean).join(' | ');
}
"""

MISSING_REQUIRED_JS = """
() => {
  const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const label = el => {
    if (el.labels && el.labels[0]) return el.labels[0].innerText.trim();
    return el.getAttribute('aria-label') || el.placeholder || el.name || el.id || el.type;
  };
  const out = [], seenGroups = new Set();
  for (const el of document.querySelectorAll('input, textarea, select')) {
    const req = el.required || el.getAttribute('aria-required') === 'true';
    if (!req || el.type === 'hidden' || el.disabled) continue;
    if (el.type === 'radio' || el.type === 'checkbox') {
      if (seenGroups.has(el.name)) continue;
      seenGroups.add(el.name);
      const group = el.name ? document.querySelectorAll(`input[name="${CSS.escape(el.name)}"]`) : [el];
      if (![...group].some(g => g.checked)) out.push(label(el));
    } else if (el.type === 'file') {
      if (!el.files || el.files.length === 0) out.push(label(el));
    } else if (visible(el) && !String(el.value || '').trim()) {
      out.push(label(el));
    }
  }
  return out;
}
"""

SUBMIT_SELECTORS = [
    "#submit_app", "#btn-submit", "button[type=submit]", "input[type=submit]",
    "button:has-text('Submit application')", "button:has-text('Submit')",
]

_lock = None


def _get_lock():
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()  # one browser at a time
    return _lock


def _profile_values(profile: dict) -> dict:
    vals = dict(profile)
    vals["full_name"] = f"{profile.get('first_name', '')} {profile.get('last_name', '')}".strip()
    return vals


async def _best_frame(page):
    """Greenhouse is often embedded in an iframe on company career sites."""
    best, best_n = page.main_frame, -1
    for fr in page.frames:
        try:
            n = await fr.locator("input:not([type=hidden]), textarea").count()
        except Exception:
            continue
        if n > best_n:
            best, best_n = fr, n
    return best


LEVER_POSTING_RE = re.compile(r"(https://jobs\.(?:eu\.)?lever\.co/[^/?#]+/[0-9a-f-]{20,})/?(?:[?#].*)?$")


async def _open_form(page, url):
    await page.goto(url, wait_until="domcontentloaded", timeout=45000)
    # Newsletter links pass through a click-tracking redirect, so go by where we landed.
    lever = LEVER_POSTING_RE.match(page.url)
    if lever:   # Lever keeps the form on a separate /apply page
        await page.goto(lever.group(1) + "/apply", wait_until="domcontentloaded", timeout=45000)
    await page.wait_for_timeout(1500)
    frame = await _best_frame(page)
    if await frame.locator("input[type=email], input[type=file]").count() == 0:
        # Form not shown yet: click an "Apply" button/link if there is one.
        btn = page.locator("a:has-text('Apply'), button:has-text('Apply')").first
        if await btn.count():
            await btn.click()
            await page.wait_for_timeout(2500)
            frame = await _best_frame(page)
    return frame


async def _fill_fields(frame, values) -> list[str]:
    filled = []
    inputs = await frame.query_selector_all("input, textarea")
    for el in inputs:
        typ = (await el.get_attribute("type") or "text").lower()
        if typ in ("hidden", "submit", "button", "checkbox", "radio", "file", "image", "reset"):
            continue
        if not await el.is_visible() or (await el.input_value()).strip():
            continue
        desc = await el.evaluate(DESCRIBE_JS)
        for key, pat in FIELD_PATTERNS:
            if not pat.search(desc):
                continue
            if key == "full_name" and NOT_FULL_NAME.search(desc):
                break
            val = values.get(key)
            if val:
                await el.fill(val)
                filled.append(key)
            break
    return filled


def _resume_file(resume) -> dict:
    """The resume under the name you uploaded it with, not the app's dated copy on disk."""
    path = Path(resume["path"])
    name = Path(resume["filename"] or path.name).name
    return {"name": name, "mimeType": RESUME_TYPES.get(path.suffix.lower(), "application/octet-stream"),
            "buffer": path.read_bytes()}


async def _attach_resume(frame, resume_file) -> bool:
    files = await frame.query_selector_all("input[type=file]")
    if not files:
        return False
    target = None
    for el in files:
        desc = (await el.evaluate(DESCRIBE_JS)).lower()
        if "cover" in desc:
            continue
        if re.search(r"resume|cv\b|curriculum", desc):
            target = el
            break
        target = target or el
    if target is None:
        return False
    await target.set_input_files(resume_file)
    await frame.page.wait_for_timeout(1500)  # let the ATS parse/upload it
    return True


async def _confirmed(page) -> bool:
    for fr in page.frames:
        try:
            if CONFIRM_RE.search(await fr.inner_text("body", timeout=2000)):
                return True
        except Exception:
            pass
    return False


async def _has_captcha(page) -> bool:
    return await page.locator("iframe[src*='recaptcha'], iframe[src*='hcaptcha'], iframe[src*='turnstile']").count() > 0


async def run_application(internship_id: int):
    async with _get_lock():
        await _run(internship_id)


def _site(url: str, sites: dict):
    host = (urlsplit(url or "").hostname or "").lower()
    for domain, name in sites.items():
        if host == domain or host.endswith("." + domain):
            return name
    return None


def account_site(url: str):
    """The name of the application system if it is one that needs an account, else None."""
    return _site(url, ACCOUNT_SITES)


def listing_site(url: str):
    """The name of the job site if it is one the bot must not open (you sign in there), else None."""
    return _site(url, LISTING_SITES)


async def _run(internship_id: int):
    it = db.get_internship(internship_id)
    if it is None or it["status"] != db.APPLYING:
        return   # deleted, archived or handled by hand while it sat in the queue
    url = it["referral_link"] or it["url"]
    resume = db.latest_resume()
    profile = db.get_setting("profile") or {}
    mode = db.get_setting("apply_mode") or "auto"
    shot = db.SHOT_DIR / f"{internship_id}.png"

    def attention(msg):
        db.update_internship(internship_id, status=db.NEEDS_ATTENTION, error=msg)

    # A Handshake link (or none): apply on the company's own board if the job is there.
    searched = False
    if not it["referral_link"] and (not url or listing_site(url)) and it["title"]:
        searched = True
        found = await asyncio.to_thread(jobboards.find_posting, it["company"], it["title"], it["location"])
        if found:
            db.update_internship(internship_id, url=found["url"], source_url=it["source_url"] or it["url"])
            url = found["url"]
    not_on_board = " It isn't on the company's Greenhouse, Lever or Ashby job board either." if searched else ""

    if not url:
        return attention("No application URL.%s Paste the posting link below and retry." % not_on_board)
    lister = listing_site(url)
    if lister:
        return attention(
            "This job is listed on %s, which needs you signed in, so the bot doesn't open it.%s Open it, click "
            "Apply externally, paste the employer's link below and click Retry bot. Or apply there yourself and "
            "click “I applied manually”." % (lister, not_on_board))
    if resume is None:
        return attention("No resume uploaded yet. Upload one in the Resume & Profile tab, then retry.")
    if not profile.get("email"):
        return attention("Your profile has no email. Fill in Resume & Profile, then retry.")
    by_hand = "Open the link and apply there, then click “I applied manually”."
    site = account_site(url)
    if site:
        return attention("This posting is on %s, which makes you create an account before applying, so the "
                         "bot can't do it. %s" % (site, by_hand))

    db.update_internship(internship_id, status=db.APPLYING, error=None, resume_id=resume["id"])
    headless = os.environ.get("RF_HEADLESS", "1") != "0"
    launch = {"headless": headless}
    if os.environ.get("CHROMIUM_PATH"):
        launch["executable_path"] = os.environ["CHROMIUM_PATH"]

    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch(**launch)
            page = await browser.new_page(viewport={"width": 1280, "height": 1800})
            try:
                frame = await _open_form(page, url)
                # Never type into a sign-in or sign-up page (a tracking link can land on one).
                site = account_site(page.url) or listing_site(page.url)
                if site or await frame.locator("input[type=password]:visible").count():
                    await page.screenshot(path=str(shot), full_page=True)
                    return attention("This posting %s, so the bot can't do it. %s" % (
                        "is on %s, which makes you create an account before applying" % site if site
                        else "asks you to sign in or create an account before applying", by_hand))
                filled = await _fill_fields(frame, _profile_values(profile))
                attached = await _attach_resume(frame, _resume_file(resume))
                missing = await frame.evaluate(MISSING_REQUIRED_JS)
                await page.screenshot(path=str(shot), full_page=True)

                if not attached and not filled:
                    return attention("Couldn't find an application form on that page. Open it and apply manually.")
                if missing:
                    uniq = list(dict.fromkeys(m.strip()[:80] for m in missing if m))
                    return attention("Not submitted: these required fields need your answer: " + "; ".join(uniq))
                if mode == "dry_run":
                    return attention(f"Dry run: filled {', '.join(filled) or 'nothing'}"
                                     f"{' + resume' if attached else ''}; not submitted.")

                submit = None
                for sel in SUBMIT_SELECTORS:
                    loc = frame.locator(sel).first
                    if await loc.count() and await loc.is_visible():
                        submit = loc
                        break
                if submit is None:
                    return attention("Filled the form but couldn't find the submit button.")

                await submit.click()
                for _ in range(15):
                    await page.wait_for_timeout(1000)
                    if await _confirmed(page):
                        break
                await page.screenshot(path=str(shot), full_page=True)

                if await _confirmed(page):
                    db.update_internship(internship_id, status=db.APPLIED, applied_at=db.now(), error=None)
                else:
                    hint = " A CAPTCHA is on the page, which probably blocked it." if await _has_captcha(page) else ""
                    attention("Clicked submit but didn't see a confirmation. Check the screenshot and the posting." + hint)
            finally:
                await browser.close()
    except Exception as e:
        traceback.print_exc()
        attention(f"Bot error: {type(e).__name__}: {str(e)[:300]}")
