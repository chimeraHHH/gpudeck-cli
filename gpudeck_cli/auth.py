"""Remember passwords in macOS Keychain, never in CLI files or process arguments."""

import ctypes
import json
import os
from pathlib import Path
import sys
import tempfile

from .errors import DeckError

NOT_FOUND = -25300
DUPLICATE = -25299


class Keychain:
    @property
    def supported(self):
        return sys.platform == "darwin"

    def _load(self):
        if not self.supported:
            raise DeckError("credential_store_unavailable", "记住密码目前支持 macOS 系统钥匙串；可使用 --no-remember 和环境变量", 3)
        if hasattr(self, "security"):
            return
        try:
            self.security = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
            self.core = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        except OSError as exc:
            raise DeckError("credential_store_unavailable", "无法访问 macOS 系统钥匙串；可使用 --no-remember", 3) from exc
        ref, uint, chars = ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p
        signatures = {
            "SecKeychainFindGenericPassword": [ref, uint, chars, uint, chars,
                                                ctypes.POINTER(uint), ctypes.POINTER(ref), ctypes.POINTER(ref)],
            "SecKeychainAddGenericPassword": [ref, uint, chars, uint, chars, uint, ref, ctypes.POINTER(ref)],
            "SecKeychainItemModifyContent": [ref, ref, uint, ref],
            "SecKeychainItemFreeContent": [ref, ref],
            "SecKeychainItemDelete": [ref],
        }
        for name, argtypes in signatures.items():
            fn = getattr(self.security, name)
            fn.argtypes, fn.restype = argtypes, ctypes.c_int32
        self.core.CFRelease.argtypes = [ref]
        self.core.CFRelease.restype = None

    @staticmethod
    def _check(status, operation):
        if status != 0:
            raise DeckError("credential_store_error", f"系统钥匙串{operation}失败（OSStatus {status}）；请解锁或允许访问钥匙串", 3)

    def _item(self, service, username):
        service, username = service.encode(), username.encode()
        item = ctypes.c_void_p()
        status = self.security.SecKeychainFindGenericPassword(
            None, len(service), service, len(username), username, None, None, ctypes.byref(item)
        )
        return status, item

    def get(self, service, username):
        self._load()
        service, username = service.encode(), username.encode()
        length, data = ctypes.c_uint32(), ctypes.c_void_p()
        status = self.security.SecKeychainFindGenericPassword(
            None, len(service), service, len(username), username,
            ctypes.byref(length), ctypes.byref(data), None,
        )
        if status == NOT_FOUND:
            return None
        self._check(status, "读取")
        try:
            return ctypes.string_at(data, length.value).decode("utf-8")
        finally:
            self.security.SecKeychainItemFreeContent(None, data)

    def set(self, service, username, password):
        self._load()
        # Passwords cross the native API in memory, never a subprocess argv.
        encoded = password.encode("utf-8")
        buffer = ctypes.create_string_buffer(encoded)
        status, item = self._item(service, username)
        if status == NOT_FOUND:
            encoded_service, encoded_user = service.encode(), username.encode()
            status = self.security.SecKeychainAddGenericPassword(
                None, len(encoded_service), encoded_service, len(encoded_user), encoded_user,
                len(encoded), ctypes.cast(buffer, ctypes.c_void_p), None,
            )
            if status != DUPLICATE:
                self._check(status, "保存")
                return
            status, item = self._item(service, username)
        self._check(status, "定位")
        try:
            self._check(self.security.SecKeychainItemModifyContent(
                item, None, len(encoded), ctypes.cast(buffer, ctypes.c_void_p)
            ), "更新")
        finally:
            self.core.CFRelease(item)

    def delete(self, service, username):
        self._load()
        status, item = self._item(service, username)
        if status == NOT_FOUND:
            return
        self._check(status, "定位")
        try:
            status = self.security.SecKeychainItemDelete(item)
            if status != NOT_FOUND:
                self._check(status, "删除")
        finally:
            self.core.CFRelease(item)


class CredentialStore:
    def __init__(self, url, state_dir):
        self.service = "gpudeck-cli:" + url
        self.profile_file = state_dir / "profile.json"
        self.keychain = Keychain()
        self.profile = {}
        if self.profile_file.exists():
            try:
                profile = json.loads(self.profile_file.read_text())
                if not isinstance(profile, dict) or not isinstance(profile.get("username"), str):
                    raise ValueError("invalid profile")
                self.profile = {"username": profile["username"], "autoLogin": profile.get("autoLogin") is True}
            except (OSError, ValueError) as exc:
                raise DeckError("credential_config_error", "自动登录配置无法读取；移除 profile.json 后重新登录", 3) from exc

    @property
    def username(self):
        return self.profile.get("username")

    @property
    def enabled(self):
        return self.profile.get("autoLogin", False)

    def _write(self, username, enabled):
        # This file contains only account selection and a boolean, no password.
        profile = {"username": username, "autoLogin": enabled}
        try:
            self.profile_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.profile_file.parent.chmod(0o700)
            fd, name = tempfile.mkstemp(prefix=".profile-", dir=self.profile_file.parent)
            try:
                with os.fdopen(fd, "w") as output:
                    json.dump(profile, output)
                os.replace(name, self.profile_file)
            finally:
                Path(name).unlink(missing_ok=True)
        except OSError as exc:
            raise DeckError("credential_config_error", "无法保存自动登录配置", 3) from exc
        self.profile = profile

    def get(self, username=None):
        if not self.enabled or (username and username != self.username):
            return None
        password = self.keychain.get(self.service, self.username)
        return (self.username, password) if password is not None else None

    def remember(self, username, password):
        self.keychain.set(self.service, username, password)
        self._write(username, True)

    def disable(self):
        if self.username:
            self._write(self.username, False)

    def forget(self):
        username = self.username
        self.profile_file.unlink(missing_ok=True)
        self.profile = {}
        if username:
            self.keychain.delete(self.service, username)
