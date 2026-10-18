"""飞星(UPMee)发布验证小脚本 —— 独立测试用,未接入主流水线。

功能:分片上传视频 → 发布(默认存草稿,安全)。也能查账号列表拿 media_id。

用法:
    1) 首次运行会生成 feixing.json 模板,填入 api_key 和 team_id
    2) 查账号(拿 media_id):
         python feixing_publish.py accounts
    3) 上传+存草稿(拿一条已生成的视频测试):
         python feixing_publish.py publish "output/7.31成品/C25517 2.mp4" --media <media_id>
       想真的发出去(谨慎):加 --live
       带封面/文案/音乐位置等可选参数见下方 argparse

说明:
    - 视频 >40MB 走分片上传(每片10MB);≤40MB 也照样能传。单视频需 ≤2GB。
    - 默认 is_draft=true:只上传成草稿,不会真的公开发布,方便你先验证链路。
    - is_ai_generated 默认 true(你的内容是 AI 处理的,TikTok 要求标注)。
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import requests

# 强制 UTF-8 输出，否则 Windows 重定向到文件时 GBK 编不了 ✓ 等字符会崩
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

BASE = "https://www.upmee.cc/open_api"
CHUNK = 10 * 1024 * 1024          # 10MB / 片
ROOT = Path(__file__).resolve().parent
CFG_PATH = ROOT / "feixing.json"
_SECRETS: tuple = ()

from src import credentials  # noqa: E402

# 忽略环境里的 VPN 代理(飞星国内直连,VPN 抽风时走代理会失败)
_S = requests.Session()
_S.trust_env = False


# ----------------- 配置 -----------------
def load_cfg() -> dict:
    """环境变量优先，其次 feixing.json。缺失时生成模板并给出可读提示。"""
    global _SECRETS
    cfg = credentials.load("feixing")
    lack = credentials.missing("feixing", cfg, ("api_key", "team_id"))
    if lack:
        if not CFG_PATH.exists():
            CFG_PATH.write_text(json.dumps({
                "api_key": "在飞星平台创建的 API-Key",
                "team_id": "你的团队ID",
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"已生成配置模板: {CFG_PATH}\n请填入 api_key 和 team_id 后再运行。")
        else:
            print(f"缺少飞星配置 {', '.join(lack)}：请填写 {CFG_PATH}，"
                  "或设置 FEIXING_API_KEY / FEIXING_TEAM_ID。")
        sys.exit(1)
    _SECRETS = (cfg.get("api_key", ""), cfg.get("team_id", ""))
    return cfg


# ----------------- HTTP 封装 -----------------
def _post_retry(url, retries=3, backoff=3, **kw):
    """POST,只对网络抖动(连接/超时)重试;业务错误(code!=0)由调用方处理不在此重试。"""
    import time
    last = None
    for i in range(retries):
        try:
            return _S.post(url, **kw)
        except requests.RequestException as e:
            last = e
            if i < retries - 1:
                time.sleep(backoff * (i + 1))
    raise last


def _check(resp, where: str) -> dict:
    try:
        d = resp.json()
    except Exception:
        raise RuntimeError(credentials.redact_text(
            f"{where} 返回非JSON (HTTP {resp.status_code}): {resp.text[:200]}",
            secrets=_SECRETS))
    if d.get("code") != 0:
        raise RuntimeError(credentials.redact_text(
            f"{where} 失败: code={d.get('code')} message={d.get('message')} "
            f"request_id={d.get('request_id')}", secrets=_SECRETS))
    return d.get("data", {})


def api_json(cfg, path: str, body: dict) -> dict:
    r = _post_retry(BASE + path,
                    headers={"API-Key": cfg["api_key"],
                             "Content-Type": "application/json"},
                    json=body, timeout=60)
    return _check(r, path)


# ----------------- 上传 -----------------
def upload_video(cfg, video: Path) -> str:
    """分片上传视频,返回 object_id(= 发布时的 video_id)。"""
    size = video.stat().st_size
    total = max(1, math.ceil(size / CHUNK))
    print(f"  文件 {video.name}  {size/1024/1024:.1f}MB  分 {total} 片")
    token = api_json(cfg, "/tool/s3/upload/v2/multipart/init",
                     {"total_parts": total})["upload_token"]
    with video.open("rb") as f:
        for pn in range(1, total + 1):
            chunk = f.read(CHUNK)
            r = _post_retry(BASE + "/tool/s3/upload/v2/multipart",
                            headers={"API-Key": cfg["api_key"]},
                            data={"part_number": str(pn), "upload_token": token},
                            files={"file": ("part", chunk)}, timeout=600)
            _check(r, f"分片上传 part={pn}")
            print(f"    分片 {pn}/{total} ✓")
    obj = api_json(cfg, "/tool/s3/upload/v2/multipart/complete",
                   {"upload_token": token})["object_id"]
    print(f"  合片完成 object_id={obj}")
    return obj


def upload_single(cfg, path: Path, ftype: str) -> str:
    """单文件上传(封面用 ftype='cover',小视频用 'video'),返回 object_id。"""
    with path.open("rb") as f:
        r = _S.post(BASE + "/tool/s3/upload/v2/single",
                          headers={"API-Key": cfg["api_key"]},
                          data={"type": ftype},
                          files={"file": (path.name, f)}, timeout=300)
    obj = _check(r, "单文件上传")["object_id"]
    print(f"  封面上传完成 object_id={obj}")
    return obj


# ----------------- 账号 / 发布 -----------------
def cmd_accounts(cfg):
    data = api_json(cfg, "/media/list",
                    {"page": 1, "page_size": 20, "team_id": cfg["team_id"]})
    lst = data.get("list", [])
    print(f"共 {data.get('total_size', len(lst))} 个账号:")
    for m in lst:
        print(f"  media_id={m.get('media_id')}  "
              f"平台={m.get('media_type') or m.get('platform')}  "
              f"名称={m.get('name') or m.get('nickname')}")
    if not lst:
        print("  (没查到账号,先在飞星里授权 TikTok 账号)")


def publish_task(cfg, media_id: str, video_id: str, title: str, desc: str,
                 cover_id: str | None = None, draft: bool = True,
                 pub_time_ms: int | None = None) -> str:
    """调用发布接口,返回 task_id。可复用(调度器也调它)。

    pub_time_ms:毫秒时间戳,传入=定时发布(必须晚于当前);不传=立即发布。
    """
    body = {
        "team_id": cfg["team_id"],
        "media_ids": [media_id],
        "title": title,
        "video_id": video_id,
        "is_independence": 0,
        "is_draft": draft,
        "common_data": {"desc": desc},
        "tiktok_biz_data": {
            "disable_comment": False, "disable_duet": False, "disable_stitch": False,
            "is_brand_organic": False, "is_branded_content": False,
            # 内容由 AI 改写/配音/合成，TikTok 要求如实标注
            "is_ai_generated": True, "is_ads_only": False,
            "is_track_enabled": False,
        },
    }
    if cover_id:
        body["common_data"]["cover_id"] = cover_id
    if pub_time_ms:
        body["pub_time"] = pub_time_ms
    return api_json(cfg, "/media/task/create/v2", body)["task_id"]


def cmd_publish(cfg, args):
    video = Path(args.video)
    if not video.exists():
        print(f"找不到视频: {video}");  sys.exit(1)

    print("① 上传视频…")
    video_id = upload_video(cfg, video)

    cover_id = None
    if args.cover:
        print("② 上传封面…")
        cover_id = upload_single(cfg, Path(args.cover), "cover")

    title = args.title or video.stem
    desc = args.desc or title
    draft = not args.live

    print(f"③ 发布(is_draft={draft})…")
    task_id = publish_task(cfg, args.media, video_id, title, desc, cover_id, draft)
    print(f"\n✅ 成功! task_id={task_id}")
    print("   " + ("已存为草稿,去 TikTok App 收件箱/草稿箱查看。"
                   if draft else "已提交发布,去『查询发布任务列表』看状态。"))


def main():
    ap = argparse.ArgumentParser(description="飞星发布验证脚本")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("accounts", help="查询账号列表(拿 media_id)")
    p = sub.add_parser("publish", help="上传视频并发布(默认存草稿)")
    p.add_argument("video", help="视频文件路径")
    p.add_argument("--media", required=True, help="要发布到的 TikTok 账号 media_id")
    p.add_argument("--cover", help="封面图片路径(可选)")
    p.add_argument("--title", help="任务标题(默认用文件名)")
    p.add_argument("--desc", help="视频文案(话题#写在这里,可选)")
    p.add_argument("--live", action="store_true", help="真的发布(默认只存草稿)")
    args = ap.parse_args()

    cfg = load_cfg()
    if args.cmd == "accounts":
        cmd_accounts(cfg)
    elif args.cmd == "publish":
        cmd_publish(cfg, args)


if __name__ == "__main__":
    main()
