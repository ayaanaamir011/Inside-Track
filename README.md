# Referral First

Check for a referral before you apply. If you have no contact at the company, a bot applies for you.

1. Import your LinkedIn connections once (and again every few weeks).
2. Add internships yourself, or connect Gmail and let the app read the **Intern Insider** email every Monday.
3. For each internship the app checks whether any of your connections works there. It shows only **how many**, never who.
   - **No contact:** the bot applies right away with your latest resume.
   - **Contact found:** the internship goes to **Waiting for referral**. Find the person on LinkedIn and ask them. When you get a referral link, paste it in and click **Apply now**, and the bot applies through that link.

## Run it (macOS or Linux)

You need Python 3.9 or newer. In a terminal, from this folder:

```bash
bash run.sh
```

The first run takes a few minutes: it creates a private Python environment in `.venv`, installs what the app needs, and downloads the browser the bot uses. After that it starts in seconds. It opens http://127.0.0.1:8000 for you. Leave the terminal window open while you use the app, and press Ctrl+C to stop it.

To watch the bot fill forms in a visible browser window, start it with:

```bash
RF_HEADLESS=0 bash run.sh
```

## First-time setup

1. **Resume & profile tab:** upload your resume and fill in your name, email, phone and links. A new upload becomes the "latest resume" for every future application.
2. **Connections tab:** import `Connections.csv` from LinkedIn's data export:
   Settings & Privacy → Data privacy → *Get a copy of your data* → choose **Download larger data archive** (the option that includes connections) → *Request archive*. LinkedIn emails a download link, which can take up to 24 hours and stays valid for 72 hours. Unzip it and upload the `Connections.csv` inside. If your page offers a **Connections** checkbox under "Want something in particular?", ticking only that also works and is quicker.
3. **Gmail tab** (optional): connect Gmail and choose what to apply to. See below.
4. Recommended for your first few tries: on the Resume & profile tab, set *When the bot applies* to **Fill only**. The bot then fills each form and stops, so you can see what it would do before it submits anything.

### Why the LinkedIn export instead of scraping?
Scraping LinkedIn with a bot breaks LinkedIn's terms and gets accounts restricted or banned. The official export gives the same data (name, company, position) with no risk to your account.

## How the contact check works

Until you import your connections the app applies to nothing, because it can't tell where you have contacts.

LinkedIn's file gives **one employer per connection**: the company on their *current* position, written the way they wrote it. So:

- Someone who interned at a company last summer, or starts there next year, is listed under whatever they have now (often their university) and won't count.
- Connections with no employer on their profile can't be matched. The import tells you how many.

A posting's company counts as the same as a connection's employer when the names:

- are equal, or one starts with the other ("Amazon" and "Amazon Web Services (AWS)")
- differ only in spacing, punctuation or accents ("JPMorganChase" and "JPMorgan Chase & Co.", "Nestle" and "Nestlé")
- contain one another as whole words ("Disney" and "The Walt Disney Company")
- are a typo apart ("Nvdia" and "NVIDIA")
- are a known pair ("Meta" and "Facebook", "Chase" and "J.P. Morgan", "EY" and "Ernst & Young", and about 30 more)

Missing a contact means applying without asking for a referral, so the check leans toward finding one. Wherever it reports contacts it also shows the employer names they are listed under (never who), so a wrong match is easy to spot: click **Apply now** and move on.

On the **Connections** tab you can:

- **Search your employers** to see exactly how a company is written in your connections, and how many people are there.
- **Add your own pairs**, one per line, such as `Acme Robotics = Acme Labs`, for names the app can't know are the same company.

When a contact turns up later (after a new import, a new pair, or an update to the app), internships that haven't been submitted yet move to **Waiting for referral**. Ones you chose to apply to anyway stay where they are.

## The weekly newsletter (Gmail tab)

Intern Insider sends its list every Monday from `hi@interninsider.me`. Each listing in it looks like
`Software Engineer Intern @ PayPal – San Jose, CA`, under a heading such as *Software Engineering*, *Data & AI/ML* or *Finance & Accounting*, split into USA and Canada.

**Connecting.** The app signs in to Gmail with an *app password* (a separate 16-letter password Google makes for one app):

1. Turn on 2-Step Verification if it is off: https://myaccount.google.com/signinoptions/twosv
2. Create an app password named "Referral First": https://myaccount.google.com/apppasswords
3. Paste it into the Gmail tab with your Gmail address.

An app password can read your whole mailbox, so treat it like a password. The app keeps it in your computer's keychain (macOS Keychain, Windows Credential Locker) when there is one, otherwise in its local data folder. It opens the mailbox read-only, looks only at emails from the newsletter's address, and never marks, moves, deletes or sends anything. You can revoke the app password on the same Google page at any time.

**What it applies to.** On the Gmail tab you choose:

- **Countries:** USA, Canada or both. Nothing is checked until you pick at least one.
- **Keywords:** a listing matches when its title, or the heading it sits under, contains any keyword. The defaults cover software, data, AI/ML, and sales and finance analyst roles. Every word of a keyword has to appear, and words of four or more letters also match longer words that start with them, so `analy` covers analyst, analytics and analysis.
- **Never-apply keywords:** anything containing one of these is skipped.
- **Mode:** handle matches automatically, or only list them so you click **Apply** yourself.

**What happens each week.** While the app is running it checks Gmail every 30 minutes. When a new issue arrives (and only if Gmail confirms it really came from the newsletter), every listing is recorded on the Gmail tab with what was decided and why. Matches go through the same referral check as a manual add. Skipped listings have an **Apply anyway** button. An issue is only ever handled once, issues older than a week are ignored, and a posting you already have is not added twice.

The app has to be running to check. If your computer was off on Monday, the issue is picked up the next time you start the app that week.

## How the bot applies

`app/applier.py` uses Playwright (Chromium):

- Opens the posting. Newsletter links pass through a click-tracking redirect first. For Lever postings it goes to the `/apply` page. For career pages that embed a Greenhouse iframe, it finds the form inside the iframe.
- Fills name, email, phone, LinkedIn, GitHub, website, school and location by matching each field's label.
- Attaches your latest resume.
- **Checks for required fields that are still empty** (custom questions, essays, work-authorization and demographic dropdowns). If any are empty it does **not** submit. The internship moves to **Needs attention** with a list of the unanswered questions and a screenshot.
- Otherwise it clicks submit and waits for a "thank you / application received" page before marking the internship **Applied**.

It applies to one posting at a time, so a Monday batch takes a while to work through. If you close the app with applications still queued, they resume the next time you start it.

Works best on Greenhouse, Lever and Ashby. Postings on Workday, Oracle Recruiting, iCIMS, Taleo or SuccessFactors need an account, so the bot doesn't try them: they go straight to **Needs attention** with a note. Anything with a CAPTCHA ends up there too. That's on purpose: the bot never guesses answers on your behalf.

**Expect most newsletter matches to need you.** In four recent Intern Insider issues (12 Oct to 9 Nov 2026), the default US filters matched 11 to 16 roles per issue. About a quarter of those were on Greenhouse, Lever or Ashby, nearly half were on Workday, and the rest were on other company sites. So each week the app mostly gives you a referral-checked to-do list with links, plus a few applications it can finish itself.

## Data and privacy

Everything stays on your machine in the `data/` folder next to `run.sh` (SQLite database, resumes, screenshots). That folder is git-ignored. Set `RF_DATA_DIR` to store it somewhere else.

The app has no login, so it only answers requests from its own page on the computer it runs on. Don't expose it to a network.

## Tests

```bash
.venv/bin/python -m pytest
```

The tests use a fake Gmail server, a sample newsletter email in `tests/fixtures/`, and a real browser against local fake application forms.
