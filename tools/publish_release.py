"""发布 GitHub Release 并上传资产（无需 gh CLI）。

用法:
    python tools/publish_release.py --version 0.6.0 \
        --notes build/release_notes_v0.6.0.md \
        dist/SnapPin-0.6.0-windows-x64.zip dist/SnapPin-0.6.0-full-windows-x64.zip

- 令牌取自 git credential（git push 能用就能用），不落盘、不打印。
- 断点续传：已存在的同名资产自动跳过；失败自动重试（curl 带 Expect 头禁用，
  规避中间设备对大 POST 的 100-continue 复位）。
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

API = "https://api.github.com/repos/shihenglong/SnapPin"
UPLOAD = "https://uploads.github.com/repos/shihenglong/SnapPin"


def token(repo_dir):
    out = subprocess.run(["git", "credential", "fill"], cwd=repo_dir,
                         input=b"protocol=https\nhost=github.com\n\n",
                         capture_output=True).stdout.decode()
    for line in out.splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("找不到 git 凭证（先配置好 git push 凭据）")


def api(tok, method, url, payload=None):
    h = {"Authorization": f"Bearer {tok}", "User-Agent": "SnapPin-release",
         "Accept": "application/vnd.github+json"}
    data = json.dumps(payload).encode() if payload is not None else None
    if data:
        h["Content-Type"] = "application/json"
    r = urllib.request.Request(url, data=data, headers=h, method=method)
    with urllib.request.urlopen(r, timeout=120) as resp:
        return json.loads(resp.read() or b"{}")


def upload(tok, rel_id, name, path, attempts=4):
    url = f"{UPLOAD}/releases/{rel_id}/assets?name={name}"
    size = os.path.getsize(path)
    for i in range(1, attempts + 1):
        print(f"  上传 {name} ({size/1048576:.1f} MB) 第 {i} 次…", flush=True)
        p = subprocess.run(
            ["curl", "-sS", "-X", "POST", "--http1.1",
             "-H", f"Authorization: Bearer {tok}",
             "-H", "Content-Type: application/zip", "-H", "Expect:",
             "--data-binary", f"@{path}", "--connect-timeout", "30",
             "--max-time", "3600", "-o", os.devnull, "-w", "%{http_code}", url],
            capture_output=True, text=True)
        code = (p.stdout or "").strip()
        if code in ("201", "200"):
            print(f"  完成 {name}", flush=True)
            return True
        print(f"  失败 http={code} {p.stderr.strip()[:160]}", flush=True)
        time.sleep(5 * i)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", required=True)
    ap.add_argument("--notes", required=True)
    ap.add_argument("--repo", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    ap.add_argument("assets", nargs="*")
    args = ap.parse_args()
    tok = token(args.repo)
    tag = f"v{args.version}"
    body = open(args.notes, encoding="utf-8").read()
    try:
        rel = api(tok, "POST", API + "/releases",
                  {"tag_name": tag, "name": f"SnapPin v{args.version}", "body": body,
                   "draft": False, "prerelease": False, "target_commitish": "main"})
        print("已创建 release:", rel["html_url"])
    except urllib.error.HTTPError as e:
        if e.code != 422:
            raise
        rel = api(tok, "GET", API + f"/releases/tags/{tag}")
        print("release 已存在，复用:", rel["html_url"])
    have = {a["name"] for a in rel.get("assets", [])}
    ok = True
    for path in args.assets:
        name = os.path.basename(path)
        if not os.path.exists(path):
            print("跳过（文件不存在）:", path)
            ok = False
            continue
        if name in have:
            print("跳过（已存在）:", name)
            continue
        ok &= upload(tok, rel["id"], name, path)
    final = api(tok, "GET", API + f"/releases/tags/{tag}")
    print("release:", final["html_url"])
    for a in final["assets"]:
        print(f"  资产 {a['name']} {a['size']/1048576:.1f} MB")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
