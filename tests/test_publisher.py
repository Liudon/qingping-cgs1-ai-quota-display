import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from service.main import Store
from service.publisher import PublishError, SshPublisher, public_payload, remote_command, validate_config
from scripts.device import validate_url


def config(path="/web/quota-data/quota.json"):
    return dict(enabled=True, ssh_host="test-vps", remote_path=path, interval_seconds=60, timeout_seconds=20)


class PublisherTests(unittest.TestCase):
    def test_whitelist_drops_private_fields_at_each_level(self):
        snapshot = Store().snapshot()
        snapshot["access_token"] = "SECRET"
        p = snapshot["providers"][0]
        p["accountId"] = "SECRET"
        p["windows"] = [dict(id="short", label="5 小时", used_percent=12,
                             remaining_percent=88, resets_at=2000, window_minutes=300,
                             limit_reached=False, api_key="SECRET")]
        body = public_payload(snapshot)
        self.assertNotIn(b"SECRET", body)
        self.assertEqual(json.loads(body)["providers"][0]["windows"][0]["remaining_percent"], 88)

    def test_ssh_receives_only_public_json_and_noninteractive_options(self):
        with patch("service.publisher.subprocess.run") as run:
            run.return_value.returncode = 0
            digest = SshPublisher(config()).publish(Store().snapshot())
        args, kwargs = run.call_args
        self.assertIn("BatchMode=yes", args[0])
        self.assertIn("StrictHostKeyChecking=yes", args[0])
        self.assertEqual(hashlib.sha256(kwargs["input"]).hexdigest(), digest)
        self.assertEqual(kwargs["timeout"], 20)

    def test_remote_script_installs_complete_json_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            # 路径含空格/单引号，确保 shell 引用不会破坏文件目标。
            directory = Path(tmp) / "quota data's"
            directory.mkdir()
            target = directory / "quota.json"
            target.write_text("old")
            body = b'{"schema_version":1}\n'
            command = remote_command(str(target), hashlib.sha256(body).hexdigest())
            subprocess.run(["sh", "-c", command], input=body, check=True)
            self.assertEqual(target.read_bytes(), body)
            self.assertEqual(target.stat().st_mode & 0o777, 0o644)
            self.assertEqual([p.name for p in directory.iterdir()], ["quota.json"])

    def test_truncated_transfer_preserves_previous_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "quota.json"
            target.write_text("previous")
            body = b'{"schema_version":1}\n'
            command = remote_command(str(target), hashlib.sha256(body).hexdigest())
            result = subprocess.run(["sh", "-c", command], input=body[:5])
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(target.read_text(), "previous")
            self.assertEqual(len(list(Path(tmp).iterdir())), 1)

    def test_ssh_failure_and_timeout_are_bounded_and_sanitized(self):
        publisher = SshPublisher(config())
        with patch("service.publisher.subprocess.run") as run:
            run.return_value.returncode = 255
            with self.assertRaisesRegex(PublishError, "ssh_exit_255"):
                publisher.publish(Store().snapshot())
            run.side_effect = subprocess.TimeoutExpired("ssh", 20)
            with self.assertRaisesRegex(PublishError, "ssh_timeout"):
                publisher.publish(Store().snapshot())

    def test_disabled_publish_is_opt_in(self):
        self.assertEqual(validate_config({"enabled": False}), {"enabled": False})
        with self.assertRaises(ValueError):
            SshPublisher({"enabled": False})

    def test_rejects_ssh_options_and_parent_paths(self):
        for values in ({"ssh_host": "-oProxyCommand=bad"}, {"ssh_host": "server;bad"},
                       {"remote_path": "/web/../etc/quota.json"}, {"remote_path": "quota.json"},
                       {"interval_seconds": 1}, {"timeout_seconds": True}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                validate_config(dict(config(), **values))

    def test_device_accepts_public_https_static_json(self):
        self.assertEqual(validate_url("https://example.com/quota.json"), "https://example.com/quota.json")


if __name__ == "__main__":
    unittest.main()
