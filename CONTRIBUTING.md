# Contributing

## Adding a plugin

1. Create `ghelp/plugins/yourfeature.py`.
2. Import the shared helpers, do not re-implement them:

```python
from pyrogram import enums, filters, types
from ghelp import app
from ghelp.database import db
from ghelp.helpers import ikb, ikm, mention, require_admin

PRE = ["/", "?", "!", "."]
```

3. Register handlers on `app`, always with a prefix list:

```python
@app.on_message(filters.group & filters.command("mycmd", prefixes=PRE))
async def mycmd(_, message: types.Message) -> None:
    ...
```

4. **Document it.** Add an entry to `HELP_SECTIONS` in
   `ghelp/plugins/basic.py`. A command that is not in help is a bug.

## The checks every change must pass

```bash
python3 -m compileall -q ghelp          # syntax
python3 -m pyflakes ghelp | grep -v "unable to detect"   # undefined names
```

Then confirm on a running instance:

- the help crawler reports `problems: NONE`
- the command count matches the documented count
- no command is registered twice **in the same dispatcher group** (the second
  registration is silently dead - this is the most common real bug in this
  codebase)

## House style

- Messages are **short**: one idea per line, real gaps, no walls of text.
- Buttons carry **no emoji** in the help grid.
- Never hard-code a prefix list; use `PRE`.
- Never leave a bare `except Exception: pass` without a `logger` line - a
  swallowed error cost this project a whole broken feature once.
- Use `logger.info` for expected conditions, `logger.warning` for real faults.
