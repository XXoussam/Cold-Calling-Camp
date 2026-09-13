"""Check whether a Twilio account is likely able to place outbound +1 calls:
does it have an approved Primary Customer Profile, and what's its balance?

    uv run python trust_hub_check_account.py                    # your own account (.env.france)
    uv run python trust_hub_check_account.py --env .env.us-friend  # friend's account
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

account = client.api.v2010.accounts(os.environ["TWILIO_ACCOUNT_SID"]).fetch()
print(f"Account: {account.friendly_name} ({account.sid})")
print(f"Status: {account.status}")

balance = client.api.v2010.accounts(os.environ["TWILIO_ACCOUNT_SID"]).balance.fetch()
print(f"Balance: {balance.balance} {balance.currency}")

print()
print("Customer Profiles:")
profiles = client.trusthub.v1.customer_profiles.list()
if not profiles:
    print("  none found")
for p in profiles:
    print(f"  {p.sid} | {p.friendly_name} | status: {p.status}")

print()
print("Phone numbers with Voice capability:")
numbers = client.incoming_phone_numbers.list()
for n in numbers:
    voice = n.capabilities.get("voice") if n.capabilities else None
    print(f"  {n.phone_number} | voice capable: {voice}")
