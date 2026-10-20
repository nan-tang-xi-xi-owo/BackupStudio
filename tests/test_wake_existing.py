"""回归测试：``app._wake_existing`` 二次启动硬崩溃（0xC0000409）。

背景（S3/S4/R5 实测）：旧实现在 QLocalSocket 连接失败分支调用
``QMessageBox.information(None, ...)``，但该函数在 ``QApplication``
创建 *之前* 执行（app.py main() 内的单实例唤醒路径）→ Qt qFatal
直接终止进程，Python 层不可捕获。修复后失败提示必须走
非 Qt 通道（Windows: MessageBoxW；其他: emergency 日志 + stderr）。

测试策略：
1. 静态断言：源码不得再出现 QMessageBox / QApplication / QtWidgets / getlogin；
2. 行为测试：无 QApplication 环境下调用函数，monkeypatch 掉
   QLocalSocket 与原生弹窗，断言走到函数尾且走对分支、不抛异常。
"""

from __future__ import annotations

import ctypes
import inspect
import sys

import pytest

from backupstudio import app

APP_NAME_ZH = "备份工坊"
EXPECTED_MSG = "程序已在运行，请在系统托盘找到它。"


# ---------------------------------------------------------------------------
# 静态断言（防回归：根源模式是不该在 QApplication 前触碰 QtWidgets）
# ---------------------------------------------------------------------------

def test_source_has_no_qt_widgets_or_getlogin() -> None:
    src = inspect.getsource(app._wake_existing)
    assert "QMessageBox" not in src, "失败提示不得使用 Qt 弹窗（无 QApplication 时 qFatal 硬崩溃）"
    assert "QApplication" not in src
    assert "QtWidgets" not in src
    assert "getlogin" not in src, "os.getlogin 在无控制台上下文会抛 OSError，应使用 getpass.getuser"


# ---------------------------------------------------------------------------
# 夹具：假 QLocalSocket（成功 / 失败两种）
# ---------------------------------------------------------------------------

class _FakeSocket:
    def __init__(self, connected: bool) -> None:
        self._connected = connected
        self.written = b""
        self.server_name = None
        self.disconnected = False

    def connectToServer(self, name: str) -> None:  # noqa: N802
        self.server_name = name

    def waitForConnected(self, ms: int) -> bool:  # noqa: N802
        return self._connected

    def write(self, data: bytes) -> int:  # noqa: N802
        self.written += data
        return len(data)

    def flush(self) -> bool:
        return True

    def waitForBytesWritten(self, ms: int) -> bool:  # noqa: N802
        return True

    def disconnectFromServer(self) -> None:  # noqa: N802
        self.disconnected = True


@pytest.fixture()
def patch_qlocal_socket(monkeypatch):
    """返回 make(connected) -> socket 实例记录，替换 PySide6 的 QLocalSocket。"""
    qtnetwork = pytest.importorskip("PySide6.QtNetwork")
    instances: list[_FakeSocket] = []

    def make(connected: bool) -> _FakeSocket:
        def _factory() -> _FakeSocket:
            sock = _FakeSocket(connected)
            instances.append(sock)
            return sock

        monkeypatch.setattr(qtnetwork, "QLocalSocket", _factory)
        return instances

    return make


# ---------------------------------------------------------------------------
# 行为测试
# ---------------------------------------------------------------------------

def test_failure_branch_non_windows_logs_without_raising(monkeypatch, patch_qlocal_socket, capsys) -> None:
    """非 Windows 失败分支：写 emergency 日志 + stderr，不抛异常、不碰 Qt。"""
    instances = patch_qlocal_socket(False)
    monkeypatch.setattr(sys, "platform", "linux")
    recorded: list[str] = []
    monkeypatch.setattr(app, "write_emergency", lambda text, filename="emergency.log": recorded.append(text))

    app._wake_existing()  # 不抛异常即为通过（旧实现此处进程级崩溃）

    assert instances and instances[0].server_name.startswith("backupstudio-")
    assert not instances[0].disconnected  # 未连接，不应走 disconnect
    assert recorded and EXPECTED_MSG in recorded[0]
    assert EXPECTED_MSG in capsys.readouterr().err


@pytest.mark.skipif(sys.platform != "win32", reason="MessageBoxW 仅 Windows")
def test_failure_branch_windows_uses_messageboxw(monkeypatch, patch_qlocal_socket) -> None:
    """Windows 失败分支：调用 user32.MessageBoxW(MB_ICONINFORMATION)，不真实弹窗。"""
    patch_qlocal_socket(False)
    calls: list[tuple] = []

    class _FakeUser32:
        def MessageBoxW(self, hwnd, text, title, flags):  # noqa: N802
            calls.append((hwnd, text, title, flags))
            return 1

    class _FakeWindll:
        user32 = _FakeUser32()

    monkeypatch.setattr(ctypes, "windll", _FakeWindll(), raising=False)

    app._wake_existing()

    assert calls == [(0, EXPECTED_MSG, APP_NAME_ZH, 0x40)]


def test_success_branch_sends_show_without_raising(patch_qlocal_socket) -> None:
    """连接成功分支：写入 b"show" 并断开，不抛异常。"""
    instances = patch_qlocal_socket(True)

    app._wake_existing()

    assert instances and instances[0].written == b"show"
    assert instances[0].disconnected
