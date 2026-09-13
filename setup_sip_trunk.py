"""One-time script: register a campaign's Twilio SIP trunk as a LiveKit
outbound trunk, so coldcall_agent.py / dialer.py can dial real phone numbers.

Fill in TWILIO_SIP_TRUNK_HOSTNAME / _USERNAME / _PASSWORD / TWILIO_TRUNK_NUMBER
in .env.<campaign> first (these are only read here, not by the agent/dialer
afterward), then run:

    uv run python setup_sip_trunk.py --campaign france
    uv run python setup_sip_trunk.py --campaign us-friend

Copy the printed trunk_id into SIP_OUTBOUND_TRUNK_ID in that same .env file.
"""

import argparse
import asyncio
import os

from dotenv import load_dotenv
from livekit import api
from livekit.protocol.sip import SIPOutboundTrunkInfo


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--campaign", required=True, choices=["france", "us-friend"], help="which campaign config (.env.<campaign>) to register a trunk for")
    args = parser.parse_args()

    load_dotenv(f".env.{args.campaign}")

    hostname = os.environ["TWILIO_SIP_TRUNK_HOSTNAME"]
    username = os.environ.get("TWILIO_SIP_TRUNK_USERNAME") or None
    password = os.environ.get("TWILIO_SIP_TRUNK_PASSWORD") or None
    trunk_number = os.environ["TWILIO_TRUNK_NUMBER"]  # E.164, the Caller ID this trunk uses

    async with api.LiveKitAPI(
        url=os.environ["LIVEKIT_URL"],
        api_key=os.environ["LIVEKIT_API_KEY"],
        api_secret=os.environ["LIVEKIT_API_SECRET"],
    ) as lkapi:
        trunk = await lkapi.sip.create_outbound_trunk(
            api.CreateSIPOutboundTrunkRequest(
                trunk=SIPOutboundTrunkInfo(
                    name=f"immoops-coldcaller-outbound-{args.campaign}",
                    address=hostname,
                    numbers=[trunk_number],
                    auth_username=username,
                    auth_password=password,
                )
            )
        )
        print(f"Created outbound trunk: {trunk.sip_trunk_id}")
        print(f"Save this as SIP_OUTBOUND_TRUNK_ID in .env.{args.campaign}")


if __name__ == "__main__":
    asyncio.run(main())
