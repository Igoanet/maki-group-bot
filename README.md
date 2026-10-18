<p align="center"><img src="assets/banner.svg" alt="Maki Group Bot" width="940"></p>
<p align="center">
  <img alt="license" src="https://img.shields.io/badge/license-MIT-2ea043?style=for-the-badge">
  <img alt="python" src="https://img.shields.io/badge/Python-3.11%2B-3776AB?style=for-the-badge&logo=python&logoColor=white">
  <img alt="telegram" src="https://img.shields.io/badge/Telegram-Bot-26A5E4?style=for-the-badge&logo=telegram&logoColor=white">
  <img alt="status" src="https://img.shields.io/badge/status-active-2ea043?style=for-the-badge">
</p>

<h3 align="center">The moderation suite that keeps a busy Telegram group readable.</h3>

<p align="center"><i>300+ commands · 43 functions · every one documented in the in-bot help menu.</i></p>

---

## Why this bot

Most group bots ship a wall of commands and hope you find them. Maki is built
the other way round: everything is reachable from one flat menu, the replies
are short, and the destructive actions are the ones that ask before they fire.

## Features

| | Area | What you get |
|---|---|---|
| 🛡 | **Moderation** | ban, kick, mute, warn with limits, purge, pin, promote tiers |
| 🔒 | **Protection** | locks, blocklists, antiflood, shield, link deleter, edit guard, CAPTCHA |
| 👋 | **Members** | greetings, join requests, voice-chat join notices, tag-all |
| 📝 | **Content** | notes, filters, rules, formatting, buttons, quote stickers |
| 🧹 | **Cleanup** | auto-delete, clean commands, clean service messages |
| 📨 | **Reports** | `@admin` alerts every admin — in the group and in their DM, with the original message |
| 🤝 | **Trust** | the owner marks members *edit-free*, and can delegate that power to an **editor** |
| ⚙️ | **Owner** | federation bans, sudo list, broadcast, group console |

## Design choices

- **One flat help menu.** Every function is a button, three per row, ordered by
  how often an admin actually needs it. No nesting, no dead ends.
- **Short messages.** One idea per line, real gaps between blocks, no walls of text.
- **Ephemeral replies where it matters** — a member-facing card is visible only to
  the person who asked, instead of pinging the whole group.
- **Collapsible lists.** Long command lists fold into a Telegram expandable quote.
- **Verify before act.** An anonymous admin's command is held back until they prove
  their identity, then runs as them.
- **Explicit trust.** Edit Guard exemptions are a named list the owner controls.

## Quick start

```bash
git clone https://github.com/<you>/maki-group-bot && cd maki-group-bot
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env          # then fill it in
python3 -m ghelp
```

Add the bot to a group and promote it with **Ban users · Delete messages ·
Pin messages · Change group info · Invite users**, then send `/help`.

## Configuration

| Variable | Purpose |
|---|---|
| `API_ID`, `API_HASH` | Telegram app credentials |
| `BOT_TOKEN` | the bot token from @BotFather |
| `OWNER_ID` | your numeric Telegram id |
| `LOGGER_ID` | optional log channel |
| `GH_BOT_NAME` | name shown in the help header |

## Security

`.env`, `*.session`, cookies and all runtime data are excluded by
`.gitignore`. **A `.session` file grants full account access** — treat it like a
password. Never commit one, and rotate your token if one ever leaks.

## License

MIT — see [LICENSE](LICENSE).
