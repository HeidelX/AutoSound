"""
gen_session.py — Generate a Telethon StringSession for GitHub Actions.

Run once locally:
    python gen_session.py

Then paste the printed string into the TELEGRAM_SESSION GitHub secret.
"""

from telethon.sync import TelegramClient
from telethon.sessions import StringSession
import os
from dotenv import load_dotenv

load_dotenv()

api_id = int(os.environ["TELEGRAM_API_ID"])
api_hash = os.environ["TELEGRAM_API_HASH"]

with TelegramClient(StringSession(), api_id, api_hash) as client:
    client.start()
    me = client.get_me()
    print(f"\nLogged in as: {me.first_name} (@{me.username})")
    print("\n=== TELEGRAM_SESSION (copy this into your GitHub secret) ===")
    print(client.session.save())
    print("=============================================================\n")
