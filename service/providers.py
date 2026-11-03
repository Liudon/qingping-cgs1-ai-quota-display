"""只读采集；不调用模型，不将上游原始响应或凭据交给展示层。"""
import datetime as dt
import json
import math
import os
from pathlib import Path
import selectors
import subprocess
import time
import urllib.error
import urllib.request


class QuotaError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise QuotaError("invalid_response")
    return value


def timestamp(value):
    if value is None:
        return None
    if isinstance(value, str):
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError()
            return int(parsed.timestamp())
        except ValueError:
            raise QuotaError("invalid_response") from None
    return int(number(value))


def window(key, label, used, reset, minutes=None, limited=False):
    used = number(used)
    if used < 0:
        raise QuotaError("invalid_response")
    return dict(id=key, label=label, used_percent=used,
                remaining_percent=round(max(0, 100 - used), 1),
                resets_at=timestamp(reset), window_minutes=minutes,
                limit_reached=limited or used >= 100)


def duration_label(minutes, fallback):
    if minutes is None:
        return fallback
    number(minutes)
    if minutes <= 0:
        raise QuotaError("invalid_response")
    if minutes == 10080:
        return "每周"
    if minutes % 1440 == 0:
        return "%g 天" % (minutes / 1440)
    if minutes % 60 == 0:
        return "%g 小时" % (minutes / 60)
    return "%g 分钟" % minutes


def parse_codex(result):
    buckets = result.get("rateLimitsByLimitId")
    if not buckets:
        old = result.get("rateLimits")
        buckets = {old.get("limitId") or "codex": old} if isinstance(old, dict) else {}
    if not isinstance(buckets, dict):
        raise QuotaError("invalid_response")
    windows = []
    for key, bucket in buckets.items():
        if not isinstance(bucket, dict):
            raise QuotaError("invalid_response")
        for kind, fallback in (("primary", "短时"), ("secondary", "长期")):
            data = bucket.get(kind)
            if data is None:
                continue
            if not isinstance(data, dict):
                raise QuotaError("invalid_response")
            minutes = data.get("windowDurationMins")
            label = duration_label(minutes, fallback)
            if len(buckets) > 1:
                label = str(bucket.get("limitName") or key)[:60] + " · " + label
            windows.append(window(str(key) + ":" + kind, label,
                                  data.get("usedPercent"), data.get("resetsAt"), minutes))
    if not windows:
        raise QuotaError("no_quota")
    return windows


def parse_go(result):
    usage = result.get("usage")
    if not isinstance(usage, dict):
        raise QuotaError("invalid_response")
    windows = []
    for key, label, minutes in (("rolling", "5 小时", 300), ("weekly", "每周", 10080), ("monthly", "每月", None)):
        data = usage.get(key)
        if not isinstance(data, dict):
            raise QuotaError("invalid_response")
        status = data.get("status")
        if status not in ("ok", "rate-limited"):
            raise QuotaError("invalid_response")
        windows.append(window(key, label, data.get("percent"), data.get("resetsAt"),
                              minutes, status == "rate-limited"))
    return windows


class NoRedirect(urllib.request.HTTPRedirectHandler):
    # 凭据只发往固定官方地址，不跟随重定向。
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GoProvider:
    def __init__(self, auth_file, timeout=20):
        self.auth_file = Path(auth_file).expanduser()
        self.timeout = timeout

    def fetch(self):
        try:
            auth = json.loads(self.auth_file.read_text())["opencode-go"]
            key = auth["key"]
            if auth.get("type") != "api_key" or not isinstance(key, str) or not key or "\n" in key or "\r" in key:
                raise ValueError()
        except (OSError, ValueError, KeyError, TypeError):
            raise QuotaError("auth_required") from None
        request = urllib.request.Request(
            "https://opencode.ai/inference/go/v1/usage",
            headers={"Authorization": "Bearer " + key, "User-Agent": "ai-quota-display/0.1"})
        try:
            with urllib.request.build_opener(NoRedirect()).open(request, timeout=self.timeout) as response:
                body = response.read(262145)
                if len(body) > 262144:
                    raise QuotaError("invalid_response")
                result = json.loads(body)
            return parse_go(result)
        except urllib.error.HTTPError as exc:
            code = "auth_required" if exc.code in (401, 403) else "rate_limited" if exc.code == 429 else "upstream_error"
            exc.close()
            raise QuotaError(code) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise QuotaError("unavailable") from None
        except (ValueError, TypeError, AttributeError):
            raise QuotaError("invalid_response") from None

    def close(self):
        pass


class CodexProvider:
    """单个采集线程独占 stdio 子进程，超时后丢弃并在下次重连。"""
    def __init__(self, executable="codex", timeout=25):
        self.executable = executable
        self.timeout = timeout
        self.process = None
        self.selector = None
        self.buffer = b""
        self.request_id = 0

    def send(self, message):
        self.process.stdin.write((json.dumps(message) + "\n").encode())
        self.process.stdin.flush()

    def request(self, method, params=None):
        self.request_id += 1
        message = dict(id=self.request_id, method=method)
        if params is not None:
            message["params"] = params
        self.send(message)
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if b"\n" not in self.buffer:
                if not self.selector.select(max(0, deadline - time.monotonic())):
                    break
                chunk = os.read(self.process.stdout.fileno(), 65536)
                if not chunk:
                    raise QuotaError("unavailable")
                self.buffer += chunk
                if len(self.buffer) > 2_000_000:
                    raise QuotaError("invalid_response")
                continue
            line, self.buffer = self.buffer.split(b"\n", 1)
            data = json.loads(line)
            if data.get("id") != self.request_id:
                continue
            if "error" in data:
                # 只分类，不记录服务端消息（可能带账户信息）。
                detail = str(data["error"].get("message", "")).lower()
                code = "auth_required" if any(s in detail for s in ("auth", "login", "sign in", "unauthorized", "401")) else "upstream_error"
                raise QuotaError(code)
            result = data.get("result")
            if not isinstance(result, dict):
                raise QuotaError("invalid_response")
            return result
        raise QuotaError("timeout")

    def fetch(self):
        try:
            if self.process is None or self.process.poll() is not None:
                self.close()
                self.process = subprocess.Popen(
                    [self.executable, "app-server", "--stdio"], stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
                self.selector = selectors.DefaultSelector()
                self.selector.register(self.process.stdout, selectors.EVENT_READ)
                self.request("initialize", {"clientInfo": {"name": "ai_quota_display", "version": "0.1.0"}})
                self.send({"method": "initialized"})
            return parse_codex(self.request("account/rateLimits/read"))
        except QuotaError:
            self.close()
            raise
        except (OSError, ValueError, TypeError, AttributeError):
            self.close()
            raise QuotaError("unavailable") from None

    def close(self):
        if self.selector:
            self.selector.close()
            self.selector = None
        if self.process:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
            self.process.stdin.close()
            self.process.stdout.close()
            self.process = None
        self.buffer = b""
