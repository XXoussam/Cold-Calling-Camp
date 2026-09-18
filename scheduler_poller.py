"""Long-lived poller: checks this campaign's "Scheduled Runs" Airtable table
every POLL_INTERVAL_SECONDS, and runs dialer.run_campaign() for any row whose
scheduled time has arrived.

This is the "cron from Airtable" piece — schedule a run by adding a row
directly in the Airtable UI (Run At, Lead Limit, optionally Region), no need
to touch this machine or the deployed container at all. Runs as a second
process alongside coldcall_agent.py in the same container, sharing the same
.env.<campaign> and the same persistent disk (so the do-not-call ledger it
reads is always current with what the worker has actually logged).

Usage:
    CAMPAIGN=us-friend uv run python scheduler_poller.py
"""

import asyncio
import logging
import os
import pathlib
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv

from dialer import DEFAULT_MAX_NO_ANSWER_ATTEMPTS, run_campaign, within_calling_hours

logger = logging.getLogger("scheduler_poller")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

CAMPAIGN = os.environ.get("CAMPAIGN")
if not CAMPAIGN:
    raise SystemExit(
        "CAMPAIGN env var is required (e.g. CAMPAIGN=us-friend uv run python scheduler_poller.py) "
        "— same rule as coldcall_agent.py, no default, to avoid ever polling the wrong campaign's table."
    )
load_dotenv(f".env.{CAMPAIGN}")

AIRTABLE_API_KEY = os.environ["AIRTABLE_API_KEY"]
AIRTABLE_BASE_ID = os.environ["AIRTABLE_BASE_ID"]
AIRTABLE_SCHEDULE_TABLE_ID = os.environ["AIRTABLE_SCHEDULE_TABLE_ID"]

LEADS_PATH = pathlib.Path(os.environ["LEADS_PATH"])
CALL_LOG_PATH = pathlib.Path(os.environ["CALL_LOG_PATH"])
AGENT_NAME = os.environ["AGENT_NAME"]
CALLING_TZ = ZoneInfo(os.environ.get("CALLING_TZ", "Europe/Paris"))
MAX_NO_ANSWER_ATTEMPTS = int(os.environ.get("MAX_NO_ANSWER_ATTEMPTS", DEFAULT_MAX_NO_ANSWER_ATTEMPTS))

POLL_INTERVAL_SECONDS = 60
AIRTABLE_URL = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_SCHEDULE_TABLE_ID}"
HEADERS = {"Authorization": f"Bearer {AIRTABLE_API_KEY}"}


async def _fetch_pending(client: httpx.AsyncClient) -> list[dict]:
    resp = await client.get(
        AIRTABLE_URL, headers=HEADERS, params={"filterByFormula": "{Status}='pending'"}
    )
    resp.raise_for_status()
    return resp.json().get("records", [])


async def _set_status(client: httpx.AsyncClient, record_id: str, status: str, result: str = "") -> None:
    await client.patch(
        f"{AIRTABLE_URL}/{record_id}",
        headers=HEADERS,
        json={"fields": {"Status": status, "Result": result}},
    )


async def _run_due_record(client: httpx.AsyncClient, record: dict) -> None:
    fields = record["fields"]
    record_id = record["id"]
    run_at_raw = fields.get("Run At")
    if not run_at_raw:
        return
    run_at = datetime.fromisoformat(run_at_raw.replace("Z", "+00:00"))
    if run_at > datetime.now(timezone.utc):
        return  # not due yet

    logger.info(f"scheduled run due: {record_id} (Run At={run_at_raw})")
    await _set_status(client, record_id, "running")

    # Weekday-only pre-check here (universal across US timezones at any hour
    # this would run) — actual time-of-day is enforced per-lead inside
    # run_campaign(), against each lead's own region, not this one CALLING_TZ.
    # A leads file spanning multiple US timezones would otherwise get
    # incorrectly gated (or incorrectly allowed) by a single blanket check.
    if datetime.now(CALLING_TZ).weekday() >= 5:
        msg = f"skipped - weekend at execution time ({datetime.now(CALLING_TZ).isoformat()})"
        logger.warning(f"{record_id}: {msg}")
        await _set_status(client, record_id, "skipped", msg)
        return

    try:
        candidates = await run_campaign(
            leads_path=LEADS_PATH,
            call_log_path=CALL_LOG_PATH,
            agent_name=AGENT_NAME,
            livekit_url=os.environ["LIVEKIT_URL"],
            livekit_api_key=os.environ["LIVEKIT_API_KEY"],
            livekit_api_secret=os.environ["LIVEKIT_API_SECRET"],
            calling_tz=CALLING_TZ,
            max_no_answer_attempts=MAX_NO_ANSWER_ATTEMPTS,
            limit=int(fields.get("Lead Limit") or 5),
            region=fields.get("Region") or None,
            delay=5.0,
            dry_run=False,
        )
        result = f"dispatched {len(candidates)} lead(s): " + ", ".join(
            c.get("Name", "?") for c in candidates
        )
        logger.info(f"{record_id}: {result}")
        await _set_status(client, record_id, "done", result)
    except Exception as e:
        logger.exception(f"{record_id}: run failed")
        await _set_status(client, record_id, "failed", str(e))


async def poll_loop() -> None:
    logger.info(
        f"scheduler_poller started for CAMPAIGN={CAMPAIGN}, "
        f"polling every {POLL_INTERVAL_SECONDS}s"
    )
    async with httpx.AsyncClient(timeout=15.0) as client:
        while True:
            try:
                records = await _fetch_pending(client)
                for record in records:
                    await _run_due_record(client, record)
            except Exception:
                logger.exception("poll cycle failed, will retry next interval")
            await asyncio.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    asyncio.run(poll_loop())
