"""英文改文 Agent —— 5 步流水线(写审分离,标问题不打分,自动修复)。

    python rewrite_agent.py test             # 内置样例快速试跑
    python rewrite_agent.py golden           # 跑 golden/*.txt 金标集(prompt 改动后回归用)
    python rewrite_agent.py compare <txt文件> # 同一部小说 agent vs simple 双版对比

流水线:ANALYZE(题材+名场面) -> SELECT(题材加权钩子分配,零调用)
       -> DRAFT(节拍模板+caption+标签, 1次调用出N稿)
       -> 规则预检(词数/首句/CTA/剧透/风险词, 零调用)
       -> REVIEW(独立审稿人,引证式标问题) -> REVISE(仅问题稿, ≤2轮)
       -> TTS规范化(数字/缩写/标点, 零调用)

被 rewrite.py 调用:generate_scripts(text, keyword, app, n) -> [dict],
任何不可恢复失败抛 AgentError,由 rewrite.py 回落 simple 模式。
凭证读 deepseek.json。决策日志落 logs/rewrite_agent/。
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import sys
import time
from pathlib import Path

import requests

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from src import credentials  # noqa: E402

LOG_ROOT = ROOT / "logs" / "rewrite_agent"
HOOK_HISTORY = ROOT / "logs" / "hook_lines.txt"   # 不能放 output/scripts/,会被当脚本配音

_DS = requests.Session()
_DS.trust_env = False          # DeepSeek 国内直连,忽略 VPN 代理

MAX_CALLS_PER_NOVEL = 10       # 预算护栏:每部小说 LLM 调用硬上限
MAX_REVISE_ROUNDS = 2          # 每稿最多修 2 轮,仍不过则带 flag 接受

TEMP = {"analyze": 0.3, "draft": 1.0, "review": 0.2, "revise": 0.9}


class AgentError(RuntimeError):
    """不可恢复失败(调 rewrite.py 回落 simple 模式)。"""


# ==================== 钩子风格库(英文短视频版) ====================
HOOK_LIBRARY = {
    "shock_line": {
        "name": "Shock line",
        "rule": "Open with ONE short statement that violates normal expectations.",
        "example": '"She buried her husband on Monday. He texted her on Friday."',
    },
    "burning_question": {
        "name": "Burning question",
        "rule": "Open with a direct question to the viewer that they can't help answering in their head.",
        "example": '"What would you do if your husband\'s mistress showed up at your mother\'s funeral?"',
    },
    "mid_scene_drop": {
        "name": "Mid-scene drop",
        "rule": "Drop the viewer into the MIDDLE of the most dramatic scene — dialogue or action already in motion, no setup.",
        "example": '"Sign it," he said, sliding the divorce papers across her hospital bed.',
    },
    "betrayal_reveal": {
        "name": "Betrayal reveal",
        "rule": "Front-load a betrayal or identity reversal in the first sentence.",
        "example": '"Her sister\'s name was on the divorce papers — as the other woman."',
    },
    "countdown_stakes": {
        "name": "Countdown stakes",
        "rule": "Open with a deadline plus what it costs to miss it.",
        "example": '"She had 24 hours to find a husband, or lose everything her father left her."',
    },
    "pov_whisper": {
        "name": "POV whisper",
        "rule": "First person, confessional, like whispering a secret to the viewer.",
        "example": '"I wasn\'t supposed to see that letter. Now I can\'t un-know what my husband did."',
    },
}

# 题材 -> 钩子偏好顺序(A4 题材感知;v2 会换成按发布数据加权)
GENRE_WEIGHTS = {
    "revenge":     ["betrayal_reveal", "shock_line", "countdown_stakes", "mid_scene_drop", "pov_whisper", "burning_question"],
    "werewolf":    ["shock_line", "betrayal_reveal", "mid_scene_drop", "pov_whisper", "burning_question", "countdown_stakes"],
    "billionaire": ["betrayal_reveal", "pov_whisper", "shock_line", "mid_scene_drop", "burning_question", "countdown_stakes"],
    "romance":     ["pov_whisper", "betrayal_reveal", "burning_question", "shock_line", "mid_scene_drop", "countdown_stakes"],
    "thriller":    ["countdown_stakes", "shock_line", "mid_scene_drop", "burning_question", "betrayal_reveal", "pov_whisper"],
    "fantasy":     ["shock_line", "mid_scene_drop", "burning_question", "pov_whisper", "betrayal_reveal", "countdown_stakes"],
    "drama":       ["betrayal_reveal", "shock_line", "pov_whisper", "burning_question", "mid_scene_drop", "countdown_stakes"],
    "other":       ["shock_line", "betrayal_reveal", "burning_question", "mid_scene_drop", "pov_whisper", "countdown_stakes"],
}
GENRES = list(GENRE_WEIGHTS.keys())

# 题材 -> BookTok 标签池(C3)
GENRE_TAGS = {
    "revenge":     "#revenge #karma",
    "werewolf":    "#werewolf #alphamale #fatedmates",
    "billionaire": "#billionaire #ceo #contractmarriage",
    "romance":     "#romance #romancebooks",
    "thriller":    "#thriller #suspense",
    "fantasy":     "#fantasy #fantasybooks",
    "drama":       "#familydrama #storytime",
    "other":       "#storytime",
}
BASE_TAGS = "#BookTok #fyp #novel"


# ==================== LLM 调用(预算+重试+日志) ====================
class _Budget:
    def __init__(self, limit: int = MAX_CALLS_PER_NOVEL):
        self.used, self.limit = 0, limit

    def spend(self, tag: str):
        if self.used >= self.limit:
            raise AgentError(f"预算耗尽({self.limit}次调用),最后步骤: {tag}")
        self.used += 1


def _chat(system: str, user: str, temperature: float, max_tokens: int = 4000,
          retries: int = 3) -> str:
    cfg = credentials.load("deepseek")
    if credentials.missing("deepseek", cfg, ("api_key",)):
        raise AgentError(
            "缺少 DeepSeek 凭证：请设置 DEEPSEEK_API_KEY 或创建 deepseek.json。")
    body = {"model": cfg.get("model", "deepseek-chat"),
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": temperature, "max_tokens": max_tokens}
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    headers = {"Authorization": "Bearer " + cfg["api_key"],
               "Content-Type": "application/json"}
    last = None
    for i in range(retries):
        try:
            r = _DS.post(url, headers=headers, json=body, timeout=180).json()
            if "choices" in r:
                return r["choices"][0]["message"]["content"]
            last = RuntimeError(credentials.redact_text(
                f"DeepSeek 返回异常: {str(r)[:200]}", secrets=(cfg["api_key"],)))
        except requests.RequestException as e:
            last = e
        if i < retries - 1:
            time.sleep(3 * (i + 1))
    raise AgentError(f"LLM 调用失败: {last}")


class _Logger:
    """决策日志:每步 prompt+response 落盘,坏稿可溯源。"""

    def __init__(self, keyword: str):
        stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.dir = LOG_ROOT / f"{stamp}_{keyword}"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.step = 0

    def save(self, name: str, content: str):
        self.step += 1
        (self.dir / f"{self.step:02d}_{name}.txt").write_text(content, encoding="utf-8")


# ==================== 解析工具(分隔符格式,容错) ====================
def _split_blocks(raw: str, marker: str = "SCRIPT") -> dict[int, str]:
    """按 ---SCRIPT N--- 切块,返回 {N: 块内容}。容错:允许 #/*/空格变体。"""
    s = re.sub(r"^```.*$", "", raw.strip(), flags=re.MULTILINE)
    parts = re.split(rf"-{{2,}}\s*{marker}\s*(\d+)\s*-{{2,}}", s, flags=re.I)
    out = {}
    for i in range(1, len(parts) - 1, 2):
        try:
            out[int(parts[i])] = parts[i + 1].strip()
        except (ValueError, IndexError):
            continue
    return out


def _field(block: str, name: str) -> str:
    m = re.search(rf"^\s*{name}\s*[::]\s*(.+?)\s*$", block, flags=re.I | re.M)
    return m.group(1).strip() if m else ""


def _body_of(block: str) -> str:
    """取 BODY: 之后的全部文本;没有 BODY 标记就把非字段行当正文。"""
    m = re.search(r"^\s*BODY\s*[::]\s*$", block, flags=re.I | re.M)
    if m:
        return block[m.end():].strip()
    m = re.search(r"^\s*BODY\s*[::]\s*(.+)", block, flags=re.I | re.M | re.S)
    if m:
        return m.group(1).strip()
    # 容错:去掉已知字段行,剩下的是正文
    lines = [ln for ln in block.splitlines()
             if not re.match(r"^\s*(HOOK_TYPE|HOOK_LINE|CAPTION|HASHTAGS|VERDICT|PROBLEMS)\s*[::]", ln, flags=re.I)]
    return "\n".join(lines).strip()


# ==================== ① ANALYZE:题材 + 名场面提取 ====================
ANALYZE_SYS = (
    "You are a story analyst for viral book-promo TikTok videos. You dissect web "
    "novels to find the exact moments that hook viewers. Be precise; quote the "
    "source verbatim when asked. Output in the exact format requested, nothing else.")

ANALYZE_USER = """Analyze the opening chapters of this web novel.

OUTPUT FORMAT (exactly this structure, plain text):
GENRE: <one of: {genres}>
SYNOPSIS: <3-5 sentences: main characters, central conflict, what is at stake>
---SCENE 1---
TYPE: <betrayal / shock / twist / confrontation / emotional / mystery>
POTENTIAL: <high / medium>
QUOTE: <1-3 verbatim sentences copied from the source that anchor this moment>
SUMMARY: <1-2 sentences: what happens and why it grips a viewer>
---SCENE 2---
...

RULES:
- Extract 4-8 scenes with the strongest hook potential (conflict, suspense, reversal, emotional spike).
- QUOTE must be copied verbatim from the text below - do not paraphrase inside QUOTE.
- Rank nothing; just mark POTENTIAL high or medium.
- Scenes must come only from the text provided.

SOURCE TEXT:
\"\"\"
{text}
\"\"\"

# CHECK before you output #
Every scene has TYPE / POTENTIAL / QUOTE / SUMMARY. QUOTE lines are verbatim. GENRE is one of the allowed values. If any check fails, fix it and output again."""


def _analyze(text: str, budget: _Budget, log: _Logger) -> dict:
    budget.spend("analyze")
    user = ANALYZE_USER.format(genres=" / ".join(GENRES), text=text[:12000])
    raw = _chat(ANALYZE_SYS, user, TEMP["analyze"], max_tokens=3000)
    log.save("analyze", raw)
    genre = (_field(raw, "GENRE") or "other").lower().strip()
    genre = next((g for g in GENRES if g in genre), "other")
    syn_m = re.search(r"SYNOPSIS\s*[::]\s*(.+?)(?=^-{2,}\s*SCENE|\Z)", raw,
                      flags=re.I | re.M | re.S)
    synopsis = syn_m.group(1).strip() if syn_m else ""
    scenes = []
    for _, blk in sorted(_split_blocks(raw, "SCENE").items()):
        sc = {"type": _field(blk, "TYPE"), "potential": _field(blk, "POTENTIAL").lower(),
              "quote": _field(blk, "QUOTE") or _body_of(blk)[:300],
              "summary": _field(blk, "SUMMARY")}
        if sc["quote"] or sc["summary"]:
            scenes.append(sc)
    if not scenes:
        raise AgentError("ANALYZE 未解析出任何名场面")
    scenes.sort(key=lambda s: 0 if s["potential"] == "high" else 1)
    return {"genre": genre, "synopsis": synopsis, "scenes": scenes}


# ==================== ② SELECT:题材加权钩子分配(零调用) ====================
def _select(genre: str, n: int, scenes: list[dict]) -> list[dict]:
    """给 N 稿各分配 钩子套路 + 名场面。

    钩子(hook_type)= 杠杆框架的一个杠杆;选臂逻辑(explore/exploit / 冻结冠军)
    全部走 src/levers.choose_arms,方向文件 logs/direction_hook_type.json。
    无方向文件时回退静态 GENRE_WEIGHTS,行为接近旧版。只决定用哪个钩子,不碰写法。
    """
    from src import levers
    static = GENRE_WEIGHTS.get(genre, GENRE_WEIGHTS["other"])
    hooks = levers.choose_arms("hook_type", genre, n, static)
    return [{"hook_type": hooks[i], "scene": scenes[i % len(scenes)]} for i in range(n)]


# ==================== ③ DRAFT:起草(节拍模板 + caption + 标签) ====================
DRAFT_SYS = (
    "You are a top scriptwriter for viral faceless TikTok book-promo videos. Your "
    "narration scripts hook viewers in the first seconds and make them desperate to "
    "keep reading. You follow format and hard rules exactly.")

DRAFT_USER = """Write {n} narration script(s) promoting this web novel, one per assignment below.

STORY BRIEF:
Genre: {genre}
Synopsis: {synopsis}

ASSIGNMENTS (each script MUST use its assigned hook style and anchor scene):
{assignments}

BEAT STRUCTURE (every script follows this arc):
1. HOOK (first sentence, 0-5s): apply the assigned hook style. HARD RULE: first sentence <= 15 words.
2. ESCALATE: expand the conflict fast - who, what betrayal/danger/secret.
3. MINI-CLIFF (~every 70-90 words): drop a new question or reversal so viewers don't swipe away.
4. EMOTIONAL SPIKE: the moment that makes viewers feel for the character.
5. FINAL CLIFF: end mid-tension on the biggest unanswered question. NEVER resolve or reveal the ending.

HARD RULES:
- LENGTH: 300-700 words of narration per script. Tight beats padded.
- STAY FAITHFUL: use ONLY characters and events from the synopsis/scenes/source. No invented plot.
- Anchor each script on its assigned scene; you may pull supporting detail from the source text.
- Keep sentences short (mostly <= 20 words) - this is read aloud and shown as subtitles.
- Natural spoken US English. No literary/bookish phrasing.
- Do NOT write any call-to-action, app name, "download", "read more", or links.
- Do NOT reveal or hint at the story's ending.
- Avoid profanity and sexually explicit or graphically violent wording (imply, don't state).
- Avoid these overused opening patterns from past videos:
{avoid_openings}

SOURCE TEXT (for supporting detail):
\"\"\"
{text}
\"\"\"

OUTPUT FORMAT for each script (plain text, exactly this structure):
---SCRIPT 1---
HOOK_TYPE: <the assigned hook style id>
CAPTION: <one suspense-bait caption for the TikTok post, <=100 chars, no app names, no spoilers>
HASHTAGS: {base_tags} {genre_tags} <plus 0-2 more relevant tags>
BODY:
<the narration script>
---SCRIPT 2---
...

# CHECK before you output #
For every script: first sentence <= 15 words? uses its assigned hook style? 300-700 words? no app/download words? ending not revealed? CAPTION present and spoiler-free? If any check fails, fix it and output again."""


def _draft(text: str, analysis: dict, picks: list[dict], budget: _Budget,
           log: _Logger) -> dict[int, dict]:
    budget.spend("draft")
    assignments = []
    for i, p in enumerate(picks, 1):
        h = HOOK_LIBRARY[p["hook_type"]]
        sc = p["scene"]
        assignments.append(
            f"SCRIPT {i}: hook style [{p['hook_type']}] - {h['rule']} Example: {h['example']}\n"
            f"  Anchor scene: {sc['summary']}\n  Scene quote: {sc['quote']}")
    avoid = _recent_hook_lines()
    user = DRAFT_USER.format(
        n=len(picks), genre=analysis["genre"], synopsis=analysis["synopsis"],
        assignments="\n".join(assignments),
        avoid_openings="\n".join(f"  - {ln}" for ln in avoid) if avoid else "  (none yet)",
        text=text[:12000], base_tags=BASE_TAGS,
        genre_tags=GENRE_TAGS.get(analysis["genre"], GENRE_TAGS["other"]))
    raw = _chat(DRAFT_SYS, user, TEMP["draft"], max_tokens=8000)
    log.save("draft", raw)
    blocks = _split_blocks(raw, "SCRIPT")
    out = {}
    for i, p in enumerate(picks, 1):
        blk = blocks.get(i)
        if not blk:
            continue
        out[i] = {"hook_type": _field(blk, "HOOK_TYPE") or p["hook_type"],
                  "caption": _field(blk, "CAPTION"),
                  "hashtags": _field(blk, "HASHTAGS"),
                  "body": _body_of(blk)}
    if not out:
        raise AgentError("DRAFT 未解析出任何脚本")
    return out


# ==================== ④a 规则预检(零调用,B1) ====================
CTA_RE = re.compile(
    r"\b(download|install|app\s*store|link\s+in\s+bio|read\s+(the\s+)?(full|more|rest)|"
    r"good\s*novel|goodnovel|moto\s*novel|novel\s*master|dreame|galatea)\b", re.I)
SPOILER_RE = re.compile(
    r"\b(in\s+the\s+end|the\s+ending|at\s+the\s+end\s+of\s+the\s+story|"
    r"ends?\s+up\s+(marrying|together|dead|dying)|spoiler)\b", re.I)
RISK_RE = re.compile(
    r"\b(fuck\w*|shit\w*|bitch\w*|asshole|cunt|rape\w*|molest\w*|incest|orgasm|porn\w*|"
    r"slaughter\w*|mutilat\w*|decapitat\w*|dismember\w*|suicide)\b", re.I)


def _first_sentence(body: str) -> str:
    m = re.match(r"\s*(.+?[.!?])(\s|$)", body.replace("\n", " "))
    return m.group(1).strip() if m else body.strip().split("\n")[0]


def rule_precheck(body: str, hybrid: bool = False) -> list[str]:
    """确定性硬检查,免费。返回问题清单(空=通过)。hybrid=True 时跳过首句词数(风格规则)。"""
    problems = []
    words = len(body.split())
    if not 250 <= words <= 780:
        problems.append(f"[length] {words} words - must be 300-700")
    first = _first_sentence(body)
    if not hybrid and len(first.split()) > 15:
        problems.append(f"[hook] first sentence has {len(first.split())} words - must be <= 15: \"{first[:80]}\"")
    for regex, tag in ((CTA_RE, "cta-leak"), (SPOILER_RE, "spoiler"), (RISK_RE, "risk-word")):
        hits = sorted({m.group(0).lower() for m in regex.finditer(body)})
        if hits:
            problems.append(f"[{tag}] contains: {', '.join(hits)} - remove/rephrase")
    return problems


# ==================== ④b REVIEW:独立审稿(引证式,标问题不打分) ====================
REVIEW_SYS = (
    "You are a harsh, independent script reviewer for TikTok book-promo videos. You "
    "do not score; you mark concrete problems tied to exact sentences. You verify "
    "factual claims against the provided source material and mark anything "
    "unsupported as invented. If a script is genuinely strong, pass it.")

REVIEW_USER = """Review the following narration script(s) against these 4 criteria ONLY:

1. HOOK: Is the first sentence genuinely gripping for a cold viewer scrolling past? (weak = problem)
2. SUSPENSE CHAIN: Does a new question/reversal land roughly every 70-90 words? Mark dead stretches.
3. FAITHFULNESS: For each major plot claim, check it against the SOURCE MATERIAL below.
   If you cannot point to a supporting quote/synopsis line, mark it: [invented] "<claim>" - no support.
4. SPOKEN FLOW: Mark sentences that are bookish, tangled, or too long to speak naturally.

VERDICT POLICY (strict):
- FAIL only for CONCRETE defects: invented content, ending revealed/hinted, a dead
  stretch of 120+ words with no new question, sentences unspeakable aloud, a first
  sentence that is confusing or generic, or reads like a summary instead of a hook.
- Do NOT fail a script merely because the hook "could be even stronger" or for
  taste-level polish - write those as "- [advisory] ..." lines and still PASS.

SOURCE MATERIAL (the only ground truth):
Synopsis: {synopsis}
Scene quotes:
{quotes}

SCRIPTS TO REVIEW:
{scripts}

OUTPUT FORMAT (plain text, exactly):
---SCRIPT 1---
VERDICT: PASS or FAIL
PROBLEMS:
- [hook|suspense|invented|flow|ending] <specific problem, quote the offending sentence>
(write "- none" if PASS with no problems)
---SCRIPT 2---
...

# CHECK before you output #
Every script has a VERDICT. Every FAIL has at least one concrete problem with a quoted sentence. Do not rewrite the scripts; only review."""


def _review(analysis: dict, drafts: dict[int, dict], indices: list[int],
            budget: _Budget, log: _Logger) -> dict[int, dict]:
    budget.spend("review")
    quotes = "\n".join(f"  - {s['quote']}" for s in analysis["scenes"] if s["quote"])
    scripts_txt = "\n".join(f"---SCRIPT {i}---\n{drafts[i]['body']}" for i in indices)
    user = REVIEW_USER.format(synopsis=analysis["synopsis"], quotes=quotes,
                              scripts=scripts_txt)
    raw = _chat(REVIEW_SYS, user, TEMP["review"], max_tokens=3000)
    log.save("review", raw)
    blocks = _split_blocks(raw, "SCRIPT")
    out = {}
    for i in indices:
        blk = blocks.get(i, "")
        verdict = _field(blk, "VERDICT").upper()
        probs = [ln.strip("- ").strip() for ln in blk.splitlines()
                 if ln.strip().startswith("-") and "none" not in ln.lower()
                 and "[advisory]" not in ln.lower()]     # advisory 不算问题,不触发修稿
        out[i] = {"pass": verdict.startswith("PASS"), "problems": probs}
        if not blk:  # 审稿输出缺失该稿 -> 视为通过(不阻塞),打标
            out[i] = {"pass": True, "problems": ["(review missing - accepted unreviewed)"]}
    return out


# ==================== ⑤ REVISE:修稿(仅问题稿) ====================
REVISE_SYS = DRAFT_SYS

REVISE_USER = """Rewrite the following script(s) to fix ALL listed problems. Keep each script's
assigned hook style, anchor scene and overall story. Keep all HARD RULES from before:
first sentence <= 15 words; 300-700 words; short spoken sentences; faithful to source;
no CTA/app names; never reveal the ending; no profanity/explicit wording.

STORY BRIEF:
Genre: {genre}
Synopsis: {synopsis}
Scene quotes (ground truth):
{quotes}

SCRIPTS TO FIX:
{items}

OUTPUT FORMAT (plain text, exactly):
---SCRIPT <same number>---
HOOK_TYPE: <keep the same>
CAPTION: <keep or improve, <=100 chars, no spoilers/app names>
HASHTAGS: <keep or improve>
BODY:
<the fixed narration script>

# CHECK before you output #
Every listed problem is actually fixed. First sentence <= 15 words. Ending still unrevealed. If any check fails, fix and output again."""


def _revise(analysis: dict, drafts: dict[int, dict], problems: dict[int, list[str]],
            budget: _Budget, log: _Logger) -> dict[int, dict]:
    budget.spend("revise")
    quotes = "\n".join(f"  - {s['quote']}" for s in analysis["scenes"] if s["quote"])
    items = []
    for i, probs in problems.items():
        d = drafts[i]
        plist = "\n".join(f"  - {p}" for p in probs)
        items.append(f"---SCRIPT {i}--- (hook style: {d['hook_type']})\n"
                     f"PROBLEMS TO FIX:\n{plist}\nCURRENT BODY:\n{d['body']}")
    user = REVISE_USER.format(genre=analysis["genre"], synopsis=analysis["synopsis"],
                              quotes=quotes, items="\n\n".join(items))
    raw = _chat(REVISE_SYS, user, TEMP["revise"], max_tokens=8000)
    log.save("revise", raw)
    blocks = _split_blocks(raw, "SCRIPT")
    out = {}
    for i in problems:
        blk = blocks.get(i)
        if not blk:
            continue
        body = _body_of(blk)
        if len(body.split()) < 100:      # 解析噪音防御:过短视为失败,保留原稿
            continue
        out[i] = {"hook_type": _field(blk, "HOOK_TYPE") or drafts[i]["hook_type"],
                  "caption": _field(blk, "CAPTION") or drafts[i]["caption"],
                  "hashtags": _field(blk, "HASHTAGS") or drafts[i]["hashtags"],
                  "body": body}
    return out


# ==================== ⑥ TTS 规范化(零调用,C1/C2) ====================
_ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
         "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
         "sixteen", "seventeen", "eighteen", "nineteen"]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy",
         "eighty", "ninety"]
_ORDINALS = {"1st": "first", "2nd": "second", "3rd": "third", "4th": "fourth",
             "5th": "fifth", "6th": "sixth", "7th": "seventh", "8th": "eighth",
             "9th": "ninth", "10th": "tenth"}
_ABBREV = {"Mr.": "Mister", "Mrs.": "Missus", "Ms.": "Miss", "Dr.": "Doctor",
           "St.": "Saint", "Jr.": "Junior", "Sr.": "Senior", "vs.": "versus",
           "No.": "number", "Ave.": "Avenue"}


def _n2w(n: int) -> str:
    if n < 20:
        return _ONES[n]
    if n < 100:
        return _TENS[n // 10] + ("-" + _ONES[n % 10] if n % 10 else "")
    if n < 1000:
        return _ONES[n // 100] + " hundred" + (" " + _n2w(n % 100) if n % 100 else "")
    if n < 10**6:
        return _n2w(n // 1000) + " thousand" + (" " + _n2w(n % 1000) if n % 1000 else "")
    if n < 10**9:
        return _n2w(n // 10**6) + " million" + (" " + _n2w(n % 10**6) if n % 10**6 else "")
    return str(n)


def _num_to_words(m: re.Match) -> str:
    raw = m.group(0)
    n = int(raw.replace(",", ""))
    if 1900 <= n <= 2099 and "," not in raw:          # 年份:读成两段
        if n % 100 == 0:
            return _n2w(n // 100) + " hundred"
        if n % 100 < 10:
            return _n2w(n // 100) + " oh " + _n2w(n % 100)
        return _n2w(n // 100) + " " + _n2w(n % 100)
    return _n2w(n)


def normalize_tts(text: str) -> str:
    """数字/货币/百分比/缩写/标点 规范化,让 XTTS 读得顺、字幕切得齐。"""
    t = text
    t = t.replace("—", ", ").replace("–", ", ")     # em/en dash
    t = t.replace("…", "...")
    t = (t.replace("“", '"').replace("”", '"')
          .replace("‘", "'").replace("’", "'"))
    t = re.sub(r"[*_#>`]+", "", t)                            # markdown 残留
    for a, b in _ABBREV.items():
        t = re.sub(rf"(?<!\w){re.escape(a)}", b, t)
    for a, b in _ORDINALS.items():
        t = re.sub(rf"\b{a}\b", b, t)
    t = re.sub(r"\$\s*([\d,]+)(?:\.\d+)?\s*(million|billion|thousand)?",
               lambda m: (_n2w(int(m.group(1).replace(",", ""))) +
                          (" " + m.group(2) if m.group(2) else "") + " dollars"), t)
    t = re.sub(r"([\d,]+)\s*%", lambda m: _n2w(int(m.group(1).replace(",", ""))) + " percent", t)
    t = re.sub(r"\b\d{1,3}(?:,\d{3})+\b|\b\d+\b", _num_to_words, t)
    t = t.translate(str.maketrans("éèêëáàâäíìîïóòôöúùûüçñ",
                                  "eeeeaaaaiiiioooouuuucn"))  # 重音转写(fiancé→fiance)
    t = re.sub(r"[^\x00-\x7F]+", " ", t)                      # 兜底去非 ASCII(emoji等)
    t = re.sub(r"\s+([,.!?;])", r"\1", t)                     # 标点前多余空格(破折号转逗号产生)
    t = re.sub(r",{2,}", ",", t)
    t = re.sub(r"[ \t]{2,}", " ", t)
    return t.strip()


# ==================== 跨稿首句历史(B3 轻量版:仅提示避开) ====================
def _recent_hook_lines(limit: int = 20) -> list[str]:
    if not HOOK_HISTORY.exists():
        return []
    lines = [ln.strip() for ln in HOOK_HISTORY.read_text(encoding="utf-8").splitlines()
             if ln.strip()]
    return lines[-limit:]


def _record_hook_lines(hooks: list[str]):
    HOOK_HISTORY.parent.mkdir(parents=True, exist_ok=True)
    with HOOK_HISTORY.open("a", encoding="utf-8") as f:
        for h in hooks:
            f.write(h + "\n")


# ==================== HYBRID:旧版文笔 + 新版素材/保真/多样性 ====================
# 设计原则:分析层只提供"素材和方向",prompt 绝不规定"怎么写"(无句长/首句/节拍硬规则)。
HYBRID_DRAFT_SYS = (
    "You are a top scriptwriter for viral faceless TikTok book-promo videos. Your "
    "narration scripts hook viewers in the first seconds and make them desperate to "
    "keep reading, so they download the app to continue.")

HYBRID_DRAFT_USER = """Write {n} narration script(s) for short videos promoting this web novel.

REQUIREMENTS (follow strictly):
- LENGTH: aim for 2 to 5 minutes of spoken narration (roughly 300-700 words). Length should serve the hook — a tight, gripping script beats a long padded one. Never add filler just to reach a length.
- STAY FAITHFUL: build the hook ONLY from the actual story, characters, and events below. You may heighten the drama, tension, and tone, but do NOT invent new characters, new plot twists, or an ending that the text does not support.
- Open with an irresistible hook in the very first sentence (a shocking line, a burning question, a dramatic moment).
- Build suspense and emotional tension using the ACTUAL story below.
- Create a strong curiosity gap: tease the central conflict / mystery / romance / twist.
- END on a cliffhanger. NEVER resolve the story or reveal the ending — leave the viewer aching to know what happens next.
- Do NOT write any call-to-action, app name, "download", "read more", or links. The desire to continue must come purely from the story.
- Natural spoken English, vivid, punchy, conversational — written to be read aloud by a narrator. First-person confessional voice is often the most gripping when it fits the story.
- Avoid profanity and sexually explicit or graphically violent wording (imply, don't state).

STORY MATERIAL (raw material from analysis — use freely, this is NOT an outline to follow):
Genre: {genre}
Synopsis: {synopsis}
Most gripping moments found in the text:
{scenes}

ANGLES — each script should OPEN from a different angle so the {n} videos don't feel like copies.
Suggested angle per script (adapt freely; quality always beats compliance):
{angles}

Avoid echoing these opening lines from past videos:
{avoid}

FULL TEXT:
\"\"\"
{text}
\"\"\"

OUTPUT FORMAT for each script (plain text, exactly this structure):
---SCRIPT 1---
ANGLE: <the angle id you used>
CAPTION: <one suspense-bait caption for the TikTok post, <=100 chars, no app names, no spoilers>
HASHTAGS: {base_tags} {genre_tags} <plus 0-2 more relevant tags>
BODY:
<the narration script>
---SCRIPT 2---
...

# CHECK before you output #
Every script: faithful to the story? ends on an unresolved cliffhanger? no app/download words? CAPTION present and spoiler-free? scripts open from different angles? If any check fails, fix it and output again."""

HYBRID_REVISE_USER = """Fix ONLY the listed problems in the script(s) below. This is surgical repair:
- PRESERVE the script's voice, point of view, sentence rhythm and emotional flow.
- Do NOT restructure, shorten or rewrite beyond what the fixes strictly require.
- Keep faithful to the story; keep the ending unresolved; no app names or call-to-action.

STORY GROUND TRUTH:
Synopsis: {synopsis}
Scene quotes:
{quotes}

SCRIPTS TO FIX:
{items}

OUTPUT FORMAT (plain text, exactly):
---SCRIPT <same number>---
ANGLE: <keep the same>
CAPTION: <keep or minimally fix>
HASHTAGS: <keep>
BODY:
<the repaired script>"""


def _draft_hybrid(text: str, analysis: dict, picks: list[dict], budget: _Budget,
                  log: _Logger) -> dict[int, dict]:
    budget.spend("draft_hybrid")
    scenes_txt = "\n".join(f"  - {s['summary']} (from text: {s['quote']})"
                           for s in analysis["scenes"])
    angles = []
    for i, p in enumerate(picks, 1):
        h = HOOK_LIBRARY[p["hook_type"]]
        angles.append(f"  Script {i} [{p['hook_type']}]: open on — {p['scene']['summary']} "
                      f"(flavor: {h['rule']})")
    avoid = _recent_hook_lines()
    user = HYBRID_DRAFT_USER.format(
        n=len(picks), genre=analysis["genre"], synopsis=analysis["synopsis"],
        scenes=scenes_txt, angles="\n".join(angles),
        avoid="\n".join(f"  - {ln}" for ln in avoid) if avoid else "  (none yet)",
        text=text[:12000], base_tags=BASE_TAGS,
        genre_tags=GENRE_TAGS.get(analysis["genre"], GENRE_TAGS["other"]))
    raw = _chat(HYBRID_DRAFT_SYS, user, TEMP["draft"], max_tokens=8000)
    log.save("draft_hybrid", raw)
    blocks = _split_blocks(raw, "SCRIPT")
    out = {}
    for i, p in enumerate(picks, 1):
        blk = blocks.get(i)
        if not blk:
            continue
        out[i] = {"hook_type": _field(blk, "ANGLE") or p["hook_type"],
                  "caption": _field(blk, "CAPTION"),
                  "hashtags": _field(blk, "HASHTAGS"),
                  "body": _body_of(blk)}
    if not out:
        raise AgentError("HYBRID DRAFT 未解析出任何脚本")
    return out


def _revise_hybrid(analysis: dict, drafts: dict[int, dict], problems: dict[int, list[str]],
                   budget: _Budget, log: _Logger) -> dict[int, dict]:
    budget.spend("revise_hybrid")
    quotes = "\n".join(f"  - {s['quote']}" for s in analysis["scenes"] if s["quote"])
    items = []
    for i, probs in problems.items():
        d = drafts[i]
        plist = "\n".join(f"  - {p}" for p in probs)
        items.append(f"---SCRIPT {i}--- (angle: {d['hook_type']})\n"
                     f"PROBLEMS TO FIX:\n{plist}\nCURRENT BODY:\n{d['body']}")
    user = HYBRID_REVISE_USER.format(synopsis=analysis["synopsis"], quotes=quotes,
                                     items="\n\n".join(items))
    raw = _chat(HYBRID_DRAFT_SYS, user, TEMP["revise"], max_tokens=8000)
    log.save("revise_hybrid", raw)
    blocks = _split_blocks(raw, "SCRIPT")
    out = {}
    for i in problems:
        blk = blocks.get(i)
        if not blk:
            continue
        body = _body_of(blk)
        if len(body.split()) < 100:
            continue
        out[i] = {"hook_type": _field(blk, "ANGLE") or drafts[i]["hook_type"],
                  "caption": _field(blk, "CAPTION") or drafts[i]["caption"],
                  "hashtags": _field(blk, "HASHTAGS") or drafts[i]["hashtags"],
                  "body": body}
    return out


def generate_hybrid(text: str, keyword: str, app: str, n: int,
                    log_print=print) -> list[dict]:
    """杂交流水线:ANALYZE素材 -> 旧版风格自由起草(带角度多样性) ->
    规则预检+保真审核(仅致命缺陷) -> 手术式修复(≤1轮,保文风) -> TTS规范化。"""
    budget = _Budget()
    log = _Logger(keyword)
    log.save("input", f"[hybrid] keyword={keyword} app={app} n={n}\n\n{text[:12000]}")

    analysis = _analyze(text, budget, log)
    log_print(f"    [hybrid] 题材={analysis['genre']} 名场面={len(analysis['scenes'])}")
    picks = _select(analysis["genre"], n, analysis["scenes"])
    drafts = _draft_hybrid(text, analysis, picks, budget, log)
    log_print(f"    [hybrid] 起草 {len(drafts)} 稿: "
              + ", ".join(d["hook_type"] for d in drafts.values()))

    flags: dict[int, str] = {}
    pending = list(drafts.keys())
    for round_no in range(2):                        # 最多 1 轮修复
        rule_probs = {i: rule_precheck(drafts[i]["body"], hybrid=True) for i in pending}
        try:
            reviews = _review(analysis, drafts, pending, budget, log)
        except Exception as e:
            log_print(f"    [hybrid] 审核失败,接受未审稿: {e}")
            for i in pending:
                flags[i] = "unreviewed"
            break
        failing = {}
        for i in pending:
            probs = rule_probs[i] + (reviews[i]["problems"] if not reviews[i]["pass"] else [])
            if rule_probs[i] or not reviews[i]["pass"]:
                failing[i] = probs
        if not failing:
            break
        if round_no >= 1 or budget.used >= budget.limit - 1:
            for i in failing:
                flags[i] = "unresolved:" + "; ".join(failing[i])[:200]
            log_print(f"    [hybrid] {len(failing)} 稿修1轮未全过,带标接受")
            break
        log_print(f"    [hybrid] 手术式修复: {len(failing)} 稿 "
                  f"({sum(len(v) for v in failing.values())} 个问题)")
        fixed = _revise_hybrid(analysis, drafts, failing, budget, log)
        for i, d in fixed.items():
            drafts[i] = d
        pending = list(failing.keys())

    results = _finalize(drafts, analysis, flags)
    log.save("final", json.dumps(results, ensure_ascii=False, indent=2))
    log_print(f"    [hybrid] 完成 {len(results)} 稿(LLM调用 {budget.used} 次)")
    if not results:
        raise AgentError("hybrid 流水线未产出任何脚本")
    return results


def _finalize(drafts: dict[int, dict], analysis: dict, flags: dict[int, str]) -> list[dict]:
    """终稿组装:TTS规范化 + 标签去重 + 首句历史记录。"""
    results, hook_lines = [], []
    for i in sorted(drafts.keys()):
        d = drafts[i]
        body = normalize_tts(d["body"])
        hook_line = _first_sentence(body)
        hook_lines.append(hook_line)
        caption = (d.get("caption") or "").strip()
        hashtags = (d.get("hashtags") or
                    f"{BASE_TAGS} {GENRE_TAGS.get(analysis['genre'], '')}").strip()
        seen, tags = set(), []
        for tok in (caption + " " + hashtags).split():
            if tok.startswith("#"):
                if tok.lower() in seen:
                    continue
                seen.add(tok.lower())
                tags.append(tok)
        cap_text = " ".join(t for t in caption.split() if not t.startswith("#"))
        results.append({
            "body": body,
            "hook_type": d["hook_type"],
            "genre": analysis["genre"],
            "caption": (cap_text + " " + " ".join(tags)).strip(),
            "hook_line": hook_line,
            "review_flag": flags.get(i, "pass"),
        })
    _record_hook_lines(hook_lines)
    return results


# ==================== simple 模式附加层(不改正文,只加 caption/题材) ====================
CAPTION_SYS = ("You write TikTok post captions for book-promo videos. "
               "Output exactly in the requested format, nothing else.")

CAPTION_USER = """For each narration script below, write ONE TikTok caption.

RULES:
- Suspense-bait, <=100 chars, no spoilers, no app names, no "download".
- Also classify the story's genre as one of: {genres}

OUTPUT FORMAT (plain text, exactly):
GENRE: <genre>
---CAPTION 1---
<caption for script 1>
---CAPTION 2---
...

SCRIPTS:
{scripts}"""


def caption_pass(scripts: list[str], keyword: str) -> list[dict]:
    """给旧版(simple)脚本补 英文caption+标签+题材,1次调用。失败抛异常由调用方忽略。"""
    joined = "\n".join(f"---SCRIPT {i}---\n{s[:1500]}" for i, s in enumerate(scripts, 1))
    raw = _chat(CAPTION_SYS, CAPTION_USER.format(genres=" / ".join(GENRES), scripts=joined),
                temperature=0.7, max_tokens=1000)
    genre = (_field(raw, "GENRE") or "other").lower()
    genre = next((g for g in GENRES if g in genre), "other")
    blocks = _split_blocks(raw, "CAPTION")
    tags = f"{BASE_TAGS} {GENRE_TAGS.get(genre, GENRE_TAGS['other'])}"
    out = []
    for i in range(1, len(scripts) + 1):
        cap = (blocks.get(i) or "").strip().strip('"')[:120]
        out.append({"genre": genre,
                    "caption": (cap + " " + tags).strip() if cap else ""})
    return out


# ==================== 公开 API ====================
def generate_scripts(text: str, keyword: str, app: str, n: int,
                     log_print=print) -> list[dict]:
    """完整 agent 流水线。返回 [{body, hook_type, caption, hashtags, hook_line, genre,
    review_flag}];失败抛 AgentError(调用方回落 simple)。"""
    budget = _Budget()
    log = _Logger(keyword)
    log.save("input", f"keyword={keyword} app={app} n={n}\n\n{text[:12000]}")

    analysis = _analyze(text, budget, log)
    log_print(f"    [agent] 题材={analysis['genre']} 名场面={len(analysis['scenes'])}")
    picks = _select(analysis["genre"], n, analysis["scenes"])
    drafts = _draft(text, analysis, picks, budget, log)
    log_print(f"    [agent] 起草 {len(drafts)} 稿: "
              + ", ".join(d["hook_type"] for d in drafts.values()))

    flags: dict[int, str] = {}
    pending = list(drafts.keys())
    for round_no in range(MAX_REVISE_ROUNDS + 1):
        # 规则预检(免费) + LLM 审稿
        rule_probs = {i: rule_precheck(drafts[i]["body"]) for i in pending}
        try:
            reviews = _review(analysis, drafts, pending, budget, log)
        except AgentError:
            raise
        except Exception as e:                      # 审稿挂了:接受草稿,打标
            log_print(f"    [agent] 审稿失败,接受未审稿: {e}")
            for i in pending:
                flags[i] = "unreviewed"
            break
        failing = {}
        for i in pending:
            probs = rule_probs[i] + (reviews[i]["problems"] if not reviews[i]["pass"] else [])
            if rule_probs[i] or not reviews[i]["pass"]:
                failing[i] = probs
        if not failing:
            break
        if round_no >= MAX_REVISE_ROUNDS or budget.used >= budget.limit - 1:
            for i in failing:
                flags[i] = "unresolved:" + "; ".join(failing[i])[:200]
            log_print(f"    [agent] {len(failing)} 稿修{round_no}轮未全过,带标接受")
            break
        log_print(f"    [agent] 第{round_no+1}轮修稿: {len(failing)} 稿 "
                  f"({sum(len(v) for v in failing.values())} 个问题)")
        fixed = _revise(analysis, drafts, failing, budget, log)
        for i, d in fixed.items():
            drafts[i] = d
        pending = list(failing.keys())

    # 终稿:TTS 规范化 + 组装
    results, hook_lines = [], []
    for i in sorted(drafts.keys()):
        d = drafts[i]
        body = normalize_tts(d["body"])
        hook_line = _first_sentence(body)
        hook_lines.append(hook_line)
        caption = (d["caption"] or "").strip()
        hashtags = (d["hashtags"] or f"{BASE_TAGS} {GENRE_TAGS.get(analysis['genre'], '')}").strip()
        # 标签去重(大小写不敏感,保序);caption 里已带的标签不再重复
        seen, tags = set(), []
        for tok in (caption + " " + hashtags).split():
            if tok.startswith("#"):
                if tok.lower() in seen:
                    continue
                seen.add(tok.lower())
                tags.append(tok)
        cap_text = " ".join(t for t in caption.split() if not t.startswith("#"))
        results.append({
            "body": body,
            "hook_type": d["hook_type"],
            "genre": analysis["genre"],
            "caption": (cap_text + " " + " ".join(tags)).strip(),
            "hook_line": hook_line,
            "review_flag": flags.get(i, "pass"),
        })
    _record_hook_lines(hook_lines)
    log.save("final", json.dumps(results, ensure_ascii=False, indent=2))
    log_print(f"    [agent] 完成 {len(results)} 稿(LLM调用 {budget.used} 次)")
    if not results:
        raise AgentError("流水线未产出任何脚本")
    return results


# ==================== CLI:test / golden / compare ====================
_SAMPLE = (
    "The morning Eleanor was supposed to marry the Duke, she found a dead man in "
    "her wardrobe wearing her father's ring. No note. No blood. Just the ring she "
    "had buried with her father three years ago. Before she could scream, a hand "
    "covered her mouth and a voice she thought she'd never hear again whispered, "
    "'They're coming. If you want to live, pretend you never saw me.' It was "
    "Julian - her father's ward, the boy who vanished the night of the fire, the "
    "one they said had set it. Eleanor's wedding gown suddenly felt like a shroud. "
    "Downstairs, two hundred guests were waiting. The Duke was waiting. And the "
    "man the whole county believed was a murderer was hiding in her wardrobe with "
    "her dead father's ring. 'Marry him,' Julian whispered, 'and you will be dead "
    "before the honeymoon. Your father found out what the Duke did to his first "
    "wife. That is why your father is dead.' The wedding bells began to ring.")


def _print_scripts(results: list[dict]):
    for i, r in enumerate(results, 1):
        wc = len(r["body"].split())
        print(f"\n----- 脚本 {i} [{r['hook_type']}] {wc}词 flag={r['review_flag']} -----")
        print(f"HOOK: {r['hook_line']}")
        print(f"CAPTION: {r['caption']}")
        print(r["body"])


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "test"
    if cmd == "test":
        print("=== hybrid 试跑(内置样例, 2稿)===")
        _print_scripts(generate_hybrid(_SAMPLE, "TESTKEY", "Good Novel", 2))
    elif cmd == "golden":
        gdir = ROOT / "golden"
        files = sorted(gdir.glob("*.txt"))
        if not files:
            print(f"{gdir} 为空。放 2-3 部小说 txt 进去作金标集。"); return
        for f in files:
            print(f"\n===== 金标: {f.stem} =====")
            _print_scripts(generate_hybrid(f.read_text(encoding="utf-8"), f.stem,
                                           "Good Novel", 3))
    elif cmd == "compare":
        if len(sys.argv) < 3:
            print("用法: python rewrite_agent.py compare <小说txt路径>"); return
        text = Path(sys.argv[2]).read_text(encoding="utf-8")
        kw = Path(sys.argv[2]).stem
        print("=== HYBRID 版(新) ===")
        _print_scripts(generate_hybrid(text, kw, "Good Novel", 2))
        print("\n\n=== SIMPLE 版(旧流程)===")
        import rewrite
        for i, sc in enumerate(rewrite.rewrite_novel(text, kw, "Good Novel", 2), 1):
            wc = len(sc.split())
            print(f"\n----- 脚本 {i} ({wc}词) -----\n{sc}")
    else:
        print(f"未知命令 {cmd}。可用: test / golden / compare")


if __name__ == "__main__":
    main()
