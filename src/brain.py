"""状态库(大脑)—— SQLite。

记录:关键词 / 音频 / 文案 / 视频池 / 账号 / 发布记录。
让"三段式流水线"有记忆,把你的规则(30天冷却、每号配额、错峰、序号、背景去重、
文案配对)从"难写的逻辑"变成"几句查询"。

就一个文件 state.db(项目根),可用 DB Browser for SQLite 打开查看/编辑。
本模块只管数据库,不碰飞星/出片,保持解耦。
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Docker / 测试可用 NOVEL_DB 指向任意位置；默认仍在项目根。
DB_PATH = Path(os.environ.get("NOVEL_DB") or ROOT / "state.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS keywords (
    keyword     TEXT PRIMARY KEY,
    txt_path    TEXT,
    bg_dir      TEXT,                    -- 该关键词专用素材视频文件夹(空=用全局)
    status      TEXT DEFAULT 'ready',   -- ready / rewritten ...
    created_at  TEXT
);
CREATE TABLE IF NOT EXISTS audios (
    audio_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword     TEXT,
    file_path   TEXT UNIQUE,
    voice       TEXT,
    duration    REAL,
    created_at  TEXT
);
CREATE TABLE IF NOT EXISTS captions (
    caption_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword     TEXT,
    text        TEXT,
    used        INTEGER DEFAULT 0,
    created_at  TEXT
);
CREATE TABLE IF NOT EXISTS videos (
    video_id    TEXT PRIMARY KEY,        -- 如 "C25517 2"
    keyword     TEXT,
    app         TEXT,                    -- 所属 app(改文出片时按文件名带入)
    seq         INTEGER,
    file_path   TEXT,
    cover_path  TEXT,
    caption_id  INTEGER,
    backgrounds TEXT,                    -- JSON 数组:用过的背景素材
    duration    REAL,
    size_mb     REAL,
    status      TEXT DEFAULT 'pending',  -- pending 待发 / scheduled 已排 / published 已发 / failed
    created_at  TEXT
);
CREATE TABLE IF NOT EXISTS accounts (
    account_id  TEXT PRIMARY KEY,        -- 飞星 media_id
    name        TEXT,
    platform    TEXT,
    daily_min   INTEGER DEFAULT 3,
    daily_max   INTEGER DEFAULT 5,
    active      INTEGER DEFAULT 1,
    created_at  TEXT
);
CREATE TABLE IF NOT EXISTS publish_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id       TEXT,
    account_id     TEXT,
    keyword        TEXT,                 -- 冗余存一份,查冷却快
    scheduled_time TEXT,                 -- 排定发布时间(ISO)
    published_at   TEXT,
    status         TEXT,                 -- scheduled / reviewing / published / failed
    task_id        TEXT,                 -- 飞星返回
    error          TEXT,
    views          INTEGER DEFAULT 0,    -- 回填:播放数
    likes          INTEGER DEFAULT 0,    -- 回填:点赞数
    created_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_pub_acct_kw ON publish_log(account_id, keyword);
CREATE INDEX IF NOT EXISTS idx_videos_status ON videos(status);
"""


def _now() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


def connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    path = Path(db_path or DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con


def init_db(db_path: str | Path | None = None) -> None:
    con = connect(db_path)
    con.executescript(SCHEMA)
    # 迁移:老库补上新列(已存在则忽略)
    for col, ddl in [("views", "ALTER TABLE publish_log ADD COLUMN views INTEGER DEFAULT 0"),
                     ("likes", "ALTER TABLE publish_log ADD COLUMN likes INTEGER DEFAULT 0"),
                     ("app", "ALTER TABLE videos ADD COLUMN app TEXT"),
                     ("bg_dir", "ALTER TABLE keywords ADD COLUMN bg_dir TEXT"),
                     ("hook_type", "ALTER TABLE videos ADD COLUMN hook_type TEXT"),
                     ("genre", "ALTER TABLE videos ADD COLUMN genre TEXT"),
                     # --- 回填:飞星 /stat/detail 全量指标(views=play, likes=digg 已在上) ---
                     ("reach", "ALTER TABLE publish_log ADD COLUMN reach INTEGER DEFAULT 0"),                        # 观看人数
                     ("play_rate", "ALTER TABLE publish_log ADD COLUMN play_rate REAL"),                            # 播放率
                     ("digg_rate", "ALTER TABLE publish_log ADD COLUMN digg_rate REAL"),                            # 点赞率
                     ("comment", "ALTER TABLE publish_log ADD COLUMN comment INTEGER DEFAULT 0"),                   # 评论数
                     ("comment_rate", "ALTER TABLE publish_log ADD COLUMN comment_rate REAL"),                      # 评论率
                     ("share", "ALTER TABLE publish_log ADD COLUMN share INTEGER DEFAULT 0"),                       # 分享数
                     ("share_rate", "ALTER TABLE publish_log ADD COLUMN share_rate REAL"),                          # 分享率
                     ("forward", "ALTER TABLE publish_log ADD COLUMN forward INTEGER DEFAULT 0"),                   # 转发数
                     ("forward_rate", "ALTER TABLE publish_log ADD COLUMN forward_rate REAL"),                      # 转发率
                     ("favorite", "ALTER TABLE publish_log ADD COLUMN favorite INTEGER DEFAULT 0"),                 # 收藏数
                     ("download", "ALTER TABLE publish_log ADD COLUMN download INTEGER DEFAULT 0"),                 # 下载(保存)数
                     ("download_rate", "ALTER TABLE publish_log ADD COLUMN download_rate REAL"),                    # 下载率
                     ("total_time_watched", "ALTER TABLE publish_log ADD COLUMN total_time_watched REAL"),          # 观看总时长(秒)
                     ("average_time_watched", "ALTER TABLE publish_log ADD COLUMN average_time_watched REAL"),      # 平均播放时长(秒) ★
                     ("full_video_watched", "ALTER TABLE publish_log ADD COLUMN full_video_watched INTEGER DEFAULT 0"),   # 完整观看次数
                     ("full_video_watched_rate", "ALTER TABLE publish_log ADD COLUMN full_video_watched_rate REAL"),     # 完播率 ★主信号
                     ("stat_updated_at", "ALTER TABLE publish_log ADD COLUMN stat_updated_at TEXT")]:               # 本行回填时间
        try:
            con.execute(ddl)
        except sqlite3.OperationalError:
            pass
    con.commit()
    con.close()


# ---------------- 写入 ----------------
def upsert_keyword(keyword: str, txt_path: str | None = None, status: str = "ready",
                   bg_dir: str | None = None):
    con = connect()
    con.execute(
        "INSERT INTO keywords(keyword,txt_path,bg_dir,status,created_at) VALUES(?,?,?,?,?) "
        "ON CONFLICT(keyword) DO UPDATE SET "
        "txt_path=COALESCE(excluded.txt_path,keywords.txt_path), "
        "bg_dir=COALESCE(excluded.bg_dir,keywords.bg_dir)",
        (keyword, txt_path, bg_dir, status, _now()))
    con.commit(); con.close()


def get_keyword_bg_dir(keyword: str) -> str | None:
    con = connect()
    row = con.execute("SELECT bg_dir FROM keywords WHERE keyword=?", (keyword,)).fetchone()
    con.close()
    return row["bg_dir"] if row and row["bg_dir"] else None


def register_video(video_id: str, keyword: str, seq: int, file_path: str,
                   cover_path: str | None = None, backgrounds=None,
                   duration: float | None = None, size_mb: float | None = None,
                   app: str | None = None, hook_type: str | None = None,
                   genre: str | None = None, caption_text: str | None = None):
    con = connect()
    caption_id = None
    if caption_text:                      # 改文 agent 产出的英文 caption+标签,发布时优先用
        cur = con.execute("INSERT INTO captions(keyword,text,created_at) VALUES(?,?,?)",
                          (keyword, caption_text, _now()))
        caption_id = cur.lastrowid
    con.execute(
        "INSERT INTO videos(video_id,keyword,app,seq,file_path,cover_path,caption_id,"
        "hook_type,genre,backgrounds,duration,size_mb,status,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?, 'pending', ?) "
        "ON CONFLICT(video_id) DO UPDATE SET file_path=excluded.file_path,"
        "cover_path=excluded.cover_path,size_mb=excluded.size_mb,app=excluded.app,"
        "caption_id=COALESCE(excluded.caption_id,videos.caption_id),"
        "hook_type=COALESCE(excluded.hook_type,videos.hook_type),"
        "genre=COALESCE(excluded.genre,videos.genre)",
        (video_id, keyword, app, seq, file_path, cover_path, caption_id,
         hook_type, genre, json.dumps(backgrounds or [], ensure_ascii=False),
         duration, size_mb, _now()))
    con.execute("INSERT OR IGNORE INTO keywords(keyword,status,created_at) VALUES(?,?,?)",
                (keyword, "ready", _now()))
    con.commit(); con.close()


def upsert_account(account_id: str, name: str = "", platform: str = "",
                   daily_min: int = 3, daily_max: int = 5, active: int = 1):
    con = connect()
    con.execute(
        "INSERT INTO accounts(account_id,name,platform,daily_min,daily_max,active,created_at) "
        "VALUES(?,?,?,?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET "
        "name=excluded.name, platform=excluded.platform",
        (account_id, name, platform, daily_min, daily_max, active, _now()))
    con.commit(); con.close()


def record_publish(video_id: str, account_id: str, keyword: str, status: str,
                   scheduled_time: str | None = None, task_id: str | None = None,
                   error: str | None = None, published_at: str | None = None):
    con = connect()
    con.execute(
        "INSERT INTO publish_log(video_id,account_id,keyword,scheduled_time,"
        "published_at,status,task_id,error,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (video_id, account_id, keyword, scheduled_time, published_at,
         status, task_id, error, _now()))
    if status in ("published", "reviewing", "scheduled"):
        con.execute("UPDATE videos SET status=? WHERE video_id=?",
                    ("published" if status == "published" else "scheduled", video_id))
    con.commit(); con.close()


def set_video_status(video_id: str, status: str):
    con = connect()
    con.execute("UPDATE videos SET status=? WHERE video_id=?", (status, video_id))
    con.commit(); con.close()


def mark_file_deleted(video_id: str):
    """发布成功后本地文件已删:清空路径(记录仍在,用于冷却/统计)。"""
    con = connect()
    con.execute("UPDATE videos SET file_path=NULL, cover_path=NULL WHERE video_id=?",
                (video_id,))
    con.commit(); con.close()


# ---------------- 规则查询(大脑的威力) ----------------
# 统一状态词表(全英文,回填/记录都用这套):
#   scheduled 已排定 / published 已发布 / reviewing 审核中 / rejected 审核失败 / failed 上传失败
# 「占额度/占冷却」的状态 = 上传成功进入过发布流程的(不含 failed 上传失败,那种可重试)。
# 冷却和每日额度都按 created_at(=上传/记录时间,机器本地即中国时区)算,避免跟 ET 发布时间混。
ACTIVE_SQL = "status IN ('scheduled','published','reviewing','rejected')"


def can_publish(account_id: str, keyword: str, cooldown_days: int = 30) -> bool:
    """该账号在冷却期内是否发过该关键词(含被拒,不含上传失败)。True=可以发。"""
    cutoff = (_dt.datetime.now() - _dt.timedelta(days=cooldown_days)).isoformat()
    con = connect()
    row = con.execute(
        f"SELECT 1 FROM publish_log WHERE account_id=? AND keyword=? AND {ACTIVE_SQL} "
        "AND created_at > ? LIMIT 1",
        (account_id, keyword, cutoff)).fetchone()
    con.close()
    return row is None


def count_today(account_id: str) -> int:
    """该账号今天(中国时区,按上传时间)已发/已排的条数,用于每日配额。"""
    today = _dt.date.today().isoformat()
    con = connect()
    n = con.execute(
        f"SELECT COUNT(*) FROM publish_log WHERE account_id=? AND {ACTIVE_SQL} "
        "AND date(created_at)=?",
        (account_id, today)).fetchone()[0]
    con.close()
    return n


def count_today_all() -> int:
    """今天(中国时区)全部账号已发/已排总数,用于飞星每日全局上限。"""
    today = _dt.date.today().isoformat()
    con = connect()
    n = con.execute(
        f"SELECT COUNT(*) FROM publish_log WHERE {ACTIVE_SQL} AND date(created_at)=?",
        (today,)).fetchone()[0]
    con.close()
    return n


def next_seq(keyword: str) -> int:
    con = connect()
    m = con.execute("SELECT MAX(seq) FROM videos WHERE keyword=?", (keyword,)).fetchone()[0]
    con.close()
    return (m or 0) + 1


def pending_videos(keyword: str | None = None) -> list[dict]:
    con = connect()
    if keyword:
        rows = con.execute("SELECT * FROM videos WHERE status='pending' AND keyword=? "
                           "ORDER BY keyword,seq", (keyword,)).fetchall()
    else:
        rows = con.execute("SELECT * FROM videos WHERE status='pending' "
                           "ORDER BY keyword,seq").fetchall()
    con.close()
    return [dict(r) for r in rows]


def status_report() -> dict:
    con = connect()

    def one(q):
        return con.execute(q).fetchone()[0]

    rep = {
        "keywords": one("SELECT COUNT(*) FROM keywords"),
        "videos_total": one("SELECT COUNT(*) FROM videos"),
        "videos_pending": one("SELECT COUNT(*) FROM videos WHERE status='pending'"),
        "videos_published": one("SELECT COUNT(*) FROM videos WHERE status IN('published','scheduled')"),
        "accounts_active": one("SELECT COUNT(*) FROM accounts WHERE active=1"),
        "publish_total": one("SELECT COUNT(*) FROM publish_log"),
    }
    accts = con.execute("SELECT account_id,name,daily_min,daily_max FROM accounts "
                        "WHERE active=1 ORDER BY name").fetchall()
    today = _dt.date.today().isoformat()
    detail = []
    for a in accts:
        n = con.execute(
            "SELECT COUNT(*) FROM publish_log WHERE account_id=? AND status!='failed' "
            "AND date(COALESCE(scheduled_time,published_at,created_at))=?",
            (a["account_id"], today)).fetchone()[0]
        detail.append({"name": a["name"], "account_id": a["account_id"],
                       "today": n, "quota": f'{a["daily_min"]}-{a["daily_max"]}'})
    con.close()
    rep["accounts_detail"] = detail
    return rep
