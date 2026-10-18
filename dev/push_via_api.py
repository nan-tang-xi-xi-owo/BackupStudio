"""通过 GitHub REST API 把本地仓库推上去（git 传输被网络阻断时的替代方案）。

用法：
    python dev/push_via_api.py [--dry-run] [--message "..."]
环境变量 GH_TOKEN 提供 token（repo + workflow 权限）。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OWNER = "nan-tang-xi-xi-owo"
REPO = "BackupStudio"
API = f"https://api.github.com/repos/{OWNER}/{REPO}"
BRANCH = "main"
TOKEN = os.environ.get("GH_TOKEN", "")


def api(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    url = path if path.startswith("http") else API + path
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"token {TOKEN}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "BackupStudio-pusher")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            parsed = {"raw": body}
        return exc.code, parsed


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8").stdout


def tracked_files() -> list[str]:
    out = git("-c", "core.quotepath=false", "ls-files")
    return [ln for ln in out.splitlines() if ln.strip()]


def main() -> int:
    if not TOKEN:
        print("缺少 GH_TOKEN", file=sys.stderr)
        return 2
    message = git("log", "-1", "--pretty=%B").strip() or "Initial commit"
    files = tracked_files()
    print(f"待上传文件 {len(files)} 个")

    # 1) 当前分支指针（空仓库 → 404）
    status, ref = api("GET", f"/git/ref/heads/{BRANCH}")
    parent_sha = ref["object"]["sha"] if status == 200 else None
    print("父提交:", parent_sha or "（无，首个提交）")

    # 2) 逐个上传 blob
    tree_items: list[dict] = []
    committed = {}
    cursor_path = ROOT / "build" / "upload-state.json"
    if cursor_path.exists():
        try:
            committed = json.loads(cursor_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            committed = {}
    cursor_path.parent.mkdir(parents=True, exist_ok=True)

    for i, rel in enumerate(files, 1):
        p = ROOT / rel
        data = p.read_bytes()
        sha = hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()
        if committed.get(rel) == sha:
            tree_items.append({"path": rel, "mode": "100755" if os.access(p, os.X_OK) else "100644",
                               "type": "blob", "sha": sha})
            continue
        st, res = api("POST", "/git/blobs", {
            "content": base64.b64encode(data).decode("ascii"),
            "encoding": "base64",
        })
        if st not in (200, 201):
            print(f"× {rel}: {st} {res.get('message')}")
            return 1
        committed[rel] = res["sha"]
        tree_items.append({
            "path": rel,
            "mode": "100755" if os.access(p, os.X_OK) else "100644",
            "type": "blob",
            "sha": res["sha"],
        })
        if i % 10 == 0 or i == len(files):
            cursor_path.write_text(json.dumps(committed, ensure_ascii=False), encoding="utf-8")
            print(f"  已上传 {i}/{len(files)}")

    # 3) 建树
    st, tree = api("POST", "/git/trees", {"tree": tree_items})
    if st not in (200, 201):
        print("建树失败:", st, tree)
        return 1
    # 4) 建提交
    commit_payload = {"message": message, "tree": tree["sha"]}
    if parent_sha:
        commit_payload["parents"] = [parent_sha]
    st, commit = api("POST", "/git/commits", commit_payload)
    if st not in (200, 201):
        print("提交失败:", st, commit)
        return 1
    print("提交:", commit["sha"])
    # 5) 更新分支
    if parent_sha:
        st, res = api("PATCH", f"/git/refs/heads/{BRANCH}", {"sha": commit["sha"], "force": False})
    else:
        st, res = api("POST", "/git/refs", {"ref": f"refs/heads/{BRANCH}", "sha": commit["sha"]})
    if st not in (200, 201):
        print("更新分支失败:", st, res)
        return 1
    print(f"✓ 已推送到 {OWNER}/{REPO}#{BRANCH}：https://github.com/{OWNER}/{REPO}/commit/{commit['sha']}")
    # 记录本地 ref，方便后续 push 时快进
    try:
        (ROOT / ".git" / "refs" / "remotes" / "origin").mkdir(parents=True, exist_ok=True)
        (ROOT / ".git" / "refs" / "remotes" / "origin" / "main").write_text(commit["sha"] + "\n", encoding="ascii")
    except OSError:
        pass
    cursor_path.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
