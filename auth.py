"""
auth.py — Interactive Telegram login to create autosound.session

Run once locally:
    python auth.py
"""

from telethon.sync import TelegramClient
import os
from dotenv import load_dotenv

load_dotenv()

api_id = int(os.environ["TELEGRAM_API_ID"])
api_hash = os.environ["TELEGRAM_API_HASH"]

with TelegramClient("autosound", api_id, api_hash) as client:
    client.start()
    me = client.get_me()
    print(f"Logged in as: {me.first_name} (@{me.username})")
    print("autosound.session created successfully.")
