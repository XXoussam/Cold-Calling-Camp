"""CLI to place outbound cold calls for a given campaign (--campaign france|us-friend).

Manual, explicit use only — this does not run on a schedule. Each run dials a
bounded number of leads, one dispatch at a time, and only within that
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
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from livekit import api

BASE_DIR = pathlib.Path(__file__).parent

DO_NOT_CALL_STATUSES = {"not_interested", "do_not_call"}

CALLING_WINDOWS = [(9, 30, 12, 30), (14, 0, 18, 30)]  # (start_h, start_m, end_h, end_m)


def within_calling_hours(tz: ZoneInfo, now: datetime | None = None) -> bool:
    now = now or datetime.now(tz)
    if now.weekday() >= 5:  # Sat/Sun
        return False
    minutes_now = now.hour * 60 + now.minute
    for sh, sm, eh, em in CALLING_WINDOWS:
        if sh * 60 + sm <= minutes_now <= eh * 60 + em:
            return True
    return False


def load_leads() -> list[dict]:
    with open(LEADS_PATH, encoding="utf-8") as f:
        return json.load(f)


def load_do_not_call() -> set[str]:
    if not CALL_LOG_PATH.exists():
        return set()
    phones: set[str] = set()
    with open(CALL_LOG_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            if entry.get("status") in DO_NOT_CALL_STATUSES and entry.get("phone"):
                phones.add(entry["phone"])
    return phones


async def dispatch_call(lkapi: api.LiveKitAPI, lead: dict, override_to: str | None) -> None:
    metadata = dict(lead)
    if override_to:
        metadata["Phone"] = override_to

    room_name = f"coldcall-{uuid.uuid4().hex[:8]}"
    await lkapi.agent_dispatch.create_dispatch(
        api.CreateAgentDispatchRequest(
            agent_name=AGENT_NAME,
            room=room_name,
            metadata=json.dumps(metadata, ensure_ascii=False),
        )
    )
    print(f"dispatched: {lead.get('Name')} ({lead.get('Phone')}) -> room {room_name}")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--campaign", required=True, choices=["france", "us-friend"], help="which campaign config (.env.<campaign>) to run")
    parser.add_argument("--leads-file", type=str, default=None, help="override LEADS_PATH for this run, e.g. leads/test-lead-us.json for a self-test with a fake realistic lead")
    parser.add_argument("--limit", type=int, default=5, help="max number of leads to call this run")
    parser.add_argument("--region", type=str, default=None, help="only call leads in this Region")
    parser.add_argument("--to", type=str, default=None, help="override phone number (E.164) for all calls this run — use your own number for a test run")
    parser.add_argument("--delay", type=float, default=5.0, help="seconds to wait between dispatches")
    parser.add_argument("--dry-run", action="store_true", help="print who would be called, dial nothing")
    parser.add_argument("--ignore-hours", action="store_true", help="bypass the calling-hours check (for test calls)")
    args = parser.parse_args()

    load_dotenv(f".env.{args.campaign}")
    global LEADS_PATH, CALL_LOG_PATH, AGENT_NAME
    LEADS_PATH = pathlib.Path(args.leads_file or os.environ["LEADS_PATH"])
    CALL_LOG_PATH = pathlib.Path(os.environ["CALL_LOG_PATH"])
    AGENT_NAME = os.environ["AGENT_NAME"]
    calling_tz = ZoneInfo(os.environ.get("CALLING_TZ", "Europe/Paris"))

    if not args.dry_run and not args.ignore_hours and not within_calling_hours(calling_tz):
        print(f"Outside calling hours (Mon-Fri 9h30-12h30 / 14h-18h30 {calling_tz.key}). Not dialing.")
        print("Use --ignore-hours to override for a deliberate test call, or --dry-run to preview anytime.")
        return

    leads = load_leads()
    dnc = load_do_not_call()

    candidates = [lead for lead in leads if lead.get("Phone") not in dnc]
    if args.region:
        candidates = [lead for lead in candidates if lead.get("Region") == args.region]
    candidates = candidates[: args.limit]

    if not candidates:
        print("No candidates match the given filters.")
        return

    print(f"{len(candidates)} lead(s) queued this run:")
    for lead in candidates:
        target = args.to or lead.get("Phone")
        print(f"  - {lead.get('Name')} ({lead.get('Region')}) -> {target}")

    if args.dry_run:
        return

    async with api.LiveKitAPI(
        url=os.environ["LIVEKIT_URL"],
        api_key=os.environ["LIVEKIT_API_KEY"],
        api_secret=os.environ["LIVEKIT_API_SECRET"],
    ) as lkapi:
        for i, lead in enumerate(candidates):
            await dispatch_call(lkapi, lead, args.to)
            if i < len(candidates) - 1:
                await asyncio.sleep(args.delay)


if __name__ == "__main__":
    asyncio.run(main())
