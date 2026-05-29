"""
ASU Workday On-Campus Job Monitor
===================================
Checks ASU's Workday portal for new on-campus job postings,
emails you with the job details, and saves a tailored resume for each one.

SETUP (run once):
  1. pip install -r requirements.txt
  2. python workday_jobs.py --setup-auth
     └─ A browser opens. Navigate to the ASU on-campus jobs page,
        log in via SSO, wait until you can see job listings, then press Enter.
  3. python workday_jobs.py --once     ← test run
  4. python workday_jobs.py            ← runs every 12 hours automatically

OPTIONS:
  --once              Run a single check and exit
  --setup-auth        Open browser to save ASU Workday session
  --interval-minutes  How often to check (default: 720 = 12 hours)
  --email-to          Override EMAIL_TO from .env
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import smtplib
import ssl
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

# Force UTF-8 on Windows consoles
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ── Optional deps ──────────────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False
    print("[warn] playwright not installed — run: pip install playwright && playwright install chromium")

try:
    from google import genai as google_genai
    HAS_GEMINI = True
except ImportError:
    HAS_GEMINI = False
    google_genai = None

try:
    from openai import OpenAI
    HAS_OPENAI = True
except ImportError:
    HAS_OPENAI = False

# ── Resume text ────────────────────────────────────────────────────────────────
RESUME_TEXT = """
KOUSTUBH KRISHNA SADALAGI
[phone redacted for public repo] | [email redacted] | [LinkedIn profile]

EDUCATION
- MS Robotics & Autonomous Systems – Systems Engineering, Arizona State University (Aug 2025–May 2027), GPA 3.33/4.0
  Coursework: Advanced Robotics, Embedded Systems, AI & Machine Learning, Control Systems
- B.E. Information Science & Engineering, Jyothy Institute of Technology, India (2020–2024), GPA 7.35/10

TECHNICAL SKILLS
Programming:              C, C++ (intermediate), Python, Java
Machine Learning & CV:    TensorFlow, OpenCV, Gemini API, GPT-4
Embedded & Real-Time:     Embedded C/C++, Infineon AURIX TC397, CAN, LIN, Ethernet, Microcontrollers
Control & Modeling:       MATLAB, Simulink, Control System Analysis, PWM Control
Robotics:                 ROS2, Real-Time Systems, Control Systems
Web & Automation:         HTML, CSS, JavaScript, Microsoft Power Platform, SharePoint
OS:                       Linux, Windows

PROFESSIONAL EXPERIENCE
Oregano Solutions — Mumbai | RPA Developer | Oct 2024 – Jul 2025
  - Designed workflow automation with Microsoft Power Platform, reducing manual processing time
  - Tested/refined automation logic across multiple business processes

Nexteer Automotive — Bangalore | Embedded SW Intern | Nov–Dec 2023
  - Real-time embedded software on Infineon AURIX TC397 for automotive ECU
  - GPIO & MCMCAN modules with CAN, LIN, Ethernet protocols
  - System debugging/validation with Trace and miniWiggler tools
  - Safety-conscious embedded environment; cross-functional collaboration

PROJECTS
- Transformer-Based Imitation Learning (ACT) for Robotic Manipulation [ongoing]:
  Dual-arm leader-follower robot, CVAE transformer architecture, dual-camera perception-to-action pipeline,
  training pipeline, cloth-folding task evaluation.
- Autonomous Driving with CARLA Simulator [ongoing]:
  Perception-to-control, lane following, obstacle avoidance, checkpoint training pipeline.
- Parameter-Efficient Fine-Tuning with LoRA [ongoing]:
  LoRA fine-tuning on transformer models, memory benchmarking vs full fine-tuning.
- LLM-Controlled Agentic Robot for Pick-and-Place [Dec 2025]:
  OpenCV + LLM pipeline, affine transforms, Dobot robotic arm, real-world deployment.
- Autonomous Maze Solving Robot Agent [Nov 2025]:
  BFS path planning, homography warping, OpenCV, colored-marker detection, Dobot execution.
- Brain Tumor Detection CNN + Gemini Pro [May 2024]:
  EfficientNetB1 MRI classifier with TensorFlow; integrated Gemini Pro Vision for diagnostics.

RESEARCH INTERESTS
Wearable robotics, exoskeletons, soft robotics, human-robot interaction, AI for healthcare,
embodied intelligence
""".strip()

# ── Config ─────────────────────────────────────────────────────────────────────
AUTH_FILE       = "workday_auth_state.json"
SEEN_FILE       = "seen_workday_jobs.json"
RESUMES_DIR     = Path("workday_resumes")

# ASU Workday on-campus student jobs URL
# This is the public-facing student employment search page
WORKDAY_URL     = "https://asu.joinhandshake.com/job-search"  # updated in setup-auth

# Known ASU Workday URLs to try during setup
ASU_WORKDAY_URLS = [
    "https://www.myworkday.com/asu/d/inst/1$9925/9925$3084.htmld",
    "https://wd1.myworkdayjobs.com/en-US/ASU_Internal",
    "https://www.myworkday.com/asu/d/task/1422$3.htmld",
    "https://my.asu.edu",   # user can navigate from here
]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
]


# ── Data model ─────────────────────────────────────────────────────────────────
@dataclass
class WorkdayJob:
    title: str
    department: str = ""
    location: str = ""
    job_id: str = ""
    url: str = ""
    description: str = ""
    posted_date: str = ""

    def uid(self) -> str:
        key = self.job_id or f"{self.title}|{self.department}"
        return hashlib.md5(key.lower().strip().encode()).hexdigest()[:14]


# ── Seen-jobs tracker ──────────────────────────────────────────────────────────
def load_seen() -> Set[str]:
    p = Path(SEEN_FILE)
    if p.exists():
        try:
            return set(json.loads(p.read_text()))
        except Exception:
            return set()
    return set()


def save_seen(seen: Set[str]) -> None:
    Path(SEEN_FILE).write_text(json.dumps(sorted(seen), indent=2))


# ── Browser context ────────────────────────────────────────────────────────────
def _browser_context(pw):
    browser = pw.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
        ],
    )
    storage = AUTH_FILE if Path(AUTH_FILE).exists() else None
    ctx = browser.new_context(
        storage_state=storage,
        viewport={"width": 1440, "height": 900},
        user_agent=USER_AGENTS[0],
    )
    ctx.add_init_script(
        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
    )
    return browser, ctx


# ── Auth setup ─────────────────────────────────────────────────────────────────
def setup_auth() -> None:
    """Open a visible browser so you can log in to ASU Workday once."""
    if not HAS_PLAYWRIGHT:
        print("Playwright not installed. Run: pip install playwright && playwright install chromium")
        return

    print("\n[Setup] Opening browser for ASU Workday login...")
    print("   Steps:")
    print("   1. The browser will open to my.asu.edu")
    print("   2. Log in with your ASU credentials (SSO)")
    print("   3. Navigate to the on-campus student jobs page")
    print("      (Workday > Jobs > Student/On-Campus filter)")
    print("   4. Wait until you can see the job listings")
    print("   5. Come back here and press Enter\n")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        page.goto("https://my.asu.edu", wait_until="domcontentloaded")
        input("   [Setup] Press Enter once you can see job listings... ")
        # Save the URL so we know where to navigate next time
        final_url = page.url
        ctx.storage_state(path=AUTH_FILE)
        browser.close()

    # Save the landing URL for future automated runs
    meta = {"jobs_url": final_url}
    Path("workday_meta.json").write_text(json.dumps(meta, indent=2))
    print(f"\n   Session saved to: {AUTH_FILE}")
    print(f"   Jobs page URL saved: {final_url}")
    print("   Run: python workday_jobs.py --once   to test\n")


def _load_jobs_url() -> str:
    """Load the saved jobs page URL from setup, or use default."""
    try:
        meta = json.loads(Path("workday_meta.json").read_text())
        return meta.get("jobs_url", ASU_WORKDAY_URLS[0])
    except Exception:
        return ASU_WORKDAY_URLS[0]


# ── Workday job parser ─────────────────────────────────────────────────────────
def _parse_workday_entry(entry: Any) -> Optional[WorkdayJob]:
    """Parse a single job dict from Workday's API response."""
    if not isinstance(entry, dict):
        return None

    # Title — Workday uses several field names
    title = (
        entry.get("title") or
        entry.get("jobTitle") or
        entry.get("jobPostingTitle") or
        entry.get("name") or
        entry.get("requisitionTitle") or ""
    )
    if not title:
        return None

    # Department / supervisory org
    dept_raw = (
        entry.get("organizationData") or
        entry.get("primaryOrganization") or
        entry.get("department") or
        entry.get("supervisoryOrganization") or {}
    )
    if isinstance(dept_raw, dict):
        department = dept_raw.get("name") or dept_raw.get("descriptor") or ""
    else:
        department = str(dept_raw) if dept_raw else ""
    department = department or entry.get("departmentName") or entry.get("org") or ""

    # Location
    loc_raw = (
        entry.get("primaryLocation") or
        entry.get("location") or
        entry.get("jobSite") or {}
    )
    if isinstance(loc_raw, dict):
        location = loc_raw.get("descriptor") or loc_raw.get("name") or loc_raw.get("city") or ""
    else:
        location = str(loc_raw) if loc_raw else ""

    # Job ID
    job_id = (
        str(entry.get("id") or "") or
        str(entry.get("jobReqId") or "") or
        str(entry.get("jobPostingId") or "") or
        str(entry.get("bulletFields", [{}])[0] if isinstance(entry.get("bulletFields"), list) and entry.get("bulletFields") else "") or
        ""
    )
    # Also try descriptor/externalId
    if not job_id:
        job_id = str(entry.get("externalId") or entry.get("requisitionId") or "")

    # URL
    url = entry.get("externalUrl") or entry.get("url") or entry.get("applyUrl") or ""
    if not url and job_id:
        # Construct a Workday job URL pattern
        url = f"https://www.myworkday.com/asu/d/inst/1$9925/9925$3084.htmld?jobReqId={job_id}"

    # Description
    description = str(
        entry.get("jobDescription") or
        entry.get("description") or
        entry.get("jobPostingDescription") or ""
    )[:4000]

    # Posted date
    posted_date = str(
        entry.get("postedDate") or
        entry.get("startDate") or
        entry.get("publishedDate") or ""
    )[:20]

    return WorkdayJob(
        title=str(title),
        department=str(department),
        location=str(location),
        job_id=str(job_id),
        url=url,
        description=description,
        posted_date=posted_date,
    )


def _find_job_lists(obj: Any, depth: int = 0) -> List[List[Any]]:
    """Recursively find job-list arrays in any JSON structure."""
    if depth > 8:
        return []
    found = []
    if isinstance(obj, list) and len(obj) >= 1:
        first = obj[0]
        if isinstance(first, dict):
            has_title = any(k in first for k in (
                "title", "jobTitle", "jobPostingTitle", "name", "requisitionTitle"))
            if has_title:
                found.append(obj)
            elif "node" in first and isinstance(first["node"], dict):
                nodes = [e["node"] for e in obj if isinstance(e, dict) and "node" in e]
                found.extend(_find_job_lists(nodes, depth + 1))
    elif isinstance(obj, dict):
        for key in ("data", "jobs", "jobPostings", "items", "values",
                    "results", "edges", "nodes", "hits", "records"):
            if key in obj:
                found.extend(_find_job_lists(obj[key], depth + 1))
        for v in obj.values():
            if isinstance(v, (dict, list)):
                found.extend(_find_job_lists(v, depth + 1))
    return found


# ── Main scraper ───────────────────────────────────────────────────────────────
def fetch_workday_jobs() -> List[WorkdayJob]:
    """Navigate to ASU Workday and intercept job listing API calls."""
    if not HAS_PLAYWRIGHT:
        return []
    if not Path(AUTH_FILE).exists():
        print("  [Workday] No auth state — run: python workday_jobs.py --setup-auth")
        return []

    jobs: List[WorkdayJob] = []
    jobs_url = _load_jobs_url()

    try:
        with sync_playwright() as pw:
            browser, ctx = _browser_context(pw)
            page = ctx.new_page()

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

            print(f"  [Workday] Navigating to: {jobs_url}")
            page.goto(jobs_url, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(6000)

            # Check for redirect to login
            current_url = page.url
            if any(x in current_url.lower() for x in ["login", "signin", "sso", "saml", "idp", "auth"]):
                print("  [Workday] Session expired — run: python workday_jobs.py --setup-auth")
                browser.close()
                return []

            # Scroll to load more results
            for _ in range(3):
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(3000)

            print(f"  [Workday] Captured {len(captured)} API responses")

            # Filter to Workday/ASU API calls only
            wd_responses = [
                c for c in captured
                if any(x in c["url"] for x in (
                    "myworkday.com", "workdayjobs.com", "asu.edu",
                    "api", "job", "posting", "requisition"
                ))
            ]
            print(f"  [Workday] Workday API calls: {len(wd_responses)}")

            # Try to extract jobs from captured responses
            all_lists: List[List[Any]] = []
            for item in wd_responses:
                all_lists.extend(_find_job_lists(item["body"]))

            if all_lists:
                # Use the largest list found
                best = max(all_lists, key=len)
                print(f"  [Workday] Best job list: {len(best)} entries")
                for entry in best:
                    job = _parse_workday_entry(entry)
                    if job:
                        jobs.append(job)
            else:
                # Fallback: try scraping visible job cards from the DOM
                print("  [Workday] No API jobs found — trying DOM scrape...")
                jobs = _scrape_dom_jobs(page)

            # Debug: show data keys if still empty
            if not jobs:
                print("  [Workday] DEBUG — captured response data keys:")
                for item in wd_responses[:5]:
                    body = item["body"]
                    if isinstance(body, dict):
                        print(f"    {item['url'][:80]}")
                        print(f"      keys: {list(body.keys())[:10]}")

            browser.close()

    except Exception as exc:
        print(f"  [Workday] Error: {exc}")

    # Deduplicate
    seen_uids: dict[str, WorkdayJob] = {}
    for j in jobs:
        if j.uid() not in seen_uids:
            seen_uids[j.uid()] = j
    jobs = list(seen_uids.values())

    print(f"  [Workday] {len(jobs)} unique jobs found")
    return jobs


def _scrape_dom_jobs(page) -> List[WorkdayJob]:
    """Fallback: scrape visible job card text directly from the DOM."""
    jobs = []
    try:
        # Common Workday job card selectors
        selectors = [
            "[data-automation-id='jobTitle']",
            "[class*='jobTitle']",
            "li[class*='job']",
            "[role='listitem']",
            "a[data-automation-id]",
        ]
        for sel in selectors:
            cards = page.query_selector_all(sel)
            if cards:
                print(f"  [Workday] DOM: found {len(cards)} cards with selector '{sel}'")
                for card in cards:
                    try:
                        title = card.inner_text().strip()
                        href  = card.get_attribute("href") or ""
                        if title:
                            jobs.append(WorkdayJob(
                                title=title,
                                url=href,
                                job_id=hashlib.md5(title.encode()).hexdigest()[:8],
                            ))
                    except Exception:
                        continue
                if jobs:
                    break
    except Exception as exc:
        print(f"  [Workday] DOM scrape error: {exc}")
    return jobs


# ── AI resume generation ───────────────────────────────────────────────────────
def _resume_prompt(job: WorkdayJob) -> str:
    return f"""You are an expert resume writer. Rewrite the candidate's resume to better target this specific on-campus job at ASU.

RULES:
- Keep ALL facts, dates, GPAs, and technologies accurate — do NOT invent or exaggerate
- Reorder bullet points to surface the most relevant experience and skills first
- Adjust the objective/summary line to mirror the job's language and department
- Add 1-2 strong action-verb bullets per role/project only if genuinely derivable from existing experience
- Keep the same sections and similar length
- Output plain text ready to paste into a document

CANDIDATE RESUME:
{RESUME_TEXT}

TARGET JOB:
Title:       {job.title}
Department:  {job.department or 'ASU'}
Location:    {job.location or 'ASU Campus'}
Description:
{job.description or '(no description captured — tailor based on title and department)'}

Output the full tailored resume text only — no commentary, no markdown."""


def generate_resume(job: WorkdayJob, clients: dict) -> Optional[Path]:
    """Generate and save a tailored resume for the given job."""
    RESUMES_DIR.mkdir(exist_ok=True)
    prompt = _resume_prompt(job)
    text = None

    if clients.get("gemini"):
        try:
            client = clients["gemini"]["client"]
            model  = clients["gemini"]["model"]
            resp   = client.models.generate_content(model=model, contents=prompt)
            text   = resp.text
        except Exception as exc:
            print(f"    [Resume] Gemini error: {exc}")

    if not text and clients.get("openai"):
        try:
            resp = clients["openai"].chat.completions.create(
                model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=1500,
            )
            text = resp.choices[0].message.content or ""
        except Exception as exc:
            print(f"    [Resume] OpenAI error: {exc}")

    if not text:
        print("    [Resume] Could not generate resume (no AI available)")
        return None

    safe = re.sub(r'[\\/:*?"<>|]', "", f"{job.department}_{job.title}").replace(" ", "_")[:60]
    filepath = RESUMES_DIR / f"{safe}_{datetime.now().strftime('%Y%m%d_%H%M')}.txt"
    filepath.write_text(text, encoding="utf-8")
    print(f"    [Resume] Saved → {filepath}")
    return filepath


# ── Email ──────────────────────────────────────────────────────────────────────
def send_email(job: WorkdayJob, resume_path: Optional[Path], to_addr: str) -> None:
    smtp_user = os.getenv("SMTP_USER", "").strip()
    smtp_pass = os.getenv("SMTP_PASS", "").strip()
    if not smtp_user or not smtp_pass:
        print("    [Email] SMTP_USER/SMTP_PASS not set — skipping")
        return

    subject = f"[ASU On-Campus] New Job: {job.title}" + (f" | {job.department}" if job.department else "")

    body = f"""\
╔══════════════════════════════════════════════════════════╗
   🎓  ASU ON-CAMPUS JOB ALERT  —  {datetime.now().strftime('%b %d, %Y  %H:%M')}
╚══════════════════════════════════════════════════════════╝

POSITION   : {job.title}
DEPARTMENT : {job.department or 'N/A'}
LOCATION   : {job.location or 'ASU Campus'}
POSTED     : {job.posted_date or 'N/A'}
JOB ID     : {job.job_id or 'N/A'}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🔗  APPLY LINK:
    {job.url or 'Log in to Workday to apply — my.asu.edu > Workday > Jobs'}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

JOB DESCRIPTION:
──────────────────────────────────────────────────────────
{job.description[:2500] if job.description else '(Open the link above for full details)'}

──────────────────────────────────────────────────────────
{"Tailored resume saved to: " + str(resume_path) if resume_path else "Resume generation skipped (no AI configured)"}

Sent by ASU Workday Job Monitor  •  running every 12 hours
"""

    msg = EmailMessage()
    msg["From"]    = smtp_user
    msg["To"]      = to_addr
    msg["Subject"] = subject
    msg.set_content(body)

    host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    port = int(os.getenv("SMTP_PORT", "465"))
    try:
        with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context()) as s:
            s.login(smtp_user, smtp_pass)
            s.send_message(msg)
        print(f"    [Email] Sent: {subject}")
    except Exception as exc:
        print(f"    [Email] Failed: {exc}")


# ── Main cycle ─────────────────────────────────────────────────────────────────
def run_cycle(email_to: str, clients: dict) -> None:
    print(f"\n{'═'*60}")
    print(f"  ASU Workday Job Monitor — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'═'*60}")

    seen = load_seen()
    jobs = fetch_workday_jobs()

    new_jobs = [j for j in jobs if j.uid() not in seen]
    print(f"\n  {len(jobs)} total jobs  |  {len(new_jobs)} new\n")

    notified = 0
    for job in new_jobs:
        print(f"  >> NEW: {job.title} | {job.department or 'N/A'} | {job.location or 'ASU'}")

        # Mark seen immediately so crashes don't re-notify
        seen.add(job.uid())
        save_seen(seen)

        # Generate tailored resume
        resume_path = generate_resume(job, clients)

        # Send email notification
        if email_to:
            send_email(job, resume_path, email_to)
        else:
            print("    [Email] EMAIL_TO not set — skipping email")

        notified += 1
        time.sleep(1)

    if not new_jobs:
        print("  No new jobs since last check.")

    print(f"\n  Done — {notified} new job(s) notified\n")


# ── CLI ────────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="ASU Workday on-campus job monitor")
    parser.add_argument("--email-to", default=os.getenv("EMAIL_TO", ""),
                        help="Email to notify (or set EMAIL_TO in .env)")
    parser.add_argument("--interval-minutes", type=int, default=720,
                        help="Check interval in minutes (default: 720 = 12 hours)")
    parser.add_argument("--once", action="store_true", help="Run once and exit")
    parser.add_argument("--setup-auth", action="store_true",
                        help="Open browser to log in to ASU Workday")
    args = parser.parse_args()

    if args.setup_auth:
        setup_auth()
        return

    # Init AI clients
    clients: dict = {}

    gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
    if HAS_GEMINI and gemini_key:
        gemini_client = google_genai.Client(api_key=gemini_key)
        for candidate in ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-2.0-flash-lite"]:
            try:
                gemini_client.models.generate_content(model=candidate, contents="hi")
                clients["gemini"] = {"client": gemini_client, "model": candidate}
                print(f"[OK] Gemini ready  ({candidate})")
                break
            except Exception as e:
                print(f"[warn] Gemini {candidate}: {e}")

    openai_key = os.getenv("OPENAI_API_KEY", "").strip()
    if HAS_OPENAI and openai_key:
        clients["openai"] = OpenAI(api_key=openai_key)
        print(f"[OK] OpenAI ready  ({os.getenv('OPENAI_MODEL', 'gpt-4.1-mini')})")

    if not clients:
        print("[warn] No AI keys — resumes will not be generated")

    if not args.email_to:
        print("[warn] EMAIL_TO not set — jobs will be printed but not emailed")

    cycle = 1
    while True:
        try:
            run_cycle(email_to=args.email_to, clients=clients)
        except KeyboardInterrupt:
            print("\nStopped. Goodbye!")
            break
        except Exception as exc:
            print(f"\n[error] Cycle {cycle} failed: {exc}")

        if args.once:
            break

        wait_sec = max(60, args.interval_minutes * 60)
        next_ts  = datetime.fromtimestamp(time.time() + wait_sec).strftime("%H:%M:%S")
        print(f"  Next check at {next_ts}  (Ctrl+C to stop)\n")
        try:
            time.sleep(wait_sec)
        except KeyboardInterrupt:
            print("\nStopped. Goodbye!")
            break
        cycle += 1


if __name__ == "__main__":
    main()
