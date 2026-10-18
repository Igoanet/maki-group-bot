# Architecture

Maki is a **plugin-first** Telegram group-management bot. The core stays tiny;
every feature is a self-registering plugin.

## Runtime shape

```mermaid
graph TD
    TG[Telegram] -->|updates| P[Pyrogram Client]
    P --> D[Dispatcher]
    D --> R[RateGuard middleware]
    R --> PL[Plugins  -  57 modules]

    PL --> H[helpers: ikb / ikm / mention / require_admin]
    PL --> DB[(SQLite  -  ghelp.database)]
    PL --> SD[selfdel  -  auto-delete replies]
    PL --> LG[logger / audit log]

    subgraph Core modules
      CL[chat_lifecycle]
      VD[vc_monitor_store]
      BD[bulk_delete engine]
    end
    PL --> CL
    PL --> VD
    PL --> BD
```

## Layering

| Layer | Responsibility | Rule |
|---|---|---|
| `ghelp/__init__.py` | builds the `app` client | no feature logic |
| `ghelp/plugins/*.py` | one feature each | registers its own handlers |
| `ghelp/helpers.py` | shared UI + permission helpers | no feature state |
| `ghelp/database.py` | SQLite access, `DEFAULTS` schema | single source of truth |
| `ghelp/rateguard.py` | per-user flood protection | wraps every handler |

**Why this matters:** a plugin that fails to import cannot take the bot down
with it, and features are added by dropping in a file rather than editing core.

## The help system

```mermaid
flowchart LR
    A[/help/] --> B[HELP_SECTIONS dict]
    B --> C[_ordered_function_keys]
    C --> D[_help_keyboard - 3 per row]
    D --> E[tap a function]
    E --> F[_section_text - paged]
    F --> G[collapsible blockquote]
    F --> H[nav + Back]
```

- One flat menu, **no nesting** - every function is a button.
- Order is **explicit** (`_ordered_function_keys`), most-used first; anything
  unlisted is appended so a new feature can never vanish.
- Long command lists fold into `<blockquote expandable>`.
- A test crawls all 82 help screens and fails on a dead callback, an
  over-length page, an unbalanced tag, or an emoji left on a button.

## Report flow (`@admin`)

```mermaid
sequenceDiagram
    participant U as Member
    participant B as Maki
    participant G as Group
    participant A as Admins DM
    U->>B: @admin  (reply to the problem)
    B->>G: name + italic "Report sent" + tags ONLY admins
    B->>A: ATTENTION card, quoted message, Go-to-message link
    B->>A: forwarded copy of the original
    A->>B: taps "Solved"
    B->>A: card rewritten as Solved
```

Recipients are **only that group's admins**. The reporter is never notified
back, and the bot owner is not copied into groups they do not administer.

## Verify-before-act (anonymous admins)

An anonymous admin has no `from_user`, so identity is unknown. Maki **holds the
command back** rather than letting it run:

```mermaid
flowchart TD
    M[anon admin sends /ban] --> C{power use?}
    C -->|no| P[pass through]
    C -->|yes| V{already verified?}
    V -->|yes| R[run normally]
    V -->|no| S[StopPropagation - command blocked]
    S --> B[Verify button]
    B --> D{real admin?}
    D -->|no| X[refused]
    D -->|yes| I[impersonate + re-dispatch the withheld command]
```

The re-dispatched message is given the verified user's identity so every
decorator (`language`, admin checks, mention rendering) sees a real member.

## Edit Guardian trust

Three roles, deliberately separated:

| Role | May do | Granted by |
|---|---|---|
| **edit-free** member | edits are never deleted | owner or an editor |
| **editor** | run `/editfree` on others | owner or an editor |
| owner | everything, including who may be an editor | Telegram |

`/editfree`, `/editor`. `/trust` and `/exempt` remain as older names.
An exemption is checked **before** the admin test, so it wins even when admin
coverage is enabled.

## Data model

Single SQLite file, one `chats` row per group, with a `DEFAULTS` schema that
migrates forward automatically. Feature settings live as flat keys
(`editguard_on`, `editguard_window`, `editguard_exempt`, ...) so a new setting
never needs a migration.

## Failure philosophy

- A plugin import failure is contained to that plugin.
- Cosmetic failures (a message edit, a `FloodWait` on a panel) must never be
  treated as a feature failure.
- Destructive actions re-read state immediately before acting.
- Every background task is a named `asyncio` task with its own try/except.
