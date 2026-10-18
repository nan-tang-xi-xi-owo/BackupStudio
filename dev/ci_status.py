"""CI 辅助脚本：查看最新 workflow run 各 job 状态，失败时下载并打印关键日志。"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
import zipfile
import io

TOKEN = open(os.path.join(os.environ.get("TEMP", "."), "gh-tok.txt"), encoding="ascii").read().strip()
REPO = "nan-tang-xi-xi-owo/BackupStudio"
API = f"https://api.github.com/repos/{REPO}"


def call(path: str) -> dict:
    req = urllib.request.Request(API + path)
    req.add_header("Authorization", f"token {TOKEN}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "bs")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def fetch_log(job_id: int) -> str:
    # 日志下载会 302 到签名 URL；签名 URL 不能带 Authorization 头
    req = urllib.request.Request(f"{API}/actions/jobs/{job_id}/logs")
    req.add_header("Authorization", f"token {TOKEN}")
    req.add_header("User-Agent", "bs")
    import subprocess

    out = subprocess.run(
        ["curl.exe", "-s", "-L", "-H", f"Authorization: token {TOKEN}", "-H", "User-Agent: bs",
         f"{API}/actions/jobs/{job_id}/logs"],
        capture_output=True, timeout=120,
    )
    raw = out.stdout
    if raw[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            return "\n".join(zf.read(n).decode("utf-8", "replace") for n in zf.namelist())
    return raw.decode("utf-8-sig", "replace")


def main() -> int:
    runs = call("/actions/runs?per_page=3")["workflow_runs"]
    for run in runs[:3]:
        print(f"run #{run['run_number']} sha={run['head_sha'][:7]} {run['status']}/{run['conclusion']} ({run['display_title'][:40]})")
    run = runs[0]
    if run["status"] != "completed":
        print("仍在运行中，稍后再查")
        return 0
    jobs = call(f"/actions/runs/{run['id']}/jobs")["jobs"]
    print()
    for j in jobs:
        print(f"  {j['name']:<24} {j['status']:<12} {j['conclusion']}")
    failed = [j for j in jobs if j["conclusion"] == "failure"]
    for j in failed[:1]:
        print(f"\n=== {j['name']} 日志（节选） ===")
        text = fetch_log(j["id"])
        lines = text.splitlines()
        keep = [ln for ln in lines if any(k in ln for k in ("FAILED", "AssertionError", "short test summary", "##[error]", "Error:", "failed"))]
        if not keep:
            keep = lines[-40:]
        print("\n".join(keep[-40:]))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
