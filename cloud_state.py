"""Restore and encrypt Job Hunter SQLite state between GitHub Actions runs."""

from __future__ import annotations

import argparse
import io
import os
import sqlite3
import tempfile
import zipfile
import zlib
from pathlib import Path

import requests
from cryptography.fernet import Fernet


ROOT = Path(__file__).resolve().parent
DB = ROOT / "state" / "job_hunter_state.sqlite"
SEED = ROOT / "cloud_state_seed.enc"
OUTPUT = ROOT / "state.enc"
ARTIFACT_NAME = "job-hunter-state"
API = "https://api.github.com"


def cipher() -> Fernet:
    key = os.environ.get("STATE_KEY", "").strip()
    if not key:
        raise RuntimeError("STATE_KEY secret is missing")
    return Fernet(key.encode("ascii"))


def api_get(path: str, *, download: bool = False) -> requests.Response:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if not token or not repo or "/" not in repo:
        raise RuntimeError("GitHub token or repository context is missing")
    response = requests.get(
        f"{API}/repos/{repo}/{path}",
        headers={"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}"},
        timeout=60 if download else 20,
    )
    response.raise_for_status()
    return response


def latest_artifact() -> dict | None:
    artifacts = api_get(f"actions/artifacts?name={ARTIFACT_NAME}&per_page=100").json()["artifacts"]
    eligible = [item for item in artifacts if not item.get("expired")
                and item.get("workflow_run", {}).get("head_branch") == "main"]
    return max(eligible, key=lambda item: item["created_at"], default=None)


def latest_previous_run() -> dict | None:
    current = int(os.environ.get("GITHUB_RUN_ID", "0"))
    runs = api_get("actions/workflows/job-hunter.yml/runs?per_page=30").json()["workflow_runs"]
    previous = [run for run in runs if int(run["id"]) != current and run["status"] == "completed"]
    return max(previous, key=lambda run: int(run["id"]), default=None)


def check_database(path: Path) -> None:
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        result = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise RuntimeError("SQLite state failed integrity check")
        connection.execute("SELECT count(*) FROM jobs").fetchone()
    finally:
        connection.close()


def restore() -> None:
    artifact = latest_artifact()
    previous = latest_previous_run()
    if artifact:
        artifact_run = int(artifact["workflow_run"]["id"])
        if previous and int(previous["id"]) > artifact_run:
            raise RuntimeError("Latest workflow run has no saved state; stopping to avoid duplicate alerts")
        archive = api_get(f"actions/artifacts/{artifact['id']}/zip", download=True).content
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            encrypted = bundle.read("state.enc")
        print(f"Restoring encrypted state from artifact {artifact['id']}")
    else:
        if previous:
            raise RuntimeError("No state artifact exists after a previous run; stopping to avoid duplicate alerts")
        if not SEED.exists():
            raise RuntimeError("No encrypted state seed is present")
        encrypted = SEED.read_bytes()
        print("Restoring encrypted initial state seed")

    plaintext = zlib.decompress(cipher().decrypt(encrypted))
    DB.parent.mkdir(exist_ok=True)
    temporary = DB.with_suffix(".restore")
    temporary.write_bytes(plaintext)
    check_database(temporary)
    temporary.replace(DB)


def save() -> None:
    check_database(DB)
    with tempfile.TemporaryDirectory() as directory:
        snapshot = Path(directory) / "snapshot.sqlite"
        original = sqlite3.connect(DB)
        copy = sqlite3.connect(snapshot)
        try:
            original.backup(copy)
        finally:
            copy.close()
            original.close()
        check_database(snapshot)
        OUTPUT.write_bytes(cipher().encrypt(zlib.compress(snapshot.read_bytes(), level=9)))
    print("Encrypted SQLite state for artifact upload")


def validate_env() -> None:
    cipher()
    required = ("DISCORD_WEBHOOK", "SERPAPI_KEYS")
    missing = [name for name in required if not os.environ.get(name, "").strip()]
    if not os.environ.get("GEMINI_API_KEY", "").strip() and not os.environ.get("OPENAI_API_KEY", "").strip():
        missing.append("GEMINI_API_KEY or OPENAI_API_KEY")
    if missing:
        raise RuntimeError("Missing required GitHub Actions secrets: " + ", ".join(missing))
    print("Required cloud secrets are present")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("restore", "save", "validate-env"))
    command = parser.parse_args().command
    {"restore": restore, "save": save, "validate-env": validate_env}[command]()
