# GroupHelp-style group-management bot — env configuration.
# Launched by bots/run_all.sh with BOT_TOKEN mapped from GROUPHELP_BOT_TOKEN.

from os import getenv
from pathlib import Path


class Config:
    def __init__(self):
        self.API_ID = int(getenv("API_ID", 0))
        self.API_HASH = getenv("API_HASH")
        self.BOT_TOKEN = getenv("BOT_TOKEN")
        try:
            self.OWNER_ID = int(getenv("OWNER_ID", 0))
        except ValueError:
            self.OWNER_ID = 0

        # Accounts Maki must never ban, kick or restrict — its own voice-chat
        # monitoring assistant lives here (2026-09-19).
        self.PROTECTED_IDS = {
            int(x)
            for x in getenv("PROTECTED_IDS", "6189989810").replace(" ", ",").split(",")
            if x.strip().isdigit()
        }

        self.BOT_NAME = getenv("GH_BOT_NAME", "GroupHelp")
        self.SUPPORT_CHAT = getenv("SUPPORT_CHAT", "https://t.me/toolatetobloom")
        self.UPDATES_CHANNEL = getenv(
            "UPDATES_CHANNEL", "https://t.me/TooLateToBloom"
        )
        # start panel: Support and Owner both open the owner (same person)
        self.OWNER_LINK = getenv("OWNER_LINK", "https://t.me/RohanDevta")

        # start-panel poster shown in private chat; falls back to text if missing
        self.START_IMG = str(Path(__file__).resolve().parent / "assets" / "start.jpg")

        # flood defaults (per chat, overridable via /setflood)
        self.DEFAULT_FLOOD_LIMIT = 5
        # anti-raid: joins per 10s that trip a raid lock
        self.RAID_JOIN_LIMIT = 8


config = Config()
