"""One-time diagnostic: fetch a Trust Hub policy's detailed requirements.

    uv run python trust_hub_policy_details.py RN6433641899984f951173ef1738c3bdd0
    uv run python trust_hub_policy_details.py --env .env.us-friend RN6433641899984f951173ef1738c3bdd0
"""

import argparse
import json
import os

from dotenv import load_dotenv
from twilio.rest import Client

parser = argparse.ArgumentParser()
parser.add_argument("--env", default=".env.france", help="which env file to load credentials from")
parser.add_argument("policy_sid")
args = parser.parse_args()

load_dotenv(args.env)

policy_sid = args.policy_sid
client = Client(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])

policy = client.trusthub.v1.policies(policy_sid).fetch()
print(f"sid: {policy.sid}")
print(f"friendly_name: {policy.friendly_name}")
print("requirements:")
print(json.dumps(policy.requirements, indent=2, ensure_ascii=False))
