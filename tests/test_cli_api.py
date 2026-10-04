"""Exercise the CLI and real urllib transport against a local contract server."""

import contextlib
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid

from gpudeck_cli.cli import main, parser
from gpudeck_cli.client import Client, DeckError, normalize_url
from gpudeck_cli.reservations import UTC, wire_time


USER_ID = str(uuid.uuid4())
GPU_ID = str(uuid.uuid4())


class ContractHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, data=None, headers=None):
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if data is not None:
            try:
                self.wfile.write(json.dumps(data).encode())
            except BrokenPipeError:
                pass

    def authorized(self):
        return self.server.session in self.headers.get("Cookie", "")

    def do_GET(self):
        if not self.authorized():
            return self.reply(401, {"error": "请登录"})
        path = self.path.removeprefix("/api/v1")
        if path == "/auth/me":
            return self.reply(200, {"id": USER_ID, "username": self.server.username,
                                   "linuxUsername": self.server.username, "role": "user",
                                   "concurrentGpuLimit": 2, "csrfToken": "secret-csrf"})
        if path == "/resources":
            return self.reply(200, {"serverTime": wire_time(datetime.now(UTC)), "nodes": [{
                "id": "node", "name": "WHUServer-H200", "lastSeenAt": wire_time(datetime.now(UTC)),
                "gpus": [{"id": GPU_ID, "index": 0, "name": "NVIDIA H200", "maintenance": False,
                          "missing": False, "memoryUsedMb": 0, "processes": []}]}]})
        if path == "/reservations":
            if self.server.fail_verification:
                return self.reply(503, {"error": "暂时不可用"})
            return self.reply(200, self.server.records)
        self.reply(404, {"error": "不存在"})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
        path = self.path.removeprefix("/api/v1")
        if path == "/auth/login":
            self.server.logins += 1
            if body["password"] != "test-pass":
                return self.reply(401, {"error": "用户名或密码错误"})
            self.server.username = body["username"]
            return self.reply(200, {"csrfToken": "secret-csrf"},
                              {"Set-Cookie": f"gpudeck_session={self.server.session}; Path=/; HttpOnly; Max-Age=600"})
        if not self.authorized():
            return self.reply(401, {"error": "请登录"})
        if self.headers.get("X-CSRF-Token") != "secret-csrf":
            return self.reply(403, {"error": "CSRF 无效"})
        if path == "/auth/logout":
            self.server.session = str(uuid.uuid4())
            return self.reply(204)
        if path == "/reservations":
            self.server.creates += 1
            if self.server.conflict:
                return self.reply(409, {"error": "预约状态已变化"})
            reservation_id = str(uuid.uuid4())
            self.server.records.append({"id": reservation_id, "ownerId": USER_ID, "ownerName": "Test",
                                        "status": "scheduled", "checkedInAt": None, **body})
            if self.server.delay_create:
                time.sleep(self.server.delay_create)
            if self.server.fail_after_create:
                self.server.fail_verification = True
            if self.server.error_after_create:
                return self.reply(500, {"error": "提交后发生错误"})
            return self.reply(201, {"id": reservation_id})
        for suffix, status in [("/cancel", "cancelled"), ("/end", "completed")]:
            if path.endswith(suffix):
                reservation_id = path.split("/")[2]
                record = next(r for r in self.server.records if r["id"] == reservation_id)
                record["status"] = status
                record["gpuIds"] = []
                self.server.actions += 1
                return self.reply(204)
        self.reply(404, {"error": "不存在"})


class CliContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), ContractHandler)
        for key, value in {"session": str(uuid.uuid4()), "username": "test", "records": [],
                           "logins": 0, "creates": 0, "actions": 0, "conflict": False,
                           "delay_create": 0, "fail_after_create": False,
                           "fail_verification": False, "error_after_create": False}.items():
            setattr(self.server, key, value)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.global_args = ["--url", self.url, "--state-dir", self.tmp.name]
        self.env = patch.dict("os.environ", {"GPUDECK_USERNAME": "test", "GPUDECK_PASSWORD": "test-pass"})
        self.env.start()
        self.start = wire_time(datetime.now(UTC) + timedelta(hours=1))
        self.reserve_args = ["reserve", "--server", "H200", "--gpus", "0", "--start", self.start,
                             "--hours", "1", "--project", "CLI test", "--purpose", "debug"]

    def tearDown(self):
        self.env.stop()
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def cli(self, args, **env):
        output = io.StringIO()
        with patch.dict("os.environ", env), contextlib.redirect_stdout(output):
            code = main(self.global_args + args)
        text = output.getvalue()
        self.assertNotIn("test-pass", text)
        self.assertNotIn("secret-csrf", text)
        return code, json.loads(text)

    def test_login_session_reuse_and_private_cookie_file(self):
        code, result = self.cli(["login"])
        self.assertEqual(code, 0)
        path = Path(result["data"]["sessionFile"])
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        self.assertNotIn("test-pass", path.read_text())
        code, result = self.cli(["whoami"], GPUDECK_PASSWORD="", GPUDECK_USERNAME="")
        self.assertEqual(code, 0)
        self.assertEqual(self.server.logins, 1)
        self.assertNotIn("csrfToken", result["data"])

    def test_dry_run_create_duplicate_cancel_verified(self):
        code, preview = self.cli(self.reserve_args + ["--dry-run"])
        self.assertEqual(code, 0)
        self.assertTrue(preview["data"]["dryRun"])
        self.assertEqual(self.server.creates, 0)
        code, created = self.cli(self.reserve_args)
        self.assertEqual(code, 0)
        self.assertTrue(created["data"]["verified"])
        reservation_id = created["data"]["reservation"]["id"]
        code, again = self.cli(self.reserve_args)
        self.assertEqual(code, 0)
        self.assertTrue(again["data"]["reused"])
        self.assertEqual(self.server.creates, 1)
        code, result = self.cli(["cancel", reservation_id, "--dry-run"])
        self.assertEqual(code, 0)
        self.assertEqual(self.server.actions, 0)
        code, result = self.cli(["cancel", reservation_id])
        self.assertEqual(code, 0)
        self.assertEqual(result["data"]["reservation"]["status"], "cancelled")
        code, result = self.cli(["cancel", reservation_id])
        self.assertEqual(code, 0)
        self.assertFalse(result["data"]["changed"])
        self.assertEqual(self.server.actions, 1)

    def test_end_and_owner_guard(self):
        _, created = self.cli(self.reserve_args)
        reservation_id = created["data"]["reservation"]["id"]
        self.server.records[0]["ownerId"] = "other-owner"
        code, result = self.cli(["cancel", reservation_id])
        self.assertEqual(code, 3)
        self.assertEqual(self.server.actions, 0)
        self.server.records[0]["ownerId"] = USER_ID
        code, result = self.cli(["end", reservation_id])
        self.assertEqual(code, 0)
        self.assertEqual(result["data"]["reservation"]["status"], "completed")

    def test_server_race_conflict_is_structured_and_not_retried(self):
        self.server.conflict = True
        code, result = self.cli(self.reserve_args)
        self.assertEqual(code, 4)
        self.assertEqual(result["error"]["status"], 409)
        self.assertEqual(self.server.creates, 1)

    def test_expired_session_reauthenticates_once(self):
        self.cli(["login"])
        self.server.session = "new-session"
        code, result = self.cli(["whoami"])
        self.assertEqual(code, 0)
        self.assertEqual(self.server.logins, 2)

    def test_requested_account_does_not_reuse_other_account(self):
        self.cli(["login"])
        code, result = self.cli(["whoami"], GPUDECK_USERNAME="different")
        self.assertEqual(code, 0)
        self.assertEqual(result["data"]["username"], "different")
        self.assertEqual(self.server.logins, 2)

    def test_timeout_after_commit_does_not_retry_post(self):
        self.cli(["login"])
        self.server.delay_create = 0.2
        code, result = self.cli(self.reserve_args + ["--timeout", "0.05"])
        self.assertEqual(code, 5)
        self.assertEqual(result["error"]["code"], "outcome_unknown")
        self.assertEqual(self.server.creates, 1)
        self.assertEqual(len(self.server.records), 1)
        self.assertIn("request", result["error"]["details"])

    def test_server_error_after_commit_does_not_retry_post(self):
        self.server.error_after_create = True
        code, result = self.cli(self.reserve_args)
        self.assertEqual(code, 5)
        self.assertEqual(self.server.creates, 1)
        self.assertEqual(len(self.server.records), 1)

    def test_post_create_verification_failure_preserves_id(self):
        self.server.fail_after_create = True
        code, result = self.cli(self.reserve_args)
        self.assertEqual(code, 5)
        self.assertEqual(result["error"]["code"], "verification_failed")
        self.assertEqual(result["error"]["details"]["reservationId"], self.server.records[0]["id"])

    def test_wrong_password_missing_credentials_and_usage_are_json(self):
        code, result = self.cli(["login"], GPUDECK_PASSWORD="wrong")
        self.assertEqual(code, 3)
        code, result = self.cli(["whoami"], GPUDECK_USERNAME="", GPUDECK_PASSWORD="")
        self.assertEqual(code, 3)
        code, result = self.cli(["reserve", "--unknown"])
        self.assertEqual(code, 2)
        self.assertEqual(result["error"]["code"], "usage_error")

    def test_availability_auto_selection_and_read_commands(self):
        for args in [["resources"], ["gpus", "--model", "H200", "--idle"], ["processes", "--mine"],
                     ["reservations", "--mine"], ["availability", "--hours", "2"]]:
            code, result = self.cli(args)
            self.assertEqual(code, 0)
        args = ["reserve", "--auto", "--count", "1", "--model", "H200", "--start", self.start,
                "--hours", "1", "--project", "auto", "--purpose", "debug", "--dry-run"]
        code, result = self.cli(args)
        self.assertEqual(code, 0)
        self.assertEqual(result["data"]["request"]["gpuIds"], [GPU_ID])
        self.assertEqual(self.server.creates, 0)

    def test_logout_revokes_session_and_removes_cookie(self):
        _, result = self.cli(["login"])
        cookie_path = Path(result["data"]["sessionFile"])
        code, result = self.cli(["logout"])
        self.assertEqual(code, 0)
        self.assertFalse(cookie_path.exists())
        code, result = self.cli(["whoami"], GPUDECK_USERNAME="", GPUDECK_PASSWORD="")
        self.assertEqual(code, 3)


class TransportTests(unittest.TestCase):
    def test_url_and_origin_isolation(self):
        for url in ["http://example.com", "https://user:pass@example.com", "https://example.com/api/v1",
                    "https://example.com?token=x", "https://example.com:bad"]:
            with self.assertRaises(DeckError):
                normalize_url(url)
        self.assertEqual(normalize_url("https://GPUDECK.cslee.cc:443/"), "https://gpudeck.cslee.cc")
        with tempfile.TemporaryDirectory() as state:
            self.assertNotEqual(Client("https://a.example", state).cookie_file,
                                Client("https://b.example", state).cookie_file)

    def test_global_flags_work_before_and_after_subcommand(self):
        for args in [["--pretty", "whoami"], ["whoami", "--pretty"],
                     ["--username", "test", "whoami"], ["whoami", "--username", "test"]]:
            parsed = parser().parse_args(args)
            self.assertEqual(parsed.command, "whoami")
            self.assertTrue(getattr(parsed, "pretty", False) or getattr(parsed, "username", None) == "test")

    def test_redirect_does_not_forward_credentials(self):
        posts = []
        class Redirect(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                posts.append(self.path)
                self.send_response(307)
                self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/stolen")
                self.end_headers()
        server = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as state:
                client = Client(f"http://127.0.0.1:{server.server_port}", state)
                with self.assertRaises(DeckError) as result:
                    client.login("test", "test-pass")
                self.assertEqual(result.exception.code, "redirect_refused")
                self.assertEqual(posts, ["/api/v1/auth/login"])
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
