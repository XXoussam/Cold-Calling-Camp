import asyncio
import json
import logging
import os
import pathlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np
from dotenv import load_dotenv
from livekit import rtc
from livekit.agents import (
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


def log_call_outcome(lead: dict, status: str, note: str = "") -> None:
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


@dataclass
class CallContext:
    lead: dict


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
        log_call_outcome(call_ctx.lead, status, note or "")
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

    if phone and trunk_id:
        try:
            await ctx.add_sip_participant(
                call_to=_to_e164(phone, PHONE_COUNTRY),
                trunk_id=trunk_id,
                participant_identity="lead",
                participant_name=lead.get("Name", "lead"),
            )
        except Exception as e:
            logger.warning(f"outbound call failed for {lead.get('Name')}: {e}")
            log_call_outcome(lead, "no_answer", str(e))
            ctx.shutdown()
            return

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
        userdata=CallContext(lead=lead),
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

    ctx.add_shutdown_callback(log_usage)
    ctx.add_shutdown_callback(_save_transcript)

    try:
        await session.start(
            agent=ColdCallAgent(lead),
            room=ctx.room,
            room_input_options=RoomInputOptions(
                noise_cancellation=noise_cancellation.BVC(),
            ),
        )

        # Small pause after pickup before speaking — jumping in instantly felt
        # abrupt, this gives the person a beat to actually settle into the call.
        await asyncio.sleep(1.5)

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
        log_call_outcome(lead, "no_answer", f"session ended before speaking: {e}")


if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=prewarm,
            agent_name=AGENT_NAME,
        )
    )
