"""删除池中旧片(pre-改文,app 为空)的成品文件并标 retired。

    python cleanup_old.py

锁定中的文件(正被上传)会自动跳过、下次再删,可安全反复运行。
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from src import brain  # noqa: E402


def main():
    con = brain.connect()
    rows = con.execute(
        "SELECT video_id,file_path,cover_path FROM videos "
        "WHERE status='pending' AND app IS NULL").fetchall()
    freed = 0
    deleted = 0
    skipped = 0
    for r in rows:
        locked = False
        for p in (r["file_path"], r["cover_path"]):
            if p and os.path.exists(p):
                try:
                    sz = os.path.getsize(p)
                    os.remove(p)
                    freed += sz
                except PermissionError:
                    locked = True
        if locked:
            skipped += 1
            continue
        con.execute("UPDATE videos SET status='retired', file_path=NULL, "
                    "cover_path=NULL WHERE video_id=?", (r["video_id"],))
        deleted += 1
    con.commit()
    con.close()
    print(f"删除旧片 {deleted} 条,释放 {freed/1024/1024/1024:.1f}GB,"
          f"跳过锁定中 {skipped} 条(下次再删)")


if __name__ == "__main__":
    main()
