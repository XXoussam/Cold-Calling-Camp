"""CLI to place outbound cold calls for a given campaign (--campaign france|us-friend).

Manual, explicit use only — this does not run on a schedule. Each run dials a
bounded number of leads, --max-concurrent (default 2) at a time — the run
waits for each batch to actually finish before dispatching the next, rather
than firing off --limit calls in quick succession — and only within that
campaign's business-hours window (CALLING_TZ in .env.<campaign>). Leads
previously logged as not interested are skipped permanently (CALL_LOG_PATH is
the do-not-call source of truth, kept separate per campaign).

Usage:
  python dialer.py --campaign france --dry-run     # show who'd be called
  python dialer.py --campaign us-friend --limit 1 --to +15551234567 --ignore-hours
                                                    # first real test: dial your
                                                    # own number instead of a lead
  python dialer.py --campaign france --limit 5 --region "Valbonne / Sophia Antipolis"
"""

import argparse
import asyncio
import json
import os
import pathlib
import sys
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from livekit import api

# Windows' console defaults to cp1252, which can't encode stray Unicode
# control/formatting characters that show up in scraped lead data (e.g. a
# phone number with a trailing U+202C) — that crashed the candidate-list
# print before any call was dispatched. Force UTF-8 so a bad character in
# one lead can't take down the whole run.
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR = pathlib.Path(__file__).parent

DO_NOT_CALL_STATUSES = {"not_interested", "do_not_call"}

# no_answer retry policy default: up to this many no_answer attempts total,
# never more than one attempt per calendar day (campaign-local). Once
# exhausted, coldcall_agent.py auto-flags the lead not_interested — this
# same limit is also checked here as a backstop in case that auto-flag write
# is ever missing (e.g. leads that hit the limit before this policy existed).
# Read lazily (not at import time) since load_dotenv() for the target
# campaign hasn't necessarily run yet when this module is first imported —
# see run_campaign()'s max_no_answer_attempts parameter.
DEFAULT_MAX_NO_ANSWER_ATTEMPTS = 3

CALLING_WINDOWS = [(9, 30, 12, 30), (14, 0, 18, 30)]  # (start_h, start_m, end_h, end_m)

# US state -> IANA timezone, for per-lead calling-hours enforcement. A single
# --calling-tz applied to a whole run is only correct if every lead in that
# run is actually in that timezone — leads-us.json spans every US timezone,
# so that assumption silently breaks the moment a run isn't perfectly
# filtered to one region. This table lets every lead be checked against its
# *own* region's local time instead, regardless of what --calling-tz was
# passed. Unlisted regions (e.g. French city/area names) fall back to
# --calling-tz/CALLING_TZ — harmless, since those campaigns are single-timezone
# anyway. Indiana and Kentucky are genuinely split Eastern/Central by county;
# mapped to their majority zone (Eastern) here rather than modeled per-county.
US_REGION_TIMEZONES: dict[str, str] = {
    "California": "America/Los_Angeles", "Washington": "America/Los_Angeles",
    "Oregon": "America/Los_Angeles", "Idaho": "America/Los_Angeles", "Nevada": "America/Los_Angeles",
    "Texas": "America/Chicago", "Illinois": "America/Chicago", "Minnesota": "America/Chicago",
    "Iowa": "America/Chicago", "Wisconsin": "America/Chicago", "Kansas": "America/Chicago",
    "Nebraska": "America/Chicago", "Arkansas": "America/Chicago", "Missouri": "America/Chicago",
    "Louisiana": "America/Chicago", "Tennessee": "America/Chicago", "Oklahoma": "America/Chicago",
    "Mississippi": "America/Chicago", "Alabama": "America/Chicago",
    "Colorado": "America/Denver", "Montana": "America/Denver", "Utah": "America/Denver",
    "Wyoming": "America/Denver", "New Mexico": "America/Denver",
    "Arizona": "America/Phoenix",  # no DST, deliberately not America/Denver
    "New York": "America/New_York", "North Carolina": "America/New_York",
    "Massachusetts": "America/New_York", "Michigan": "America/New_York",
    "Pennsylvania": "America/New_York", "South Carolina": "America/New_York",
    "Maryland": "America/New_York", "Ohio": "America/New_York", "Georgia": "America/New_York",
    "Maine": "America/New_York", "Virginia": "America/New_York", "Connecticut": "America/New_York",
    "New Hampshire": "America/New_York", "New Jersey": "America/New_York",
    "West Virginia": "America/New_York", "Indiana": "America/New_York", "Kentucky": "America/New_York",
    "Florida": "America/New_York", "Delaware": "America/New_York", "Vermont": "America/New_York",
    "Rhode Island": "America/New_York", "Hawaii": "Pacific/Honolulu", "Alaska": "America/Anchorage",
}


def within_calling_hours(tz: ZoneInfo, now: datetime | None = None) -> bool:
    now = now or datetime.now(tz)
    if now.weekday() >= 5:  # Sat/Sun
        return False
    minutes_now = now.hour * 60 + now.minute
    for sh, sm, eh, em in CALLING_WINDOWS:
        if sh * 60 + sm <= minutes_now <= eh * 60 + em:
            return True
    return False


def lead_calling_tz(lead: dict, default_tz: ZoneInfo) -> ZoneInfo:
    """The correct timezone to gate this specific lead's calling hours by —
    its own region if known, otherwise the campaign's default."""
    region = lead.get("Region")
    tz_name = US_REGION_TIMEZONES.get(region) if region else None
    return ZoneInfo(tz_name) if tz_name else default_tz


def load_leads(leads_path: pathlib.Path) -> list[dict]:
    with open(leads_path, encoding="utf-8") as f:
        return json.load(f)


def _load_call_log_entries(call_log_path: pathlib.Path) -> list[dict]:
    if not call_log_path.exists():
        return []
    entries = []
    with open(call_log_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries


def compute_excluded_phones(
    call_log_path: pathlib.Path,
    calling_tz: ZoneInfo,
    max_no_answer_attempts: int = DEFAULT_MAX_NO_ANSWER_ATTEMPTS,
    now: datetime | None = None,
) -> set[str]:
    """Phones to skip this run: permanently declined, already attempted
    today (any status — never dial the same number twice in one day), or a
    no_answer lead that's exhausted its retry budget (backstop; normally
    coldcall_agent.py already auto-flags these not_interested directly)."""
    entries = _load_call_log_entries(call_log_path)
    today = (now or datetime.now(calling_tz)).astimezone(calling_tz).date()

    excluded: set[str] = set()
    no_answer_counts: dict[str, int] = {}
    for entry in entries:
        phone = entry.get("phone")
        if not phone:
            continue
        status = entry.get("status")
        if status in DO_NOT_CALL_STATUSES:
            excluded.add(phone)
        if status == "no_answer":
            no_answer_counts[phone] = no_answer_counts.get(phone, 0) + 1
        ts = entry.get("timestamp")
        if ts:
            entry_date = datetime.fromisoformat(ts).astimezone(calling_tz).date()
            if entry_date == today:
                excluded.add(phone)  # already attempted today, regardless of outcome

    for phone, count in no_answer_counts.items():
        if count >= max_no_answer_attempts:
            excluded.add(phone)

    return excluded


async def dispatch_call(
    lkapi: api.LiveKitAPI,
    lead: dict,
    override_to: str | None,
    agent_name: str,
    call_log_path: pathlib.Path,
) -> str:
    metadata = dict(lead)
    if override_to:
        metadata["Phone"] = override_to

    room_name = f"coldcall-{uuid.uuid4().hex[:8]}"
    await lkapi.agent_dispatch.create_dispatch(
        api.CreateAgentDispatchRequest(
            agent_name=agent_name,
            room=room_name,
            metadata=json.dumps(metadata, ensure_ascii=False),
        )
    )
    print(f"dispatched: {lead.get('Name')} ({lead.get('Phone')}) -> room {room_name}")

    # Record the attempt immediately, not just once the call finishes and
    # coldcall_agent.py logs a real outcome (that can be minutes later). This
    # is what stops two overlapping dispatchers (e.g. two Scheduled Runs due
    # in the same poll cycle) from both reading a lead as untouched and
    # dispatching it twice — compute_excluded_phones() already excludes any
    # phone with ANY entry timestamped today, so this alone closes the race
    # for anything processed sequentially after it, no new logic needed.
    call_log_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "lead_name": lead.get("Name"),
        "phone": lead.get("Phone"),
        "region": lead.get("Region"),
        "status": "dispatched",
        "note": f"dispatched to room {room_name}",
    }
    with open(call_log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    return room_name


async def _wait_for_rooms_to_end(
    lkapi: api.LiveKitAPI, room_names: list[str], poll_interval: float = 5.0, max_wait: float = 900.0
) -> None:
    """Poll until none of the given rooms are still active.

    Backstop against polling forever: coldcall_agent.py caps every call at
    max_call_duration=600s server-side, so a room outliving max_wait here
    means something's actually stuck — proceed anyway rather than hang the
    whole batch.
    """
    waited = 0.0
    while waited < max_wait:
        rooms = await lkapi.room.list_rooms(api.ListRoomsRequest(names=room_names))
        still_active = {r.name for r in rooms.rooms}
        if not still_active:
            return
        await asyncio.sleep(poll_interval)
        waited += poll_interval
    print(f"warning: gave up waiting for {still_active} to end after {max_wait:.0f}s, moving on anyway")


async def run_campaign(
    *,
    leads_path: pathlib.Path,
    call_log_path: pathlib.Path,
    agent_name: str,
    livekit_url: str,
    livekit_api_key: str,
    livekit_api_secret: str,
    calling_tz: ZoneInfo,
    limit: int = 5,
    region: str | None = None,
    to: str | None = None,
    delay: float = 5.0,
    dry_run: bool = False,
    max_no_answer_attempts: int = DEFAULT_MAX_NO_ANSWER_ATTEMPTS,
    max_concurrent: int = 2,
    ignore_hours: bool = False,
) -> list[dict]:
    """Load leads, filter out do-not-call/already-attempted-today/exhausted-retries,
    dispatch up to `limit` calls, `max_concurrent` at a time.

    Shared by the CLI (main(), below) and by scheduler_poller.py — the single
    place this logic lives, so a scheduled run and a manual run can never
    drift apart. Returns the list of candidate leads (dispatched, unless
    dry_run).

    max_concurrent caps how many calls run at once regardless of `limit` —
    each batch is dispatched, then the run waits for that whole batch to
    finish (call ended, room closed) before dispatching the next one. This
    exists because dispatching --limit 10 with only a few seconds between
    each meant up to ~8 real calls running concurrently on one machine at
    once, each running its own STT/LLM/TTS/VAD pipeline — confirmed via
    real call metrics (playback_latency spiking to 3+ seconds, e2e_latency
    hitting 5-6s, a lead literally saying "I can't hear you") that this
    degrades every call running at the same time, not just the excess ones.
    """
    leads = load_leads(leads_path)
    excluded = compute_excluded_phones(call_log_path, calling_tz, max_no_answer_attempts)

    candidates = [lead for lead in leads if lead.get("Phone") not in excluded]
    if region:
        candidates = [lead for lead in candidates if lead.get("Region") == region]

    # Per-lead calling-hours check — not the same as the single `calling_tz`
    # gate a caller might apply upstream. A leads file can span many US
    # timezones (leads-us.json does), so each lead is checked against its
    # own region's actual local time, not whatever timezone this run happens
    # to be invoked with. --ignore-hours bypasses this entirely, same as it
    # always has.
    if not ignore_hours:
        before = len(candidates)
        candidates = [
            lead for lead in candidates if within_calling_hours(lead_calling_tz(lead, calling_tz))
        ]
        skipped_for_hours = before - len(candidates)
        if skipped_for_hours:
            print(f"({skipped_for_hours} lead(s) skipped — outside calling hours in their own region right now)")

    candidates = candidates[:limit]

    if not candidates:
        print("No candidates match the given filters.")
        return []

    print(f"{len(candidates)} lead(s) queued this run:")
    for lead in candidates:
        target = to or lead.get("Phone")
        print(f"  - {lead.get('Name')} ({lead.get('Region')}) -> {target}")

    if dry_run:
        return candidates

    async with api.LiveKitAPI(
        url=livekit_url, api_key=livekit_api_key, api_secret=livekit_api_secret
    ) as lkapi:
        for batch_start in range(0, len(candidates), max_concurrent):
            batch = candidates[batch_start : batch_start + max_concurrent]
            room_names = []
            for i, lead in enumerate(batch):
                room_names.append(await dispatch_call(lkapi, lead, to, agent_name, call_log_path))
                if i < len(batch) - 1:
                    await asyncio.sleep(delay)

            is_last_batch = batch_start + max_concurrent >= len(candidates)
            if not is_last_batch:
                print(f"waiting for this batch of {len(batch)} to finish before dispatching more...")
                await _wait_for_rooms_to_end(lkapi, room_names)

    return candidates


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--campaign", required=True, choices=["france", "us-friend"], help="which campaign config (.env.<campaign>) to run")
    parser.add_argument("--leads-file", type=str, default=None, help="override LEADS_PATH for this run, e.g. leads/test-lead-us.json for a self-test with a fake realistic lead, or a region-specific split like leads/us/leads-florida.json")
    parser.add_argument("--limit", type=int, default=5, help="max number of leads to call this run")
    parser.add_argument("--region", type=str, default=None, help="only call leads in this Region")
    parser.add_argument("--calling-tz", type=str, default=None, help="override CALLING_TZ for this run, e.g. America/Los_Angeles for a California-only leads file — the campaign's leads span every US timezone, so a single CALLING_TZ in .env.<campaign> can't be correct for all of them")
    parser.add_argument("--to", type=str, default=None, help="override phone number (E.164) for all calls this run — use your own number for a test run")
    parser.add_argument("--delay", type=float, default=5.0, help="seconds to wait between dispatches within a batch")
    parser.add_argument("--max-concurrent", type=int, default=2, help="max calls running at once, regardless of --limit — the run waits for each batch to finish before dispatching the next")
    parser.add_argument("--dry-run", action="store_true", help="print who would be called, dial nothing")
    parser.add_argument("--ignore-hours", action="store_true", help="bypass the calling-hours check (for test calls)")
    args = parser.parse_args()

    load_dotenv(f".env.{args.campaign}")
    leads_path = pathlib.Path(args.leads_file or os.environ["LEADS_PATH"])
    call_log_path = pathlib.Path(os.environ["CALL_LOG_PATH"])
    agent_name = os.environ["AGENT_NAME"]
    calling_tz = ZoneInfo(args.calling_tz or os.environ.get("CALLING_TZ", "Europe/Paris"))

    # Fast pre-exit on weekends only — weekday is the same across every US
    # timezone at any hour this campaign would ever call, so a single tz is
    # fine for this part. Actual time-of-day is no longer gated by this one
    # calling_tz — that's now enforced per-lead, against each lead's own
    # region, inside run_campaign() (a leads file can span many timezones,
    # so a single blanket check here was only ever correct by accident).
    if not args.dry_run and not args.ignore_hours and datetime.now(calling_tz).weekday() >= 5:
        print(f"It's the weekend ({calling_tz.key}). Not dialing.")
        print("Use --ignore-hours to override for a deliberate test call, or --dry-run to preview anytime.")
        return

    await run_campaign(
        leads_path=leads_path,
        call_log_path=call_log_path,
        agent_name=agent_name,
        livekit_url=os.environ["LIVEKIT_URL"],
        livekit_api_key=os.environ["LIVEKIT_API_KEY"],
        livekit_api_secret=os.environ["LIVEKIT_API_SECRET"],
        calling_tz=calling_tz,
        limit=args.limit,
        region=args.region,
        to=args.to,
        delay=args.delay,
        dry_run=args.dry_run,
        max_no_answer_attempts=int(os.environ.get("MAX_NO_ANSWER_ATTEMPTS", DEFAULT_MAX_NO_ANSWER_ATTEMPTS)),
        max_concurrent=args.max_concurrent,
        ignore_hours=args.ignore_hours,
    )


if __name__ == "__main__":
    asyncio.run(main())
