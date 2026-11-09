"""检查更新 — GitHub Releases API。

纯 stdlib（urllib），子线程执行，结果一次性回调：
    check_async(callback)
    callback("update", info)  有新版本，info = {"tag", "url", "notes"}
    callback("latest", None)  已是最新
    callback("error", str)    网络失败等原因
"""
import json
import re
import threading
import urllib.request

import version


def parse_ver(s: str) -> tuple:
    """'v0.4.1' / '0.4' → (0,4,1)；解析失败返回 (0,)。"""
    parts = re.findall(r"\d+", (s or "").strip().lstrip("vV"))
    return tuple(int(p) for p in parts[:3]) or (0,)


def is_newer(remote: str, local: str) -> bool:
    return parse_ver(remote) > parse_ver(local)


def fetch_latest(timeout: float = 6.0):
    """拉取最新 release 信息；失败抛异常。"""
    req = urllib.request.Request(version.RELEASE_API, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": f"{version.APP_NAME}/{version.APP_VERSION}",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8"))
    if not data or not data.get("tag_name"):     # 无任何 release
        raise ValueError("no releases")
    return {"tag": data["tag_name"],
            "url": data.get("html_url") or version.RELEASE_PAGE,
            "notes": (data.get("body") or "").strip()}


def check_async(on_result, timeout: float = 6.0) -> None:
    """后台线程检查更新，结果在子线程回调 on_result(status, payload)。

    UI 侧需自行切回主线程（QMetaObject.invokeMethod / 信号槽）。
    """
    def _run():
        try:
            info = fetch_latest(timeout)
        except Exception as exc:
            try:
                on_result("error", str(exc))
            except Exception:
                pass
            return
        if is_newer(info["tag"], version.APP_VERSION):
            try:
                on_result("update", info)
            except Exception:
                pass
        else:
            try:
                on_result("latest", info)
            except Exception:
                pass
    threading.Thread(target=_run, daemon=True, name="snappin-update-check").start()


if __name__ == "__main__":   # 离屏自测：python -I updater.py
    assert parse_ver("v0.4.1") == (0, 4, 1)
    assert parse_ver("0.4") == (0, 4)
    assert is_newer("v0.5.0", "0.4.0")
    assert not is_newer("v0.4.0", "0.4.0")
    assert not is_newer("v0.3.9", "0.4.0")
    result = []

    def _cb(status, payload):
        result.append((status, payload))

    check_async(_cb, timeout=3)
    threading.Event().wait(5)    # 等 URI 处理结束
    print("check_async ->", result[0][0] if result else "(no callback: 离线?)")
    print("updater self-test ok")
