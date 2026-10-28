"""
Job Hunter — Automated Internship Search & Email Alerts
========================================================
Searches LinkedIn (no login needed) and Handshake (login required once)
for internships, scores them against Koustubh's resume using Gemini (free)
or OpenAI as backup, and sends Discord alerts every 30 minutes.

SETUP (run once):
  1. pip install -r requirements.txt
  2. playwright install chromium
  3. Copy .env.example to .env and fill in your values
  4. python job_hunter.py --setup-auth   ← log in to Handshake in the browser
  5. python job_hunter.py --discord-webhook YOUR_WEBHOOK_URL

OPTIONS:
  --once              Run a single cycle and exit
  --setup-auth        Open browser to save Handshake login session
  --interval-minutes  How often to run (default: 30)
  --discord-webhook   Discord Webhook URL for alerts (or set DISCORD_WEBHOOK in .env)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import sys

# Force UTF-8 output on Windows so emoji/unicode in print() don't crash
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

# ── Optional deps ─────────────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    import requests
    from bs4 import BeautifulSoup
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False
    print("[warn] requests / beautifulsoup4 not installed — LinkedIn scraping disabled")
    print("       Run: pip install requests beautifulsoup4")

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False
    print("[warn] playwright not installed — Handshake scraping disabled")
    print("       Run: pip install playwright && playwright install chromium")

try:
    from google import genai as google_genai
    from google.genai import types as genai_types
    HAS_GEMINI = True
except ImportError:
    HAS_GEMINI = False
    google_genai = None
    print("[warn] google-genai not installed — Gemini scoring disabled")
    print("       Run: pip install google-genai")

try:
    from openai import OpenAI
    HAS_OPENAI = True
except ImportError:
    HAS_OPENAI = False

try:
    import docx
    HAS_DOCX = True
except ImportError:
    HAS_DOCX = False
    print("[warn] python-docx not installed — Cover letter generation disabled")
    print("       Run: pip install python-docx")

# ── Resume text (Koustubh Sadalagi) ──────────────────────────────────────────
RESUME_TEXT = """
KOUSTUBH KRISHNA SADALAGI
+1 480-374-9476 | sadalagi.koustubh@gmail.com | linkedin.com/in/koustubh-sadalagi-b544a0257 | github.com/koustubhks

SUMMARY
MS Robotics & Autonomous Systems student at Arizona State University focused on robotics software,
computer vision, embedded systems, robot manipulation, robot learning, and autonomous systems.
Hands-on experience with Python, C/C++, OpenCV, ROS 2, embedded microcontrollers, CAN/LIN/Ethernet,
perception-to-control pipelines, teleoperation, path planning, motion planning, real-time inference,
and AI-assisted robotics projects. Seeking robotics software, computer vision, embedded systems,
autonomous systems, robot learning, or AI/ML engineering internships, co-ops, and full-time roles.

EDUCATION
- MS Robotics & Autonomous Systems - Systems Engineering, Arizona State University, Tempe, AZ
  Aug 2025 - May 2027 | GPA 3.61/4.0
  Coursework: Advanced Robotics, Embedded Systems, Artificial Intelligence, Machine Learning, Control Systems
- B.E. Information Science & Engineering, Jyothy Institute of Technology, Bengaluru, India
  2020 - 2024 | GPA 7.35/10

TECHNICAL SKILLS
Programming: Python, C, C++, Java, MATLAB, Git, GitHub
Robotics, AI & Computer Vision: ROS 2, OpenCV, PyTorch, TensorFlow, Transformers, Hugging Face,
LoRA, imitation learning, ACT, CARLA, perception, planning, controls
Embedded & Real-Time Systems: Embedded C/C++, Infineon AURIX TC397, GPIO, MCMCAN, CAN, LIN,
Ethernet, Trace, miniWiggler
Automation & Tools: Linux, Windows, Simulink, Microsoft Power Platform, SharePoint, Playwright,
Discord webhooks, APIs

PROFESSIONAL EXPERIENCE
- IT Office Aide, Arizona State University, Tempe, AZ | May 2026 - Present
  Provide technical support for students, faculty, and staff; troubleshoot hardware, software,
  account, and classroom technology issues.
- RPA Engineer, Oregano Solutions, Mumbai, India | Oct 2024 - Jul 2025
  Designed and deployed workflow automation using Microsoft Power Platform, reducing manual processing
  time and improving business-process reliability. Tested automation logic across multiple workflows
  and supported end users with troubleshooting, reporting, and process documentation.
- Embedded Software Development Intern, Nexteer Automotive, Bengaluru, India | Nov 2023 - Dec 2023
  Developed and debugged embedded software for Infineon AURIX TC397XX microcontrollers using AURIX
  Development Studio, GPIO, and MCMCAN. Worked with CAN, LIN, and Ethernet communication concepts
  while validating automotive ECU behavior with Trace and miniWiggler debugging tools. Documented
  issues, analyzed failures, and collaborated with engineering mentors in a safety-critical automotive
  software environment.

PROJECTS
- Transformer-Based Imitation Learning for SO-101 Robot Manipulation:
  LeRobot, ACT, PyTorch, OpenCV. Built a dual-arm leader-follower data-collection setup using SO-101
  arms and camera inputs for manipulation tasks such as clutter sorting, pick-and-place, and cloth
  handling. Designed a perception-to-action pipeline with teleoperation, synchronized robot trajectories,
  visual observations, curated demonstrations, and transformer/CVAE action-sequence prediction.
- TCP-GRU Autonomous Driving Pipeline:
  CARLA, Python, PyTorch, Computer Vision. Developed an autonomous-driving research pipeline in CARLA
  for perception-to-control policy training, checkpoint evaluation, route following, motion planning,
  and scenario testing. Used temporal modeling concepts with GRU-based sequence learning to improve
  decision consistency across sequential driving observations.
- LLM-Controlled Agentic Robot for Pick-and-Place:
  Python, OpenCV, LLM Planner, Dobot. Built an agentic robot system connecting natural-language
  instructions to visual perception, task planning, and physical robot execution. Implemented OpenCV
  scene detection and affine coordinate transformation to map camera observations into robot-frame
  pick-and-place actions.
- Vision-Guided Maze-Solving Robot Agent:
  Python, OpenCV, BFS, Homography. Created a computer-vision robotics pipeline for maze detection,
  colored-marker recognition, homography-based grid mapping, BFS path planning, and Dobot motion execution.
- LoRA Adapter Merge and Parameter-Efficient Fine-Tuning:
  PyTorch, Transformers, Hugging Face. Experimented with LoRA adapter merging, task arithmetic,
  rank ablation, and model evaluation to study efficient adaptation of transformer models.
- AI Job Hunter Automation:
  Python, Playwright, LLM APIs, Discord. Built a robust job-search automation system that collects
  postings from LinkedIn, Handshake, and SerpApi; filters relevant opportunities; scores role fit with
  LLMs; tracks state; and sends Discord alerts.

RESEARCH INTERESTS
Robot learning, embodied AI, autonomous systems, machine vision, embedded robotics, sensor fusion,
human-robot interaction
""".strip()

# ── Config ────────────────────────────────────────────────────────────────────
MATCH_THRESHOLD = 70        # email if score >= 70%
PAY_BOOST_MIN_SCORE = 60    # also email if score >= 60% AND pay is high
HIGH_PAY_HOURLY = 25        # $/hr threshold for pay boost rule

SEARCH_KEYWORDS = [
    "robotics internship",
    "robotics co-op",
    "robotics coop",
    "robotics co op",
    "embedded systems internship",
    "embedded systems co-op",
    "embedded systems coop",
    "embedded systems co op",
    "computer vision internship",
    "computer vision co-op",
    "computer vision coop",
    "computer vision co op",
    "autonomous systems internship",
    "autonomous systems co-op",
    "autonomous systems coop",
    "autonomous systems co op",
    "ROS2 internship",
    "machine learning robotics internship",
    "software engineer internship robotics",
    "software engineer co-op robotics",
    "software engineer coop robotics",
    "software engineer co op robotics",
]

LINKEDIN_LOCATION = "United States"
LINKEDIN_PAGES = max(1, int(os.getenv("LINKEDIN_PAGES", "3")))
LINKEDIN_RESULTS_PER_PAGE = max(1, int(os.getenv("LINKEDIN_RESULTS_PER_PAGE", "10")))
LINKEDIN_TIME_FILTER = os.getenv("LINKEDIN_TIME_FILTER", "r604800").strip()
AUTH_STATE_FILE = "auth_state.json"
SEEN_JOBS_FILE = "seen_jobs.json"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
]


def _is_coop_query(keyword: str) -> bool:
    normalized = re.sub(r"[-_/]+", " ", (keyword or "").lower())
    return "co op" in normalized or "coop" in normalized or "cooperative" in normalized


# ── Data model ────────────────────────────────────────────────────────────────
@dataclass
class Job:
    title: str
    company: str = ""
    location: str = ""
    pay: str = ""
    url: str = ""
    description: str = ""
    source: str = ""
    posted_date: str = ""

    def uid(self) -> str:
        url_lower = self.url.lower().rstrip("/")
        if "linkedin.com/jobs" in url_lower:
            # Match any numeric job ID (relaxed from 8+ to 5+ digits)
            m = re.search(r"(\d{5,})", url_lower)
            if m:
                return f"li_{m.group(1)}"
        elif "handshake.com" in url_lower:
            m = re.search(r"/jobs/(\d+)", url_lower)
            if m:
                return f"hs_{m.group(1)}"
        # Fallback: use normalized URL (strip query params + trailing slash)
        url_clean = self.url.split("?")[0].rstrip("/").lower().strip()
        key = url_clean or f"{self.title.lower().strip()}|{self.company.lower().strip()}"
        return hashlib.md5(key.encode()).hexdigest()[:14]


# ── Seen-jobs tracker (avoids repeat emails) ─────────────────────────────────
def load_seen() -> Set[str]:
    p = Path(SEEN_JOBS_FILE)
    if p.exists():
        try:
            return set(json.loads(p.read_text()))
        except Exception:
            return set()
    return set()


def save_seen(seen: Set[str]) -> None:
    Path(SEEN_JOBS_FILE).write_text(json.dumps(sorted(seen), indent=2))


# ── LinkedIn scraper (requests — no login needed) ─────────────────────────────
def _li_session() -> "requests.Session":
    s = requests.Session()
    s.headers.update({
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
        "Connection": "keep-alive",
    })
    return s


def search_linkedin(keyword: str) -> List[Job]:
    """
    Hit LinkedIn's public guest-jobs endpoint (no login required).
    Returns internship/co-op listings for the given keyword.
    """
    if not HAS_REQUESTS:
        return []

    jobs: List[Job] = []
    try:
        url = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
        seen_urls: Set[str] = set()
        for page_idx in range(LINKEDIN_PAGES):
            params = {
                "keywords": keyword,
                "location": LINKEDIN_LOCATION,
                "f_E": "1",    # Entry / Internship experience level
                "sortBy": "DD",
                "start": page_idx * LINKEDIN_RESULTS_PER_PAGE,
            }
            if not _is_coop_query(keyword):
                params["f_JT"] = "I"   # Internship job type; co-op queries are keyword-filtered instead.
            if LINKEDIN_TIME_FILTER:
                params["f_TPR"] = LINKEDIN_TIME_FILTER

            time.sleep(random.uniform(2, 5))
            resp = _li_session().get(url, params=params, timeout=30)
            if resp.status_code != 200:
                print(f"    [LinkedIn] HTTP {resp.status_code} for '{keyword}' page {page_idx + 1}")
                break

            soup = BeautifulSoup(resp.text, "html.parser")
            page_jobs = 0
            for card in soup.find_all("li"):
                try:
                    title_el = card.find("h3", class_=re.compile("base-search-card__title"))
                    co_el    = card.find("h4", class_=re.compile("base-search-card__subtitle"))
                    loc_el   = card.find("span", class_=re.compile("job-search-card__location"))
                    link_el  = card.find("a", class_=re.compile("base-card__full-link"))
                    time_el  = card.find("time")

                    title   = (title_el.get_text(strip=True) if title_el else "")
                    company = (co_el.get_text(strip=True)    if co_el    else "")
                    loc     = (loc_el.get_text(strip=True)   if loc_el   else "")
                    href    = (link_el["href"].split("?")[0]  if link_el  else "")
                    posted  = ""
                    if time_el:
                        posted = time_el.get("datetime") or time_el.get_text(strip=True)

                    if title and href and href not in seen_urls:
                        seen_urls.add(href)
                        jobs.append(Job(title=title, company=company,
                                        location=loc, url=href, source="linkedin",
                                        posted_date=posted))
                        page_jobs += 1
                except Exception:
                    continue

            if page_jobs == 0:
                break

        print(f"    [LinkedIn] {len(jobs)} results for '{keyword}'")
    except Exception as exc:
        print(f"    [LinkedIn] Search error: {exc}")
    return jobs


def get_linkedin_details(job: Job) -> Job:
    """Fetch full description + salary from a LinkedIn job page."""
    if not job.url or not HAS_REQUESTS:
        return job
    try:
        time.sleep(random.uniform(2, 5))
        resp = _li_session().get(job.url, timeout=30)
        if resp.status_code != 200:
            return job
        soup = BeautifulSoup(resp.text, "html.parser")

        # Description
        desc_el = (
            soup.find("div", class_=re.compile(r"show-more-less-html__markup"))
            or soup.find("div", class_=re.compile(r"description__text"))
            or soup.find("section", class_=re.compile(r"description"))
        )
        if desc_el:
            job.description = desc_el.get_text(separator="\n", strip=True)[:5000]

        # Salary
        salary_el = (
            soup.find("div", class_=re.compile(r"compensation"))
            or soup.find("span", class_=re.compile(r"salary"))
        )
        if salary_el and not job.pay:
            job.pay = salary_el.get_text(strip=True)

        if not job.posted_date:
            time_el = soup.find("time")
            if time_el:
                job.posted_date = time_el.get("datetime") or time_el.get_text(strip=True)

    except Exception as exc:
        print(f"    [LinkedIn] Detail fetch failed: {exc}")
    return job


# ── SerpApi (Google Jobs / Indeed FAANG) ──────────────────────────────────────
_SERPAPI_KEY_INDEX = 0

def get_serpapi_key() -> str:
    global _SERPAPI_KEY_INDEX
    raw = os.getenv("SERPAPI_KEYS", os.getenv("SERPAPI_KEY", ""))
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    if not keys:
        return ""
    if _SERPAPI_KEY_INDEX >= len(keys):
        return "" # All exhausted
    return keys[_SERPAPI_KEY_INDEX]

def mark_serpapi_key_exhausted():
    global _SERPAPI_KEY_INDEX
    _SERPAPI_KEY_INDEX += 1

def search_serpapi(keyword: str) -> List[Job]:
    """Search Google Jobs directly using SerpApi. Automatically rotates keys."""
    if not HAS_REQUESTS:
        return []
    key = get_serpapi_key()
    if not key:
        return []
        
    jobs: List[Job] = []
    try:
        url = "https://serpapi.com/search.json"
        params = {
            "engine": "google_jobs",
            "q": keyword,
            "hl": "en",
            "gl": "us",
            "api_key": key
        }
        resp = requests.get(url, params=params, timeout=30)
        
        # Check quota/auth
        if resp.status_code in [401, 403, 429]:
            r_json = resp.json() if "json" in resp.headers.get("Content-Type", "") else {}
            err_msg = r_json.get("error", "").lower()
            if "exhausted" in err_msg or "limit" in err_msg or resp.status_code == 429:
                print(f"    [SerpApi] API Key limit reached. Switching keys... ({_SERPAPI_KEY_INDEX + 1} exhausted)")
                mark_serpapi_key_exhausted()
                return search_serpapi(keyword) # Retry with next key
            else:
                print(f"    [SerpApi] API key error or unauthorized: HTTP {resp.status_code}")
                return []
                
        if resp.status_code != 200:
            print(f"    [SerpApi] Error fetching jobs: HTTP {resp.status_code}")
            return []
            
        data = resp.json()
        for j in data.get("jobs_results", []):
            title = j.get("title", "")
            company = j.get("company_name", "")
            location = j.get("location", "")
            description = j.get("description", "")
            detected = j.get("detected_extensions") or {}
            posted = ""
            if isinstance(detected, dict):
                posted = detected.get("posted_at") or detected.get("posted_at_date") or ""
            if not posted:
                extensions = j.get("extensions") or []
                if isinstance(extensions, list):
                    posted = next((str(x) for x in extensions if "ago" in str(x).lower() or "posted" in str(x).lower()), "")
            
            # Get best application URL
            apply_opts = j.get("apply_options", [])
            link = apply_opts[0].get("link", "") if apply_opts else j.get("share_link", "")
            
            import urllib.parse
            if not link:
                link = f"https://www.google.com/search?q={urllib.parse.quote(title + ' ' + company)}&ibp=htl;jobs"

            if title:
                jobs.append(Job(
                    title=title,
                    company=company,
                    location=location,
                    description=description[:5000] if description else "",
                    url=link,
                    source=(f"indeed via google_jobs" if "indeed" in str(j).lower() else "google_jobs"),
                    posted_date=str(posted)
                ))
                
        print(f"    [SerpApi] {len(jobs)} FAANG/Indeed results retrieved")
    except Exception as exc:
        print(f"    [SerpApi] API failure: {exc}")
        
    return jobs


# ── Handshake scraper (Playwright + saved auth state) ────────────────────────
def setup_handshake_auth() -> None:
    """Open a visible browser window so you can log in to Handshake once."""
    if not HAS_PLAYWRIGHT:
        print("Playwright not installed. Run: pip install playwright && playwright install chromium")
        return
    print("\n🔐 Opening browser for Handshake login…")
    print("   1. Complete your ASU SSO login in the browser window")
    print("   2. Wait until you can see the Handshake internship listings")
    print("   3. Come back here and press Enter\n")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        ctx = browser.new_context(viewport={"width": 1400, "height": 900})
        page = ctx.new_page()
        page.goto(
            "https://asu.joinhandshake.com/job-search"
            "?page=1&per_page=25&job_types[]=Internship",
            wait_until="domcontentloaded",
        )
        input("   ✅ Press Enter once you're logged in and see jobs… ")
        ctx.storage_state(path=AUTH_STATE_FILE)
        browser.close()
    print(f"   Auth state saved to: {AUTH_STATE_FILE}\n")


def _hs_context(pw):
    """Create a Playwright context that reuses the saved Handshake session."""
    browser = pw.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
        ],
    )
    ctx = browser.new_context(
        storage_state=AUTH_STATE_FILE,
        viewport={"width": 1440, "height": 900},
        user_agent=random.choice(USER_AGENTS),
    )
    # Remove the navigator.webdriver flag so Handshake doesn't detect automation
    ctx.add_init_script(
        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
    )
    return browser, ctx


def _parse_hs_api_entry(entry: Any) -> Optional[Job]:
    """Parse one job entry from Handshake's GraphQL or REST API response."""
    if not isinstance(entry, dict):
        return None

    # ── Title (snake_case REST or camelCase GraphQL) ──
    # Skip wrapper nodes (id='job_search_result_N') that have no real job title
    raw_id = str(entry.get("id") or "")
    if raw_id.startswith("job_search_result"):
        return None
    title = (entry.get("title") or entry.get("jobTitle") or
             entry.get("job_title") or entry.get("position_title") or
             entry.get("positionTitle") or "")
    if not title:
        return None

    # ── Company / Employer ──
    emp = entry.get("employer") or entry.get("company") or {}
    if isinstance(emp, dict):
        company = emp.get("name") or emp.get("employerName") or ""
    else:
        company = ""
    company = company or (entry.get("employer_name") or entry.get("employerName") or
                          entry.get("company_name") or entry.get("companyName") or "")

    # ── Location ──
    loc_obj = entry.get("location") or {}
    if isinstance(loc_obj, dict):
        location = loc_obj.get("city") or loc_obj.get("name") or loc_obj.get("display") or ""
    else:
        location = str(loc_obj) if loc_obj else ""
    location = location or (entry.get("city") or entry.get("remote_option") or
                             entry.get("remoteOption") or entry.get("locationName") or "")

    # ── Pay ──
    raw_pay = (entry.get("compensation") or entry.get("pay_range") or
               entry.get("payRange") or entry.get("salary") or
               entry.get("wage") or entry.get("pay") or
               entry.get("salaryRange") or "")
    if isinstance(raw_pay, dict):
        # Handshake salary object: amounts are in cents, paySchedule tells the period
        pay_min = raw_pay.get("min") or 0
        pay_max = raw_pay.get("max") or pay_min
        schedule = raw_pay.get("paySchedule") or {}
        behavior = (schedule.get("behaviorIdentifier") or "") if isinstance(schedule, dict) else ""
        if pay_min:
            lo = pay_min / 100
            hi = pay_max / 100
            if behavior == "HOURLY_WAGE":
                pay = f"${lo:,.0f} - ${hi:,.0f}/hr" if lo != hi else f"${lo:,.0f}/hr"
            elif behavior == "ANNUAL_SALARY":
                pay = f"${lo:,.0f} - ${hi:,.0f}/year" if lo != hi else f"${lo:,.0f}/year"
            elif behavior == "MONTHLY_STIPEND":
                pay = f"${lo:,.0f} - ${hi:,.0f}/month" if lo != hi else f"${lo:,.0f}/month"
            else:
                pay = f"${lo:,.0f} - ${hi:,.0f}" if lo != hi else f"${lo:,.0f}"
        else:
            pay = ""
    else:
        pay = str(raw_pay) if raw_pay else ""

    # ── Job ID → URL ──
    # Only use numeric IDs for the URL (skip synthetic 'job_search_result_N' ids)
    for id_key in ("id", "job_id", "jobId", "posting_id", "postingId"):
        candidate_id = str(entry.get(id_key) or "")
        if candidate_id.isdigit():
            job_id = candidate_id
            break
    else:
        job_id = ""
    url = f"https://asu.joinhandshake.com/stu/jobs/{job_id}" if job_id else ""

    description = str(entry.get("description") or entry.get("jobDescription") or
                      entry.get("job_description") or "")[:3000]

    posted_date = str(
        entry.get("postedDate") or entry.get("posted_date") or
        entry.get("createdAt") or entry.get("created_at") or
        entry.get("publishedAt") or entry.get("published_at") or
        entry.get("publicationDate") or entry.get("publication_date") or ""
    )

    return Job(title=str(title), company=str(company), location=str(location),
               pay=str(pay), url=url, description=description, source="handshake",
               posted_date=posted_date)


def search_handshake(keyword: str) -> List[Job]:
    """
    Search Handshake by intercepting its own JSON API calls.
    Handshake is a React SPA — job cards are <div> elements with no <a> href.
    Capturing the XHR/fetch responses that load the job list is far more reliable.
    """
    if not HAS_PLAYWRIGHT:
        return []
    if not Path(AUTH_STATE_FILE).exists():
        print("    [Handshake] No auth state — run: python job_hunter.py --setup-auth")
        return []

    jobs: List[Job] = []
    try:
        with sync_playwright() as pw:
            browser, ctx = _hs_context(pw)
            page = ctx.new_page()

            # ── Intercept every JSON response the page makes ──────────────────
            captured: List[Dict[str, Any]] = []

            def on_response(resp):
                try:
                    if resp.status != 200:
                        return
                    ct = resp.headers.get("content-type", "")
                    if "json" not in ct:
                        return
                    body = resp.json()
                    captured.append({"url": resp.url, "body": body})
                except Exception:
                    pass

            page.on("response", on_response)

            # Navigate to the base search page (no keyword in URL — React ignores URL params)
            page.goto("https://asu.joinhandshake.com/job-search",
                      wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(4000)

            # ── Flush initial-load responses so we only see SEARCH responses ──
            # The page load itself fires a GraphQL call that returns ~6 default
            # recommended jobs.  Clearing here means `captured` will only contain
            # responses triggered by our keyword search, so max() picks the real
            # search results instead of the default list.
            captured.clear()

            # Use page.locator() — re-queries the DOM on each action so
            # React re-renders don't cause "element detached" errors
            search_box = page.locator(
                'input[placeholder*="job"], '
                'input[placeholder*="Describe"], '
                'input[placeholder*="search"], '
                'input[placeholder*="Search"], '
                'input[placeholder*="Find"], '
                'input[type="search"], '
                '[data-hook="search-bar"] input, '
                '[class*="search"] input'
            ).first
            search_typed = False
            try:
                search_box.wait_for(state="visible", timeout=8000)
                search_box.click()
                page.wait_for_timeout(400)
                # Select all and replace so we don't append to existing text
                search_box.press("Control+a")
                search_box.type(keyword, delay=50)
                page.wait_for_timeout(600)
                page.keyboard.press("Enter")
                search_typed = True
                print(f"    [Handshake] Typed '{keyword}' into search box")
            except Exception as e:
                print(f"    [Handshake] Search box interaction failed: {e}")

            if not search_typed:
                # Fall back: try navigating to a search URL with query params
                import urllib.parse
                safe_kw = urllib.parse.quote(keyword)
                if _is_coop_query(keyword):
                    fallback_urls = [
                        f"https://asu.joinhandshake.com/stu/jobs?page=1&per_page=25&sort_direction=desc&query={safe_kw}",
                        f"https://asu.joinhandshake.com/job-search?query={safe_kw}",
                        f"https://asu.joinhandshake.com/stu/jobs?page=1&per_page=25&sort_direction=desc&query={safe_kw}&job_types[]=3",
                    ]
                else:
                    # job_types[]=3 is internship in Handshake's enum
                    fallback_urls = [
                        f"https://asu.joinhandshake.com/stu/jobs?page=1&per_page=25&sort_direction=desc&query={safe_kw}&job_types[]=3",
                        f"https://asu.joinhandshake.com/job-search?query={safe_kw}&job_types[]=Internship",
                    ]
                for furl in fallback_urls:
                    try:
                        page.goto(furl, wait_until="domcontentloaded", timeout=60_000)
                        print(f"    [Handshake] Fell back to URL search for '{keyword}'")
                        break
                    except Exception:
                        continue

            # Wait for first page of results to load
            page.wait_for_timeout(8000)

            current_url = page.url
            if any(x in current_url.lower() for x in ["sign_in", "login", "sso", "saml"]):
                print("    [Handshake] Session expired — run --setup-auth again")
                browser.close()
                return []

            # ── Scroll to load more pages (infinite scroll = more GraphQL calls) ─
            for _ in range(3):
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(4000)

            print(f"    [Handshake] Intercepted {len(captured)} JSON responses")

            # ── Parse captured API responses (GraphQL + REST) ─────────────────
            def find_job_lists(obj: Any, depth: int = 0) -> List[List[Any]]:
                """Recursively find lists of job-like dicts in a JSON structure.
                Handles REST lists, GraphQL nodes[], and GraphQL edges[]{node} patterns.
                """
                if depth > 8:
                    return []
                found = []

                if isinstance(obj, list) and len(obj) >= 3:
                    first = obj[0]
                    if isinstance(first, dict):
                        # Must have a title AND an employer/company field to be a job list
                        has_title = any(k in first for k in (
                            "title", "jobTitle", "job_title", "positionTitle"))
                        has_employer = any(k in first for k in (
                            "employer", "employerName", "employer_name",
                            "company", "companyName", "company_name",
                            "organization", "hiring_organization"))
                        if has_title and has_employer:
                            found.append(obj)
                        # GraphQL edges pattern: [{node: {...}}, ...]
                        elif "node" in first and isinstance(first["node"], dict):
                            nodes = [e["node"] for e in obj if "node" in e]
                            found.extend(find_job_lists(nodes, depth + 1))

                elif isinstance(obj, dict):
                    # GraphQL nodes pattern: {"nodes": [...]}
                    if "nodes" in obj and isinstance(obj["nodes"], list):
                        found.extend(find_job_lists(obj["nodes"], depth + 1))
                    # GraphQL edges pattern: {"edges": [...]}
                    if "edges" in obj and isinstance(obj["edges"], list):
                        found.extend(find_job_lists(obj["edges"], depth + 1))
                    # Recurse into all values
                    for v in obj.values():
                        if isinstance(v, (dict, list)):
                            found.extend(find_job_lists(v, depth + 1))

                return found

            # Only look at Handshake's own GraphQL/API calls, skip analytics
            hs_responses = [
                c for c in captured
                if "joinhandshake.com" in c["url"] and
                   any(x in c["url"] for x in ("graphql", "posting", "job", "search", "api", "query"))
            ]
            print(f"    [Handshake] Handshake API calls: {len(hs_responses)}")

            # ── Pass 1: directly parse Handshake's jobSearch GraphQL pattern ──
            # Structure: data.jobSearch.edges[].node  (node may wrap a 'job' sub-obj)
            for item in hs_responses:
                body = item.get("body", {})
                if not isinstance(body, dict):
                    continue
                data = body.get("data", {})
                if not isinstance(data, dict):
                    continue
                job_search = data.get("jobSearch", {})
                if not isinstance(job_search, dict):
                    continue
                edges = job_search.get("edges", [])
                total = job_search.get("totalCount", 0)
                if not edges:
                    continue
                print(f"    [Handshake] jobSearch response: {total} total, {len(edges)} edges")
                for edge in edges:
                    if not isinstance(edge, dict):
                        continue
                    node = edge.get("node", {}) or {}
                    # The node may be a wrapper (id='job_search_result_N') with
                    # job fields directly on it, OR nested under a 'job' key.
                    for candidate in [node, node.get("job") or {}]:
                        if isinstance(candidate, dict) and candidate:
                            parsed = _parse_hs_api_entry(candidate)
                            if parsed:
                                jobs.append(parsed)
                                break

            # ── Pass 1b: parse single job detail GraphQL responses ──
            # Some fallback navigations return data.job instead of data.jobSearch.
            # Capturing these is still useful when the search UI changed.
            if not jobs:
                for item in hs_responses:
                    body = item.get("body", {})
                    if not isinstance(body, dict):
                        continue
                    data = body.get("data", {})
                    if not isinstance(data, dict):
                        continue
                    single_job = data.get("job")
                    if isinstance(single_job, dict):
                        parsed = _parse_hs_api_entry(single_job)
                        if parsed:
                            jobs.append(parsed)

            # ── Pass 2: generic recursive scanner as fallback ─────────────────
            if not jobs:
                all_candidate_lists: List[List[Any]] = []
                for item in hs_responses:
                    all_candidate_lists.extend(find_job_lists(item["body"]))

                if all_candidate_lists:
                    best_list = max(all_candidate_lists, key=len)
                    sizes = sorted([len(l) for l in all_candidate_lists], reverse=True)
                    print(f"    [Handshake] Fallback list sizes: {sizes[:8]}")
                    for entry in best_list[:30]:
                        parsed = _parse_hs_api_entry(entry)
                        if parsed:
                            jobs.append(parsed)

            if not jobs:
                # Debug: dump GraphQL response keys to help diagnose future breakage
                gql = [c for c in captured if "graphql" in c["url"]]
                if gql:
                    print("    [Handshake] GraphQL data keys (for debugging):")
                    for g in gql[:3]:
                        data = g["body"].get("data", {}) if isinstance(g["body"], dict) else {}
                        print(f"      data keys: {list(data.keys()) if isinstance(data, dict) else type(data)}")
                        if isinstance(data, dict):
                            for k, v in data.items():
                                print(f"        {k}: {type(v).__name__} = {str(v)[:120]}")
                print("    [Handshake] 0 jobs found in API — see debug above")

            browser.close()
        print(f"    [Handshake] {len(jobs)} results for '{keyword}'")
    except Exception as exc:
        print(f"    [Handshake] Search error: {exc}")
    return jobs


def get_handshake_details(job: Job) -> Job:
    """Fetch full description from a Handshake job page."""
    if not job.url or not Path(AUTH_STATE_FILE).exists() or not HAS_PLAYWRIGHT:
        return job
    try:
        with sync_playwright() as pw:
            browser, ctx = _hs_context(pw)
            page = ctx.new_page()
            page.goto(job.url, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(3000)

            desc_el = (
                page.query_selector("[data-hook='job-description']")
                or page.query_selector("[class*='description']")
            )
            if desc_el:
                job.description = desc_el.inner_text().strip()[:5000]

            pay_el = page.query_selector("[class*='pay'],[class*='wage'],[class*='salary']")
            if pay_el and not job.pay:
                job.pay = pay_el.inner_text().strip()

            browser.close()
    except Exception as exc:
        print(f"    [Handshake] Detail fetch failed: {exc}")
    return job


# ── Pay extraction ────────────────────────────────────────────────────────────
def extract_hourly(text: str) -> Optional[float]:
    if not text:
        return None
    t = text.lower().replace(",", "")
    # "$20 - $35/hour", "$25/hr", "25 per hour", etc.
    m = re.search(
        r"\$?\s*(\d+(?:\.\d+)?)\s*(?:[-–]|to)?\s*\$?\s*(\d+(?:\.\d+)?)?"
        r"\s*(?:/|per\s*)(?:hour|hr|h)\b",
        t,
    )
    if m:
        lo = float(m.group(1))
        hi = float(m.group(2)) if m.group(2) else lo
        return max(lo, hi)
    return None


# ── Job scoring ───────────────────────────────────────────────────────────────
KEYWORDS = [
    "robotics", "robot", "autonomous", "embedded", "firmware", "control",
    "computer vision", "opencv", "python", "c++", "matlab", "simulink",
    "ros", "ros2", "can bus", "lin protocol", "ethernet",
    "imitation learning", "transformer", "real-time", "microcontroller",
    "sensor", "perception", "navigation", "manipulation", "lora",
    "llm", "machine learning", "tensorflow", "pytorch", "deep learning",
    "slam", "carla", "reinforcement learning", "motion planning",
    "localization", "mechatronics", "actuator", "servo",
]


def keyword_score(job: Job) -> Tuple[int, str]:
    text = f"{job.title} {job.description}".lower()
    hits = [kw for kw in KEYWORDS if kw in text]
    score = min(len(hits) * 7, 82)
    reason = f"Keyword hits ({len(hits)}): {', '.join(hits[:8]) or 'none'}"
    return score, reason


def _parse_ai_response(raw: str, fallback_job: Job) -> Tuple[int, str]:
    """Parse JSON from AI response, fall back to keyword score on failure."""
    try:
        m = re.search(r"\{.*\}", raw, re.S)
        if m:
            data   = json.loads(m.group(0))
            score  = max(0, min(100, int(data.get("score", 0))))
            reason = str(data.get("reason", ""))
            hits   = data.get("key_matches", [])
            gaps   = data.get("gaps", [])
            detail = (
                f"{reason} "
                f"| Matches: {', '.join(hits[:6])}."
                f"{' | Gaps: ' + ', '.join(gaps[:3]) + '.' if gaps else ''}"
            )
            return score, detail
    except Exception:
        pass
    score, reason = keyword_score(fallback_job)
    return score, f"Fallback keyword score (invalid AI response): {reason}"


def _score_prompt(job: Job) -> str:
    return f"""You are an expert technical recruiter. Score how well this internship or co-op matches the candidate's resume.

CANDIDATE RESUME:
{RESUME_TEXT}

INTERNSHIP / CO-OP POSTING:
Title:       {job.title}
Company:     {job.company}
Location:    {job.location}
Pay:         {job.pay or 'not listed'}
Description:
{job.description or '(no description fetched yet)'}

Return ONLY valid JSON with these exact keys:
{{
  "score": <integer 0-100>,
  "reason": "<2-3 sentence explanation of the match quality>",
  "key_matches": ["<skill or keyword that matched>", ...],
  "gaps": ["<requirement candidate may lack>", ...]
}}

Scoring guide:
- 85-100: Near-perfect technical and domain fit
- 70-84:  Strong match, most key skills present
- 60-69:  Moderate match, worth applying if pay is good
- 40-59:  Partial match
- 0-39:   Poor match

Be conservative and honest. The candidate is an MS Robotics student at ASU with embedded + robotics background.

CRITICAL FILTERING RULES:
Before scoring, check for these explicit disqualifiers in the posting. If ANY apply, you MUST return a score of 0 and note the exact reason in "gaps":
1. Requires "US Citizenship", "U.S. Citizenship", "Green Card", "Security Clearance", or explicitly states "No international students" / "No CPT/OPT" / "Will not sponsor" / "Visa sponsorship is not available".
2. Is strictly for PhD students OR explicitly "for undergrads only" / bachelor's students only (candidate is an MS student).
3. The internship or co-op starts BEFORE Summer 2026 (e.g., Fall 2025, Winter 2025, Spring 2026, Sep 2025). Only allow Summer 2026 or later, or if the date is unspecified."""

TAILORED_RESUMES_DIR = Path("tailored_resumes")


def _tailored_resume_prompt(job: Job) -> str:
    return f"""You are an expert resume writer. Rewrite the candidate's resume to better target this specific internship or co-op.

RULES:
- Keep ALL facts, dates, GPAs, and technologies accurate — do NOT invent or exaggerate anything
- Reorder bullet points to surface the most relevant experience first
- Add 1-2 strong action-verb bullets per role/project that highlight skills this job explicitly requires (only if they are genuinely derivable from the existing experience)
- Adjust the summary/objective line to mirror the job's language
- Keep the same sections and overall length
- Output plain text, ready to paste into a document

CANDIDATE RESUME:
{RESUME_TEXT}

TARGET INTERNSHIP / CO-OP:
Title:       {job.title}
Company:     {job.company}
Description:
{job.description or '(no description available)'}

Output the full tailored resume text only — no commentary, no markdown headers."""


def generate_tailored_resume(job: Job, score: int, clients: dict) -> Optional[Path]:
    """Generate a tailored resume for a matched job and save to tailored_resumes/."""
    TAILORED_RESUMES_DIR.mkdir(exist_ok=True)

    prompt = _tailored_resume_prompt(job)
    resume_text = None

    # Try Gemini first, then OpenAI
    if clients.get("gemini"):
        try:
            client = clients["gemini"]["client"]
            model  = clients["gemini"]["model"]
            resp   = client.models.generate_content(model=model, contents=prompt)
            resume_text = resp.text
        except Exception as exc:
            print(f"    [Resume] Gemini failed: {exc}")

    if not resume_text and clients.get("openai"):
        try:
            resp = clients["openai"].chat.completions.create(
                model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=1500,
            )
            resume_text = resp.choices[0].message.content or ""
        except Exception as exc:
            print(f"    [Resume] OpenAI failed: {exc}")

    if not resume_text:
        print("    [Resume] Could not generate tailored resume (no AI available)")
        return

    # Build a safe filename: Company_Title_Score_Date.txt
    safe = re.sub(r'[\\/:*?"<>|]', "", f"{job.company}_{job.title}").replace(" ", "_")[:60]
    filename = TAILORED_RESUMES_DIR / f"{safe}_{score}pct_{datetime.now().strftime('%Y%m%d')}.txt"
    filename.write_text(resume_text, encoding="utf-8")
    print(f"    [Resume] Saved tailored resume → {filename}")
    return filename


COVER_LETTERS_DIR = Path("cover_letters")

def _cover_letter_prompt(job: Job) -> str:
    return f"""You are an expert career coach and professional copywriter. Write a compelling, highly professional cover letter for the candidate tailored to this specific internship or co-op.

RULES:
- Format the output EXACTLY as the body of a cover letter. Do not include your own commentary or markdown blocks.
- Use a professional, confident, and enthusiastic tone.
- Keep it concise (no more than 3-4 paragraphs).
- Map the candidate's robotic/embedded systems experience directly to the job description constraints.
- Emphasize the core skills in the candidate's resume that match the role.
- Be straightforward and do not hallucinate details.
- Add standard placeholders like [Date], [Company Address], [Hiring Manager], etc. at the top.

CANDIDATE RESUME:
{RESUME_TEXT}

TARGET INTERNSHIP / CO-OP:
Title:       {job.title}
Company:     {job.company}
Description:
{job.description or '(no description available)'}

Output the professional cover letter text only."""

def generate_cover_letter_docx(job: Job, score: int, clients: dict) -> Optional[Path]:
    """Generate a cover letter for a matched job and save to cover_letters/ as .docx."""
    if not HAS_DOCX:
        return
    COVER_LETTERS_DIR.mkdir(exist_ok=True)

    prompt = _cover_letter_prompt(job)
    cl_text = None

    # Try Gemini first, then OpenAI
    if clients.get("gemini"):
        try:
            client = clients["gemini"]["client"]
            model  = clients["gemini"]["model"]
            resp   = client.models.generate_content(model=model, contents=prompt)
            cl_text = resp.text
        except Exception as exc:
            print(f"    [CoverLetter] Gemini failed: {exc}")

    if not cl_text and clients.get("openai"):
        try:
            resp = clients["openai"].chat.completions.create(
                model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
                messages=[{"role": "user", "content": prompt}],
                temperature=0.4,
                max_tokens=1500,
            )
            cl_text = resp.choices[0].message.content or ""
        except Exception as exc:
            print(f"    [CoverLetter] OpenAI failed: {exc}")

    if not cl_text:
        print("    [CoverLetter] Could not generate cover letter (no AI available)")
        return

    # Build docx
    try:
        doc = docx.Document()
        for p in cl_text.split('\n'):
            if p.strip():
                doc.add_paragraph(p.strip())
        
        safe = re.sub(r'[\\/:*?"<>|]', "", f"{job.company}_{job.title}").replace(" ", "_")[:60]
        filename = COVER_LETTERS_DIR / f"{safe}_{score}pct_{datetime.now().strftime('%Y%m%d')}.docx"
        doc.save(filename)
        print(f"    [CoverLetter] Saved tailored cover letter → {filename}")
        return filename
    except Exception as exc:
        print(f"    [CoverLetter] Failed to save docx: {exc}")


_gemini_quota_exhausted = False

def gemini_score(job: Job, gemini_info: dict) -> Optional[Tuple[int, str]]:
    """Score using Google Gemini (free tier, primary scorer)."""
    global _gemini_quota_exhausted
    if _gemini_quota_exhausted:
        return None

    client = gemini_info["client"]
    model  = gemini_info["model"]
    for attempt in range(2):
        try:
            resp = client.models.generate_content(model=model, contents=_score_prompt(job))
            return _parse_ai_response(resp.text, job)
        except Exception as exc:
            err = str(exc)
            if "429" in err or "RESOURCE_EXHAUSTED" in err:
                print("    [Gemini] Quota exhausted or rate limit hit — switching to fallback.")
                _gemini_quota_exhausted = True
                break
            elif "503" in err or "UNAVAILABLE" in err:
                wait = 15
                print(f"    [Gemini] Service unavailable, retrying in {wait}s…")
                time.sleep(wait)
            else:
                print(f"    [Gemini] Scoring error: {exc}")
                break
    return None


_openai_quota_exhausted = False  # suppress repeated quota messages

def openai_score(job: Job, openai_client) -> Optional[Tuple[int, str]]:
    """Score using OpenAI (backup if Gemini unavailable)."""
    global _openai_quota_exhausted
    if _openai_quota_exhausted:
        return None
    try:
        resp = openai_client.chat.completions.create(
            model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
            messages=[{"role": "user", "content": _score_prompt(job)}],
            temperature=0.1,
            max_tokens=600,
        )
        return _parse_ai_response(resp.choices[0].message.content or "", job)
    except Exception as exc:
        err = str(exc)
        if "insufficient_quota" in err or "quota" in err.lower():
            if not _openai_quota_exhausted:
                print("    [OpenAI] Quota exhausted — switching to keyword scoring for this cycle")
                _openai_quota_exhausted = True
        else:
            print(f"    [OpenAI] Scoring error: {exc}")
    return None


def ai_score(job: Job, clients: dict) -> Tuple[int, str]:
    """Score job: tries Gemini first, then OpenAI, then keywords."""
    if clients.get("gemini"):
        res = gemini_score(job, clients["gemini"])
        if res: return res
    if clients.get("openai"):
        res = openai_score(job, clients["openai"])
        if res: return res
    score, reason = keyword_score(job)
    return score, f"Fallback keyword score (AI unavailable): {reason}"


def should_notify(score: int, pay_text: str) -> Tuple[bool, str]:
    """Returns (send_flag, trigger_reason)."""
    if score >= MATCH_THRESHOLD:
        return True, f"≥{MATCH_THRESHOLD}% match"
    hourly = extract_hourly(pay_text)
    if score >= PAY_BOOST_MIN_SCORE and hourly and hourly >= HIGH_PAY_HOURLY:
        return True, f"≥{PAY_BOOST_MIN_SCORE}% match + high pay (${hourly:.0f}/hr)"
    return False, ""


# ── Discord ───────────────────────────────────────────────────────────────────
def format_discord_message(job: Job, score: int, reason: str, trigger: str) -> dict:
    hourly = extract_hourly(job.pay)
    pay_line = job.pay if job.pay else "Not listed"
    if hourly:
        pay_line += f"  (≈ ${hourly:.0f}/hr)"

    color = 0x00FF00 if score >= 85 else (0xFFFF00 if score >= 70 else 0xFFA500)

    desc_snippet = job.description[:500] + "..." if job.description and len(job.description) > 500 else (job.description or '(No description)')

    embed = {
        "title": f"[{score}% Match] {job.title}",
        "url": job.url if job.url else None,
        "color": color,
        "fields": [
            {"name": "🏢 Company", "value": job.company or "N/A", "inline": True},
            {"name": "📍 Location", "value": job.location or "N/A", "inline": True},
            {"name": "💰 Pay", "value": pay_line, "inline": True},
            {"name": "📊 Match Score", "value": f"{score}%  *(Trigger: {trigger})*", "inline": False},
            {"name": "🧠 AI Reasoning", "value": reason, "inline": False},
            {"name": "🔍 Source", "value": job.source.capitalize(), "inline": True},
            {"name": "🗓️ Posted", "value": job.posted_date or "Unknown", "inline": True},
        ],
        "description": desc_snippet,
        "footer": {
            "text": f"Job Hunter • {datetime.now().strftime('%b %d, %Y %H:%M')}"
        }
    }
    
    return {"embeds": [embed]}


def send_discord_webhook(payload: dict, webhook_url: str) -> None:
    if not HAS_REQUESTS:
        print("    [Discord] 'requests' library not installed — skipping webhook")
        return
    if not webhook_url:
        print("    [Discord] DISCORD_WEBHOOK not set — skipping")
        return

    try:
        resp = requests.post(webhook_url, json=payload, timeout=10)
        resp.raise_for_status()
        title = payload.get("embeds", [{}])[0].get("title", "Job alert")
        print(f"    💬 Sent Discord Alert: {title}")
    except Exception as exc:
        print(f"    [Discord] Webhook send failed: {exc}")


# ── Deduplication ─────────────────────────────────────────────────────────────
def dedup(jobs: List[Job]) -> List[Job]:
    """Deduplicate by UID first, then by normalized title+company as secondary key."""
    seen_uid: dict[str, Job] = {}
    seen_title_co: set[str] = set()
    result: List[Job] = []
    for j in jobs:
        uid = j.uid()
        # Secondary key: normalized title + company (catches same job with different URLs)
        title_co = f"{j.title.lower().strip()}|{j.company.lower().strip()}"
        if uid not in seen_uid and title_co not in seen_title_co:
            seen_uid[uid] = j
            seen_title_co.add(title_co)
            result.append(j)
    return result


# ── Main search cycle ─────────────────────────────────────────────────────────
def run_cycle(discord_webhook: str, clients: dict) -> None:
    print(f"\n{'═'*62}")
    print(f"  🔍  Job Hunter cycle — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'═'*62}")

    seen = load_seen()
    all_jobs: List[Job] = []

    for kw in SEARCH_KEYWORDS:
        print(f"\n  Keyword: \"{kw}\"")
        all_jobs += search_linkedin(kw)
        time.sleep(random.uniform(3, 7))
        all_jobs += search_handshake(kw)
        time.sleep(random.uniform(3, 7))

    print(f"\n  Keyword: \"SerpApi FAANG/Indeed Mega-Query\"")
    mega_query = '("Robotics" OR "Embedded" OR "Computer Vision" OR "Autonomous Systems") (Internship OR "Co-op" OR Coop) posted after October 1 2026'
    all_jobs += search_serpapi(mega_query)

    all_jobs = dedup(all_jobs)

    def _is_new(j: Job) -> bool:
        # Primary check: UID (url-based) in seen set
        if j.uid() in seen:
            return False
        # Secondary check: tc:title|company key persisted from previous runs.
        # This catches jobs whose URL/UID changed between cycles or script restarts.
        tc_key = f"tc:{j.title.lower().strip()}|{j.company.lower().strip()}"
        if tc_key in seen:
            return False
        return True

    new_jobs = [j for j in all_jobs if _is_new(j)]

    print(f"\n  📋  {len(all_jobs)} total  |  {len(new_jobs)} new this cycle")

    sent = 0
    for job in new_jobs:
        print(f"\n  ── {job.title} @ {job.company or '?'} [{job.source}]")

        # Fetch full description if missing
        if not job.description:
            if job.source == "linkedin":
                job = get_linkedin_details(job)
            elif job.source == "handshake":
                job = get_handshake_details(job)

        # Pre-filter checks
        is_disqualified = False
        dq_reason = ""
        text_to_check = f"{job.title} {job.description}".lower()
        dq_terms = [
            "us citizenship", "u.s. citizenship", "green card", "security clearance",
            "clearance required", "no international students", "no cpt/opt",
            "will not sponsor", "visa sponsorship is not available",
            "undergrads only", "bachelor's students only", "bachelors only",
            "fall 2025", "winter 2025", "spring 2026", "sep 2025", "september 2025"
        ]
        for term in dq_terms:
            if term in text_to_check:
                is_disqualified = True
                dq_reason = f"Contains disqualifier: '{term}'"
                break

        if is_disqualified:
            score, reason = 0, dq_reason
            hourly = extract_hourly(job.pay)
            pay_str = f"${hourly:.0f}/hr" if hourly else (job.pay or "pay N/A")
            print(f"     Score: {score}%  |  {pay_str}  |  {reason}")
        else:
            # Score
            score, reason = ai_score(job, clients)
            hourly = extract_hourly(job.pay)
            pay_str = f"${hourly:.0f}/hr" if hourly else (job.pay or "pay N/A")
            print(f"     Score: {score}%  |  {pay_str}")

        # Mark seen regardless of score (avoid reprocessing next cycle)
        # Also store a title|company secondary key so that if the same job
        # returns with a different URL next cycle it is still filtered out.
        seen.add(job.uid())
        seen.add(f"tc:{job.title.lower().strip()}|{job.company.lower().strip()}")
        save_seen(seen)

        # Decide whether to notify
        send_flag, trigger = should_notify(score, job.pay)
        if send_flag and discord_webhook:
            payload = format_discord_message(job, score, reason, trigger)
            send_discord_webhook(payload, discord_webhook)
            generate_tailored_resume(job, score, clients)
            generate_cover_letter_docx(job, score, clients)
            sent += 1
        elif send_flag:
            print(f"     ⚠️  Would notify but DISCORD_WEBHOOK not set (score={score}%, trigger={trigger})")
            generate_tailored_resume(job, score, clients)
            generate_cover_letter_docx(job, score, clients)
        else:
            print(f"     skip (below threshold)")

        time.sleep(random.uniform(4, 7))

    print(f"\n  ✅  Cycle done — {sent} alert(s) sent\n")


# ── CLI entry point ───────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Automated internship search & Discord alert bot"
    )
    parser.add_argument(
        "--discord-webhook",
        default=os.getenv("DISCORD_WEBHOOK", ""),
        help="Discord Webhook URL for alerts (or set DISCORD_WEBHOOK in .env)",
    )
    parser.add_argument(
        "--interval-minutes",
        type=int,
        default=1440,
        help="How often to run in minutes (default: 1440 = 24 hours / once a day)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single cycle then exit",
    )
    parser.add_argument(
        "--setup-auth",
        action="store_true",
        help="Open browser to log in to Handshake and save session",
    )
    args = parser.parse_args()

    # Auth setup mode
    if args.setup_auth:
        setup_handshake_auth()
        return

    # Init AI clients — Gemini (free, primary) → OpenAI (backup) → keywords
    clients: dict = {}

    gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
    if HAS_GEMINI and gemini_key:
        gemini_client = google_genai.Client(api_key=gemini_key)
        # Try models in order — newest first
        gemini_model_name = None
        for candidate in [
            "gemini-2.5-flash",
            "gemini-2.0-flash",
            "gemini-2.0-flash-001",
            "gemini-2.0-flash-lite",
            "gemini-1.5-flash",
        ]:
            try:
                gemini_client.models.generate_content(model=candidate, contents="hi")
                gemini_model_name = candidate
                clients["gemini"] = {"client": gemini_client, "model": gemini_model_name}
                break
            except Exception as e:
                print(f"    [Gemini] {candidate} probe failed: {e}")
        if gemini_model_name:
            print(f"✅  Gemini ready  (model: {gemini_model_name} — FREE tier)")
        else:
            print("⚠️  Gemini quota issue — OpenAI will handle scoring instead")
            print("    (To fix Gemini: generate a fresh key at aistudio.google.com)")
    else:
        print("⚠️  Gemini not configured  (set GEMINI_API_KEY in .env for free AI scoring)")

    openai_key = os.getenv("OPENAI_API_KEY", "").strip()
    if HAS_OPENAI and openai_key:
        clients["openai"] = OpenAI(api_key=openai_key)
        print(f"✅  OpenAI ready  (backup, model: {os.getenv('OPENAI_MODEL','gpt-4.1-mini')})")

    if not clients:
        print("⚠️  No AI keys found — using keyword matching only")

    if not args.discord_webhook:
        print("⚠️  DISCORD_WEBHOOK not set — matches will be printed but NOT sent to Discord")

    serp_key = os.getenv("SERPAPI_KEYS", os.getenv("SERPAPI_KEY", ""))
    if serp_key:
        count = len([k for k in serp_key.split(",") if k.strip()])
        print(f"✅  SerpApi configured ({count} key(s) loaded for Google Jobs / Indeed FAANG search)")
    else:
        print("⚠️  SerpApi not configured — skipping automatic Google Jobs / Indeed FAANG searches")

    # Run loop
    cycle = 1
    while True:
        try:
            run_cycle(discord_webhook=args.discord_webhook, clients=clients)
        except KeyboardInterrupt:
            print("\n\n👋  Stopped by user. Goodbye!")
            break
        except Exception as exc:
            print(f"\n❌  Unexpected error in cycle {cycle}: {exc}")

        if args.once:
            break

        wait_sec = max(60, args.interval_minutes * 60)
        next_ts  = datetime.fromtimestamp(time.time() + wait_sec).strftime("%H:%M:%S")
        print(f"  ⏱   Next run at {next_ts}   (Ctrl+C to stop)\n")
        try:
            time.sleep(wait_sec)
        except KeyboardInterrupt:
            print("\n\n👋  Stopped. Goodbye!")
            break
        cycle += 1


if __name__ == "__main__":
    main()
