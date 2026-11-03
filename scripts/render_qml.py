#!/usr/bin/env python3
"""可选桌面渲染检查：依赖 PySide6-Essentials，仅使用模拟额度。"""
import argparse
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")

from PySide6.QtCore import QTimer, QUrl
from PySide6.QtGui import QColor, QGuiApplication
from PySide6.QtQuick import QQuickView

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=str(ROOT / "runtime/qml-preview.png"))
    parser.add_argument("--state", choices=["fresh", "stale", "error", "expired-file"], default="fresh")
    args = parser.parse_args()
    now = int(time.time())
    providers = []
    for key, name, amounts in (("codex", "Codex", [73, 76]), ("opencode-go", "OpenCode GO", [100, 18, 8])):
        providers.append(dict(id=key, name=name, status="ok" if args.state in ("fresh", "expired-file") else args.state,
                              message="" if args.state in ("fresh", "expired-file") else "需检查登录或凭据", updated_at=now if args.state == "fresh" else now-600,
                              windows=[] if args.state == "error" else [dict(label=label, remaining_percent=amount, resets_at=now+delay)
                              for label, amount, delay in zip(["5 小时", "每周", "每月"], amounts, [16000, 260000, 160000])]))
    data = dict(schema_version=1, generated_at=now-600 if args.state == "expired-file" else now, stale_after_seconds=180, providers=providers)
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps(data).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    app = QGuiApplication([])
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        for name in ("CustomView.qml", "Quota.js"):
            shutil.copy2(ROOT / "qml" / name, tmp / name)
        (tmp / "Settings.js").write_text('.pragma library\nvar serverUrl = "http://127.0.0.1:' + str(server.server_port) + '/api/v1/quotas"\nvar refreshSeconds = 300\n')
        view = QQuickView()
        view.setColor(QColor("#10171d"))
        view.setResizeMode(QQuickView.SizeRootObjectToView)
        view.setSource(QUrl.fromLocalFile(str(tmp / "CustomView.qml")))
        if view.status() == QQuickView.Error:
            return 1
        view.resize(720, 720)
        view.show()
        def capture():
            output = Path(args.output)
            output.parent.mkdir(parents=True, exist_ok=True)
            # 请求成功后页面会改变此字段；验证实际 XHR → JSON → QML 链路。
            root = view.rootObject()
            if "已连接额度服务" not in root.property("connectionMessage"):
                print("页面未收到模拟额度：", root.property("connectionMessage"))
                app.exit(1)
                return
            if args.state == "expired-file" and root.property("now") < now - 30:
                print("静态文件时间被错误当作当前时间")
                app.exit(1)
                return
            if not view.grabWindow().save(str(output)):
                app.exit(1)
                return
            print("QML 渲染与 HTTP 读取通过：", output)
            app.exit(0)
        QTimer.singleShot(1500, capture)
        result = app.exec()
        view.close()
    server.shutdown()
    server.server_close()
    thread.join()
    return result


if __name__ == "__main__":
    raise SystemExit(main())
