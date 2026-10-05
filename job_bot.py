"""
Free job-alert bot for QA / test / SDET roles.
Sources:
  1. Company career boards (Greenhouse + Lever public JSON APIs) - fastest
  2. Job-alert emails from LinkedIn / Naukri / Indeed via Gmail (official, no scraping)
Scores each job against your profile and sends HIGH matches to Telegram.
You tap the link and apply yourself.
"""
import os
import re
import json
import email
import imaplib
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ------------------------- CONFIG (edit these) -------------------------
MIN_LEVEL = "High"  # "High" = only best matches, "Medium" = High + Medium

ROLE_KEYWORDS = [
    "qa", "quality assurance", "quality engineer", "test engineer", "sdet",
    "software development engineer in test", "automation test", "test automation",
    "automation engineer", "software engineer in test", "quality analyst",
    "test analyst", "qe ", "software tester", "validation engineer",
]
EXCLUDE_IN_TITLE = ["intern", "director", "vp ", "head of", "principal", "manual tester"]

LOCATIONS = ["pune", "india", "remote", "hybrid"]  # empty location always passes

# Skills from your resume - more matches in the job text = higher score
SKILLS = [
    "python", "selenium", "robot framework", "pytest", "playwright", "api testing",
    "rest", "postman", "sql", "jenkins", "ci/cd", "git", "bdd", "cucumber",
    "page object", "jira", "docker", "regression", "automation framework",
]

# Company career boards. Find the slug in the careers URL:
#   boards.greenhouse.io/<slug>   or   jobs.lever.co/<slug>
GREENHOUSE_BOARDS = [
    # "examplecompany",
]
LEVER_BOARDS = [
    # "examplecompany",
]

EMAIL_SENDERS = ["linkedin.com", "naukri.com", "indeed.com"]
# -----------------------------------------------------------------------

SEEN_FILE = Path("seen.json")
LEVELS = {"Low": 0, "Medium": 1, "High": 2}


def score(title, text="", location=""):
    t = title.lower() + " "
    if any(x in t for x in EXCLUDE_IN_TITLE):
        return 0
    s = 0
    if any(k in t for k in ROLE_KEYWORDS):
        s += 50
    blob = (t + text).lower()
    s += min(sum(1 for k in SKILLS if k in blob), 6) * 5
    if not location or any(l in location.lower() for l in LOCATIONS):
        s += 20
    return s


def level(s):
    return "High" if s >= 70 else "Medium" if s >= 45 else "Low"


def clean_html(html):
    return BeautifulSoup(html or "", "html.parser").get_text(" ", strip=True)


def fetch_greenhouse():
    jobs = []
    for slug in GREENHOUSE_BOARDS:
        try:
            r = requests.get(
                f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true",
                timeout=20,
            )
            r.raise_for_status()
            for j in r.json().get("jobs", []):
                jobs.append({
                    "id": f"gh-{slug}-{j['id']}",
                    "title": j["title"],
                    "company": slug,
                    "location": (j.get("location") or {}).get("name", ""),
                    "url": j["absolute_url"],
                    "text": clean_html(j.get("content", "")),
                    "board": True,
                })
        except Exception as e:
            print(f"greenhouse {slug} failed: {e}")
    return jobs


def fetch_lever():
    jobs = []
    for slug in LEVER_BOARDS:
        try:
            r = requests.get(f"https://api.lever.co/v0/postings/{slug}?mode=json", timeout=20)
            r.raise_for_status()
            for j in r.json():
                jobs.append({
                    "id": f"lv-{slug}-{j['id']}",
                    "title": j["text"],
                    "company": slug,
                    "location": (j.get("categories") or {}).get("location", ""),
                    "url": j["hostedUrl"],
                    "text": j.get("descriptionPlain", ""),
                    "board": True,
                })
        except Exception as e:
            print(f"lever {slug} failed: {e}")
    return jobs


def fetch_emails():
    user = os.environ.get("GMAIL_USER")
    pwd = os.environ.get("GMAIL_APP_PASSWORD")
    if not (user and pwd):
        return []
    jobs = []
    try:
        m = imaplib.IMAP4_SSL("imap.gmail.com")
        m.login(user, pwd)
        m.select("INBOX")
        for sender in EMAIL_SENDERS:
            _, data = m.search(None, f'(UNSEEN FROM "{sender}")')
            for num in data[0].split():
                _, msg_data = m.fetch(num, "(RFC822)")
                msg = email.message_from_bytes(msg_data[0][1])
                html = ""
                for part in msg.walk():
                    if part.get_content_type() == "text/html":
                        html = part.get_payload(decode=True).decode(errors="ignore")
                        break
                for a in BeautifulSoup(html, "html.parser").find_all("a", href=True):
                    href, title = a["href"], a.get_text(" ", strip=True)
                    if len(title) < 5:
                        continue
                    mo = re.search(r"linkedin\.com/(?:comm/)?jobs/view/(\d+)", href)
                    if mo:
                        jid, url = f"li-{mo.group(1)}", f"https://www.linkedin.com/jobs/view/{mo.group(1)}"
                    elif "naukri.com" in href and "job-listings" in href:
                        url = href.split("?")[0]
                        jid = f"nk-{url}"
                    elif "indeed.com" in href and ("viewjob" in href or "/rc/clk" in href):
                        url = href
                        jid = f"in-{href.split('jk=')[-1][:20]}" if "jk=" in href else f"in-{href[:80]}"
                    else:
                        continue
                    jobs.append({"id": jid, "title": title, "company": "", "location": "",
                                 "url": url, "text": "", "board": False})
        m.logout()
    except Exception as e:
        print(f"gmail failed: {e}")
    return jobs


def notify(job, lvl, s):
    token, chat = os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
    lines = [f"\U0001F6A8 {lvl} match ({s}/100)", job["title"]]
    if job["company"]:
        lines.append(f"Company: {job['company']}")
    if job["location"]:
        lines.append(f"Location: {job['location']}")
    lines.append(job["url"])
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data={"chat_id": chat, "text": "\n".join(lines)},
        timeout=20,
    )
    if r.status_code != 200:
        print(f"telegram send failed: {r.status_code} {r.text[:200]}")


def main():
    first_run = not SEEN_FILE.exists()
    seen = set(json.loads(SEEN_FILE.read_text())) if not first_run else set()
    jobs = fetch_greenhouse() + fetch_lever() + fetch_emails()
    sent = 0
    for j in jobs:
        if j["id"] in seen:
            continue
        seen.add(j["id"])
        if first_run and j["board"]:
            continue  # don't flood you with every existing opening on day one
        s = score(j["title"], j["text"], j["location"])
        lvl = level(s)
        if LEVELS[lvl] >= LEVELS[MIN_LEVEL]:
            notify(j, lvl, s)
            sent += 1
    SEEN_FILE.write_text(json.dumps(sorted(seen)))
    print(f"checked {len(jobs)} jobs, sent {sent} alerts")


if __name__ == "__main__":
    main()
