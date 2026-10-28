"""
Job Hunter Robust Automation v2
===============================

This script keeps your existing source collectors and AI/document generators,
but wraps them in a more resilient pipeline:

- SQLite state instead of a flat seen_jobs.json file
- explicit job statuses, so failed alerts can be retried
- source isolation, retry/backoff, and run summaries
- config-driven thresholds, keywords, sources, and disqualifiers
- health-check and dry-run modes

It does not auto-apply to jobs. It finds, scores, prepares documents, and alerts.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import random
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple


PROJECT_DIR = Path(__file__).resolve().parent
os.chdir(PROJECT_DIR)
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import job_hunter as legacy  # noqa: E402


STATE_DIR = PROJECT_DIR / "state"
REPORT_DIR = PROJECT_DIR / "reports"
LOG_DIR = PROJECT_DIR / "logs"
DB_PATH = STATE_DIR / "job_hunter_state.sqlite"
DEFAULT_CONFIG_PATH = PROJECT_DIR / "job_hunter_robust_config.example.json"

FINAL_STATUSES = {"notified", "rejected", "disqualified"}
RETRYABLE_STATUSES = {"discovered", "scored", "notify_failed", "doc_failed", "processing_failed"}


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def safe_text(value: Any, limit: int = 8000) -> str:
    if value is None:
        return ""
    return str(value)[:limit]


def setup_logging(verbose: bool = False) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    level = logging.DEBUG if verbose else logging.INFO
    handlers = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_DIR / "job_hunter_robust.log", encoding="utf-8"),
    ]
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
        force=True,
    )


def load_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(path: Optional[Path]) -> Dict[str, Any]:
    if not DEFAULT_CONFIG_PATH.exists():
        raise FileNotFoundError(f"Missing default config: {DEFAULT_CONFIG_PATH}")
    config = load_json(DEFAULT_CONFIG_PATH)
    if path and path.exists():
        config = deep_merge(config, load_json(path))
    elif path and not path.exists():
        logging.warning("Config file not found, using defaults: %s", path)
    return config


@dataclass
class RetryPolicy:
    attempts: int = 3
    base_delay_seconds: float = 4
    max_delay_seconds: float = 45

    @classmethod
    def from_config(cls, config: Dict[str, Any]) -> "RetryPolicy":
        raw = config.get("retry", {})
        return cls(
            attempts=max(1, int(raw.get("attempts", 3))),
            base_delay_seconds=float(raw.get("base_delay_seconds", 4)),
            max_delay_seconds=float(raw.get("max_delay_seconds", 45)),
        )

    def call(self, label: str, fn: Callable[[], Any]) -> Any:
        last_error: Optional[BaseException] = None
        for attempt in range(1, self.attempts + 1):
            try:
                return fn()
            except Exception as exc:  # keep the cycle alive; record the source/job failure
                last_error = exc
                if attempt >= self.attempts:
                    break
                delay = min(
                    self.max_delay_seconds,
                    self.base_delay_seconds * (2 ** (attempt - 1)) + random.uniform(0, 2),
                )
                logging.warning("%s failed on attempt %s/%s: %s; retrying in %.1fs",
                                label, attempt, self.attempts, exc, delay)
                time.sleep(delay)
        raise RuntimeError(f"{label} failed after {self.attempts} attempts: {last_error}") from last_error


class StateStore:
    def __init__(self, path: Path) -> None:
        STATE_DIR.mkdir(exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def close(self) -> None:
        self.conn.close()

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                uid TEXT PRIMARY KEY,
                identity_key TEXT,
                title TEXT NOT NULL,
                company TEXT,
                source TEXT,
                url TEXT,
                location TEXT,
                pay TEXT,
                posted_date TEXT,
                description TEXT,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                status TEXT NOT NULL,
                score INTEGER,
                reason TEXT,
                trigger TEXT,
                notified_at TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                payload_json TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_jobs_identity ON jobs(identity_key);
            CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);

            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                total_found INTEGER DEFAULT 0,
                processable INTEGER DEFAULT 0,
                notified INTEGER DEFAULT 0,
                rejected INTEGER DEFAULT 0,
                errors INTEGER DEFAULT 0,
                summary_json TEXT
            );
            """
        )
        self._ensure_column("jobs", "posted_date", "TEXT")
        self._ensure_column("jobs", "documents_status", "TEXT NOT NULL DEFAULT 'unknown'")
        self._ensure_column("jobs", "documents_error", "TEXT")
        self._ensure_column("jobs", "documents_generated_at", "TEXT")
        self.conn.commit()

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        cur = self.conn.execute(f"PRAGMA table_info({table})")
        columns = {str(row["name"]) for row in cur.fetchall()}
        if column not in columns:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def start_run(self) -> int:
        cur = self.conn.execute(
            "INSERT INTO runs(started_at, status) VALUES(?, ?)",
            (now_iso(), "running"),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, status: str, summary: Dict[str, Any]) -> None:
        self.conn.execute(
            """
            UPDATE runs
               SET finished_at = ?, status = ?, total_found = ?, processable = ?,
                   notified = ?, rejected = ?, errors = ?, summary_json = ?
             WHERE id = ?
            """,
            (
                now_iso(),
                status,
                int(summary.get("total_found", 0)),
                int(summary.get("processable", 0)),
                int(summary.get("notified", 0)),
                int(summary.get("rejected", 0)),
                int(summary.get("errors", 0)),
                json.dumps(summary, indent=2),
                run_id,
            ),
        )
        self.conn.commit()

    def get_by_uid(self, uid: str) -> Optional[sqlite3.Row]:
        cur = self.conn.execute("SELECT * FROM jobs WHERE uid = ?", (uid,))
        return cur.fetchone()

    def get_by_identity(self, identity_key: str) -> Optional[sqlite3.Row]:
        cur = self.conn.execute(
            "SELECT * FROM jobs WHERE identity_key = ? ORDER BY first_seen LIMIT 1",
            (identity_key,),
        )
        return cur.fetchone()

    def upsert_discovered(self, job: legacy.Job) -> Tuple[str, sqlite3.Row, bool]:
        uid = job.uid()
        identity = identity_key(job)
        now = now_iso()
        existing = self.get_by_uid(uid) or self.get_by_identity(identity)
        payload = json.dumps(job_to_dict(job), ensure_ascii=False)

        if existing:
            canonical_uid = str(existing["uid"])
            self.conn.execute(
                """
                UPDATE jobs
                   SET last_seen = ?, title = ?, company = ?, source = ?, url = ?,
                       location = ?, pay = COALESCE(NULLIF(?, ''), pay),
                       posted_date = COALESCE(NULLIF(?, ''), posted_date),
                       description = COALESCE(NULLIF(?, ''), description),
                       payload_json = ?
                 WHERE uid = ?
                """,
                (
                    now,
                    job.title,
                    job.company,
                    job.source,
                    job.url,
                    job.location,
                    job.pay,
                    getattr(job, "posted_date", ""),
                    job.description,
                    payload,
                    canonical_uid,
                ),
            )
            self.conn.commit()
            return canonical_uid, self.get_by_uid(canonical_uid), False

        self.conn.execute(
            """
            INSERT INTO jobs(uid, identity_key, title, company, source, url, location,
                             pay, posted_date, description, first_seen, last_seen, status, payload_json)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                uid,
                identity,
                job.title,
                job.company,
                job.source,
                job.url,
                job.location,
                job.pay,
                getattr(job, "posted_date", ""),
                job.description,
                now,
                now,
                "discovered",
                payload,
            ),
        )
        self.conn.commit()
        return uid, self.get_by_uid(uid), True

    def mark_scored(self, uid: str, score: int, reason: str, status: str = "scored",
                    trigger: str = "") -> None:
        self.conn.execute(
            """
            UPDATE jobs
               SET status = ?, score = ?, reason = ?, trigger = ?, attempts = attempts + 1,
                   last_error = NULL
             WHERE uid = ?
            """,
            (status, int(score), safe_text(reason, 3000), trigger, uid),
        )
        self.conn.commit()

    def mark_notified(self, uid: str, score: int, reason: str, trigger: str) -> None:
        self.conn.execute(
            """
            UPDATE jobs
               SET status = 'notified', score = ?, reason = ?, trigger = ?,
                   notified_at = ?, attempts = attempts + 1, last_error = NULL
             WHERE uid = ?
            """,
            (int(score), safe_text(reason, 3000), trigger, now_iso(), uid),
        )
        self.conn.commit()

    def mark_error(self, uid: str, status: str, message: str) -> None:
        self.conn.execute(
            """
            UPDATE jobs
               SET status = ?, attempts = attempts + 1, last_error = ?
             WHERE uid = ?
            """,
            (status, safe_text(message, 3000), uid),
        )
        self.conn.commit()

    def mark_documents(self, uid: str, success: bool, error: str = "") -> None:
        self.conn.execute(
            """UPDATE jobs SET documents_status = ?, documents_error = ?,
                      documents_generated_at = ? WHERE uid = ?""",
            ("generated" if success else "failed", None if success else safe_text(error, 3000),
             now_iso() if success else None, uid),
        )
        self.conn.commit()

    def failed_documents(self, limit: int = 20) -> List[sqlite3.Row]:
        return self.conn.execute(
            """SELECT * FROM jobs WHERE status = 'notified' AND documents_status = 'failed'
               ORDER BY notified_at DESC LIMIT ?""", (limit,)
        ).fetchall()

    def reset_failed_alerts(self) -> int:
        cur = self.conn.execute(
            "UPDATE jobs SET status = 'scored', last_error = NULL WHERE status = 'notify_failed'"
        )
        self.conn.commit()
        return int(cur.rowcount)


def job_to_dict(job: legacy.Job) -> Dict[str, Any]:
    return {
        "title": job.title,
        "company": job.company,
        "location": job.location,
        "pay": job.pay,
        "posted_date": getattr(job, "posted_date", ""),
        "url": job.url,
        "description": job.description,
        "source": job.source,
    }


def normalize_piece(value: str) -> str:
    value = re.sub(r"\s+", " ", (value or "").lower()).strip()
    return re.sub(r"[^a-z0-9+.# /-]", "", value)


def config_terms(config: Dict[str, Any], key: str) -> List[str]:
    values = config.get(key, [])
    if not isinstance(values, list):
        return []
    return [str(value).strip() for value in values if str(value).strip()]


def text_has_any_term(text: str, terms: Iterable[str]) -> bool:
    normalized = normalize_piece(text)
    return any(normalize_piece(term) in normalized for term in terms)


def text_has_company_term(text: str, term: str) -> bool:
    normalized = normalize_piece(text)
    normalized_term = normalize_piece(term)
    if not normalized_term:
        return False
    compact = re.sub(r"[^a-z0-9]", "", normalized_term)
    if len(compact) <= 3:
        return re.search(rf"(?<![a-z0-9]){re.escape(normalized_term)}(?![a-z0-9])", normalized) is not None
    return normalized_term in normalized


def job_text(job: legacy.Job, fields: Iterable[str]) -> str:
    return " ".join(str(getattr(job, field, "") or "") for field in fields)


def is_watchlist_company(job: legacy.Job, config: Dict[str, Any]) -> bool:
    terms = config_terms(config, "company_watchlist")
    if not terms:
        return False
    text = job_text(job, ("company", "title"))
    return any(text_has_company_term(text, term) for term in terms)


def is_student_role(job: legacy.Job, config: Dict[str, Any]) -> bool:
    terms = config_terms(config, "student_role_terms")
    if not terms:
        return True
    return text_has_any_term(job_text(job, ("title",)), terms)


def is_watchlist_relevant_role(job: legacy.Job, config: Dict[str, Any]) -> bool:
    terms = config_terms(config, "watchlist_title_terms")
    if not terms:
        return True
    return text_has_any_term(job_text(job, ("title", "description")), terms)


def is_watchlist_candidate(job: legacy.Job, config: Dict[str, Any]) -> bool:
    if not is_watchlist_company(job, config):
        return False
    if bool(config.get("watchlist_require_student_role", True)) and not is_student_role(job, config):
        return False
    return is_watchlist_relevant_role(job, config)


def identity_key(job: legacy.Job) -> str:
    title = normalize_piece(job.title)
    company = normalize_piece(job.company)
    if title and company:
        return f"{title}|{company}"
    return job.uid()


def dedupe_jobs(jobs: Iterable[legacy.Job]) -> List[legacy.Job]:
    seen: set[str] = set()
    result: List[legacy.Job] = []
    for job in jobs:
        if not job or not job.title:
            continue
        keys = {job.uid(), identity_key(job)}
        if seen.intersection(keys):
            continue
        seen.update(keys)
        result.append(job)
    return result


def random_sleep(config: Dict[str, Any], key: str) -> None:
    limits = config.get("rate_limits", {}).get(key, [0, 0])
    if not isinstance(limits, list) or len(limits) != 2:
        return
    lo, hi = float(limits[0]), float(limits[1])
    if hi > 0:
        time.sleep(random.uniform(lo, hi))


def source_enabled(config: Dict[str, Any], name: str) -> bool:
    return bool(config.get("sources", {}).get(name, True))


def configure_legacy_collectors(config: Dict[str, Any]) -> None:
    if "linkedin_pages" in config:
        setattr(legacy, "LINKEDIN_PAGES", max(1, int(config.get("linkedin_pages", 3))))
    if "linkedin_results_per_page" in config:
        setattr(
            legacy,
            "LINKEDIN_RESULTS_PER_PAGE",
            max(1, int(config.get("linkedin_results_per_page", 10))),
        )
    if "linkedin_time_filter" in config:
        setattr(legacy, "LINKEDIN_TIME_FILTER", str(config.get("linkedin_time_filter", "")).strip())


def safe_source_call(label: str, retry: RetryPolicy, fn: Callable[[], List[legacy.Job]],
                     errors: List[str]) -> List[legacy.Job]:
    try:
        jobs = retry.call(label, fn)
        logging.info("%s returned %s jobs", label, len(jobs))
        return jobs
    except Exception as exc:
        message = f"{label}: {exc}"
        logging.error("Source failed: %s", message)
        errors.append(message)
        return []


def collect_jobs(config: Dict[str, Any], retry: RetryPolicy) -> Tuple[List[legacy.Job], List[str]]:
    configure_legacy_collectors(config)
    all_jobs: List[legacy.Job] = []
    errors: List[str] = []

    for keyword in config.get("keywords", []):
        logging.info("Searching keyword: %s", keyword)

        if source_enabled(config, "linkedin"):
            all_jobs.extend(safe_source_call(
                f"linkedin[{keyword}]",
                retry,
                lambda kw=keyword: legacy.search_linkedin(kw),
                errors,
            ))
            random_sleep(config, "between_linkedin_searches_seconds")

        if source_enabled(config, "handshake"):
            all_jobs.extend(safe_source_call(
                f"handshake[{keyword}]",
                retry,
                lambda kw=keyword: legacy.search_handshake(kw),
                errors,
            ))
            random_sleep(config, "between_handshake_searches_seconds")

        random_sleep(config, "between_keywords_seconds")

    if source_enabled(config, "serpapi"):
        for query in config.get("serpapi_queries", []):
            logging.info("Searching SerpApi query: %s", query)
            all_jobs.extend(safe_source_call(
                f"serpapi[{query[:40]}]",
                retry,
                lambda q=query: legacy.search_serpapi(q),
                errors,
            ))
            random_sleep(config, "between_serpapi_queries_seconds")

    deduped = dedupe_jobs(all_jobs)
    total_deduped = len(deduped)
    max_candidates = int(config.get("max_candidates_per_cycle", 300))
    if max_candidates > 0:
        deduped = deduped[:max_candidates]
    if len(deduped) != total_deduped:
        logging.info("Candidate cap applied: %s deduped jobs -> %s inspected", total_deduped, len(deduped))
    return deduped, errors


def fetch_details_if_needed(job: legacy.Job, retry: RetryPolicy) -> legacy.Job:
    if job.description:
        return job
    if job.source == "linkedin":
        return retry.call(f"details[{job.uid()}]", lambda: legacy.get_linkedin_details(job))
    if job.source == "handshake":
        return retry.call(f"details[{job.uid()}]", lambda: legacy.get_handshake_details(job))
    return job


TERM_ORDER = {"winter": 0, "spring": 1, "summer": 2, "fall": 3}


def parse_iso_date(value: str) -> Optional[date]:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def parse_posted_date(value: str, today: Optional[date] = None) -> Optional[date]:
    """Parse source-provided posted dates, including ISO and relative strings."""
    raw = (value or "").strip()
    if not raw:
        return None

    today = today or datetime.now().date()
    lowered = raw.lower()

    parsed = parse_iso_date(raw)
    if parsed:
        return parsed

    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%b %d, %Y", "%B %d, %Y"):
        try:
            dt = datetime.strptime(raw, fmt)
            candidate = dt.date()
            if candidate > today + timedelta(days=30):
                candidate = date(candidate.year - 1, candidate.month, candidate.day)
            return candidate
        except ValueError:
            continue

    for fmt in ("%b %d %Y", "%B %d %Y"):
        try:
            dt = datetime.strptime(f"{raw} {today.year}", fmt)
            candidate = dt.date()
            if candidate > today + timedelta(days=30):
                candidate = date(candidate.year - 1, candidate.month, candidate.day)
            return candidate
        except ValueError:
            continue

    if any(token in lowered for token in ("today", "just now", "moments ago")):
        return today
    if "yesterday" in lowered:
        return today - timedelta(days=1)

    rel = re.search(r"(\d+)\s*\+?\s*(minute|hour|day|week|month|year)s?\s+ago", lowered)
    if rel:
        amount = int(rel.group(1))
        unit = rel.group(2)
        if unit in ("minute", "hour"):
            return today
        if unit == "day":
            return today - timedelta(days=amount)
        if unit == "week":
            return today - timedelta(days=amount * 7)
        if unit == "month":
            return today - timedelta(days=amount * 30)
        if unit == "year":
            return today - timedelta(days=amount * 365)

    return None


def posted_date_filter_reason(job: legacy.Job, config: Dict[str, Any]) -> str:
    cutoff_raw = str(config.get("posted_after", "")).strip()
    if not cutoff_raw:
        return ""

    cutoff = parse_iso_date(cutoff_raw)
    if not cutoff:
        logging.warning("Invalid posted_after config value: %s", cutoff_raw)
        return ""

    posted_raw = str(getattr(job, "posted_date", "") or "").strip()
    posted = parse_posted_date(posted_raw)
    required = bool(config.get("posted_date_required", True))
    inclusive = bool(config.get("posted_after_inclusive", False))
    relation = "on or after" if inclusive else "after"

    if is_watchlist_candidate(job, config):
        watch_cutoff_raw = str(config.get("watchlist_posted_after", "")).strip()
        watch_cutoff = parse_iso_date(watch_cutoff_raw) if watch_cutoff_raw else None
        if watch_cutoff_raw and not watch_cutoff:
            logging.warning("Invalid watchlist_posted_after config value: %s", watch_cutoff_raw)
        if not posted and bool(config.get("watchlist_allow_missing_posted_date", False)):
            return ""
        if posted and watch_cutoff:
            if inclusive:
                if posted < watch_cutoff:
                    return f"Watchlist target posted {posted.isoformat()}, before watchlist cutoff {watch_cutoff.isoformat()}"
            elif posted <= watch_cutoff:
                return f"Watchlist target posted {posted.isoformat()}, not after watchlist cutoff {watch_cutoff.isoformat()}"
            return ""

    if not posted:
        if required:
            return f"Posted date unavailable; required to be {relation} {cutoff.isoformat()}"
        return ""

    if inclusive:
        if posted < cutoff:
            return f"Posted {posted.isoformat()}, before required cutoff {cutoff.isoformat()}"
    elif posted <= cutoff:
        return f"Posted {posted.isoformat()}, not after required cutoff {cutoff.isoformat()}"
    return ""


def parse_term(value: str) -> Optional[Tuple[int, int]]:
    match = re.search(r"\b(winter|spring|summer|fall)\s+(20\d{2})\b", value.lower())
    if not match:
        return None
    season, year = match.group(1), int(match.group(2))
    return year, TERM_ORDER[season]


def disqualification_reason(job: legacy.Job, config: Dict[str, Any]) -> str:
    text = normalize_piece(f"{job.title} {job.company} {job.location} {job.pay} {job.description}")
    for term in config.get("disqualifiers", []):
        normalized = normalize_piece(term)
        if normalized not in text:
            continue
        # Some listings explicitly say a credential is not required. Do not
        # reject those simply because the credential name appears in the text.
        if normalized in {"us citizenship", "u.s. citizenship", "green card",
                          "security clearance"}:
            escaped = re.escape(normalized)
            negated = (rf"\b(?:no|without)\s+{escaped}\s+(?:is\s+)?required\b",
                       rf"\b{escaped}\s+(?:is\s+)?not\s+required\b",
                       rf"\b(?:does\s+not|do\s+not)\s+require\s+{escaped}\b")
            if any(re.search(pattern, text) for pattern in negated):
                continue
        return f"Contains disqualifier: {term}"

    earliest = parse_term(str(config.get("earliest_allowed_start", "")))
    if earliest:
        for match in re.finditer(r"\b(winter|spring|summer|fall)\s+(20\d{2})\b", text):
            term = parse_term(match.group(0))
            if term and term < earliest:
                return f"Starts before allowed term: {match.group(0)}"
    return ""


def should_notify(job: legacy.Job, score: int, pay_text: str, config: Dict[str, Any]) -> Tuple[bool, str]:
    if is_watchlist_candidate(job, config):
        watch_score = int(config.get("watchlist_notify_min_score", 62))
        if score >= watch_score:
            company = str(getattr(job, "company", "") or "target company").strip()
            return True, f">={watch_score}% watchlist target ({company})"

    match_threshold = int(config.get("match_threshold", 72))
    if score >= match_threshold:
        return True, f">={match_threshold}% match"

    hourly = legacy.extract_hourly(pay_text)
    pay_score = int(config.get("pay_boost_min_score", 62))
    high_pay = float(config.get("high_pay_hourly", 25))
    if score >= pay_score and hourly and hourly >= high_pay:
        return True, f">={pay_score}% match + high pay (${hourly:.0f}/hr)"
    return False, ""


def build_ai_clients() -> Dict[str, Any]:
    clients: Dict[str, Any] = {}

    gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
    if getattr(legacy, "HAS_GEMINI", False) and gemini_key:
        try:
            client = legacy.google_genai.Client(api_key=gemini_key)
            candidates = [
                m.strip()
                for m in os.getenv(
                    "GEMINI_MODELS",
                    "gemini-2.5-flash,gemini-2.0-flash,gemini-2.0-flash-lite,gemini-1.5-flash",
                ).split(",")
                if m.strip()
            ]
            for model in candidates:
                try:
                    client.models.generate_content(model=model, contents="health check")
                    clients["gemini"] = {"client": client, "model": model}
                    logging.info("Gemini ready: %s", model)
                    break
                except Exception as exc:
                    logging.warning("Gemini probe failed for %s: %s", model, exc)
        except Exception as exc:
            logging.warning("Gemini initialization failed: %s", exc)
    else:
        logging.info("Gemini not configured or library unavailable")

    openai_key = os.getenv("OPENAI_API_KEY", "").strip()
    if getattr(legacy, "HAS_OPENAI", False) and openai_key:
        try:
            clients["openai"] = legacy.OpenAI(api_key=openai_key)
            logging.info("OpenAI backup ready: %s", os.getenv("OPENAI_MODEL", "gpt-4.1-mini"))
        except Exception as exc:
            logging.warning("OpenAI initialization failed: %s", exc)

    if not clients:
        logging.warning("No AI clients are available; keyword scoring will be used")
    return clients


def post_discord(payload: Dict[str, Any], webhook_url: str) -> None:
    if not webhook_url:
        raise RuntimeError("DISCORD_WEBHOOK is not configured")
    if not getattr(legacy, "HAS_REQUESTS", False):
        raise RuntimeError("requests is not installed")
    response = legacy.requests.post(webhook_url, json=payload, timeout=15)
    response.raise_for_status()


def generate_documents(job: legacy.Job, score: int, clients: Dict[str, Any],
                       config: Dict[str, Any]) -> None:
    notifications = config.get("notifications", {})
    if notifications.get("generate_tailored_resume", True):
        if not legacy.generate_tailored_resume(job, score, clients):
            raise RuntimeError("Tailored resume generation did not produce a file")
    if notifications.get("generate_cover_letter", True):
        if not legacy.generate_cover_letter_docx(job, score, clients):
            raise RuntimeError("Cover letter generation did not produce a file")


def write_report(summary: Dict[str, Any]) -> None:
    REPORT_DIR.mkdir(exist_ok=True)
    json_path = REPORT_DIR / "last_run_summary.json"
    md_path = REPORT_DIR / "last_run_summary.md"

    tmp_json = json_path.with_suffix(".json.tmp")
    tmp_json.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    tmp_json.replace(json_path)

    lines = [
        "# Job Hunter Robust Run Summary",
        "",
        f"- Started: {summary.get('started_at', '')}",
        f"- Finished: {summary.get('finished_at', '')}",
        f"- Status: {summary.get('status', '')}",
        f"- Total found: {summary.get('total_found', 0)}",
        f"- Skipped known/final: {summary.get('skipped_known', 0)}",
        f"- Skipped after process limit: {summary.get('skipped_after_process_limit', 0)}",
        f"- Processable: {summary.get('processable', 0)}",
        f"- Notified: {summary.get('notified', 0)}",
        f"- Would notify: {summary.get('would_notify', 0)}",
        f"- Documents retried successfully: {summary.get('documents_retried', 0)}",
        f"- Date filtered: {summary.get('date_filtered', 0)}",
        f"- Rejected/disqualified: {summary.get('rejected', 0)}",
        f"- Errors: {summary.get('errors', 0)}",
    ]
    if summary.get("source_errors"):
        lines.extend(["", "## Source Errors"])
        lines.extend(f"- {err}" for err in summary["source_errors"])
    if summary.get("processed_jobs"):
        lines.extend(["", "## Processed Jobs"])
        for item in summary["processed_jobs"]:
            lines.append(
                f"- {item.get('status')} | {item.get('score')}% | "
                f"{item.get('title')} @ {item.get('company') or 'N/A'}"
                f" | posted: {item.get('posted_date') or 'unknown'}"
            )

    tmp_md = md_path.with_suffix(".md.tmp")
    tmp_md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp_md.replace(md_path)


def export_jobs_csv(state: StateStore) -> Path:
    """Export reviewable job history without changing its processing state."""
    REPORT_DIR.mkdir(exist_ok=True)
    output = REPORT_DIR / "jobs.csv"
    temporary = REPORT_DIR / "jobs.csv.tmp"
    fields = ("title", "company", "source", "url", "posted_date", "status",
              "score", "reason", "trigger", "first_seen", "last_seen",
              "notified_at", "last_error")
    with temporary.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.writer(stream)
        writer.writerow(fields)
        rows = state.conn.execute(
            "SELECT " + ", ".join(fields) + " FROM jobs ORDER BY last_seen DESC"
        )
        for row in rows:
            writer.writerow([row[field] for field in fields])
    temporary.replace(output)
    return output


def send_cycle_digest(summary: Dict[str, Any], webhook_url: str) -> None:
    digest_day = str(summary["finished_at"])[:10]
    marker = REPORT_DIR / "digest_state.json"
    previous = load_json(marker) if marker.exists() else {}
    if previous.get("sent_for_day") == digest_day:
        return
    source_errors = summary.get("source_errors", [])
    lines = [
        "Job Hunter cycle summary",
        f"Status: {summary['status']}",
        f"Found: {summary['total_found']} | Processed: {summary['processable']} | "
        f"Alerted: {summary['notified']} | Rejected: {summary['rejected']}",
        f"Errors: {summary['errors']} | Source errors: {len(source_errors)}",
        f"Document retries completed: {summary['documents_retried']}",
    ]
    if source_errors:
        lines.append("Sources needing attention: " + "; ".join(source_errors)[:900])
    post_discord({"content": "\n".join(lines)}, webhook_url)
    temporary = marker.with_suffix(".tmp")
    temporary.write_text(json.dumps({"sent_for_day": digest_day}), encoding="utf-8")
    temporary.replace(marker)


def row_is_processable(row: sqlite3.Row) -> bool:
    status = str(row["status"])
    return status in RETRYABLE_STATUSES


def run_cycle(config: Dict[str, Any], state: Optional[StateStore],
              clients: Dict[str, Any], dry_run: bool = False) -> Dict[str, Any]:
    retry = RetryPolicy.from_config(config)
    started = now_iso()
    run_id = state.start_run() if state and not dry_run else None
    summary: Dict[str, Any] = {
        "started_at": started,
        "finished_at": "",
        "status": "running",
        "dry_run": dry_run,
        "total_found": 0,
        "skipped_known": 0,
        "skipped_after_process_limit": 0,
        "processable": 0,
        "notified": 0,
        "would_notify": 0,
        "documents_retried": 0,
        "date_filtered": 0,
        "rejected": 0,
        "errors": 0,
        "source_errors": [],
        "processed_jobs": [],
    }

    try:
        if state and not dry_run:
            for row in state.failed_documents():
                try:
                    payload = json.loads(row["payload_json"] or "{}")
                    job_for_docs = legacy.Job(**payload)
                    generate_documents(job_for_docs, int(row["score"] or 0), clients, config)
                    state.mark_documents(str(row["uid"]), True)
                    summary["documents_retried"] += 1
                except Exception as exc:
                    state.mark_documents(str(row["uid"]), False, str(exc))
                    summary["errors"] += 1
                    logging.error("Document retry failed for %s: %s", row["uid"], exc)

        jobs, source_errors = collect_jobs(config, retry)
        summary["source_errors"] = source_errors
        summary["errors"] += len(source_errors)
        summary["total_found"] = len(jobs)
        logging.info("%s unique jobs found", len(jobs))
        process_limit = int(config.get("max_jobs_per_cycle", 80))

        webhook = os.getenv("DISCORD_WEBHOOK", "").strip()
        discord_enabled = bool(config.get("notifications", {}).get("discord", True))

        for job in jobs:
            if process_limit > 0 and summary["processable"] >= process_limit:
                summary["skipped_after_process_limit"] += 1
                logging.info("Stopping after %s processable jobs; remaining candidates will be checked next run", process_limit)
                break

            canonical_uid = job.uid()
            is_new = True
            row = None
            if state and not dry_run:
                canonical_uid, row, is_new = state.upsert_discovered(job)
                if row and not row_is_processable(row):
                    summary["skipped_known"] += 1
                    logging.info("Skipping known final job: %s @ %s (%s)",
                                 job.title, job.company, row["status"])
                    continue

            summary["processable"] += 1
            logging.info("Processing: %s @ %s [%s]", job.title, job.company or "N/A", job.source)

            job_item = {
                "uid": canonical_uid,
                "title": job.title,
                "company": job.company,
                "source": job.source,
                "posted_date": getattr(job, "posted_date", ""),
                "status": "processing",
                "score": None,
                "trigger": "",
            }

            try:
                job = fetch_details_if_needed(job, retry)
                job_item["posted_date"] = getattr(job, "posted_date", "")
                date_reason = posted_date_filter_reason(job, config)
                if date_reason:
                    job_item.update({
                        "status": "date_filtered",
                        "score": 0,
                        "posted_date": getattr(job, "posted_date", ""),
                    })
                    summary["rejected"] += 1
                    summary["date_filtered"] += 1
                    if state and not dry_run:
                        state.mark_scored(canonical_uid, 0, date_reason, "rejected")
                    logging.info("Rejected by posted-date filter: %s", date_reason)
                    summary["processed_jobs"].append(job_item)
                    random_sleep(config, "between_jobs_seconds")
                    continue

                dq_reason = disqualification_reason(job, config)
                if dq_reason:
                    score, reason = 0, dq_reason
                    job_item.update({"status": "disqualified", "score": score})
                    summary["rejected"] += 1
                    if state and not dry_run:
                        state.mark_scored(canonical_uid, score, reason, "disqualified")
                    logging.info("Disqualified: %s", dq_reason)
                    summary["processed_jobs"].append(job_item)
                    random_sleep(config, "between_jobs_seconds")
                    continue

                score, reason = retry.call(
                    f"score[{canonical_uid}]",
                    lambda j=job: legacy.ai_score(j, clients),
                )
                send_flag, trigger = should_notify(job, score, job.pay, config)
                job_item.update({"score": score, "trigger": trigger})

                if not send_flag:
                    job_item["status"] = "rejected"
                    summary["rejected"] += 1
                    if state and not dry_run:
                        state.mark_scored(canonical_uid, score, reason, "rejected", trigger)
                    logging.info("Rejected below threshold: %s%%", score)
                    summary["processed_jobs"].append(job_item)
                    random_sleep(config, "between_jobs_seconds")
                    continue

                if dry_run:
                    job_item["status"] = "would_notify"
                    summary["would_notify"] += 1
                    logging.info("Dry run: would notify for %s%% match (%s)", score, trigger)
                else:
                    if not discord_enabled:
                        raise RuntimeError("Discord notifications are disabled in config")
                    payload = legacy.format_discord_message(job, score, reason, trigger)
                    retry.call(f"discord[{canonical_uid}]", lambda: post_discord(payload, webhook))
                    if state:
                        state.mark_notified(canonical_uid, score, reason, trigger)
                    job_item["status"] = "notified"
                    summary["notified"] += 1
                    logging.info("Alert sent for %s%% match: %s", score, job.title)
                    try:
                        generate_documents(job, score, clients, config)
                        if state:
                            state.mark_documents(canonical_uid, True)
                    except Exception as doc_exc:
                        if state:
                            state.mark_documents(canonical_uid, False, str(doc_exc))
                        summary["errors"] += 1
                        job_item["doc_error"] = str(doc_exc)
                        logging.error("Document generation failed after alert was sent: %s", doc_exc)

            except Exception as exc:
                summary["errors"] += 1
                job_item["status"] = "processing_failed"
                job_item["error"] = str(exc)
                logging.error("Job failed: %s @ %s: %s", job.title, job.company, exc)
                if state and not dry_run:
                    status = "notify_failed" if "DISCORD_WEBHOOK" in str(exc) or "discord" in str(exc).lower() else "processing_failed"
                    state.mark_error(canonical_uid, status, str(exc))

            summary["processed_jobs"].append(job_item)
            random_sleep(config, "between_jobs_seconds")

        summary["status"] = "completed_with_errors" if summary["errors"] else "completed"
    except Exception as exc:
        summary["status"] = "failed"
        summary["errors"] += 1
        summary["fatal_error"] = str(exc)
        logging.exception("Cycle failed")
    finally:
        summary["finished_at"] = now_iso()
        write_report(summary)
        if state and run_id and not dry_run:
            state.finish_run(run_id, summary["status"], summary)

    return summary


def health_check(config: Dict[str, Any]) -> int:
    checks = {
        "requests": getattr(legacy, "HAS_REQUESTS", False),
        "playwright": getattr(legacy, "HAS_PLAYWRIGHT", False),
        "gemini_library": getattr(legacy, "HAS_GEMINI", False),
        "openai_library": getattr(legacy, "HAS_OPENAI", False),
        "docx": getattr(legacy, "HAS_DOCX", False),
        "gemini_key": bool(os.getenv("GEMINI_API_KEY", "").strip()),
        "openai_key": bool(os.getenv("OPENAI_API_KEY", "").strip()),
        "discord_webhook": bool(os.getenv("DISCORD_WEBHOOK", "").strip()),
        "serpapi_key": bool(os.getenv("SERPAPI_KEYS", os.getenv("SERPAPI_KEY", "")).strip()),
        "handshake_auth_state": (PROJECT_DIR / legacy.AUTH_STATE_FILE).exists(),
    }

    logging.info("Health check")
    for name, ok in checks.items():
        logging.info("  %-22s %s", name, "OK" if ok else "MISSING")

    enabled_sources = [name for name, enabled in config.get("sources", {}).items() if enabled]
    logging.info("Enabled sources: %s", ", ".join(enabled_sources) or "none")
    logging.info("Keywords: %s", len(config.get("keywords", [])))

    required = ["requests", "discord_webhook"]
    missing_required = [name for name in required if not checks.get(name)]
    if source_enabled(config, "handshake") and not checks["handshake_auth_state"]:
        logging.warning("Handshake is enabled but auth state is missing. Run --setup-auth.")
    if missing_required:
        logging.warning("Required checks missing: %s", ", ".join(missing_required))
        return 1
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Robust Job Hunter automation v2")
    parser.add_argument("--config", type=Path, default=None,
                        help="Optional JSON config. Defaults to job_hunter_robust_config.example.json")
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="Score and report without sending alerts, writing DB state, or generating docs")
    parser.add_argument("--health-check", action="store_true", help="Check local setup and exit")
    parser.add_argument("--export-csv", action="store_true", help="Export SQLite jobs to reports/jobs.csv and exit")
    parser.add_argument("--setup-auth", action="store_true",
                        help="Delegate to the existing Handshake auth setup flow")
    parser.add_argument("--reset-failed-alerts", action="store_true",
                        help="Move notify_failed jobs back to scored so they can be retried")
    parser.add_argument("--interval-minutes", type=int, default=None,
                        help="Override the configured schedule interval")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    setup_logging(args.verbose)
    config = load_config(args.config)
    if args.interval_minutes is not None:
        config["interval_minutes"] = args.interval_minutes

    if args.setup_auth:
        legacy.setup_handshake_auth()
        return

    if args.health_check:
        raise SystemExit(health_check(config))

    if args.export_csv:
        state = StateStore(DB_PATH)
        try:
            logging.info("Exported jobs to %s", export_jobs_csv(state))
        finally:
            state.close()
        return

    state = None if args.dry_run else StateStore(DB_PATH)
    try:
        if args.reset_failed_alerts and state:
            count = state.reset_failed_alerts()
            logging.info("Reset %s failed alerts", count)

        clients = build_ai_clients()
        cycle = 1
        while True:
            logging.info("Starting cycle %s", cycle)
            summary = run_cycle(config, state, clients, dry_run=args.dry_run)
            if state and not args.dry_run:
                try:
                    export_jobs_csv(state)
                except Exception as exc:
                    logging.error("CSV export failed: %s", exc)
                digest_hour = int(config.get("notifications", {}).get("daily_digest_after_hour_local", 20))
                if (config.get("notifications", {}).get("daily_digest", True)
                        and datetime.fromisoformat(summary["finished_at"]).hour >= digest_hour):
                    try:
                        send_cycle_digest(summary, os.getenv("DISCORD_WEBHOOK", "").strip())
                    except Exception as exc:
                        logging.error("Cycle digest failed: %s", exc)
            logging.info(
                "Cycle complete: status=%s found=%s processable=%s notified=%s would_notify=%s rejected=%s errors=%s",
                summary["status"],
                summary["total_found"],
                summary["processable"],
                summary["notified"],
                summary["would_notify"],
                summary["rejected"],
                summary["errors"],
            )

            if args.once:
                break

            wait_sec = max(60, int(config.get("interval_minutes", 1440)) * 60)
            next_run = datetime.fromtimestamp(time.time() + wait_sec).strftime("%Y-%m-%d %H:%M:%S")
            logging.info("Next run at %s", next_run)
            time.sleep(wait_sec)
            cycle += 1
    finally:
        if state:
            state.close()


if __name__ == "__main__":
    main()
