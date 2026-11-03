import json
from pathlib import Path
import tempfile
import sys
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from http.server import ThreadingHTTPServer
from service.main import Store, handler_for, worker, load_config
from service.providers import CodexProvider, GoProvider, NoRedirect, QuotaError, parse_codex, parse_go
from scripts.device import validate_url


def codex(used=12, reset=2000):
    return {"rateLimits": {"primary": {"usedPercent": used, "windowDurationMins": 300, "resetsAt": reset},
                            "secondary": {"usedPercent": 22, "windowDurationMins": 10080, "resetsAt": 5000}}}


def go():
    return {"usage": {key: {"status": "ok", "percent": 6, "resetsAt": "2026-11-05T03:39:36.000Z"}
                      for key in ("rolling", "weekly", "monthly")}}


class ParsingTests(unittest.TestCase):
    def test_codex_normalizes_windows_and_excludes_identity(self):
        data = codex()
        data.update(accountId="private-id", rateLimitResetCredits={"secret": "private"})
        parsed = parse_codex(data)
        self.assertEqual([w["remaining_percent"] for w in parsed], [88, 78])
        self.assertEqual([w["label"] for w in parsed], ["5 小时", "每周"])
        self.assertNotIn("private", json.dumps(parsed))

    def test_multi_bucket_no_duplicate_legacy(self):
        data = codex()
        data["rateLimitsByLimitId"] = {"codex": data["rateLimits"], "other": {"primary": {"usedPercent": 55, "windowDurationMins": 15, "resetsAt": None}}}
        windows = parse_codex(data)
        self.assertEqual(len(windows), 3)
        self.assertEqual(windows[-1]["label"], "other · 15 分钟")
        self.assertIsNone(windows[-1]["resets_at"])

    def test_missing_quota_is_unknown_not_full(self):
        with self.assertRaises(QuotaError):
            parse_codex({"rateLimits": {"primary": None, "secondary": None}})

    def test_bad_numbers_rejected(self):
        for used in (True, None, "10", float("nan"), float("inf"), -1):
            with self.subTest(used=used), self.assertRaises(QuotaError):
                parse_codex(codex(used))

    def test_over_limit_clamps_remaining(self):
        self.assertEqual(parse_codex(codex(123))[0]["remaining_percent"], 0)
        self.assertTrue(parse_codex(codex(123))[0]["limit_reached"])

    def test_go_iso_time_and_window_labels(self):
        windows = parse_go(go())
        self.assertEqual([w["label"] for w in windows], ["5 小时", "每周", "每月"])
        self.assertEqual(windows[0]["remaining_percent"], 94)
        self.assertEqual(windows[0]["resets_at"], 1793849976)

    def test_partial_go_response_is_not_success(self):
        data = go()
        del data["usage"]["monthly"]
        with self.assertRaises(QuotaError):
            parse_go(data)

    def test_go_unknown_status_is_not_success(self):
        data = go()
        data["usage"]["monthly"]["status"] = "not-subscribed"
        with self.assertRaises(QuotaError):
            parse_go(data)

    def test_invalid_reset_not_assumed_to_be_local_timezone(self):
        data = go()
        data["usage"]["monthly"]["resetsAt"] = "2026-11-05T03:39:36"
        with self.assertRaises(QuotaError):
            parse_go(data)


class CacheTests(unittest.TestCase):
    def test_failure_preserves_last_success_and_other_provider(self):
        store = Store()
        store.update("codex", parse_codex(codex()), now=1000)
        store.update("opencode-go", parse_go(go()), now=1000)
        store.update("codex", error="auth_required", now=1010)
        a, b = store.snapshot(now=1020)["providers"]
        self.assertEqual(a["status"], "stale")
        self.assertEqual(a["updated_at"], 1000)
        self.assertEqual(a["windows"][0]["remaining_percent"], 88)
        self.assertEqual(b["status"], "ok")

    def test_time_expiry_and_reset_do_not_reset_to_full(self):
        store = Store(stale_after=180)
        store.update("codex", parse_codex(codex(reset=1100)), now=1000)
        self.assertEqual(store.snapshot(now=1100)["providers"][0]["status"], "stale")
        store.update("codex", parse_codex(codex()), now=1000)
        record = store.snapshot(now=1181)["providers"][0]
        self.assertEqual(record["status"], "stale")
        self.assertEqual(record["windows"][0]["remaining_percent"], 88)

    def test_disk_cache_loaded_as_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "quota.json"
            store = Store(cache_file=path)
            store.update("codex", parse_codex(codex()), now=1000)
            loaded = Store(cache_file=path).snapshot(now=1001)["providers"][0]
            self.assertEqual(loaded["status"], "stale")
            self.assertEqual(loaded["updated_at"], 1000)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_worker_sanitizes_unexpected_error(self):
        stop = threading.Event()
        class Failing:
            closed = False
            def fetch(self):
                stop.set()
                raise RuntimeError("secret-token")
            def close(self):
                self.closed = True
        provider, store = Failing(), Store()
        worker("codex", provider, store, stop, 60)
        self.assertTrue(provider.closed)
        self.assertNotIn("secret-token", json.dumps(store.snapshot()))


class TransportTests(unittest.TestCase):
    def test_missing_credential_and_redirect_are_safe(self):
        with self.assertRaises(QuotaError) as ctx:
            GoProvider("/does-not-exist").fetch()
        self.assertEqual(ctx.exception.code, "auth_required")
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, "", {}, "https://elsewhere.test"))

    def test_auth_failure_has_no_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "auth.json"
            path.write_text(json.dumps({"opencode-go": {"type": "api_key", "key": "secret-token"}}))
            with patch("service.providers.urllib.request.build_opener") as build:
                build.return_value.open.side_effect = urllib.error.HTTPError("https://opencode.ai", 401, "secret-token", {}, None)
                with self.assertRaises(QuotaError) as ctx:
                    GoProvider(path).fetch()
            self.assertEqual(str(ctx.exception), "auth_required")

    def test_codex_real_stdio_framing_and_child_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "fake-codex"
            script.write_text('#!' + sys.executable + '\nimport json,sys\nfor line in sys.stdin:\n d=json.loads(line)\n if "id" in d:\n  result={} if d["method"]=="initialize" else '+repr(codex())+'\n  sys.stdout.write(json.dumps({"method":"notice"})+"\\n"+json.dumps({"id":d["id"],"result":result})+"\\n"); sys.stdout.flush()\n')
            script.chmod(0o700)
            provider = CodexProvider(str(script), timeout=10)
            try:
                self.assertEqual(provider.fetch()[0]["remaining_percent"], 88)
                child = provider.process
                self.assertEqual(provider.fetch()[0]["remaining_percent"], 88)
                self.assertIs(child, provider.process)
            finally:
                provider.close()
            self.assertIsNotNone(child.poll())

    def test_codex_timeout_kills_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "silent-codex"
            script.write_text('#!/usr/bin/env python3\nimport time\ntime.sleep(60)\n')
            script.chmod(0o700)
            provider = CodexProvider(str(script), timeout=0.1)
            with self.assertRaises(QuotaError) as ctx:
                provider.fetch()
            self.assertEqual(ctx.exception.code, "timeout")
            self.assertIsNone(provider.process)

    def test_http_cache_and_health(self):
        store = Store()
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(store))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = "http://127.0.0.1:" + str(server.server_port)
            with urllib.request.urlopen(base + "/api/v1/quotas") as response:
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                self.assertEqual(json.load(response)["schema_version"], 1)
            with urllib.request.urlopen(base + "/healthz") as response:
                self.assertEqual(json.load(response)["providers"]["codex"], "loading")
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(base + "/auth.json")
            self.assertEqual(ctx.exception.code, 404)
            ctx.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


class ConfigTests(unittest.TestCase):
    def test_url_rejects_credentials_loopback_and_wrong_path(self):
        for url in ("http://user:pass@mac/api/v1/quotas", "http://127.0.0.1:8766/api/v1/quotas", "http://mac/", "file:///tmp/x"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_url(url)
        self.assertEqual(validate_url("http://192.168.50.112:8766/api/v1/quotas"), "http://192.168.50.112:8766/api/v1/quotas")

    def test_bad_config_fails_early(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text('{"poll_seconds": 100, "stale_after_seconds": 50}')
            with self.assertRaises(ValueError):
                load_config(path)


if __name__ == "__main__":
    unittest.main()
