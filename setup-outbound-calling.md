# Setting up outbound calling (LiveKit + Twilio)

One-time infrastructure setup so `coldcall_agent.py` / `dialer.py` can place real
phone calls. Do this once, in order. Nothing here places a call — the only live
test call happens in Part 6, and it dials **you**, not a real lead.

**Context:** a dedicated LiveKit Cloud project named **`cold-calls`** was created
just for this system (separate from the `oussna-voice-ai` project, which was only
used as architectural inspiration and has no SIP setup of its own). `.env.local`
in this workspace already points at `cold-calls` via `LIVEKIT_URL` /
`LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET`. A new French (+33) Twilio number is
being purchased for this too, so the Twilio trunk below is being built fresh,
not reusing an existing one.

## Part 1 — Twilio: buy a French phone number

Buy a Twilio phone number with Voice capability, **+33 (France)**. This becomes
the outbound Caller ID — a French number will get picked up by French agencies
far more than a foreign one would.

## Part 2 — Twilio: create the Elastic SIP Trunk

In the Twilio console: Elastic SIP Trunking → create a new trunk.

1. **Termination** tab → note the **Termination SIP URI** it gives you
   (looks like `your-trunk-name.pstn.twilio.com`). This is
   `TWILIO_SIP_TRUNK_HOSTNAME`.
2. **General settings** → set the French number from Part 1 as this trunk's
   **outbound Caller ID**. Without this, Twilio silently rejects outbound calls.

## Part 3 — Twilio: authentication

Still on the trunk → **Authentication**. Use a **Credential List** (username +
password) — our setup script only supports credential auth, not IP ACL.

1. Create a Credential List with a username/password of your choosing.
2. Attach it to the trunk's Authentication settings.
3. These become `TWILIO_SIP_TRUNK_USERNAME` / `TWILIO_SIP_TRUNK_PASSWORD`.

## Part 4 — Fill in `.env.local`

Directly in the file, never in chat:

```
TWILIO_SIP_TRUNK_HOSTNAME=your-trunk-name.pstn.twilio.com
TWILIO_SIP_TRUNK_USERNAME=...
TWILIO_SIP_TRUNK_PASSWORD=...
OUSSAMA_PHONE_NUMBER=+33...        # your real phone, E.164 format
```

(`LIVEKIT_URL` / `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET` should already be
filled in, pointing at the `cold-calls` LiveKit project.)

## Part 5 — Register the trunk with LiveKit

Once Parts 1–4 are done, run:

```
uv run python setup_sip_trunk.py
```

This calls LiveKit's API (not a phone call) and prints a `trunk_id`. Copy it
into `SIP_OUTBOUND_TRUNK_ID` in `.env.local`.

## Part 6 — First real test call (to yourself, not a lead)

Start the agent worker in one terminal:

```
uv run python coldcall_agent.py dev
```

Then, in another terminal:

```
uv run python dialer.py --limit 1 --to +33<your own number> --ignore-hours
```

This dials **you** — so you hear exactly what a prospect would hear, before
anything touches the 79 real leads in `leads/leads-filtered-by-region.json`.

## Part 7 — Only after Part 6 works

Small real batch, one region at a time:

```
uv run python dialer.py --limit 3 --region "Valbonne / Sophia Antipolis"
```

Check `leads/call-log.jsonl` afterward to confirm outcomes were logged
correctly.
