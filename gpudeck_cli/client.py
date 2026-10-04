"""Cookie authentication and the actual GPUDeck /api/v1 protocol."""

import hashlib
import http.client
import http.cookiejar
import json
import math
import os
from pathlib import Path
import socket
import tempfile
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_URL = "https://gpudeck.cslee.cc"


class DeckError(Exception):
    def __init__(self, code, message, exit_code=1, *, status=None, details=None):
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code
        self.status = status
        self.details = details

    def as_dict(self):
        result = {"code": self.code, "message": str(self)}
        if self.status is not None:
            result["status"] = self.status
        if self.details is not None:
            result["details"] = self.details
        return result


def public_data(value):
    """Never include session tokens in the machine-readable output."""
    if isinstance(value, dict):
        return {
            k: public_data(v) for k, v in value.items()
            if k.lower() not in {"csrftoken", "password", "token", "cookie", "set-cookie"}
        }
    if isinstance(value, list):
        return [public_data(v) for v in value]
    return value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    # Do not forward a credential body, cookie or CSRF header to another origin.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def normalize_url(url):
    try:
        parsed = urllib.parse.urlsplit(url.strip())
        port = parsed.port
    except ValueError as exc:
        raise DeckError("invalid_url", "平台 URL 无效", 2) from exc
    if (not parsed.hostname or parsed.username or parsed.password or parsed.query
            or parsed.fragment or parsed.path not in {"", "/"}):
        raise DeckError("invalid_url", "--url 必须是平台的根地址，不能含账号、路径或查询参数", 2)
    host = parsed.hostname.lower()
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and host in {"localhost", "127.0.0.1", "::1"}
    ):
        raise DeckError("invalid_url", "远程平台必须使用 HTTPS；HTTP 仅用于本机测试", 2)
    authority = f"[{host}]" if ":" in host else host
    if port and not (parsed.scheme == "https" and port == 443):
        authority += f":{port}"
    return f"{parsed.scheme}://{authority}"


class Client:
    def __init__(self, url=DEFAULT_URL, state_dir=None, timeout=20):
        self.url = normalize_url(url)
        if not math.isfinite(timeout) or timeout <= 0:
            raise DeckError("invalid_timeout", "超时必须是正的有限数值", 2)
        self.timeout = timeout
        root = Path(state_dir or os.environ.get("GPUDECK_STATE_DIR") or (
            Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "gpudeck"
        )).expanduser()
        self.state_dir = root / hashlib.sha256(self.url.encode()).hexdigest()[:16]
        self.cookie_file = self.state_dir / "cookies.txt"
        self.jar = http.cookiejar.MozillaCookieJar(str(self.cookie_file))
        if self.cookie_file.exists():
            try:
                self.jar.load(ignore_discard=True)
            except (OSError, http.cookiejar.LoadError) as exc:
                raise DeckError("session_file_error", "会话文件无法读取；移除后重新登录", 3) from exc
        self.opener = urllib.request.build_opener(
            NoRedirect(), urllib.request.HTTPCookieProcessor(self.jar)
        )
        self.csrf = ""
        self.user = None

    def save(self):
        try:
            self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.state_dir.chmod(0o700)
            fd, name = tempfile.mkstemp(prefix=".cookies-", dir=self.state_dir)
            os.close(fd)
            try:
                self.jar.save(name, ignore_discard=True)
                os.chmod(name, 0o600)
                os.replace(name, self.cookie_file)
            finally:
                Path(name).unlink(missing_ok=True)
        except OSError as exc:
            raise DeckError("session_file_error", "无法保存登录会话；检查状态目录权限", 3) from exc

    def clear(self):
        self.jar.clear()
        self.csrf = ""
        self.user = None
        self.cookie_file.unlink(missing_ok=True)

    def request(self, path, method="GET", body=None):
        headers = {"Accept": "application/json", "User-Agent": "gpudeck-cli/0.1.0"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if self.csrf:
            headers["X-CSRF-Token"] = self.csrf
        request = urllib.request.Request(
            self.url + "/api/v1" + path,
            data=json.dumps(body, ensure_ascii=False).encode() if body is not None else None,
            headers=headers, method=method,
        )
        mutation = method != "GET" and path not in {"/auth/login", "/auth/logout"}
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read()
                if response.status == 204:
                    return None
                try:
                    return json.loads(raw)
                except (ValueError, UnicodeDecodeError) as exc:
                    code = "outcome_unknown" if mutation else "invalid_response"
                    raise DeckError(code, "平台未返回有效 JSON；请查询预约记录确认结果" if mutation
                                    else "平台未返回有效 JSON", 5 if mutation else 1) from exc
        except urllib.error.HTTPError as exc:
            try:
                message = json.loads(exc.read()).get("error", f"HTTP {exc.code}")
            except (ValueError, AttributeError, UnicodeDecodeError):
                message = f"平台返回 HTTP {exc.code}"
            finally:
                exc.close()
            status = exc.code
            code, exit_code = "api_error", 1
            if status == 401:
                code, exit_code = "authentication_required", 3
                self.clear()
            elif status == 409:
                code, exit_code = "conflict", 4
            elif status in {400, 422}:
                code, exit_code = "validation_failed", 4
            elif status == 403:
                code, exit_code = "forbidden", 3
            elif 300 <= status < 400:
                code, exit_code = "redirect_refused", 1
                message = "平台返回重定向；请核对 --url，客户端不会转发登录凭据"
            elif status >= 500 and mutation:
                code, exit_code = "outcome_unknown", 5
                message = "平台内部错误，预约操作结果不确定；先查询记录，不要直接重试"
            raise DeckError(code, str(message), exit_code, status=status) from None
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError, http.client.HTTPException) as exc:
            if mutation:
                raise DeckError("outcome_unknown", "网络中断或超时，操作可能已提交；先查询预约记录，不要直接重试", 5) from exc
            raise DeckError("network_error", "无法连接 GPUDeck，或请求超时", 1) from exc

    def me(self):
        user = self.request("/auth/me")
        if not isinstance(user, dict) or not user.get("csrfToken") or not user.get("id"):
            raise DeckError("invalid_response", "登录状态响应缺少用户或 CSRF 信息", 3)
        self.csrf = user["csrfToken"]
        self.user = user
        return user

    def login(self, username, password):
        # An explicit login replaces the local jar; passwords are never saved.
        self.jar.clear()
        self.csrf = ""
        self.request("/auth/login", "POST", {"username": username, "password": password})
        user = self.me()
        self.save()
        return user

    def authenticate(self, username=None, password=None):
        if any(self.jar):
            try:
                user = self.me()
                if not username or user.get("username") == username:
                    return user
                if password is None:
                    raise DeckError("account_mismatch", "缓存会话属于其他账号；请为指定用户名提供密码重新登录", 3)
            except DeckError as exc:
                if exc.status != 401:
                    raise
        if not username or password is None:
            raise DeckError("authentication_required", "请先运行 gpudeck login，或设置 GPUDECK_USERNAME 和 GPUDECK_PASSWORD", 3)
        return self.login(username, password)
