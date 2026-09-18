import asyncio
import json
import logging
import os
import pathlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
import numpy as np
from dotenv import load_dotenv
from google.protobuf.duration_pb2 import Duration
from livekit import api, rtc
from livekit.agents import (
    AMD,
    Agent,
    AgentSession,
    JobContext,
    JobProcess,
    ConversationItemAddedEvent,
    MetricsCollectedEvent,
    ModelSettings,
    RoomInputOptions,
    RunContext,
    WorkerOptions,
    cli,
    metrics,
)
from livekit.agents.llm import function_tool
from livekit.plugins import elevenlabs, deepgram, noise_cancellation, openai, silero
from livekit.plugins.elevenlabs import VoiceSettings
from livekit.plugins.turn_detector.multilingual import MultilingualModel

from coldcall_prompt import build_instructions

logger = logging.getLogger("coldcall_agent")

CAMPAIGN = os.environ.get("CAMPAIGN")
if not CAMPAIGN:
    raise SystemExit(
        "CAMPAIGN env var is required (e.g. CAMPAIGN=us uv run python coldcall_agent.py dev) "
        "— no default, to avoid ever running the wrong campaign's persona/trunk by accident."
    )
load_dotenv(f".env.{CAMPAIGN}")

AGENT_NAME = os.environ["AGENT_NAME"]
AGENT_LANGUAGE = os.environ["AGENT_LANGUAGE"]
PHONE_COUNTRY = os.environ.get("PHONE_COUNTRY", "FR")
ELEVEN_VOICE_ID = os.environ.get("ELEVEN_VOICE_ID")
if not ELEVEN_VOICE_ID:
    raise SystemExit(
        f"ELEVEN_VOICE_ID is empty in .env.{CAMPAIGN} — pick an ElevenLabs voice "
        "for this campaign and fill it in before running the agent."
    )

CALL_LOG_PATH = pathlib.Path(
    os.environ.get("CALL_LOG_PATH", pathlib.Path(__file__).parent / "leads" / "call-log.jsonl")
)
KMS_LOGS_PATH = pathlib.Path(
    os.environ.get("KMS_LOGS_PATH", pathlib.Path(__file__).parent / "KMS" / "logs")
)
LIVE_LOG_PATH = pathlib.Path(__file__).parent / "KMS" / f"live-{CAMPAIGN}.log"

# no_answer retry policy: after this many no_answer attempts for the same
# phone, auto-flag the lead not_interested so dialer.py's do-not-call filter
# permanently excludes it — matches DEFAULT_MAX_NO_ANSWER_ATTEMPTS in
# dialer.py, kept as its own env-var read here since this module doesn't
# import dialer.py (they're separate processes).
MAX_NO_ANSWER_ATTEMPTS = int(os.environ.get("MAX_NO_ANSWER_ATTEMPTS", "3"))

# Optional — call outcomes are pushed to this Airtable table if all three are
# set (fully optional, campaigns without them just skip the push and keep
# writing CALL_LOG_PATH as before). See voice-enhancement/README.md history
# for how the "Cold-Calls" base's tables were set up to match this shape.
AIRTABLE_API_KEY = os.environ.get("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID")
AIRTABLE_TABLE_ID = os.environ.get("AIRTABLE_TABLE_ID")

# LiveKit Cloud auto-records every session's audio (record=True is the
# session default) but only exposes it through the dashboard — no API to
# fetch it, confirmed by checking the SDK, protocol files, and the `lk` CLI.
# Cheapest working option: link straight to the dashboard's per-session
# player instead of trying to hosting the audio ourselves. Only works while
# the room is live — room.sid isn't retrievable after the call ends, so
# this can only ever be captured live, never backfilled for past calls.
# Same LiveKit project for both campaigns (see .env comments), so this one
# ID covers both.
LIVEKIT_PROJECT_ID = os.environ.get("LIVEKIT_PROJECT_ID")


def _slugify(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower() or "unknown"


def save_transcript(session: AgentSession, lead: dict, room_name: str) -> None:
    KMS_LOGS_PATH.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    path = KMS_LOGS_PATH / f"{now.strftime('%Y%m%dT%H%M%SZ')}_{_slugify(lead.get('Name', 'unknown'))}_{room_name}.json"
    data = {
        "timestamp": now.isoformat(),
        "room": room_name,
        "lead": {
            "name": lead.get("Name"),
            "phone": lead.get("Phone"),
            "region": lead.get("Region"),
        },
        "transcript": session.history.to_dict(),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info(f"transcript saved: {path}")

# Neither ElevenLabs' VoiceSettings nor LiveKit's output pipeline expose a
# real volume/gain control — this applies actual digital gain to the raw PCM
# samples, clipped to int16 range to avoid distortion. 1.0 = unchanged.
TTS_GAIN = float(os.environ.get("TTS_GAIN", "1.4"))

# Per-campaign since pacing varies by which ElevenLabs voice is used — ElevenLabs' valid range is ~0.7-1.2.
TTS_SPEED = float(os.environ.get("TTS_SPEED", "1.05"))


def _apply_gain(frame: rtc.AudioFrame, gain: float) -> rtc.AudioFrame:
    samples = np.frombuffer(frame.data, dtype=np.int16).astype(np.float32)
    samples = np.clip(samples * gain, -32768, 32767).astype(np.int16)
    return rtc.AudioFrame(
        data=samples.tobytes(),
        sample_rate=frame.sample_rate,
        num_channels=frame.num_channels,
        samples_per_channel=frame.samples_per_channel,
    )


def _to_e164(phone: str, country: str = "FR") -> str:
    """Convert a local number to E.164, e.g. FR '06 81 28 81 51' -> '+33681288151',
    US '(555) 123-4567' -> '+15551234567'."""
    digits = re.sub(r"[^\d+]", "", phone)
    if digits.startswith("+"):
        return digits
    if country == "US":
        if len(digits) == 10:
            return "+1" + digits
        if len(digits) == 11 and digits.startswith("1"):
            return "+" + digits
        return "+1" + digits
    if digits.startswith("0"):
        return "+33" + digits[1:]
    return "+" + digits


async def _push_to_airtable(
    entry: dict, duration_minutes: int | None, lead: dict, session_link: str | None
) -> None:
    if not (AIRTABLE_API_KEY and AIRTABLE_BASE_ID and AIRTABLE_TABLE_ID):
        return  # not configured for this campaign — CALL_LOG_PATH is still the source of truth
    fields = {
        "Timestamp": entry["timestamp"],
        "Lead Name": entry["lead_name"],
        "Phone": entry["phone"],
        "Region": entry["region"],
        "Status": entry["status"],
        "Note": entry["note"],
    }
    if duration_minutes is not None:
        fields["Call Duration (min)"] = duration_minutes
    if website := lead.get("Website"):
        fields["Website"] = website
    if linkedin := lead.get("LinkedIn URL"):
        fields["LinkedIn URL"] = linkedin
    if session_link:
        fields["Session Recording Link"] = session_link
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_ID}"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                url,
                headers={"Authorization": f"Bearer {AIRTABLE_API_KEY}"},
                json={"fields": fields, "typecast": True},
            )
            resp.raise_for_status()
    except Exception as e:
        # Best-effort only — CALL_LOG_PATH already has this entry, a flaky
        # Airtable push must never take down a live call.
        logger.warning(f"Airtable push failed for {entry.get('lead_name')}: {e}")


async def log_call_outcome(
    lead: dict,
    status: str,
    note: str = "",
    duration_minutes: int | None = None,
    session_link: str | None = None,
) -> None:
    CALL_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "lead_name": lead.get("Name"),
        "phone": lead.get("Phone"),
        "region": lead.get("Region"),
        "status": status,
        "note": note,
    }
    with open(CALL_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    logger.info(f"call outcome logged: {entry}")
    await _push_to_airtable(entry, duration_minutes, lead, session_link)

    if status == "no_answer":
        await _auto_flag_if_exhausted(lead)


async def _auto_flag_if_exhausted(lead: dict) -> None:
    """If this phone has now hit MAX_NO_ANSWER_ATTEMPTS no_answer entries,
    log an additional not_interested entry so dialer.py's do-not-call filter
    permanently excludes it — a lead that never picks up stops being retried
    forever instead of quietly recurring in every future run."""
    phone = lead.get("Phone")
    if not phone or not CALL_LOG_PATH.exists():
        return
    count = 0
    with open(CALL_LOG_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            e = json.loads(line)
            if e.get("phone") == phone and e.get("status") == "no_answer":
                count += 1
    if count >= MAX_NO_ANSWER_ATTEMPTS:
        logger.info(
            f"auto-flagging {lead.get('Name')} not_interested after {count} no-answer attempts"
        )
        await log_call_outcome(
            lead,
            "not_interested",
            f"auto-flagged after {count} no-answer attempts with no response",
        )


@dataclass
class CallContext:
    lead: dict
    started_at: datetime | None = None
    outcome_logged: bool = False
    session_link: str | None = None


class ColdCallAgent(Agent):
    def __init__(self, lead: dict) -> None:
        contact_name = lead.get("Decision Maker Name")
        agency_name = lead.get("Name")
        city = lead.get("Region")
        super().__init__(
            instructions=build_instructions(contact_name, agency_name, city, language=AGENT_LANGUAGE)
        )

    async def tts_node(self, text, model_settings: ModelSettings):
        async for frame in Agent.default.tts_node(self, text, model_settings):
            yield _apply_gain(frame, TTS_GAIN)

    @function_tool
    async def log_call_outcome(
        self,
        context: RunContext,
        status: str,
        note: str | None = None,
    ):
        """Log the outcome of this call once it's clear how it's ending.

        Args:
            status: one of 'not_interested', 'callback_requested', 'email_requested', 'no_answer'
            note: short free-text detail (e.g. that they're interested and Oussama should follow
                up for a discovery call, or a requested callback day/time)
        """
        call_ctx: CallContext = context.session.userdata
        duration_minutes = None
        if call_ctx.started_at:
            elapsed = datetime.now(timezone.utc) - call_ctx.started_at
            duration_minutes = round(elapsed.total_seconds() / 60)
        await log_call_outcome(
            call_ctx.lead, status, note or "", duration_minutes, call_ctx.session_link
        )
        call_ctx.outcome_logged = True
        return "logged"


def prewarm(proc: JobProcess):
    # min_silence_duration default is 0.55s — lowered so the agent notices the
    # user has stopped talking sooner, on top of the endpointing delay below.
    proc.userdata["vad"] = silero.VAD.load(min_silence_duration=0.35)


async def entrypoint(ctx: JobContext):
    ctx.log_context_fields = {"room": ctx.room.name}
    await ctx.connect()

    lead = json.loads(ctx.job.metadata) if ctx.job.metadata else {}
    phone = lead.get("Phone")
    trunk_id = os.environ.get("SIP_OUTBOUND_TRUNK_ID")

    session_link = None
    if LIVEKIT_PROJECT_ID:
        try:
            room_sid = await ctx.room.sid
            session_link = (
                f"https://cloud.livekit.io/projects/{LIVEKIT_PROJECT_ID}"
                f"/sessions/{room_sid}/observability?mode=metrics"
            )
        except Exception as e:
            logger.warning(f"could not resolve room sid for session link: {e}")

    session = AgentSession(
        llm=openai.LLM(model="gpt-4o-mini"),
        stt=deepgram.STT(model="nova-3", language=AGENT_LANGUAGE),
        tts=elevenlabs.TTS(
            voice_id=ELEVEN_VOICE_ID,
            model="eleven_turbo_v2_5",
            encoding="pcm_16000",  # was mp3_22050_32 (32kbps) — heavily compressed, sounded quiet/thin
            language=AGENT_LANGUAGE,
            apply_text_normalization="on",  # was "auto" — numbers/addresses read reliably over the phone
            voice_settings=VoiceSettings(
                # was stability=0.45/style=0.35 — that style value adds breathiness that reads as
                # "swishy"/watery once downsampled to Twilio's 8kHz PSTN leg. Re-test by ear
                # (voice-enhancement/test-voice.ps1) before trusting these over the old values.
                stability=0.55,
                similarity_boost=0.75,
                style=0.0,
                speed=TTS_SPEED,
                use_speaker_boost=True,
            ),
        ),
        vad=ctx.proc.userdata["vad"],
        turn_handling={
            "turn_detection": MultilingualModel(),
            # defaults are min_delay=0.5/max_delay=3.0 — felt sluggish/awkward on calls
            "endpointing": {"min_delay": 0.3, "max_delay": 2.0},
            "preemptive_generation": {"enabled": True},
        },
        userdata=CallContext(lead=lead, session_link=session_link),
    )

    @session.on("conversation_item_added")
    def _on_conversation_item_added(ev: ConversationItemAddedEvent):
        if ev.item.type != "message":
            return  # e.g. AgentHandoff — not a spoken turn, nothing to print
        text = "".join(ev.item.content) if isinstance(ev.item.content, list) else ev.item.content
        line = f"[{ctx.room.name}] {ev.item.role}: {text}"
        colored_line = f"\033[32m{line}\033[0m" if ev.item.role == "user" else line
        print(colored_line)
        # Colored on purpose (Get-Content -Wait renders the ANSI codes fine in
        # Windows Terminal/PowerShell) — this file is for live-tailing, not
        # for opening in an editor. save_transcript()'s post-call JSON is the
        # plain-text record for that.
        LIVE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LIVE_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(colored_line + "\n")
            f.flush()

    usage_collector = metrics.UsageCollector()

    @session.on("metrics_collected")
    def _on_metrics_collected(ev: MetricsCollectedEvent):
        metrics.log_metrics(ev.metrics)
        usage_collector.collect(ev.metrics)

    async def log_usage():
        summary = usage_collector.get_summary()
        logger.info(f"Usage: {summary}")

    async def _save_transcript():
        save_transcript(session, lead, ctx.room.name)

    async def _ensure_outcome_logged():
        # Covers the gap where a call connects, then ends before the LLM
        # gets to call log_call_outcome — without this, that call leaves
        # zero trace anywhere, local or Airtable, and looks indistinguishable
        # from "never called" to a future run's do-not-call check.
        #
        # Not every unlogged ending is the same, though — distinguish by
        # what (if anything) the lead's side actually said:
        #   - nothing at all -> genuinely no_answer, retriable
        #   - voicemail/IVR greeting text -> still no_answer, retriable
        #     (a machine picking up isn't a decline)
        #   - real speech that isn't voicemail-shaped -> a human was
        #     reached and the call still ended unresolved; treat a hangup
        #     after live pickup as an implicit decline (not_interested,
        #     permanently excluded) rather than retrying them 3 more times
        call_ctx: CallContext = session.userdata
        if call_ctx.outcome_logged:
            return

        user_text = " ".join(
            "".join(item["content"]) if isinstance(item["content"], list) else str(item["content"])
            for item in session.history.to_dict().get("items", [])
            if item.get("type") == "message" and item.get("role") == "user"
        ).lower()

        voicemail_markers = (
            "voice mail", "voicemail", "leave a message", "leave your name",
            "record your message", "rerecord", "press one", "press 1",
            "not available", "mailbox", "extension", "the tone", "beep",
            # call-screening / gatekeeper systems (e.g. Google Voice screening)
            # — also not a real decision-maker, same as voicemail/IVR
            "reached google", "record your name", "your reason for calling",
            "hold while i try to connect", "screening",
        )

        if not user_text.strip():
            status, note = "no_answer", "call ended before an outcome was logged (early hangup/disconnect)"
        elif any(marker in user_text for marker in voicemail_markers):
            status, note = "no_answer", "reached voicemail/IVR, call ended before an outcome was logged"
        else:
            status, note = (
                "not_interested",
                "picked up (real speech detected), hung up before giving a clear outcome — treated as a decline",
            )
        await log_call_outcome(lead, status, note, session_link=call_ctx.session_link)

    ctx.add_shutdown_callback(log_usage)
    ctx.add_shutdown_callback(_save_transcript)
    ctx.add_shutdown_callback(_ensure_outcome_logged)

    try:
        await session.start(
            agent=ColdCallAgent(lead),
            room=ctx.room,
            room_input_options=RoomInputOptions(
                noise_cancellation=noise_cancellation.BVC(),
            ),
        )

        if phone and trunk_id:
            # AMD needs to be listening before the SIP participant joins, so
            # it has to wrap the dial itself — that's why this now happens
            # after session.start() instead of before it (AMD listens via
            # the session's own room_io). ctx.add_sip_participant() (the
            # convenience wrapper) never sets wait_until_answered, which
            # defaults to false — so it used to resolve as soon as the SIP
            # INVITE went out, not once someone actually picked up, with no
            # cap on ring time. Calling the raw API directly to set that
            # plus ringing_timeout/max_call_duration explicitly.
            async with AMD(session) as detector:
                try:
                    await ctx.api.sip.create_sip_participant(
                        api.CreateSIPParticipantRequest(
                            sip_call_to=_to_e164(phone, PHONE_COUNTRY),
                            sip_trunk_id=trunk_id,
                            room_name=ctx.room.name,
                            participant_identity="lead",
                            participant_name=lead.get("Name", "lead"),
                            wait_until_answered=True,
                            ringing_timeout=Duration(seconds=45),
                            max_call_duration=Duration(seconds=600),
                        )
                    )
                except Exception as e:
                    logger.warning(f"outbound call failed for {lead.get('Name')}: {e}")
                    await log_call_outcome(lead, "no_answer", str(e), session_link=session_link)
                    session.userdata.outcome_logged = True
                    ctx.shutdown()
                    return
                amd_result = await detector.execute()

            if amd_result.is_machine:
                # Hang up now, before the agent ever speaks — no point
                # running the identity-confirmation script into voicemail
                # or an IVR menu.
                logger.info(
                    f"AMD detected {amd_result.category.value} for {lead.get('Name')}, "
                    "hanging up before agent interaction"
                )
                await log_call_outcome(
                    lead,
                    "no_answer",
                    f"AMD detected {amd_result.category.value} — hung up before agent interaction",
                    session_link=session_link,
                )
                session.userdata.outcome_logged = True
                ctx.shutdown()
                return

        session.userdata.started_at = datetime.now(timezone.utc)

        # No manual pause here anymore — this used to sleep(1.5) to give the
        # person a beat before jumping in, but AMD (above) already listens to
        # and classifies the greeting first, which serves the same purpose.
        # Stacking a fixed 1.5s on top of AMD's own listening window plus the
        # LLM/TTS pipeline was measurably too much dead air: two real Alabama
        # calls ended with zero agent speech — the caller said "Hello?" and
        # hung up before a reply ever came. Removed rather than reduced,
        # since AMD's timing already adapts to the actual greeting length.

        opening_instructions = {
            "fr": (
                "Confirme d'abord que tu parles à la bonne personne (demande son nom, "
                "ou confirme simplement l'agence si tu n'as pas de nom précis) avant de "
                "te présenter. Ne fais pas encore l'accroche ni ta présentation à ce "
                "stade. Sois naturelle, brève, ne récite pas une phrase toute faite mot "
                "pour mot."
            ),
            "en": (
                "First confirm you're speaking with the right person (ask for their "
                "name, or just confirm the agency if you don't have a specific name) "
                "before introducing yourself. Don't do the hook or your introduction "
                "yet. Be natural, brief, don't recite a fixed line word for word."
            ),
        }
        await session.generate_reply(instructions=opening_instructions[AGENT_LANGUAGE])
    except RuntimeError as e:
        # The call "answered" at the SIP layer (voicemail, or an instant
        # hangup) so add_sip_participant() above didn't raise — but the room
        # emptied out and the session was torn down (close_on_disconnect)
        # before the agent got to speak. This is the actual point where that
        # surfaces, so it's caught here rather than crashing the job unlogged.
        logger.warning(f"session ended before speaking for {lead.get('Name')}: {e}")
        await log_call_outcome(
            lead, "no_answer", f"session ended before speaking: {e}", session_link=session_link
        )
        session.userdata.outcome_logged = True  # don't also let _ensure_outcome_logged double-log this


if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=prewarm,
            agent_name=AGENT_NAME,
        )
    )
