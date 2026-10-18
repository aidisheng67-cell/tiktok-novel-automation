"""本地控制台 —— 浏览器里改配置、测连接、跑生成/发布、看状态。

    python console.py          # 启动后自动打开 http://127.0.0.1:8770

只监听本机(127.0.0.1),不对外。改的是 config.json 和三个凭证 JSON。
被 loader 打包后作为交付包的启动入口(console.main)。
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from src import credentials  # noqa: E402
from runlock import SingleInstance  # noqa: E402

HOST, PORT = "127.0.0.1", 8770
CONFIG = ROOT / "config.json"
CREDS = {"deepseek": ROOT / "deepseek.json",
         "feishu": ROOT / "feishu.json",
         "feixing": ROOT / "feixing.json"}

# 控制台里可编辑的 config.json 字段(名称 + 说明 + 类型)
EDITABLE = [
    ("background_dir", "素材视频文件夹", "path", "留空=项目内 input/background"),
    ("output_dir", "成品视频保存文件夹", "path", "留空=项目内 output"),
    ("audio_dir", "配音输出文件夹", "path", "留空=项目内 output/audio"),
    ("icon_dir", "App 图标文件夹", "path", "留空=项目内 input/tubiao"),
    ("app_name", "默认 App 名", "text", "如 Good Novel"),
    ("rewrite_mode", "改文模式", "text", "hybrid / simple / agent"),
    ("per_account_videos", "每号每天发布条数", "int", "默认 3"),
    ("daily_publish_cap", "每天发布总上限", "int", "飞星套餐额度,如 21"),
    ("cooldown_days", "同号同题材冷却天数", "int", "默认 30"),
]


def _read_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_json(p: Path, d: dict):
    p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


def _run_py(args: list[str], timeout=1800) -> tuple[bool, str]:
    """在后台跑一个脚本(阻塞直到完成),返回(成功, 末尾输出)。"""
    try:
        r = subprocess.run([sys.executable] + args, cwd=str(ROOT),
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
        out = (r.stdout or "") + (r.stderr or "")
        return r.returncode == 0, out[-3000:]
    except Exception as e:
        return False, str(e)


# ---------------- API 实现 ----------------
def api_get_config() -> dict:
    cfg = _read_json(CONFIG)
    fields = {k: cfg.get(k, "") for k, *_ in EDITABLE}
    # 只把脱敏后的凭证发给浏览器；真实值不会出现在页面源码 / 响应里
    creds = {name: credentials.redact(_read_json(p)) for name, p in CREDS.items()}
    return {"fields": fields, "meta": EDITABLE, "creds": creds}


def api_save_config(body: dict) -> dict:
    cfg = _read_json(CONFIG)
    for k, _, typ, *_ in EDITABLE:
        if k in body.get("fields", {}):
            v = body["fields"][k]
            if typ == "int":
                try:
                    v = int(v)
                except (ValueError, TypeError):
                    continue
            cfg[k] = v
    _write_json(CONFIG, cfg)
    for name, vals in body.get("creds", {}).items():
        if name in CREDS and vals:
            cur = _read_json(CREDS[name])
            # 空值 = 不修改；*** = 页面回显的脱敏占位符，不能写回
            cur.update({k: v for k, v in vals.items()
                        if v not in ("", "***")})
            credentials.save(name, cur, root=ROOT)
    return {"ok": True, "msg": "已保存"}


def origin_allowed(headers) -> bool:
    """只接受本机页面发起的请求，降低 DNS rebinding / CSRF 风险。"""
    host = (headers.get("Host") or "").lower()
    if host not in (f"127.0.0.1:{PORT}", f"localhost:{PORT}"):
        return False
    origin = headers.get("Origin")
    if origin and origin.lower() not in (
            f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"):
        return False
    return True


def api_test(which: str) -> dict:
    if which == "deepseek":
        ok, out = _run_py(["rewrite_agent.py", "test"], timeout=120)
        return {"ok": ok, "msg": "DeepSeek 连通,改文正常" if ok else "失败:" + out[-400:]}
    if which == "feishu":
        ok, out = _run_py(["feishu_sync.py", "push"], timeout=120)
        return {"ok": ok, "msg": "飞书连通并同步" if ok else "失败:" + out[-400:]}
    if which == "feixing":
        ok, out = _run_py(["feixing_publish.py", "accounts"], timeout=60)
        return {"ok": ok, "msg": ("飞星连通\n" + out[-400:]) if ok else "失败:" + out[-400:]}
    return {"ok": False, "msg": "未知"}


def api_status() -> dict:
    try:
        from src import brain
        import datetime
        from collections import Counter
        con = brain.connect()
        today = datetime.date.today().isoformat()
        pend = con.execute("SELECT COUNT(*) FROM videos WHERE status='pending'").fetchone()[0]
        pub = con.execute("SELECT status FROM publish_log WHERE date(created_at)=?", (today,)).fetchall()
        acc = con.execute("SELECT COUNT(*) FROM accounts WHERE active=1").fetchone()[0]
        con.close()
        return {"ok": True, "pending": pend, "today_pub": len(pub),
                "today_detail": dict(Counter(r["status"] for r in pub)), "accounts": acc}
    except Exception as e:
        return {"ok": False, "msg": str(e)}


def api_action(what: str) -> dict:
    if what == "generate":
        ok, out = _run_py(["generate.py"])
        return {"ok": ok, "msg": ("生成完成\n" + out[-600:]) if ok else "失败:" + out[-600:]}
    if what == "publish":
        # schedule_cli run 需要确认,喂 y
        try:
            r = subprocess.run([sys.executable, "schedule_cli.py", "run"], cwd=str(ROOT),
                              input="y\n", capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=1800)
            out = (r.stdout or "") + (r.stderr or "")
            return {"ok": r.returncode == 0, "msg": out[-600:]}
        except Exception as e:
            return {"ok": False, "msg": str(e)}
    return {"ok": False, "msg": "未知操作"}


ROUTES = {
    "/api/config": lambda b: api_get_config(),
    "/api/save": api_save_config,
    "/api/status": lambda b: api_status(),
}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not origin_allowed(self.headers):
            return self._send(403, {"error": "forbidden"})
        if self.path in ("/", "/index.html"):
            html = (ROOT / "console.html").read_bytes()
            return self._send(200, html, "text/html")
        if self.path == "/api/config":
            return self._send(200, api_get_config())
        if self.path == "/api/status":
            return self._send(200, api_status())
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not origin_allowed(self.headers):
            return self._send(403, {"error": "forbidden"})
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        if self.path == "/api/save":
            return self._send(200, api_save_config(body))
        if self.path == "/api/test":
            return self._send(200, api_test(body.get("which", "")))
        if self.path == "/api/action":
            return self._send(200, api_action(body.get("what", "")))
        self._send(404, {"error": "not found"})


def main():
    lock_path = ROOT / "_work" / "console.lock"
    try:
        with SingleInstance(str(lock_path)):
            srv = ThreadingHTTPServer((HOST, PORT), H)
            url = f"http://{HOST}:{PORT}"
            print(f"控制台已启动:{url}(按 Ctrl+C 关闭)")
            threading.Timer(1.0, lambda: webbrowser.open(url)).start()
            try:
                srv.serve_forever()
            except KeyboardInterrupt:
                print("\n控制台已关闭")
    except RuntimeError as e:
        print(f"启动失败: {e}")


if __name__ == "__main__":
    main()
