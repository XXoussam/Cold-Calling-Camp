# Voice enhancement

Workspace for tuning the agent's voice (ElevenLabs voice settings, gain,
speed, STT/turn-detection feel) without spending Twilio call minutes.

## Test in the terminal (no phone call, no Twilio cost)

`coldcall_agent.py` is a LiveKit agent, and LiveKit's CLI has a `console`
mode that runs the full pipeline (STT → LLM → TTS) locally using your own
mic/speakers instead of dialing out over SIP. No `Phone` is ever present in
a console session, so `coldcall_agent.py`'s SIP-dialing code path
(`ctx.add_sip_participant`) never even runs — this is fully safe to run
against `.env.us-friend` too, it will never place a real call.

Run from the project root (not from this folder), one campaign at a time:

```
CAMPAIGN=france uv run python coldcall_agent.py console
CAMPAIGN=us-friend uv run python coldcall_agent.py console
```

Or use the helper script below, run from anywhere:

```
./voice-enhancement/test-voice.ps1 france
./voice-enhancement/test-voice.ps1 us-friend
```

Add `--record` to save the session audio for review:

```
CAMPAIGN=france uv run python coldcall_agent.py console --record
```

Recordings land in `console-recordings/` at the project root.

## What this does and doesn't save you

- **Saves**: Twilio per-minute call charges, and the hassle of actually
  ringing a phone for every tuning iteration.
- **Still costs a little**: OpenAI (LLM), Deepgram (STT), and ElevenLabs
  (TTS) are all still real API calls in console mode — just no telephony
  leg. Fine for iterating, just not literally free.

## Diagnosis log (2026-09-13)

Hard ceiling: Twilio only supports G.711 μ-law 8kHz for PSTN (no G.722/HD —
that's Telnyx-only). Goal is to stop adding damage on top of that ceiling,
not to beat it.

**Already fixed** (before this session): `encoding` was the ElevenLabs
plugin default `mp3_22050_32` (32kbps MP3 — the "swishy/watery" culprit in
most write-ups on this). Already changed to `pcm_16000` a while back — see
the comment on that line in `coldcall_agent.py`. So if audio is still
swishy, it's not the container format anymore.

**Applied this session** (`coldcall_agent.py`, untested by ear yet):
- `language=AGENT_LANGUAGE` and `apply_text_normalization="on"` added to
  the ElevenLabs TTS call — was implicit/`"auto"`, numbers and addresses
  should read more reliably now.
- `style` dropped from 0.35 → 0.0, `stability` raised from 0.45 → 0.55.
  Rationale: ElevenLabs' `style` exaggeration adds breathiness that turns
  to mush once downsampled to 8kHz. This reverses an earlier by-ear tuning
  pass (0.35 was chosen to fix a "robotic" complaint), so **A/B test this
  before trusting it** — run `test-voice.ps1` against both this and the old
  values (0.45 / 0.35) and compare.

**Resolved (2026-09-13):**
1. **ElevenLabs voice/model** — confirmed sorted (isolation test / voice
   choice, done by ear). No longer an open item.
2. **Network/Twilio transport** — verified via Twilio Voice Insights on a
   real `us-friend` test call (`CA82f8755f753ec8bb287414ee5aaf29dc`):
   0.0% packet loss both legs, negligible jitter (<1ms avg), ~34ms
   latency per edge. The US-account-calling-French-number distance is
   **not** the problem — transport is clean despite the geography
   (LiveKit media server in Germany → Twilio Ashburn (US) → Twilio Dublin
   → French carrier). Only structural thing surfaced: Twilio transcodes
   μ-law↔A-law between the two legs, which is standard/near-lossless for
   a US trunk calling a European number, not a fixable issue.
3. **Symmetric RTP** — dropped. 0% packet loss on the sip_edge rules out
   the NAT-misrouting theory this was meant to fix, and Twilio's own docs
   recommend leaving it disabled (more secure) absent evidence of a NAT
   problem. Also turned out not to be toggleable via the API anyway
   (Console-only, if ever needed).
4. **Krisp/noise-cancellation double-processing** — checked the Twilio
   Elastic SIP Trunk resource via the API; it exposes no
   noise-cancellation/Krisp field at all (only `secure`, `recording`,
   `symmetric_rtp_enabled`, `transfer_mode`, `cnam_lookup_enabled`,
   `auth_type`). That product appears to apply to Twilio's Voice Client
   SDKs, not Elastic SIP Trunking termination — likely not applicable to
   this architecture at all. `noise_cancellation.BVC()` in
   `coldcall_agent.py` remains the only noise-cancellation in this
   pipeline; no evidence of double-processing.

**Still open, only if quality genuinely isn't good enough now:**
- Moving the trunk to Telnyx for G.722 (HD voice) is the only way past
  the 8kHz PSTN ceiling — a bigger infra change, worth it only if the
  above turns out not to be enough.

## Where the knobs are

All in `coldcall_agent.py`, near the top and in the `AgentSession` setup:

- `TTS_GAIN` — digital gain applied to the agent's voice (env var, default 1.4)
- `TTS_SPEED` — ElevenLabs speech pace (env var, default per-campaign in `.env.<campaign>`)
- `VoiceSettings(stability=..., similarity_boost=..., style=...)` — ElevenLabs voice character
- `elevenlabs.TTS(voice_id=..., model=...)` — which voice/model
