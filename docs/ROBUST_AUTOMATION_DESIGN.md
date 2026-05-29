# Robust Job Hunter Automation v2

## What This New Version Does

The new automation keeps the useful parts of the current system:

- LinkedIn, Handshake, and SerpApi job discovery
- AI scoring against your resume
- disqualifier filtering for citizenship, sponsorship, student-level, and start-date mismatches
- Discord alerts for strong matches
- tailored resume and cover-letter generation

It changes the orchestration around those pieces so the system behaves more like a reliable service instead of a one-shot script.

## New Files

- `job_hunter_robust.py` - new robust runner
- `job_hunter_robust_config.example.json` - config-driven thresholds, sources, keywords, retries, and notification behavior
- `ROBUST_AUTOMATION_DESIGN.md` - this design note

The original `job_hunter.py`, `workday_jobs.py`, and existing state files were left untouched.

## Current Search Rule

The robust runner is now configured to search internships and co-ops, then keep only jobs posted after `2026-05-15`.

In `job_hunter_robust_config.example.json`:

```json
"posted_after": "2026-05-15",
"posted_after_inclusive": false,
"posted_date_required": true
```

With this default, May 16, 2026 and later are accepted. Jobs with no detectable posted date are rejected so old postings do not slip through.

## Recommended Run Commands

Install/update dependencies:

```powershell
python -m pip install -r requirements.txt
python -m playwright install chromium
```

Health check:

```powershell
python job_hunter_robust.py --health-check
```

Test without sending Discord alerts or writing new DB state:

```powershell
python job_hunter_robust.py --once --dry-run
```

Run one real cycle:

```powershell
python job_hunter_robust.py --once
```

Run continuously using the configured interval:

```powershell
python job_hunter_robust.py
```

Refresh Handshake login:

```powershell
python job_hunter_robust.py --setup-auth
```

Retry jobs whose Discord alert failed:

```powershell
python job_hunter_robust.py --reset-failed-alerts --once
```

## Robustness Modifications

1. SQLite state store instead of only `seen_jobs.json`

The old script stores seen jobs in a flat JSON file. The new runner stores each job in `state/job_hunter_state.sqlite` with status, score, reason, trigger, attempts, errors, first seen, last seen, and notified time.

This makes it easier to recover from crashes, inspect history, and retry failed alerts.

2. Jobs are not treated as finished before notification succeeds

The current script marks a job as seen before the Discord alert and document generation are complete. If Discord fails, that job may never be retried.

The new script uses statuses like `discovered`, `scored`, `notify_failed`, `notified`, `rejected`, and `disqualified`. A job becomes final only after it is actually notified or intentionally rejected.

3. Source isolation

LinkedIn, Handshake, and SerpApi are called independently. If one source breaks, the cycle records the error and continues with the other sources.

4. Retry and exponential backoff

Source collection, details fetching, scoring, and Discord posting run through retry logic with jitter. Temporary network failures or rate-limit hiccups are less likely to kill the cycle.

5. Config-driven behavior

Thresholds, keywords, enabled sources, pay rules, disqualifiers, retry settings, and rate limits live in `job_hunter_robust_config.example.json`.

You can create your own `job_hunter_robust_config.json` and pass it with:

```powershell
python job_hunter_robust.py --config job_hunter_robust_config.json
```

6. Dry-run mode

`--dry-run` lets you test the full search/scoring flow without sending alerts, generating documents, or writing new DB state.

7. Health check mode

`--health-check` quickly verifies local dependencies, API key presence, Discord webhook presence, SerpApi key presence, and Handshake auth-state availability.

8. Run reports and logs

Each cycle writes:

- `reports/last_run_summary.json`
- `reports/last_run_summary.md`
- `logs/job_hunter_robust.log`

This gives you a quick answer to: what ran, what failed, what was scored, and what was sent.

9. Better deduplication

The new runner deduplicates by both URL-derived UID and normalized title/company identity. This catches the same job appearing through multiple sources or with changed tracking URLs.

10. Safer automation boundary

The new runner still assists with discovery, scoring, alerts, and document drafts. It does not auto-apply to jobs. Keeping application submission human-reviewed prevents accidental spam, incorrect submissions, and terms-of-service problems.

## Additional Changes I Recommend Next

- Move `RESUME_TEXT` out of Python into `resume_master.md` so you can update it without editing code.
- Replace the Gemini package mismatch in the current project: the code imports `google.genai`, but `requirements.txt` lists `google-generativeai`. Align it to the package you actually use.
- Add a small dashboard or CSV export for jobs in SQLite so you can review `notified`, `rejected`, and `notify_failed` jobs.
- Add source-specific rate limits, especially for LinkedIn and Handshake.
- Add unit tests for pay parsing, disqualifier detection, AI response parsing, and deduplication.
- Add a weekly cleanup/archive job for old rejected postings.
- Prefer official APIs, RSS feeds, email alerts, or exported searches where available; scraping should be a fallback because page structure changes often.
