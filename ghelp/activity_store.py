"""Local, atomic Chat Fight counters. Stores metadata/counts, never message text."""

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
DEFAULT_PATH = Path(__file__).resolve().parents[1] / "data" / "chatfight.sqlite3"
PERIODS = {"day": "Today", "week": "This week", "all": "All time"}
# The values in this tuple are Telegram message fields, rather than labels
# shown to members.  Keep this allow-list small and explicit: an old row
# cannot be assigned a type after the fact, and an unrecognised new message
# must remain unclassified too.
MEDIA_KINDS = (
    "text", "photo", "sticker", "animation", "video", "document",
    "video_note", "voice", "audio", "contact", "location", "venue",
    "poll", "dice", "game", "invoice", "web_page",
)
MEDIA_LABELS = {
    "text": "Text",
    "photo": "Photo",
    "sticker": "Sticker",
    "animation": "GIF",
    "video": "Video",
    "document": "Document",
    "video_note": "Video note",
    "voice": "Voice",
    "audio": "Audio",
    "contact": "Contact",
    "location": "Location",
    "venue": "Venue",
    "poll": "Poll",
    "dice": "Dice",
    "game": "Game",
    "invoice": "Invoice",
    "web_page": "Link preview",
    "unknown": "Unclassified",
}
PAGE_SIZE = 10
MAX_PAGE = 10000


def utc(value=None):
    value = value or datetime.now(timezone.utc)
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class ActivityStore:
    def __init__(self, path=DEFAULT_PATH):
        self.path = Path(path)
        self._connection = None
        self._lock = asyncio.Lock()

    def _connect(self):
        if self._connection is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
            connection.row_factory = sqlite3.Row
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.executescript("""
                    CREATE TABLE IF NOT EXISTS activity_chats (
                        chat_id INTEGER PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1,
                        started_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS activity_members (
                        chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                        name TEXT NOT NULL, total INTEGER NOT NULL DEFAULT 0,
                        PRIMARY KEY(chat_id, user_id)
                    );
                    CREATE TABLE IF NOT EXISTS activity_daily (
                        chat_id INTEGER NOT NULL, day TEXT NOT NULL,
                        user_id INTEGER NOT NULL, count INTEGER NOT NULL,
                        PRIMARY KEY(chat_id, day, user_id)
                    );
                    CREATE TABLE IF NOT EXISTS activity_seen (
                        chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
                        PRIMARY KEY(chat_id, message_id)
                    );
                    CREATE TABLE IF NOT EXISTS activity_group_daily (
                        chat_id INTEGER NOT NULL, day TEXT NOT NULL,
                        count INTEGER NOT NULL, last_milestone INTEGER NOT NULL DEFAULT 0,
                        PRIMARY KEY(chat_id, day)
                    );
                    CREATE TABLE IF NOT EXISTS activity_media_daily (
                        chat_id INTEGER NOT NULL, day TEXT NOT NULL,
                        user_id INTEGER NOT NULL, kind TEXT NOT NULL,
                        count INTEGER NOT NULL,
                        PRIMARY KEY(chat_id, day, user_id, kind)
                    );
                """)
            except Exception:
                connection.close()
                raise
            self._connection = connection
        return self._connection

    async def _run(self, fn, *args):
        async with self._lock:
            work = asyncio.create_task(asyncio.to_thread(fn, *args))
            try:
                return await asyncio.shield(work)
            except asyncio.CancelledError:
                # A cancelled coroutine cannot cancel an active SQLite thread.
                # Keep the connection lock until its transaction has finished.
                await work
                raise

    def _chat(self, con, chat_id, now):
        con.execute(
            "INSERT OR IGNORE INTO activity_chats(chat_id,started_at) VALUES (?,?)",
            (chat_id, utc(now).isoformat()),
        )
        return con.execute(
            "SELECT * FROM activity_chats WHERE chat_id=?", (chat_id,)
        ).fetchone()

    async def record(self, chat_id, message_id, user_id, name, sent_at=None,
                     media_kind=None, *, media_type=None, kind=None, milestone=False):
        if any(isinstance(n, bool) or not isinstance(n, int) for n in (chat_id, message_id, user_id)):
            return False
        if chat_id >= 0 or message_id < 1 or user_id < 1:
            return False
        if media_kind is None:
            media_kind = media_type
        if media_kind is None:
            media_kind = kind
        if media_kind not in MEDIA_KINDS:
            # This includes None and the explicit "unknown" label.  There is
            # no honest type to persist for either case; the normal total is
            # still recorded and the ranking code derives an unclassified
            # remainder without inventing a historical type.
            media_kind = None
        return await self._run(
            self._record, chat_id, message_id, user_id, str(name or user_id)[:80],
            utc(sent_at), media_kind, milestone
        )

    def _record(self, chat_id, message_id, user_id, name, sent_at, media_kind=None,
                milestone=False):
        con = self._connect()
        with con:
            chat = self._chat(con, chat_id, sent_at)
            inserted = con.execute(
                "INSERT OR IGNORE INTO activity_seen VALUES (?,?)", (chat_id, message_id)
            ).rowcount
            if not inserted or not chat["enabled"]:
                return False
            con.execute("""
                INSERT INTO activity_members VALUES (?,?,?,1)
                ON CONFLICT(chat_id,user_id) DO UPDATE SET
                    name=excluded.name, total=activity_members.total+1
            """, (chat_id, user_id, name))
            con.execute("""
                INSERT INTO activity_daily VALUES (?,?,?,1)
                ON CONFLICT(chat_id,day,user_id) DO UPDATE SET count=activity_daily.count+1
            """, (chat_id, sent_at.astimezone(IST).date().isoformat(), user_id))
            if media_kind is not None:
                con.execute("""
                    INSERT INTO activity_media_daily VALUES (?,?,?,?,1)
                    ON CONFLICT(chat_id,day,user_id,kind)
                    DO UPDATE SET count=activity_media_daily.count+1
                """, (chat_id, sent_at.astimezone(IST).date().isoformat(),
                      user_id, media_kind))
            day = sent_at.astimezone(IST).date().isoformat()
            # Seed existing installations from their actual counts once per day.
            # The current message is already included in activity_daily.
            existing = con.execute("""
                SELECT 1 FROM activity_group_daily WHERE chat_id=? AND day=?
            """, (chat_id, day)).fetchone()
            if existing is None:
                con.execute("""
                    INSERT INTO activity_group_daily(chat_id,day,count)
                    SELECT ?,?,COALESCE(SUM(count),0) FROM activity_daily
                    WHERE chat_id=? AND day=?
                """, (chat_id, day, chat_id, day))
            else:
                con.execute("""
                    UPDATE activity_group_daily SET count=count+1
                    WHERE chat_id=? AND day=?
                """, (chat_id, day))
            row = con.execute("""
                SELECT count,last_milestone FROM activity_group_daily
                WHERE chat_id=? AND day=?
            """, (chat_id, day)).fetchone()
            reached = 0
            if milestone and row["count"] % 500 == 0 and row["count"] > row["last_milestone"]:
                reached = row["count"]
                # Claim before sending: an ambiguous Telegram timeout must not
                # cause duplicate announcements after replay/restart.
                con.execute("""
                    UPDATE activity_group_daily SET last_milestone=?
                    WHERE chat_id=? AND day=?
                """, (reached, chat_id, day))
        return reached if milestone else True

    async def set_enabled(self, chat_id, enabled):
        return await self._run(self._set_enabled, chat_id, bool(enabled))

    def _set_enabled(self, chat_id, enabled):
        con = self._connect()
        with con:
            self._chat(con, chat_id, utc())
            con.execute("UPDATE activity_chats SET enabled=? WHERE chat_id=?", (int(enabled), chat_id))

    async def snapshot(self, chat_id, period="day", user_id=None, now=None,
                       media_kind=None, page=0, kind=None, media_type=None):
        if period not in PERIODS:
            raise ValueError("Unknown activity period")
        if media_kind is None:
            media_kind = kind
        if media_kind is None:
            media_kind = media_type
        if media_kind is not None and media_kind not in (*MEDIA_KINDS, "unknown"):
            raise ValueError("Unknown media type")
        if isinstance(page, bool) or not isinstance(page, int) or page < 0 or page > MAX_PAGE:
            raise ValueError("Invalid activity page")
        return await self._run(
            self._snapshot, chat_id, period, user_id, utc(now), media_kind, page
        )

    @staticmethod
    def _period_bounds(period, now):
        today = now.astimezone(IST).date()
        if period == "all":
            return None, None
        start = today if period == "day" else today - timedelta(days=today.weekday())
        return start.isoformat(), today.isoformat()

    @classmethod
    def _score_source(cls, chat_id, period, now, media_kind):
        """Return a parameterized scores query and its matching parameters.

        ``activity_members`` and ``activity_daily`` predate media tracking.
        Their rows are deliberately never rewritten.  For an unclassified
        view, the remainder of the normal score after known media rows is
        shown as "Unclassified"; that keeps old totals intact without calling
        old text, photos, or other types.
        """
        start, end = cls._period_bounds(period, now)
        if media_kind is None:
            if period == "all":
                return (
                    "SELECT user_id,total AS n FROM activity_members WHERE chat_id=?",
                    (chat_id,),
                )
            return (
                """SELECT user_id,SUM(count) AS n FROM activity_daily
                   WHERE chat_id=? AND day>=? AND day<=? GROUP BY user_id""",
                (chat_id, start, end),
            )

        if media_kind == "unknown":
            if period == "all":
                total_source = (
                    "SELECT user_id,total AS n FROM activity_members WHERE chat_id=?"
                )
                total_params = (chat_id,)
                known_source = (
                    """SELECT user_id,SUM(count) AS n FROM activity_media_daily
                       WHERE chat_id=? AND kind<>? GROUP BY user_id"""
                )
                known_params = (chat_id, "unknown")
            else:
                total_source = (
                    """SELECT user_id,SUM(count) AS n FROM activity_daily
                       WHERE chat_id=? AND day>=? AND day<=? GROUP BY user_id"""
                )
                total_params = (chat_id, start, end)
                known_source = (
                    """SELECT user_id,SUM(count) AS n FROM activity_media_daily
                       WHERE chat_id=? AND day>=? AND day<=? AND kind<>?
                       GROUP BY user_id"""
                )
                known_params = (chat_id, start, end, "unknown")
            return (
                f"""SELECT t.user_id,
                           CASE WHEN t.n-COALESCE(k.n,0)>0
                                THEN t.n-COALESCE(k.n,0) ELSE 0 END AS n
                    FROM ({total_source}) t
                    LEFT JOIN ({known_source}) k ON k.user_id=t.user_id
                    WHERE t.n-COALESCE(k.n,0)>0""",
                total_params + known_params,
            )

        if period == "all":
            return (
                """SELECT user_id,SUM(count) AS n FROM activity_media_daily
                   WHERE chat_id=? AND kind=? GROUP BY user_id""",
                (chat_id, media_kind),
            )
        return (
            """SELECT user_id,SUM(count) AS n FROM activity_media_daily
               WHERE chat_id=? AND day>=? AND day<=? AND kind=?
               GROUP BY user_id""",
            (chat_id, start, end, media_kind),
        )

    def _snapshot(self, chat_id, period, user_id, now, media_kind=None, page=0):
        con = self._connect()
        with con:
            chat = self._chat(con, chat_id, now)
        source, params = self._score_source(chat_id, period, now, media_kind)
        cte = f"WITH scores AS ({source}), ranked AS (SELECT *,RANK() OVER (ORDER BY n DESC) AS rank FROM scores) "
        page_size = PAGE_SIZE
        offset = page * page_size
        rows = con.execute(
            cte + """SELECT r.user_id,m.name,r.n AS count,r.rank FROM ranked r
                     JOIN activity_members m ON m.chat_id=? AND m.user_id=r.user_id
                     ORDER BY r.n DESC,r.user_id LIMIT ? OFFSET ?""",
            params + (chat_id, page_size, offset)
        ).fetchall()
        totals = con.execute(
            cte + "SELECT COALESCE(SUM(n),0) AS messages, COUNT(*) AS people FROM scores", params
        ).fetchone()
        pages = max(1, (totals["people"] + page_size - 1) // page_size)
        mine = con.execute(
            cte + "SELECT n AS count,rank FROM ranked WHERE user_id=?", params + (user_id,)
        ).fetchone() if user_id is not None else None
        return {
            "enabled": bool(chat["enabled"]), "started_at": chat["started_at"],
            "period": period, "media_kind": media_kind,
            "entries": [dict(row) for row in rows], "page": page, "pages": pages,
            "has_prev": page > 0, "has_next": page + 1 < pages,
            "messages": totals["messages"], "people": totals["people"],
            "mine": dict(mine) if mine is not None else None,
        }

    async def media_summary(self, chat_id, period="day", now=None):
        """Return known media totals and an honest unclassified remainder."""
        if period not in PERIODS:
            raise ValueError("Unknown activity period")
        return await self._run(self._media_summary, chat_id, period, utc(now))

    def _media_summary(self, chat_id, period, now):
        con = self._connect()
        with con:
            self._chat(con, chat_id, now)
        start, end = self._period_bounds(period, now)
        if period == "all":
            totals = con.execute(
                "SELECT COALESCE(SUM(total),0) AS n FROM activity_members WHERE chat_id=?",
                (chat_id,),
            ).fetchone()["n"]
            rows = con.execute(
                """SELECT kind,COALESCE(SUM(count),0) AS n
                   FROM activity_media_daily WHERE chat_id=?
                   GROUP BY kind""",
                (chat_id,),
            ).fetchall()
        else:
            totals = con.execute(
                """SELECT COALESCE(SUM(count),0) AS n FROM activity_daily
                   WHERE chat_id=? AND day>=? AND day<=?""",
                (chat_id, start, end),
            ).fetchone()["n"]
            rows = con.execute(
                """SELECT kind,COALESCE(SUM(count),0) AS n
                   FROM activity_media_daily
                   WHERE chat_id=? AND day>=? AND day<=? GROUP BY kind""",
                (chat_id, start, end),
            ).fetchall()
        counts = {kind: 0 for kind in MEDIA_KINDS}
        known = 0
        for row in rows:
            if row["kind"] in counts:
                counts[row["kind"]] = row["n"]
                known += row["n"]
        # This remainder includes all messages in databases created before
        # media tracking plus any new message whose type was not recognised.
        counts["unknown"] = max(0, totals - known)
        return {
            "period": period, "messages": totals, "counts": counts,
            "labels": MEDIA_LABELS.copy(),
        }

    async def member_breakdown(self, chat_id, user_id, period="day", now=None):
        """Return one member's type breakdown without assigning old rows."""
        if period not in PERIODS:
            raise ValueError("Unknown activity period")
        if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id < 1:
            raise ValueError("Invalid activity member")
        return await self._run(
            self._member_breakdown, chat_id, user_id, period, utc(now)
        )

    def _member_breakdown(self, chat_id, user_id, period, now):
        con = self._connect()
        with con:
            chat = self._chat(con, chat_id, now)
        start, end = self._period_bounds(period, now)
        member = con.execute(
            "SELECT name,total FROM activity_members WHERE chat_id=? AND user_id=?",
            (chat_id, user_id),
        ).fetchone()
        if period == "all":
            total = member["total"] if member else 0
            rows = con.execute(
                """SELECT kind,COALESCE(SUM(count),0) AS n
                   FROM activity_media_daily
                   WHERE chat_id=? AND user_id=? GROUP BY kind""",
                (chat_id, user_id),
            ).fetchall()
        else:
            total = con.execute(
                """SELECT COALESCE(SUM(count),0) AS n FROM activity_daily
                   WHERE chat_id=? AND user_id=? AND day>=? AND day<=?""",
                (chat_id, user_id, start, end),
            ).fetchone()["n"]
            rows = con.execute(
                """SELECT kind,COALESCE(SUM(count),0) AS n
                   FROM activity_media_daily
                   WHERE chat_id=? AND user_id=? AND day>=? AND day<=?
                   GROUP BY kind""",
                (chat_id, user_id, start, end),
            ).fetchall()
        counts = {kind: 0 for kind in MEDIA_KINDS}
        known = 0
        for row in rows:
            if row["kind"] in counts:
                counts[row["kind"]] = row["n"]
                known += row["n"]
        counts["unknown"] = max(0, total - known)
        return {
            "enabled": bool(chat["enabled"]), "started_at": chat["started_at"],
            "period": period, "user_id": user_id,
            "name": member["name"] if member else str(user_id),
            "total": total, "counts": counts, "labels": MEDIA_LABELS.copy(),
        }

    async def close(self):
        async with self._lock:
            if self._connection is not None:
                await asyncio.to_thread(self._connection.close)
                self._connection = None


activity = ActivityStore()