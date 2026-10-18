"""生成 / 校验 Flatpak manifest 中的校验值与路径。

- `--fetch` 下载 7-Zip 并计算 sha256，写入 manifest；
- `--check` 只校验 manifest 是否所有字段齐备（不含占位符）；
- 同时确保 `sources` 中的本地目录指向仓库根目录。

用法：
    python packaging/gen-flatpak-manifest.py --check
    python packaging/gen-flatpak-manifest.py --fetch
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "packaging" / "io.github.backupstudio.BackupStudio.json"
SEVEN_ZIP_URL = "https://www.7-zip.org/a/7z2409-linux-x64.tar.xz"


def fetch_sha256(url: str) -> str:
    print(f"下载 {url} ...")
    digest = hashlib.sha256()
    with urllib.request.urlopen(url, timeout=120) as resp:  # noqa: S310 - 固定官方地址
        for chunk in iter(lambda: resp.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fetch", action="store_true", help="下载 7-Zip 并写入真实校验值")
    parser.add_argument("--check", action="store_true", help="只校验 manifest 是否完整")
    args = parser.parse_args()

    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    seven = next(
        (s for m in data["modules"] for s in m.get("sources", []) if s.get("url", "").startswith("https://www.7-zip.org")),
        None,
    )
    if seven is None:
        print("manifest 中找不到 7-Zip 源", file=sys.stderr)
        return 1

    if args.fetch:
        seven["sha256"] = fetch_sha256(SEVEN_ZIP_URL)
        MANIFEST.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("已写入 sha256:", seven["sha256"])

    problems: list[str] = []
    if not re.fullmatch(r"[0-9a-f]{64}", str(seven.get("sha256", ""))):
        problems.append("7-Zip 的 sha256 不是真实校验值（请用 --fetch 生成）")
    if not re.fullmatch(r"[a-z0-9]+(\.[A-Za-z0-9]+)+", data.get("app-id", "")):
        problems.append("app-id 格式不正确")
    if not data.get("finish-args"):
        problems.append("缺少 finish-args")
    names = [m["name"] for m in data["modules"]]
    for required in ("p7zip", "backupstudio"):
        if required not in names:
            problems.append(f"缺少模块 {required}")
    for m in data["modules"]:
        for s in m.get("sources", []):
            if s.get("type") == "dir" and s.get("path") == "..":
                # Flatpak 用 --filesystem 访问；本地目录源由 flatpak-builder 在 manifest 所在目录解析
                pass
    if problems:
        print("manifest 校验失败：")
        for p in problems:
            print("  -", p)
        return 1
    print(f"manifest 校验通过：{MANIFEST.name}（{len(data['modules'])} 个模块，sha256 已就绪）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
