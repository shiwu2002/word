# -*- coding: utf-8 -*-
"""PyInstaller 窗口（无控制台）运行时钩子。

打包成 --windowed / console=False 后，sys.stdout 与 sys.stderr 会是 None。
Flask / Werkzeug 的日志 handler 会在写入时抛 AttributeError，而且因为看不到
控制台极难排查。这里在启动最早期把它们替换成可丢弃的输出流，避免崩溃。
"""

import sys
import io


def _quiet_stream():
    return io.StringIO()


if sys.stdout is None:
    sys.stdout = _quiet_stream()
if sys.stderr is None:
    sys.stderr = _quiet_stream()
