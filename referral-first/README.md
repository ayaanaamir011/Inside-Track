# Referral First

Check for a referral before you apply. If you have no contact at the company, a bot applies for you.

1. Import your LinkedIn connections once (and again every few weeks).
2. When you find an internship, enter the company and the posting link.
3. The app checks whether any of your connections works there. It shows only **how many**, never who.
   - **No contact:** the bot applies right away with your latest resume.
   - **Contact found:** the internship goes to **Waiting for referral**. Find the person on LinkedIn and ask them. When you get a referral link, paste it in and click **Apply now**, and the bot applies through that link.

## Run it

```bash
cd referral-first
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000.

Set `RF_HEADLESS=0` before starting to watch the bot fill forms in a visible browser window.

## First-time setup

1. **Resume & profile tab:** upload your resume and fill in your name, email, phone and links. A new upload becomes the "latest resume" for every future application.
2. **Connections tab:** import `Connections.csv` from LinkedIn's data export:
   Settings & Privacy → Data privacy → *Get a copy of your data* → tick **Connections** → *Request archive*.
3. Optional: set *When the bot applies* to **Fill only** and add a posting to see what the bot would do without submitting.

### Why the export instead of scraping LinkedIn?
Scraping LinkedIn with a bot breaks LinkedIn's terms and gets accounts restricted or banned. The official export gives the same data (name, company, position) with no risk to your account.

## How the bot applies

`app/applier.py` uses Playwright (Chromium):

- Opens the posting. For Lever links it goes to `/apply`. For career pages that embed a Greenhouse iframe, it finds the form inside the iframe.
- Fills name, email, phone, LinkedIn, GitHub, website, school and location by matching each field's label.
- Attaches your latest resume.
- **Checks for required fields that are still empty** (custom questions, essays, EEO/work-authorization dropdowns). If any are empty it does **not** submit. The internship moves to **Needs attention** with a list of the unanswered questions and a screenshot.
- Otherwise it clicks submit and waits for a "thank you / application received" page before marking the internship **Applied**.

Works best on Greenhouse, Lever and Ashby. Workday, iCIMS, Taleo and other sites that need an account mostly end up in Needs attention. A CAPTCHA will also stop it. That's on purpose: the bot never guesses answers on your behalf.

## Data

Everything stays on your machine in `referral-first/data/` (SQLite database, resumes, screenshots). That folder is git-ignored. Set `RF_DATA_DIR` to store it somewhere else.

## Tests

```bash
pip install -r requirements.txt
pytest
```

The bot tests drive a real browser against local fake application forms in `tests/fixtures/`.
