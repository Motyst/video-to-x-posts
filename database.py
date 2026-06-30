import json
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
                scheduled_for       TEXT,
                posted_url          TEXT,
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

            CREATE TABLE IF NOT EXISTS video_jobs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                video_id    INTEGER REFERENCES videos(id),
                job_type    TEXT NOT NULL,
                ran_at      TEXT DEFAULT (datetime('now')),
                draft_count INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS command_stats (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                command    TEXT NOT NULL,
                used_at    TEXT DEFAULT (datetime('now'))
            );
        """)
    _migrate()


def _migrate():
    """Add columns introduced after initial schema."""
    with _connect() as conn:
        for col, definition in [
            ("scheduled_for",    "TEXT"),
            ("posted_url",       "TEXT"),
            ("version",          "TEXT"),      # 'original' | 'trend' | NULL (legacy)
            ("pair_id",          "INTEGER"),   # links original+trend drafts together
            ("trend_reason",     "TEXT"),      # Claude's explanation for trend version
            ("cta_reply",        "TEXT"),      # optional CTA tweet posted as reply after video
        ]:
            try:
                conn.execute(f"ALTER TABLE drafts ADD COLUMN {col} {definition}")
            except Exception:
                pass

        for col, definition in [
            ("source",           "TEXT"),      # 'youtube' | 'local'
            ("duration_seconds", "INTEGER"),   # video length in seconds
        ]:
            try:
                conn.execute(f"ALTER TABLE videos ADD COLUMN {col} {definition}")
            except Exception:
                pass  # column already exists


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


def add_draft_pair(
    video_id: int,
    fmt: str,
    original_content: str,
    trend_content: str,
    trend_reason: str,
) -> tuple[int, int]:
    """Insert original + trend versions as a linked pair. Returns (id_a, id_b)."""
    with _connect() as conn:
        cur_a = conn.execute(
            "INSERT INTO drafts (video_id, format, content, version) VALUES (?,?,?,?)",
            (video_id, fmt, original_content, "original"),
        )
        id_a = cur_a.lastrowid
        cur_b = conn.execute(
            "INSERT INTO drafts (video_id, format, content, version, trend_reason, pair_id) "
            "VALUES (?,?,?,?,?,?)",
            (video_id, fmt, trend_content, "trend", trend_reason, id_a),
        )
        id_b = cur_b.lastrowid
        conn.execute("UPDATE drafts SET pair_id=? WHERE id=?", (id_a, id_a))
        return id_a, id_b


def get_draft_partner(draft_id: int) -> dict | None:
    """Return the paired draft (other version) for a given draft_id."""
    draft = get_draft_by_id(draft_id)
    if not draft or not draft.get("pair_id"):
        return None
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM drafts WHERE pair_id=? AND id!=?",
            (draft["pair_id"], draft_id),
        ).fetchone()
        return dict(row) if row else None


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


def set_draft_cta(draft_id: int, cta_text: str):
    """Store a CTA reply text on a video_post draft."""
    with _connect() as conn:
        conn.execute(
            "UPDATE drafts SET cta_reply=? WHERE id=?",
            (cta_text, draft_id),
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
            SELECT d.id, d.format, d.content, d.updated_at,
                   COALESCE(v.title, '(no video)') AS title
            FROM   drafts d
            LEFT JOIN videos v ON v.id = d.video_id
            WHERE  d.status = 'approved'
            ORDER  BY d.updated_at DESC
        """).fetchall()
        return [dict(r) for r in rows]


def get_recent_promo_drafts(limit: int = 4) -> list:
    """Recent promo drafts for caption selection in video posts."""
    with _connect() as conn:
        rows = conn.execute("""
            SELECT d.id, d.content, COALESCE(v.title, '') AS title
            FROM   drafts d
            LEFT JOIN videos v ON v.id = d.video_id
            WHERE  d.format = 'promo'
            ORDER  BY d.created_at DESC LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]


def mark_draft_posted(draft_id: int, posted_url: str = None):
    with _connect() as conn:
        conn.execute(
            "UPDATE drafts SET status='posted', posted_url=?, updated_at=datetime('now') WHERE id=?",
            (posted_url, draft_id),
        )


def set_draft_scheduled(draft_id: int, scheduled_for: str):
    """Mark draft as scheduled with ISO UTC datetime string."""
    with _connect() as conn:
        conn.execute(
            "UPDATE drafts SET status='scheduled', scheduled_for=?, updated_at=datetime('now') WHERE id=?",
            (scheduled_for, draft_id),
        )


def unschedule_draft(draft_id: int):
    """Cancel a scheduled post — revert to approved status."""
    with _connect() as conn:
        conn.execute(
            "UPDATE drafts SET status='approved', scheduled_for=NULL, updated_at=datetime('now') WHERE id=?",
            (draft_id,),
        )


def get_scheduled_drafts() -> list:
    """All scheduled drafts whose time has arrived."""
    with _connect() as conn:
        rows = conn.execute("""
            SELECT d.id, d.format, d.content, d.scheduled_for,
                   COALESCE(v.title, '(no video)') AS title
            FROM   drafts d
            LEFT JOIN videos v ON v.id = d.video_id
            WHERE  d.status = 'scheduled'
            AND    d.scheduled_for <= datetime('now')
            ORDER  BY d.scheduled_for ASC
        """).fetchall()
        return [dict(r) for r in rows]


def get_all_scheduled_drafts() -> list:
    """All future scheduled drafts ordered by fire time."""
    with _connect() as conn:
        rows = conn.execute("""
            SELECT d.id, d.format, d.content, d.scheduled_for,
                   COALESCE(v.title, '(no video)') AS title
            FROM   drafts d
            LEFT JOIN videos v ON v.id = d.video_id
            WHERE  d.status = 'scheduled'
            ORDER  BY d.scheduled_for ASC
        """).fetchall()
        return [dict(r) for r in rows]


def get_active_video_post_paths() -> dict[str, str]:
    """Return {file_path: status} for video_post drafts not yet posted."""
    with _connect() as conn:
        rows = conn.execute("""
            SELECT content, status FROM drafts
            WHERE format = 'video_post' AND status IN ('approved', 'scheduled')
        """).fetchall()
    result = {}
    for row in rows:
        try:
            data = json.loads(row["content"])
            path = data.get("path", "")
            if path:
                result[path] = row["status"]
        except Exception:
            pass
    return result


# ── video_jobs ────────────────────────────────────────────────────────────────

def log_command(command: str):
    """Record a command invocation for usage tracking."""
    with _connect() as conn:
        conn.execute("INSERT INTO command_stats (command) VALUES (?)", (command,))


def get_command_stats() -> list[dict]:
    """Return commands ranked by usage count, with last-used date."""
    with _connect() as conn:
        rows = conn.execute("""
            SELECT command,
                   COUNT(*)       AS count,
                   MAX(used_at)   AS last_used
            FROM   command_stats
            GROUP  BY command
            ORDER  BY count DESC
        """).fetchall()
        return [dict(r) for r in rows]


def has_tweets_job(youtube_id: str) -> bool:
    """Return True if tweet posts were ever generated for this video."""
    with _connect() as conn:
        row = conn.execute("""
            SELECT 1 FROM video_jobs vj
            JOIN videos v ON vj.video_id = v.id
            WHERE v.youtube_id = ? AND vj.job_type = 'tweets'
            LIMIT 1
        """, (youtube_id,)).fetchone()
        return row is not None


def log_video_job(video_id: int, job_type: str, draft_count: int = 0):
    """Record that a content generation job was run for a video."""
    with _connect() as conn:
        conn.execute(
            "INSERT INTO video_jobs (video_id, job_type, draft_count) VALUES (?,?,?)",
            (video_id, job_type, draft_count),
        )


def get_video_jobs(video_id: int) -> list:
    """All jobs run for a specific video, newest first."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT job_type, ran_at, draft_count FROM video_jobs WHERE video_id=? ORDER BY ran_at DESC",
            (video_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_recent_video_summaries(limit: int = 10) -> list:
    """Recent videos with aggregated job history — for /status overview."""
    with _connect() as conn:
        rows = conn.execute("""
            SELECT v.id, v.title, v.processed_at, v.source, v.duration_seconds,
                   GROUP_CONCAT(vj.job_type, ',') AS jobs_run
            FROM   videos v
            LEFT   JOIN video_jobs vj ON vj.video_id = v.id
            GROUP  BY v.id
            ORDER  BY v.processed_at DESC
            LIMIT  ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]


# ── videos ────────────────────────────────────────────────────────────────────

def upsert_video(
    youtube_id: str,
    title: str,
    url: str,
    transcript_path: str = None,
    source: str = None,
    duration_seconds: int = None,
) -> int:
    """Insert or update a video row (used when re-processing an existing video)."""
    with _connect() as conn:
        conn.execute(
            """INSERT INTO videos (youtube_id, title, url, transcript_path, source, duration_seconds)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(youtube_id) DO UPDATE SET
                   title=excluded.title,
                   transcript_path=COALESCE(excluded.transcript_path, transcript_path),
                   source=COALESCE(excluded.source, source),
                   duration_seconds=COALESCE(excluded.duration_seconds, duration_seconds),
                   processed_at=datetime('now')
            """,
            (youtube_id, title, url, transcript_path, source, duration_seconds),
        )
        return conn.execute(
            "SELECT id FROM videos WHERE youtube_id=?", (youtube_id,)
        ).fetchone()["id"]
