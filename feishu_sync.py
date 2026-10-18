"""state.db <-> 飞书多维表格。

    python feishu_sync.py          # 双向同步一次:先拉账号编辑(飞书->本地),再推全部(本地->飞书)
    python feishu_sync.py push     # 只推(本地->飞书)
    python feishu_sync.py pull     # 只拉账号设置(飞书->本地)

表:视频池 / 账号 / 发布记录 / 关键词。
账号表是双向的:你在飞书改「每日下限/每日上限/启用」,下次同步会拉回 state.db(当控制台用)。
其余表是本地 -> 飞书 的镜像(全量重刷)。凭证读 feishu.json。
"""
from __future__ import annotations

import sys
from pathlib import Path

import requests

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from src import brain  # noqa: E402
from src import credentials  # noqa: E402

TEXT, NUM = 1, 2
_tok = None
_CFG = None


def _cfg() -> dict:
    global _CFG
    if _CFG is None:
        _CFG = credentials.load("feishu")
    return _CFG

# 忽略环境里的 VPN 代理(open.feishu.cn 国内直连,走代理反而失败)
_S = requests.Session()
_S.trust_env = False


def token() -> str:
    global _tok
    if _tok:
        return _tok
    cfg = _cfg()
    lack = credentials.missing(
        "feishu", cfg, ("app_id", "app_secret", "app_token"))
    if lack:
        raise RuntimeError(
            "缺少飞书凭证：" + ", ".join(lack)
            + "。请设置对应环境变量或创建 feishu.json。")
    r = _S.post(cfg["base_url"] + "/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": cfg["app_id"], "app_secret": cfg["app_secret"]},
                timeout=20).json()
    if r.get("code") != 0:
        raise RuntimeError(f"取 token 失败: {r}")
    _tok = r["tenant_access_token"]
    return _tok


def _h():
    return {"Authorization": "Bearer " + token(), "Content-Type": "application/json"}


def _req(method: str, path: str, body=None, timeout=60, retries=3):
    """带重试的请求:只对网络抖动(超时/连接失败)重试,业务错误直接抛。"""
    import time as _t
    last = None
    for i in range(retries):
        try:
            if method == "GET":
                r = _S.get(_cfg()["base_url"] + path, headers=_h(), timeout=timeout).json()
            else:
                r = _S.post(_cfg()["base_url"] + path, headers=_h(),
                            json=body, timeout=timeout).json()
            if r.get("code") != 0:
                raise RuntimeError(f"{method} {path}: {r}")
            return r["data"] if method == "GET" else r.get("data", {})
        except requests.RequestException as e:      # 超时/连接失败 -> 重试
            last = e
            if i < retries - 1:
                _t.sleep(3 * (i + 1))
    raise RuntimeError(f"{method} {path} 网络失败({retries}次重试后): {last}")


def _get(path):
    return _req("GET", path, timeout=45)


def _post(path, body):
    return _req("POST", path, body, timeout=90)


def _put(path, body):
    r = requests.put(_cfg()["base_url"] + path, headers=_h(),
                     json=body, timeout=60).json()
    if r.get("code") != 0:
        raise RuntimeError(f"PUT {path}: {r}")
    return r.get("data", {})


def _base(p):
    return f"/open-apis/bitable/v1/apps/{_cfg()['app_token']}{p}"


def _tables():
    return {t["name"]: t["table_id"] for t in _get(_base("/tables"))["items"]}


def _ensure_fields(table_id, fields):
    have = {f["field_name"] for f in _get(_base(f"/tables/{table_id}/fields"))["items"]}
    for fn, ft in fields:
        if fn not in have:
            _post(_base(f"/tables/{table_id}/fields"), {"field_name": fn, "type": ft})


def ensure_table(name, fields, existing=None):
    existing = existing if existing is not None else _tables()
    if name in existing:
        _ensure_fields(existing[name], fields)      # 已有表:补缺失的列
        return existing[name]
    data = _post(_base("/tables"), {"table": {
        "name": name, "fields": [{"field_name": fn, "type": ft} for fn, ft in fields]}})
    return data["table_id"]


def _all_records(table_id):
    items, page = [], None
    while True:
        q = _base(f"/tables/{table_id}/records?page_size=500")
        if page:
            q += "&page_token=" + page
        d = _get(q)
        items += d.get("items", [])
        if not d.get("has_more"):
            break
        page = d.get("page_token")
    return items


def wipe(table_id):
    ids = [r["record_id"] for r in _all_records(table_id)]
    for i in range(0, len(ids), 500):
        _post(_base(f"/tables/{table_id}/records/batch_delete"), {"records": ids[i:i + 500]})


def load(table_id, rows):
    for i in range(0, len(rows), 500):
        _post(_base(f"/tables/{table_id}/records/batch_create"),
              {"records": [{"fields": r} for r in rows[i:i + 500]]})


# ---------------- 双向:拉账号设置(飞书 -> 本地) ----------------
def pull_accounts():
    tbls = _tables()
    if "账号" not in tbls:
        return 0
    n = 0
    for rec in _all_records(tbls["账号"]):
        f = rec.get("fields", {})
        aid = _txt(f.get("account_id"))
        if not aid:
            continue
        con = brain.connect()
        dmin = _int(f.get("每日下限"), 3)
        dmax = _int(f.get("每日上限"), 5)
        active = 1 if _txt(f.get("启用")) in ("是", "1", "true", "True") else 0
        con.execute("UPDATE accounts SET daily_min=?, daily_max=?, active=? WHERE account_id=?",
                    (dmin, dmax, active, aid))
        con.commit(); con.close()
        n += 1
    return n


def _txt(v):
    if isinstance(v, list):
        return "".join(x.get("text", "") if isinstance(x, dict) else str(x) for x in v).strip()
    return ("" if v is None else str(v)).strip()


def _int(v, default):
    try:
        return int(float(_txt(v)))
    except (ValueError, TypeError):
        return default


# ---------------- 推送(本地 -> 飞书) ----------------
def push():
    brain.init_db()          # 确保新列(hook_type/genre)已迁移
    con = brain.connect()
    accts = {r["account_id"]: r["name"]
             for r in con.execute("SELECT account_id,name FROM accounts").fetchall()}
    caps = {r["caption_id"]: r["text"]
            for r in con.execute("SELECT caption_id,text FROM captions").fetchall()}

    vstatus_cn = {"pending": "待发", "scheduled": "已排定", "published": "已发布",
                  "failed": "失败"}
    vrows = [{
        "视频": v["video_id"] or "", "关键词": v["keyword"] or "",
        "序号": v["seq"] or 0, "状态": vstatus_cn.get(v["status"], v["status"] or ""),
        "钩子": v["hook_type"] or "", "题材": v["genre"] or "",
        "大小MB": v["size_mb"] or 0, "文案": caps.get(v["caption_id"], "") or "",
        "生成时间": v["created_at"] or "",
    } for v in con.execute(
        "SELECT * FROM videos WHERE status != 'retired' "
        "ORDER BY CASE status WHEN 'pending' THEN 0 WHEN 'scheduled' THEN 1 "
        "ELSE 2 END, keyword, seq").fetchall()]

    arows = [{
        "名称": r["name"] or "", "account_id": r["account_id"],
        "每日下限": r["daily_min"], "每日上限": r["daily_max"],
        "启用": "是" if r["active"] else "否",
    } for r in con.execute("SELECT * FROM accounts").fetchall()]

    status_cn = {"scheduled": "已排定", "published": "已发布", "reviewing": "审核中",
                 "rejected": "审核失败", "failed": "上传失败"}
    prows = [{
        "视频": r["video_id"] or "", "账号": accts.get(r["account_id"], r["account_id"] or ""),
        "关键词": r["keyword"] or "", "状态": status_cn.get(r["status"], r["status"] or ""),
        "播放": r["views"] or 0, "点赞": r["likes"] or 0,
        "排定时间": r["scheduled_time"] or "", "task_id": r["task_id"] or "",
        "记录时间": r["created_at"] or "",
    } for r in con.execute("SELECT * FROM publish_log ORDER BY id DESC").fetchall()]

    # 钩子分析表(A1 数据闭环:钩子套路 × 题材 → 发布表现,发布数据回填后自动更新)
    hrows = [{
        "钩子": r["hook"], "题材": r["genre"], "发布数": r["n_pub"],
        "平均播放": round(r["avg_views"] or 0, 1), "平均点赞": round(r["avg_likes"] or 0, 2),
        "被拒数": r["rejected"],
    } for r in con.execute("""
        SELECT COALESCE(NULLIF(v.hook_type,''),'旧版') hook,
               COALESCE(NULLIF(v.genre,''),'-') genre,
               COUNT(p.id) n_pub, AVG(p.views) avg_views, AVG(p.likes) avg_likes,
               SUM(CASE WHEN p.status='rejected' THEN 1 ELSE 0 END) rejected
        FROM publish_log p JOIN videos v ON p.video_id=v.video_id
        WHERE p.task_id IS NOT NULL
        GROUP BY hook, genre ORDER BY n_pub DESC""").fetchall()]

    # 关键词表(带统计)
    krows = []
    for k in con.execute("SELECT keyword FROM keywords ORDER BY keyword").fetchall():
        kw = k["keyword"]
        tot = con.execute("SELECT COUNT(*) FROM videos WHERE keyword=?", (kw,)).fetchone()[0]
        pub = con.execute("SELECT COUNT(*) FROM videos WHERE keyword=? AND status IN('published','scheduled')",
                          (kw,)).fetchone()[0]
        pend = con.execute("SELECT COUNT(*) FROM videos WHERE keyword=? AND status='pending'",
                           (kw,)).fetchone()[0]
        krows.append({"关键词": kw, "视频总数": tot, "已发": pub, "待发": pend})
    con.close()

    existing = _tables()
    tables = [
        ("视频池", [("视频", TEXT), ("关键词", TEXT), ("序号", NUM), ("状态", TEXT),
                  ("钩子", TEXT), ("题材", TEXT),
                  ("大小MB", NUM), ("文案", TEXT), ("生成时间", TEXT)], vrows),
        ("账号", [("名称", TEXT), ("account_id", TEXT), ("每日下限", NUM),
                 ("每日上限", NUM), ("启用", TEXT)], arows),
        ("发布记录", [("视频", TEXT), ("账号", TEXT), ("关键词", TEXT), ("状态", TEXT),
                  ("播放", NUM), ("点赞", NUM), ("排定时间", TEXT), ("task_id", TEXT),
                  ("记录时间", TEXT)], prows),
        ("关键词", [("关键词", TEXT), ("视频总数", NUM), ("已发", NUM), ("待发", NUM)], krows),
        ("钩子分析", [("钩子", TEXT), ("题材", TEXT), ("发布数", NUM),
                   ("平均播放", NUM), ("平均点赞", NUM), ("被拒数", NUM)], hrows),
    ]
    for name, fields, rows in tables:
        tid = ensure_table(name, fields, existing)
        wipe(tid)
        load(tid, rows)
        print(f"  {name}: {len(rows)} 行")


def sync():
    """默认:先拉账号编辑,再全量推。"""
    try:
        n = pull_accounts()
        if n:
            print(f"  拉回账号设置 {n} 个(飞书->本地)")
    except Exception as e:
        print(f"  [提示] 拉账号跳过: {e}")
    push()
    print("同步完成")


def append_run_log(row: dict):
    """给"运行日志"表追加一行(累积,不清空)。每天自动跑完记一条,当告警/留痕。"""
    tid = ensure_table("运行日志", [("时间", TEXT), ("计划", NUM), ("成功", NUM),
                                  ("失败", NUM), ("备注", TEXT)])
    load(tid, [row])


def auto_sync(log=print):
    """给流水线末尾自动调用,失败不影响主流程。"""
    try:
        sync()
    except Exception as e:
        log(f"[提示] 飞书自动同步失败(不影响主流程): {e}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "sync"
    {"sync": sync, "push": push, "pull": lambda: print(f"拉回 {pull_accounts()} 个账号")}\
        .get(cmd, sync)()
