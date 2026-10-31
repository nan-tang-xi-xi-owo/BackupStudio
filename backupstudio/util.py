"""路径、大小、时间等通用工具。"""

from __future__ import annotations

import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

_UNITS = ("B", "KB", "MB", "GB", "TB", "PB")


def human_size(num: float | int | None) -> str:
    """把字节数格式化成「1.5 MB」这类字符串。"""
    if num is None:
        return "-"
    n = float(num)
    neg = n < 0
    n = abs(n)
    for unit in _UNITS:
        if n < 1024 or unit == _UNITS[-1]:
            if unit == "B":
                text = f"{int(n)} {unit}"
            else:
                text = f"{n:.2f} {unit}"
            return "-" + text if neg else text
        n /= 1024
    return f"{n:.2f} PB"


def human_duration(seconds: float | int | None) -> str:
    if seconds is None:
        return "-"
    total = int(max(0, seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h} 小时 {m} 分 {s} 秒"
    if m:
        return f"{m} 分 {s} 秒"
    return f"{s} 秒"


_ILLEGAL_WIN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_WIN = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def sanitize_component(name: str, replacement: str = "_") -> str:
    """把任意字符串变成可安全用作文件/文件夹名的片段。"""
    text = _ILLEGAL_WIN.sub(replacement, name).strip().strip(".")
    text = re.sub(r"\s+", " ", text)
    if not text:
        text = "backup"
    if text.upper().split(".")[0] in _RESERVED_WIN:
        text = "_" + text
    return text[:120]


def render_template(template: str, ctx: dict[str, str]) -> str:
    """渲染文件名模板，如 ``{name}_{date}_{time}``。

    未知占位符原样保留，避免用户写错时整段丢失。
    """
    def repl(m: re.Match[str]) -> str:
        key = m.group(1)
        if key in ctx:
            return ctx[key]
        if key == "date":
            return datetime.now().strftime("%Y%m%d")
        if key == "time":
            return datetime.now().strftime("%H%M%S")
        if key == "datetime":
            return datetime.now().strftime("%Y%m%d_%H%M%S")
        return m.group(0)

    return re.sub(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", repl, template)


def template_context(name: str, extra: dict[str, str] | None = None) -> dict[str, str]:
    now = datetime.now()
    ctx = {
        "name": sanitize_component(name or "backup"),
        "date": now.strftime("%Y%m%d"),
        "time": now.strftime("%H%M%S"),
        "datetime": now.strftime("%Y%m%d_%H%M%S"),
        "yyyy": now.strftime("%Y"),
        "MM": now.strftime("%m"),
        "dd": now.strftime("%d"),
        "HH": now.strftime("%H"),
        "mm": now.strftime("%M"),
        "ss": now.strftime("%S"),
    }
    if extra:
        ctx.update({k: str(v) for k, v in extra.items()})
    return ctx


def is_path_inside(child: str | os.PathLike[str], parent: str | os.PathLike[str]) -> bool:
    """判断 child 是否位于 parent 内部（含相等）。"""
    try:
        c = Path(os.path.abspath(str(child)))
        p = Path(os.path.abspath(str(parent)))
    except (TypeError, ValueError, OSError):
        return False
    try:
        c.relative_to(p)
        return True
    except ValueError:
        return False


def default_documents_dir() -> Path:
    if sys.platform == "win32":
        try:
            user = os.environ.get("USERPROFILE") or str(Path.home())
            cand = Path(user) / "Documents"
            if cand.is_dir():
                return cand
        except Exception:  # pragma: no cover
            pass
    home = Path.home()
    for name in ("Documents", "文档"):
        p = home / name
        if p.is_dir():
            return p
    return home


def free_space(path: str | os.PathLike[str]) -> int:
    """目标卷剩余空间（字节）；路径不存在时向上找最近的已存在父目录。"""
    p = Path(str(path))
    while True:
        try:
            return shutil.disk_usage(str(p)).free
        except OSError:
            if p.parent == p:
                return 0
            p = p.parent


def same_volume(a: str | os.PathLike[str], b: str | os.PathLike[str]) -> bool:
    try:
        return Path(os.path.abspath(str(a))).anchor.lower() == Path(os.path.abspath(str(b))).anchor.lower()
    except (ValueError, OSError):
        return False


def unique_path(path: Path, max_tries: int = 1000) -> Path:
    """若 path 已存在，追加 ``(1)``、``(2)``… 生成不冲突的路径。"""
    if not path.exists():
        return path
    suffix = path.suffix
    if path.suffix in {".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst"}:
        suffix = path.suffix
    for i in range(1, max_tries):
        cand = path.with_name(f"{path.name[: -len(suffix)] if suffix else path.name} ({i}){suffix}")
        if not cand.exists():
            return cand
    return path.with_name(f"{path.name}.{os.getpid()}{suffix}")


def normalize_path(text: str) -> str:
    """用户输入的路径规范化（去引号、展开 ~ 与环境变量）。"""
    t = (text or "").strip().strip('"').strip("'")
    t = os.path.expandvars(os.path.expanduser(t))
    return t


def open_in_file_manager(path: str) -> bool:
    """在系统文件管理器中打开路径（Windows 资源管理器 / Finder / xdg-open）。"""
    p = Path(path)
    target = p if p.exists() else p.parent
    try:
        if sys.platform == "win32":
            os.startfile(str(target))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            import subprocess
            subprocess.Popen(["open", str(target)])
        else:
            import subprocess
            if target.is_file():
                subprocess.Popen(["xdg-open", str(target.parent)])
            else:
                subprocess.Popen(["xdg-open", str(target)])
        return True
    except Exception:
        return False


def reveal_file(path: str) -> bool:
    """在文件管理器中定位（选中）某个文件。"""
    p = Path(path)
    try:
        if sys.platform == "win32":
            import subprocess
            subprocess.Popen(["explorer", "/select,", str(p)])
            return True
        if sys.platform == "darwin":
            import subprocess
            subprocess.Popen(["open", "-R", str(p)])
            return True
        return open_in_file_manager(str(p))
    except Exception:
        return False
