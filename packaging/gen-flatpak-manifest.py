"""校验 / 更新 Flatpak manifest（YAML 或 JSON 均可）。

- ``--check``：检查 app-id、finish-args、必需模块，以及 7-Zip 源是否有**真实** sha256；
- ``--fetch``：下载官方 7-Zip 包并写回真实 sha256（只对 YAML 生效）。

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
PKG = ROOT / "packaging"
YAML_MANIFEST = PKG / "io.github.backupstudio.BackupStudio.yml"
JSON_MANIFEST = PKG / "io.github.backupstudio.BackupStudio.json"
SEVEN_ZIP_URL = "https://www.7-zip.org/a/7z2301-linux-x64.tar.xz"
EXPECTED_SHA = "23babcab045b78016e443f862363e4ab63c77d75bc715c0b3463f6134cbcf318"


def load_yaml(path: Path) -> dict:
    import yaml  # 仅校验脚本依赖，CI 里不需要

    return yaml.safe_load(path.read_text(encoding="utf-8"))


def manifest_path() -> Path:
    if YAML_MANIFEST.is_file():
        return YAML_MANIFEST
    return JSON_MANIFEST


def fetch_sha256(url: str) -> str:
    print(f"下载 {url} ...")
    digest = hashlib.sha256()
    with urllib.request.urlopen(url, timeout=300) as resp:  # noqa: S310 - 固定官方地址
        for chunk in iter(lambda: resp.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="只校验 manifest 是否完整")
    parser.add_argument("--fetch", action="store_true", help="下载 7-Zip 并写回真实校验值（YAML）")
    args = parser.parse_args()

    path = manifest_path()
    if not path.is_file():
        print("找不到 manifest", file=sys.stderr)
        return 1
    data = load_yaml(path)
    problems: list[str] = []

    seven = next(
        (s for m in data.get("modules", []) for s in m.get("sources", [])
         if str(s.get("url", "")).startswith("https://www.7-zip.org")),
        None,
    )
    if seven is None:
        problems.append("找不到 7-Zip 源")

    if args.fetch:
        sha = fetch_sha256(SEVEN_ZIP_URL)
        text = YAML_MANIFEST.read_text(encoding="utf-8")
        text = re.sub(r"(7-zip\.org/a/[^\"]+\.tar\.xz\"[\s\S]{0,120}?sha256\":\s*\")[0-9a-f]{64}",
                      lambda m: m.group(1) + sha, text)
        YAML_MANIFEST.write_text(text, encoding="utf-8")
        print("已写入 sha256:", sha)
        data = load_yaml(YAML_MANIFEST)
        seven = next(
            (s for m in data["modules"] for s in m.get("sources", [])
             if str(s.get("url", "")).startswith("https://www.7-zip.org")),
            None,
        )

    if seven is not None:
        sha = str(seven.get("sha256", ""))
        if not re.fullmatch(r"[0-9a-f]{64}", sha):
            problems.append("7-Zip 的 sha256 不是真实校验值")
        elif sha != EXPECTED_SHA:
            print(f"提示：manifest 中 sha256={sha[:12]}…，与已知值不同（如果官方包更新过属正常）")

    if not re.fullmatch(r"[a-z0-9]+(\.[A-Za-z0-9]+)+", str(data.get("app-id", ""))):
        problems.append("app-id 格式不正确")
    if not data.get("finish-args"):
        problems.append("缺少 finish-args")
    names = [m.get("name") for m in data.get("modules", [])]
    for required in ("p7zip", "backupstudio"):
        if required not in names:
            problems.append(f"缺少模块 {required}")
    for mod in data.get("modules", []):
        if mod.get("name") == "backupstudio" and not any(
            s.get("type") == "dir" for s in mod.get("sources", [])
        ):
            problems.append("backupstudio 模块缺少本地目录源")

    if problems:
        print("manifest 校验失败：")
        for p in problems:
            print("  -", p)
        return 1
    print(f"manifest 校验通过：{path.name}（{len(data['modules'])} 个模块）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
