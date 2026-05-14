import sqlite3
from config import DB_PATH


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _connect() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS videos (
                id                        INTEGER PRIMARY KEY AUTOINCREMENT,
                youtube_id                TEXT UNIQUE NOT NULL,
                title                     TEXT,
                url                       TEXT,
                transcript_path           TEXT,
                processed_at              TEXT DEFAULT (datetime('now')),
                retrospective_reviewed_at TEXT
            );

            CREATE TABLE IF NOT EXISTS drafts (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                video_id            INTEGER REFERENCES videos(id),
                format              TEXT NOT NULL,
                content             TEXT NOT NULL,
                status              TEXT DEFAULT 'pending',
                telegram_message_id INTEGER,
                created_at          TEXT DEFAULT (datetime('now')),
                updated_at          TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS good_posts (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                draft_id    INTEGER REFERENCES drafts(id),
                format      TEXT NOT NULL,
                content     TEXT NOT NULL,
                approved_at TEXT DEFAULT (datetime('now'))
            );
        """)


# ── videos ────────────────────────────────────────────────────────────────────

def get_processed_video_ids() -> set:
    with _connect() as conn:
        rows = conn.execute("SELECT youtube_id FROM videos").fetchall()
        return {row["youtube_id"] for row in rows}


def add_video(youtube_id: str, title: str, url: str, transcript_path: str = None) -> int:
    with _connect() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO videos (youtube_id, title, url, transcript_path) VALUES (?,?,?,?)",
            (youtube_id, title, url, transcript_path),
        )
        if cur.lastrowid:
            return cur.lastrowid
        return conn.execute(
            "SELECT id FROM videos WHERE youtube_id=?", (youtube_id,)
        ).fetchone()["id"]


def get_videos_for_retrospective() -> list:
    with _connect() as conn:
        rows = conn.execute("""
            SELECT id, youtube_id, title, url, transcript_path
            FROM   videos
            WHERE  transcript_path IS NOT NULL
            AND    (retrospective_reviewed_at IS NULL
                    OR retrospective_reviewed_at < datetime('now', '-30 days'))
            ORDER  BY processed_at DESC
        """).fetchall()
        return [dict(r) for r in rows]


def mark_retrospective_reviewed(video_id: int):
    with _connect() as conn:
        conn.execute(
            "UPDATE videos SET retrospective_reviewed_at=datetime('now') WHERE id=?",
            (video_id,),
        )


# ── drafts ────────────────────────────────────────────────────────────────────

def add_draft(video_id: int, fmt: str, content: str) -> int:
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO drafts (video_id, format, content) VALUES (?,?,?)",
            (video_id, fmt, content),
        )
        return cur.lastrowid


def get_draft_by_id(draft_id: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
        return dict(row) if row else None


def update_draft_status(draft_id: int, status: str, content: str = None):
    with _connect() as conn:
        if content is not None:
            conn.execute(
                "UPDATE drafts SET status=?, content=?, updated_at=datetime('now') WHERE id=?",
                (status, content, draft_id),
            )
        else:
            conn.execute(
                "UPDATE drafts SET status=?, updated_at=datetime('now') WHERE id=?",
                (status, draft_id),
            )


def set_draft_telegram_id(draft_id: int, telegram_message_id: int):
    with _connect() as conn:
        conn.execute(
            "UPDATE drafts SET telegram_message_id=? WHERE id=?",
            (telegram_message_id, draft_id),
        )


# ── good_posts ────────────────────────────────────────────────────────────────

def add_good_post(draft_id: int, fmt: str, content: str):
    with _connect() as conn:
        conn.execute(
            "INSERT INTO good_posts (draft_id, format, content) VALUES (?,?,?)",
            (draft_id, fmt, content),
        )


def get_good_posts(limit: int = 15) -> list:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT format, content FROM good_posts ORDER BY approved_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def count_good_posts() -> int:
    with _connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM good_posts").fetchone()[0]


def get_approved_drafts() -> list:
    """All approved drafts not yet marked as posted, with their video title."""
    with _connect() as conn:
        rows = conn.execute("""
            SELECT d.id, d.format, d.content, d.updated_at, v.title
            FROM   drafts d
            JOIN   videos v ON v.id = d.video_id
            WHERE  d.status = 'approved'
            ORDER  BY d.updated_at DESC
        """).fetchall()
        return [dict(r) for r in rows]


def mark_draft_posted(draft_id: int):
    with _connect() as conn:
        conn.execute(
            "UPDATE drafts SET status='posted', updated_at=datetime('now') WHERE id=?",
            (draft_id,),
        )


def upsert_video(youtube_id: str, title: str, url: str, transcript_path: str = None) -> int:
    """Insert or update a video row (used when re-processing an existing video)."""
    with _connect() as conn:
        conn.execute(
            """INSERT INTO videos (youtube_id, title, url, transcript_path)
               VALUES (?,?,?,?)
               ON CONFLICT(youtube_id) DO UPDATE SET
                   title=excluded.title,
                   transcript_path=COALESCE(excluded.transcript_path, transcript_path),
                   processed_at=datetime('now')
            """,
            (youtube_id, title, url, transcript_path),
        )
        return conn.execute(
            "SELECT id FROM videos WHERE youtube_id=?", (youtube_id,)
        ).fetchone()["id"]
