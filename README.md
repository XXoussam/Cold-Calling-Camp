# Cold-Calling-Camp

Outbound AI cold-calling agent for **ImmoOps AI** — an AI voice assistant that
calls real estate agencies on behalf of Oussama (founder) to introduce
ImmoOps AI's software (AI phone receptionist, automated lead follow-up,
interactive 3D property tours), gauge interest, and hand off warm leads for
a human discovery call. Built on [LiveKit Agents](https://docs.livekit.io/agents/),
Twilio SIP trunking, OpenAI, Deepgram, and ElevenLabs.

See [voice-enhancement/](voice-enhancement/) for voice quality
tuning/diagnostics. [setup-outbound-calling.md](setup-outbound-calling.md)
has the original infra bring-up notes, but predates the multi-campaign
layout (it refers to a single `.env.local` and has no `--campaign` flag) —
treat it as historical.

## How a call works

1. `dialer.py` reads leads, filters out anyone already logged as
   not-interested, and dispatches a LiveKit agent job per lead (one at a
   time, with a delay between calls, only inside calling hours).
2. `coldcall_agent.py` (the LiveKit worker) picks up the job, places the
   outbound call over the campaign's Twilio SIP trunk, and runs the voice
   pipeline: Deepgram (STT) → GPT-4o-mini (LLM) → ElevenLabs (TTS).
3. The persona (`coldcall_prompt.py`) confirms identity, gives a ~10s hook,
   asks permission for 30 seconds, pitches the offer, checks interest, and
   either logs a callback request or adds the number to the do-not-call
   list. It never pushes after a no, and never hides that it's an AI.
4. Every call's outcome (`leads/<campaign>/call-log.jsonl`) and full
   transcript (`KMS/logs*/`) are saved locally.

## Campaigns

Each campaign is fully described by one `.env.<campaign>` file (LiveKit
project, voice stack API keys, Twilio account + trunk credentials, leads
path, calling-hours timezone). These files hold live secrets, live only on
the local machine, and are git-ignored — never commit them.

| Campaign | Twilio account | Status |
|---|---|---|
| `france` | Your own account | Blocked — the account itself currently reports as not active (Twilio status 4), and the French (+33) regulatory bundle was rejected. No working trunk yet. |
| `us-friend` | Friend's account | Working — verified by real test calls |

A third `us` config (same own account, US number) was retired and archived
under `archive/` once that account was found inactive — it shares the
blocker above.

## Setup

Python 3.10–3.13 and [uv](https://docs.astral.sh/uv/).

```
uv sync
```

Requires `.env.france` and `.env.us-friend` in the project root (not
tracked in git — see `.gitignore`).

## Usage

Preview who would be called, without dialing anything:
```
uv run python dialer.py --campaign us-friend --dry-run
```

Start the agent worker (one terminal, per campaign):
```
CAMPAIGN=us-friend uv run python coldcall_agent.py dev
```

Dispatch calls (another terminal):
```
uv run python dialer.py --campaign us-friend --limit 5
```

Test the voice pipeline locally through your own mic/speakers — no Twilio
call, no per-minute cost — via LiveKit's console mode:
```
CAMPAIGN=us-friend uv run python coldcall_agent.py console
```
or use the helper: `./voice-enhancement/test-voice.ps1 us-friend`

## Safety features

- **Calling hours**: `dialer.py` refuses to dial outside Mon–Fri
  9:30–12:30 / 14:00–18:30 (campaign-local timezone) unless `--ignore-hours`
  is passed for a deliberate test call.
- **Do-not-call list**: any lead previously logged as `not_interested` or
  `do_not_call` is permanently skipped — `CALL_LOG_PATH` is the source of
  truth, kept separate per campaign.
- **Campaign isolation**: `CAMPAIGN` must be set explicitly (no default),
  so the wrong campaign's persona/trunk/leads can never run by accident.
- **Dry-run**: `--dry-run` always previews the call list without dialing.

## One-time infra scripts

- `setup_twilio_trunk.py --env .env.<campaign>` — create the Twilio-side
  Elastic SIP Trunk for a Twilio account that doesn't have one yet.
- `setup_sip_trunk.py --campaign <campaign>` — register that Twilio trunk
  as a LiveKit outbound trunk.
- `trust_hub_check_account.py` / `trust_hub_list_policies.py` /
  `trust_hub_policy_details.py` — Twilio Trust Hub / regulatory bundle
  diagnostics (`--env .env.<campaign>`, defaults to `.env.france`).
