"""One-time diagnostic: list available Trust Hub policies so we can identify
the correct PolicySid for a Business/Organization Primary Customer Profile
(needed to unblock +1 outbound calling — see error 32203).

    uv run python trust_hub_list_policies.py
    uv run python trust_hub_list_policies.py --env .env.us-friend
"""

import argparse
import os

from dotenv import load_dotenv
from twilio.rest import Client

parser = argparse.ArgumentParser()
parser.add_argument("--env", default=".env.france", help="which env file to load credentials from")
args = parser.parse_args()

load_dotenv(args.env)

client = Client(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])

policies = client.trusthub.v1.policies.list()
for p in policies:
    print(f"{p.sid}  |  {p.friendly_name}")
