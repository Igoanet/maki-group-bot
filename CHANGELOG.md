# Changelog

## Unreleased

### Added
- `/editfree` and `/editor` - edit-free members and the editors who may grant
  the role. `/trust` and `/exempt` kept as older names.
- Member tags (`/tag`) using Telegram Bot API 9.5 `setChatMemberTag`.
- `/modern` - reports which newer Bot API features the running server supports.
- Ephemeral replies (Bot API 10.2) - a member-facing card is visible only to
  the member who asked.
- Collapsible command lists via `<blockquote expandable>`.
- Assistant group-guard is documented as a separate concern from this bot.

### Fixed
- Help buttons sent a **new message** instead of editing in place (a swallowed
  `TypeError` on the edit path).
- `@admin` reports notified the bot owner for groups they do not administer.
- Link detection treated an `@username` mention as a posted link, so innocent
  messages were deleted.
- Anonymous admins could run admin commands without being identified.
- Help showed the template name `GroupHelp` instead of the configured name.

### Changed
- Help is a single flat menu; no categories.
- Buttons ordered by importance, three per row, no emoji.
- "Edit Guard" corrected to "Edit Guardian" in all user-facing text.
