"""改文:读飞书「改文输入」表 → DeepSeek 改写成 ~2分钟口播钩子脚本。

    python rewrite.py            # 处理飞书表里所有「待处理」行,脚本存 output/scripts/
    python rewrite.py test       # 用内置样例试跑一次(不碰飞书),看改写效果

目标:让观众看完想下载 app 继续读。脚本靠故事本身勾人、结在悬念,不写"下载"字样
(下载引导由视频上的引导词负责)。凭证读 deepseek.json。
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

import requests

_DS = requests.Session()
_DS.trust_env = False  # DeepSeek 国内直连,忽略VPN代理

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from src import credentials  # noqa: E402

SCRIPTS_DIR = ROOT / "output" / "scripts"

SYSTEM = (
    "You are a top scriptwriter for viral faceless TikTok book-promo videos. "
    "Your narration scripts hook viewers in the first seconds and make them "
    "desperate to keep reading, so they download the app to continue."
)

USER_TMPL = """Write {n} narration script(s) for short videos promoting this web novel.

REQUIREMENTS (follow strictly):
- LENGTH: aim for 2 to 5 minutes of spoken narration (roughly 300-700 words). Length should serve the hook — a tight, gripping script beats a long padded one. Never add filler just to reach a length.
- STAY FAITHFUL: build the hook ONLY from the actual story, characters, and events in the chapters below. You may heighten the drama, tension, and tone, but do NOT invent new characters, new plot twists, or an ending that the text does not support. The video must match what the reader will actually find.
- Open with an irresistible hook in the very first sentence (a shocking line, a burning question, a dramatic moment).
- Build suspense and emotional tension using the ACTUAL story below.
- Create a strong curiosity gap: tease the central conflict / mystery / romance / twist.
- END on a cliffhanger. NEVER resolve the story or reveal the ending — leave the viewer aching to know what happens next.
- Do NOT write any call-to-action, app name, "download", "read more", or links. The desire to continue must come purely from the story. (The app prompt is added separately on the video.)
- Natural spoken English, vivid, punchy, conversational — written to be read aloud by a narrator.
- The {n} scripts may be similar to each other; prioritize following these rules well over making them different.

Novel keyword (for your reference only, do not say it): {keyword}
App (for your reference only, do not say it): {app}

FIRST CHAPTERS:
\"\"\"
{text}
\"\"\"

OUTPUT FORMAT: plain text only. Output the {n} script(s) one after another, and
separate each script with a line containing exactly:
---SCRIPT---
Do NOT number them, do NOT use JSON or markdown, do NOT add any headings, labels,
or commentary. Just the narration text, split by the ---SCRIPT--- lines."""


def deepseek_chat(user: str, temperature: float = 0.9, retries: int = 3) -> str:
    cfg = credentials.load("deepseek")
    lack = credentials.missing("deepseek", cfg, ("api_key",))
    if lack:
        raise RuntimeError(
            "缺少 DeepSeek 凭证：请设置环境变量 DEEPSEEK_API_KEY，"
            "或在项目根创建 deepseek.json（格式见 .env.example）。")
    body = {
        "model": cfg.get("model", "deepseek-chat"),
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": user}],
        "temperature": temperature,
        "max_tokens": 4000,
    }
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    headers = {"Authorization": "Bearer " + cfg["api_key"],
               "Content-Type": "application/json"}
    last = None
    for i in range(retries):
        try:
            r = _DS.post(url, headers=headers, json=body, timeout=120).json()
            if "choices" in r:
                return r["choices"][0]["message"]["content"]
            last = RuntimeError(credentials.redact_text(
                f"DeepSeek 返回异常: {r}", secrets=(cfg["api_key"],)))
        except requests.RequestException as e:
            last = e
        if i < retries - 1:
            time.sleep(3 * (i + 1))
    raise last


def _parse_scripts(raw: str) -> list[str]:
    s = raw.strip()
    s = re.sub(r"^```.*$", "", s, flags=re.MULTILINE).strip()   # 去掉可能的代码围栏
    parts = re.split(r"-{2,}\s*SCRIPT\s*-{2,}", s, flags=re.I)
    scripts = [p.strip() for p in parts if len(p.split()) >= 30]  # 滤掉前言/碎片
    return scripts


def rewrite_novel(text: str, keyword: str, app: str, n: int = 4) -> list[str]:
    user = USER_TMPL.format(n=n, keyword=keyword, app=app, text=text[:12000])
    return _parse_scripts(deepseek_chat(user))


# ---------------- 飞书输入 ----------------
def read_pending():
    import feishu_sync as fs
    tbls = fs._tables()
    if "改文输入" not in tbls:
        return []
    rows = []
    for rec in fs._all_records(tbls["改文输入"]):
        f = rec.get("fields", {})
        kw = fs._txt(f.get("关键词"))
        text = fs._txt(f.get("原文前1-3章"))
        # 已处理的跳过;没关键词或没原文的空行跳过;其余(未处理/待处理/空状态)都算待办
        if fs._txt(f.get("状态")) == "已处理" or not kw or not text:
            continue
        rows.append({
            "record_id": rec["record_id"],
            "keyword": kw,
            "app": fs._txt(f.get("app")) or "Good Novel",
            "text": text,
            "n": fs._int(f.get("目标条数"), 0),   # 0 = 没填,按需求自动算
            "bg_dir": fs._txt(f.get("素材文件夹")),
        })
    return rows


def mark_done(record_id: str, note: str = ""):
    import feishu_sync as fs
    tid = fs._tables()["改文输入"]
    fs._put(fs._base(f"/tables/{tid}/records/{record_id}"),
            {"fields": {"状态": "已处理", "备注": note}})


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        return _test()
    rows = read_pending()
    if not rows:
        print("飞书「改文输入」表没有待处理的小说。")
        return

    # 方案B:按发布需求算改文数。总改文=⌈D/M⌉;少部小说深挖(每部≤上限),平均分。
    # 飞书填了"目标条数"的行,尊重填的;没填的自动算。
    import math
    from src import brain
    from src import config as cfgmod
    cfg = cfgmod.load()
    con = brain.connect()
    n_acc = con.execute("SELECT COUNT(*) FROM accounts WHERE active=1").fetchone()[0]
    con.close()
    D = min(n_acc * int(getattr(cfg, "per_account_videos", 3)),
            int(getattr(cfg, "daily_publish_cap", 21)))
    N = max(1, n_acc)                                     # 每关键词出 N 条视频 = 账号数
    m_min = int(getattr(cfg, "videos_per_audio_min", 2))
    keywords_per_day = min(len(rows), max(1, math.ceil(D / N)))
    S = max(1, N // m_min)                                # 每部改文数(每改文出 m_min 条 → S×m_min=N)
    rows = rows[:keywords_per_day]
    for row in rows:
        if not row.get("n"):                             # 没填目标条数 → 自动算
            row["n"] = S
    print(f"需求 D={D},每关键词 {N} 视频 → 做 {keywords_per_day} 部小说 × 每部 {S} 改文:"
          + ", ".join(r["keyword"] for r in rows))
    SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    meta_dir = SCRIPTS_DIR / "meta"
    meta_dir.mkdir(exist_ok=True)
    mode = str(getattr(cfg, "rewrite_mode", "agent")).lower()
    for row in rows:
        kw = row["keyword"] or "untitled"
        print(f"改写「{kw}」({row['app']}) 目标 {row['n']} 条 [{mode}] …")
        try:
            items = None
            if mode in ("hybrid", "agent"):
                try:
                    import rewrite_agent
                    fn = (rewrite_agent.generate_hybrid if mode == "hybrid"
                          else rewrite_agent.generate_scripts)
                    items = fn(row["text"], kw, row["app"], row["n"], log_print=print)
                except Exception as e:                 # 挂 -> 整部回落 simple
                    print(f"  [{mode}失败,回落simple] {type(e).__name__}: {e}")
            if items is None:
                scripts = rewrite_novel(row["text"], kw, row["app"], row["n"])
                items = [{"body": sc, "hook_type": "simple"} for sc in scripts]
                # 附加层(不改正文): TTS规范化 + 英文caption/标签/题材(失败就跳过)
                try:
                    import rewrite_agent
                    for it in items:
                        it["body"] = rewrite_agent.normalize_tts(it["body"])
                    for it, cap in zip(items, rewrite_agent.caption_pass(scripts, kw)):
                        it.update(cap)
                except Exception as e:
                    print(f"  [附加层跳过] {e}")
            note_hooks = []
            for i, it in enumerate(items, 1):
                stem = f"{kw}__{row['app']}__{i}"
                (SCRIPTS_DIR / f"{stem}.txt").write_text(it["body"], encoding="utf-8")
                meta = {k: v for k, v in it.items() if k != "body"}
                if meta:                               # agent 元数据 sidecar,出片时消费
                    (meta_dir / f"{stem}.json").write_text(
                        json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
                if it.get("hook_type"):
                    note_hooks.append(it["hook_type"])
            # 存该关键词的专用素材文件夹(出片时用)
            if row.get("bg_dir"):
                from src import brain
                brain.upsert_keyword(kw, bg_dir=row["bg_dir"])
            print(f"  ✓ 生成 {len(items)} 条脚本 -> {SCRIPTS_DIR}")
            note = f"生成{len(items)}条" + (f" 钩子:{','.join(note_hooks)}" if note_hooks else "")
            mark_done(row["record_id"], note)
        except Exception as e:
            print(f"  ✗ 失败: {e}")


def _test():
    sample = (
        "The morning Eleanor was supposed to marry the Duke, she found a dead "
        "man in her wardrobe wearing her father's ring. No note. No blood. Just "
        "the ring she had buried with her father three years ago. Before she "
        "could scream, a hand covered her mouth and a voice she thought she'd "
        "never hear again whispered, 'They're coming. If you want to live, "
        "pretend you never saw me.'")
    print("=== 试跑改写(内置样例)===")
    scripts = rewrite_novel(sample, "TESTKEY", "Good Novel", 2)
    for i, sc in enumerate(scripts, 1):
        wc = len(sc.split())
        print(f"\n----- 脚本 {i}  (约 {wc} 词 / {wc/145*60:.0f}秒) -----\n{sc}")


if __name__ == "__main__":
    main()
