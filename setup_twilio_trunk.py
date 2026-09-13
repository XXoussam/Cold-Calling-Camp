"""One-time script: create a Twilio Elastic SIP Trunk (Termination side) on a
given Twilio account, so LiveKit can send outbound calls through it.

This is the Twilio-side counterpart to setup_sip_trunk.py (which registers
the LiveKit-side outbound trunk against an *already-existing* Twilio trunk).
Run this first when onboarding a Twilio account that has no SIP trunk yet.

    uv run python setup_twilio_trunk.py --env .env.us-friend \
        --domain immoops-friend --number +16065032247 \
        --username immoops-friend --password <choose-a-strong-password>

Prints the values to paste into TWILIO_SIP_TRUNK_HOSTNAME/_USERNAME/_PASSWORD
and TWILIO_TRUNK_NUMBER in the target .env.<campaign> file. After that, run
setup_sip_trunk.py --campaign <campaign> to register the LiveKit-side trunk.

Only two campaign env files exist: .env.france (your own Twilio account) and
.env.us-friend (your friend's Twilio account) — pass one of those as --env.
"""

import argparse
import os

from dotenv import load_dotenv
from twilio.rest import Client

parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
parser.add_argument("--env", required=True, help="which env file to load Twilio account credentials from: .env.france or .env.us-friend")
parser.add_argument("--domain", required=True, help="unique subdomain for the trunk, e.g. 'immoops-friend' -> immoops-friend.pstn.twilio.com")
parser.add_argument("--number", required=True, help="E.164 phone number already on this Twilio account to attach as the trunk's outbound Caller ID")
parser.add_argument("--username", required=True, help="SIP credential username LiveKit will authenticate with")
parser.add_argument("--password", required=True, help="SIP credential password LiveKit will authenticate with")
args = parser.parse_args()

load_dotenv(args.env)

client = Client(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])

matching_numbers = [n for n in client.incoming_phone_numbers.list() if n.phone_number == args.number]
if not matching_numbers:
    raise SystemExit(f"{args.number} not found on this Twilio account — check --number and --env.")
number_sid = matching_numbers[0].sid

credential_list = client.sip.credential_lists.create(friendly_name=f"{args.domain}-credentials")
credential_list.credentials.create(username=args.username, password=args.password)
print(f"Created Credential List: {credential_list.sid}")

trunk = client.trunking.v1.trunks.create(
    friendly_name=f"immoops-coldcaller-{args.domain}",
    domain_name=f"{args.domain}.pstn.twilio.com",
)
print(f"Created Trunk: {trunk.sid} ({trunk.domain_name})")

trunk.credentials_lists.create(credential_list_sid=credential_list.sid)
trunk.phone_numbers.create(phone_number_sid=number_sid)
print(f"Attached number {args.number} and credential list to the trunk.")

print()
print("Paste into .env.<campaign>:")
print(f"TWILIO_SIP_TRUNK_HOSTNAME={trunk.domain_name}")
print(f"TWILIO_SIP_TRUNK_USERNAME={args.username}")
print(f"TWILIO_SIP_TRUNK_PASSWORD={args.password}")
print(f"TWILIO_TRUNK_NUMBER={args.number}")
