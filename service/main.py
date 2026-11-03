"""python3 -m service.main --config config.json"""
import argparse
import copy
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .providers import CodexProvider, GoProvider, QuotaError
from .publisher import SshPublisher, publish_worker, validate_config as validate_publish_config

ROOT = Path(__file__).resolve().parents[1]
NAMES = {"codex": "Codex", "opencode-go": "OpenCode GO"}
MESSAGES = {"auth_required": "需检查登录或凭据", "invalid_response": "额度格式发生变化",
            "no_quota": "未返回可展示额度", "rate_limited": "查询暂时限流",
            "timeout": "查询超时", "unavailable": "暂时无法连接", "upstream_error": "上游服务异常"}


class Store:
    def __init__(self, stale_after=180, cache_file=None):
        self.lock = threading.Lock()
        self.stale_after = stale_after
        self.cache_file = Path(cache_file) if cache_file else None
        self.records = {key: dict(id=key, name=name, status="loading", message="正在读取额度",
                                 updated_at=None, attempted_at=None, windows=[]) for key, name in NAMES.items()}
        # 仅加载本服务保存的展示数据；启动时始终视为旧数据。
        if self.cache_file:
            try:
                cached = json.loads(self.cache_file.read_text())
                for item in cached["providers"]:
                    if item["id"] in self.records and isinstance(item.get("windows"), list):
                        record = self.records[item["id"]]
                        record.update(windows=item["windows"], updated_at=item.get("updated_at"),
                                      status="stale", message="等待重新同步")
            except (OSError, ValueError, KeyError, TypeError):
                pass

    def update(self, key, windows=None, error=None, now=None):
        now = int(time.time()) if now is None else now
        with self.lock:
            record = self.records[key]
            record["attempted_at"] = now
            if error:
                record.update(status="stale" if record["windows"] else "error",
                              message=MESSAGES.get(error, "暂时无法读取"))
            else:
                record.update(windows=windows, updated_at=now, status="ok", message="")
            if self.cache_file:
                try:
                    self.cache_file.parent.mkdir(parents=True, exist_ok=True)
                    temp = self.cache_file.with_suffix(".tmp")
                    with open(temp, "w", encoding="utf-8") as f:
                        os.chmod(temp, 0o600)
                        json.dump({"providers": list(self.records.values())}, f, ensure_ascii=False)
                    os.replace(temp, self.cache_file)
                except OSError:
                    logging.warning("缓存写入失败")

    def snapshot(self, now=None):
        now = int(time.time()) if now is None else now
        with self.lock:
            records = copy.deepcopy(list(self.records.values()))
        for record in records:
            if record["status"] == "ok":
                expired = any(w.get("resets_at") is not None and w["resets_at"] <= now for w in record["windows"])
                if now - record["updated_at"] > self.stale_after or expired:
                    record.update(status="stale", message="等待额度刷新" if expired else "数据已过期")
        return dict(schema_version=1, generated_at=now, stale_after_seconds=self.stale_after, providers=records)


def worker(key, provider, store, stop, interval):
    failures = 0
    try:
        while not stop.is_set():
            try:
                store.update(key, windows=provider.fetch())
                failures = 0
                logging.info("%s 额度已更新", key)
            except Exception as exc:
                code = exc.code if isinstance(exc, QuotaError) else "unavailable"
                store.update(key, error=code)
                failures += 1
                logging.warning("%s: %s", key, code)
            if stop.wait(min(300, interval * (2 ** min(failures, 3)))):
                break
    finally:
        provider.close()


def handler_for(store):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            status, kind = 200, "application/json; charset=utf-8"
            if path == "/api/v1/quotas":
                body = json.dumps(store.snapshot(), ensure_ascii=False, allow_nan=False).encode()
            elif path == "/healthz":
                snap = store.snapshot()
                body = json.dumps({"status": "ok", "providers": {p["id"]: p["status"] for p in snap["providers"]}}).encode()
            elif path == "/":
                kind = "text/html; charset=utf-8"
                body = (ROOT / "service" / "preview.html").read_bytes()
            else:
                status, body = 404, b'{"error":"not_found"}'
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, fmt, *args):
            pass
    return Handler


def load_config(path):
    config = dict(host="0.0.0.0", port=8766, poll_seconds=60, stale_after_seconds=180,
                  device_refresh_seconds=300, codex_bin=str(Path.home() / ".local/bin/codex"),
                  codex_home="", pi_auth_file=str(Path.home() / ".pi/agent/auth.json"),
                  cache_file="runtime/quotas.json", publish={"enabled": False})
    if path:
        config.update(json.loads(Path(path).read_text()))
    for key, minimum, maximum in (("port", 1, 65535), ("poll_seconds", 10, 3600), ("stale_after_seconds", 30, 86400)):
        value = config[key]
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError("无效配置: " + key)
    if config["stale_after_seconds"] < config["poll_seconds"] * 2:
        raise ValueError("stale_after_seconds 必须至少为 poll_seconds 的两倍")
    if not isinstance(config["codex_home"], str):
        raise ValueError("codex_home 必须是路径字符串")
    if config["codex_home"]:
        codex_home = Path(config["codex_home"]).expanduser()
        if not codex_home.is_absolute():
            raise ValueError("codex_home 必须是绝对路径或以 ~ 开头的路径")
        config["codex_home"] = str(codex_home)
    cache = Path(config["cache_file"]).expanduser()
    config["cache_file"] = str(cache if cache.is_absolute() else ROOT / cache)
    validate_publish_config(config["publish"])
    return config


def main():
    parser = argparse.ArgumentParser(description="青萍 AI 订阅额度服务")
    parser.add_argument("--config")
    once = parser.add_mutually_exclusive_group()
    once.add_argument("--check", action="store_true", help="只读查询一次，输出脱敏后的展示数据")
    once.add_argument("--publish-once", action="store_true", help="读取额度并通过 SSH 发布一次，然后退出")
    args = parser.parse_args()
    config = load_config(args.config)
    if config["codex_home"]:
        os.environ["CODEX_HOME"] = config["codex_home"]
    if args.publish_once and not config["publish"].get("enabled"):
        parser.error("--publish-once 需要在配置中启用 publish")
    handlers = []
    if not args.check:
        log_dir = ROOT / "runtime"
        log_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(log_dir / "service.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"))
    if args.check or sys.stderr.isatty():
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)
    store = Store(config["stale_after_seconds"], None if args.check or args.publish_once else config["cache_file"])
    providers = {"codex": CodexProvider(os.path.expanduser(config["codex_bin"])),
                 "opencode-go": GoProvider(config["pi_auth_file"])}
    if args.check or args.publish_once:
        for key, provider in providers.items():
            try:
                store.update(key, windows=provider.fetch())
            except QuotaError as exc:
                store.update(key, error=exc.code)
            finally:
                provider.close()
        result = store.snapshot()
        if args.publish_once:
            SshPublisher(config["publish"]).publish(result)
            print("额度 JSON 已发布")
            return 0 if all(p["status"] == "ok" for p in result["providers"]) else 1
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if all(p["status"] == "ok" for p in result["providers"]) else 1
    server = ThreadingHTTPServer((config["host"], config["port"]), handler_for(store))
    server.daemon_threads = True
    stop = threading.Event()
    threads = [threading.Thread(target=worker, args=(key, provider, store, stop, config["poll_seconds"]), daemon=True)
               for key, provider in providers.items()]
    if config["publish"].get("enabled"):
        threads.append(threading.Thread(target=publish_worker,
                       args=(SshPublisher(config["publish"]), store, stop, ROOT / "runtime/publish-status.json"), daemon=True))
    def shutdown(signum, frame):
        if not stop.is_set():
            stop.set()
            threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    for thread in threads:
        thread.start()
    logging.info("服务已启动 http://%s:%s", config["host"], config["port"])
    try:
        server.serve_forever()
    finally:
        stop.set()
        server.server_close()
        for thread in threads:
            thread.join(timeout=55)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
