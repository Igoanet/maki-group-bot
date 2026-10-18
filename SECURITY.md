# Security

## Secrets

Never commit:
- `.env` - the bot token and API credentials
- `*.session` / `*.session-journal` - **full account access**
- `cookies.txt`
- `data/` - the runtime database

`.gitignore` blocks all of these. `git add -f` overrides it - do not use it.

If a session file or token reaches a remote host, treat that account as
compromised: revoke the token with @BotFather and terminate the session from
Telegram's *Active Sessions*.

## Reporting

Open a private security advisory, or contact the maintainer directly. Please
do not open a public issue for a vulnerability.

## Design rules this project follows

- Every destructive action re-reads its own state immediately before acting.
- Authorisation is verified live (`get_chat_member`), never from a cached
  admin list.
- A cosmetic failure must never be escalated into a destructive one.
- The bot never stores message content beyond what a feature needs.
