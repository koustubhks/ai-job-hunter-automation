# AI Job Hunter Automation

Robust multi-source job search automation for robotics, embedded systems, computer vision, and autonomous systems roles.

The system searches LinkedIn, Handshake, Workday-style pages, and SerpApi queries, filters jobs by date and eligibility constraints, scores relevance with Gemini/OpenAI fallback, and sends high-match jobs to Discord.

## Highlights

- Multi-source discovery: LinkedIn, Handshake, SerpApi, Workday-style pages.
- Robust state tracking with SQLite to avoid duplicate notifications.
- Date filtering for internships and co-ops posted after a configured date.
- AI ranking with primary and fallback model support.
- Discord notifications with match score, reason, and job metadata.
- Dry-run and health-check modes for safe testing.
- Configurable keywords, disqualifiers, thresholds, and rate limits.

## Tech Stack

Python, Playwright, Requests, BeautifulSoup, SQLite, Gemini API, OpenAI API, Discord webhooks, SerpApi.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium
Copy-Item .env.example .env
```

Fill in `.env` with your own keys.

## Run

```powershell
python job_hunter_robust.py --health-check
python job_hunter_robust.py --once --dry-run
python job_hunter_robust.py --once
python job_hunter_robust.py --export-csv
```

## Configuration

Edit `job_hunter_robust_config.example.json` and save your private runtime copy as `job_hunter_robust_config.json`.

The default posting cutoff is October 1, 2026, inclusive. Set `posted_after` and
`watchlist_posted_after` together when changing it. The E: drive scheduled tasks
run the local copy at 8 AM and 8 PM. The night cycle sends one Discord digest per
day; `monitor_job_hunter.py` can be scheduled separately to alert when no cycle
has completed within 30 hours. `--export-csv` writes `reports/jobs.csv` for review.

This repository currently has no GitHub Actions schedule. A cloud schedule needs
durable state and a safe way to refresh authenticated sources before enabling it;
otherwise repeated runs can duplicate alerts or lose access to Handshake.

Important settings:

- `posted_after`: earliest allowed posting date.
- `keywords`: internship and co-op search terms.
- `disqualifiers`: eligibility or timing filters.
- `match_threshold`: normal notification threshold.
- `pay_boost_min_score`: lower threshold for high-pay opportunities.

## Repository Hygiene

This repo intentionally excludes auth state, logs, SQLite state, generated resumes, and cover letters.

