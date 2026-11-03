"""通过现有 SSH 配置原子发布脱敏 JSON，不上传任何订阅凭据。"""
import hashlib
import json
import logging
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess
import time


class PublishError(Exception):
    pass


def validate_config(config):
    if not isinstance(config, dict) or type(config.get("enabled", False)) is not bool:
        raise ValueError("publish 必须是对象，enabled 必须是布尔值")
    if not config.get("enabled", False):
        return config
    host = config.get("ssh_host", "")
    path = config.get("remote_path", "")
    if not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]*", host):
        raise ValueError("publish.ssh_host 应为有效 SSH 别名")
    if not isinstance(path, str) or not path.startswith("/") or not path.endswith(".json") or any(c in path for c in "\n\r\0") or ".." in PurePosixPath(path).parts:
        raise ValueError("publish.remote_path 应为绝对 JSON 文件路径")
    for key, low, high in (("interval_seconds", 30, 3600), ("timeout_seconds", 5, 60)):
        value = config.get(key)
        if type(value) is not int or not low <= value <= high:
            raise ValueError("无效配置: publish." + key)
    return config


def public_payload(snapshot):
    provider_fields = ("id", "name", "status", "message", "updated_at", "attempted_at")
    window_fields = ("id", "label", "used_percent", "remaining_percent", "resets_at", "window_minutes", "limit_reached")
    providers = []
    for provider in snapshot["providers"]:
        item = {key: provider.get(key) for key in provider_fields}
        item["windows"] = [{key: window.get(key) for key in window_fields} for window in provider["windows"]]
        providers.append(item)
    # 显式白名单：未来采集接口新增字段，也不会意外公开账户标识或密钥。
    data = {key: snapshot[key] for key in ("schema_version", "generated_at", "stale_after_seconds")}
    data["providers"] = providers
    body = (json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode()
    if len(body) > 262144:
        raise PublishError("payload_too_large")
    return body


def remote_command(remote_path, digest):
    directory = str(PurePosixPath(remote_path).parent)
    # 同一目录暂存，校验完整性后 rename；断线或校验失败均不替换旧文件。
    return "\n".join([
        "set -eu",
        "umask 022",
        "tmp=$(mktemp " + shlex.quote(directory + "/.quota.XXXXXXXXXX") + ")",
        "trap 'rm -f -- \"$tmp\"' EXIT HUP INT TERM",
        'cat > "$tmp"',
        'actual=$(sha256sum "$tmp")',
        '[ "${actual%% *}" = ' + shlex.quote(digest) + ' ]',
        'chmod 644 "$tmp"',
        'mv -f -- "$tmp" ' + shlex.quote(remote_path),
    ])


class SshPublisher:
    def __init__(self, config):
        self.config = validate_config(config)
        if not self.config.get("enabled"):
            raise ValueError("SSH 发布未启用")

    def publish(self, snapshot):
        body = public_payload(snapshot)
        digest = hashlib.sha256(body).hexdigest()
        command = remote_command(self.config["remote_path"], digest)
        try:
            result = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                 "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=2",
                 self.config["ssh_host"], command],
                input=body, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=self.config["timeout_seconds"], check=False)
        except subprocess.TimeoutExpired:
            raise PublishError("ssh_timeout") from None
        except OSError:
            raise PublishError("ssh_unavailable") from None
        if result.returncode != 0:
            raise PublishError("ssh_exit_" + str(result.returncode))
        return digest


def publish_worker(publisher, store, stop, status_file):
    failures = 0
    status = {"last_success_at": None, "last_attempt_at": None, "status": "starting"}
    # 等首次采集完成；某个上游挂起时也不无限阻塞另一个订阅的发布。
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if stop.is_set():
            return
        if all(p["attempted_at"] is not None for p in store.snapshot()["providers"]):
            break
        stop.wait(0.2)
    while not stop.is_set():
        status["last_attempt_at"] = int(time.time())
        try:
            digest = publisher.publish(store.snapshot())
            status.update(status="ok", last_success_at=int(time.time()), sha256=digest, error=None)
            failures = 0
            logging.info("额度 JSON 已通过 SSH 发布")
        except Exception as exc:
            code = str(exc) if isinstance(exc, PublishError) else "publish_failed"
            status.update(status="error", error=code)
            failures += 1
            logging.warning("额度 JSON 发布失败: %s", code)
        try:
            path = Path(status_file)
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix(".tmp")
            temp.write_text(json.dumps(status), encoding="utf-8")
            temp.replace(path)
        except OSError:
            logging.warning("发布状态记录失败")
        if stop.wait(min(300, publisher.config["interval_seconds"] * 2 ** min(failures, 3))):
            break
