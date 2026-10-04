"""AI-facing JSON command line. All reservation writes are issued once."""

import argparse
from datetime import datetime
import getpass
import json
import os
import sys
import uuid

from . import __version__
from .client import Client, DeckError, DEFAULT_URL, public_data
from .reservations import (
    UTC, TERMINAL, availability, checks, duplicate, flatten, interval,
    overlaps, parse_time, select, snapshot, wire_time, zone,
)


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise DeckError("usage_error", message, 2)


def parser():
    common = Parser(add_help=False)
    for flags, settings in [
        (["--url"], {"help": "平台根地址；默认 https://gpudeck.cslee.cc"}),
        (["--state-dir"], {"help": "Cookie 状态目录；可用 GPUDECK_STATE_DIR"}),
        (["--username"], {"help": "登录用户名；可用 GPUDECK_USERNAME"}),
        (["--password-stdin"], {"action": "store_true", "help": "从标准输入读取一行密码"}),
        (["--timezone"], {"help": "无偏移时间的时区，默认 Asia/Shanghai"}),
        (["--timeout"], {"type": float, "help": "每次 HTTP 请求超时秒数，默认 20"}),
        (["--pretty"], {"action": "store_true", "help": "缩进 JSON 输出"}),
        (["--json"], {"action": "store_true", "help": "使用 JSON 输出（默认行为）"}),
    ]:
        common.add_argument(*flags, default=argparse.SUPPRESS, **settings)
    root = Parser(description="GPUDeck CLI：登录、资源查询、预约与取消；默认输出 JSON。", parents=[common])
    root.add_argument("--version", action="version", version=f"gpudeck {__version__}")
    sub = root.add_subparsers(dest="command", required=True, parser_class=Parser)
    commands = {}
    for name, help_text in [
        ("login", "登录并保存 Cookie 会话，不保存密码"),
        ("logout", "注销当前 CLI 会话"),
        ("whoami", "查询登录身份"),
        ("resources", "查询平台原始资源信息"),
        ("gpus", "查询 GPU 和实际占用"),
        ("processes", "查询 GPU 进程"),
        ("reservations", "查询预约记录"),
        ("availability", "检查指定时段的可预约 GPU"),
        ("reserve", "创建预约；支持 --dry-run"),
        ("cancel", "取消指定预约"),
        ("end", "提前结束指定预约"),
        ("schema", "输出 AI 可读取的命令说明"),
    ]:
        commands[name] = sub.add_parser(name, help=help_text, description=help_text, parents=[common])
    for name in ["gpus", "processes", "availability", "reserve"]:
        commands[name].add_argument("--server", help="服务器完整名、唯一片段或节点 ID")
    for name in ["gpus", "availability", "reserve"]:
        commands[name].add_argument("--model", help="GPU 型号子串，例如 H200")
    commands["gpus"].add_argument("--idle", action="store_true", help="仅保留当前采样实际空闲的 GPU")
    for name in ["processes", "reservations"]:
        commands[name].add_argument("--mine", action="store_true", help="仅查询当前用户")
    commands["reservations"].add_argument("--status", action="append", help="状态筛选，可重复")
    commands["reservations"].add_argument("--from", dest="from_time", help="仅保留与区间重叠的记录")
    commands["reservations"].add_argument("--to", dest="to_time", help="仅保留与区间重叠的记录")
    for name in ["reserve", "availability"]:
        p = commands[name]
        p.add_argument("--start", default="now", help="ISO 时间或 now；默认 now")
        duration = p.add_mutually_exclusive_group(required=True)
        duration.add_argument("--end", help="结束时间")
        duration.add_argument("--hours", type=float, help="预约小时数，0 < 时长 <= 48")
    p = commands["reserve"]
    selection = p.add_mutually_exclusive_group(required=True)
    selection.add_argument("--gpus", help="指定服务器上的 GPU 编号，例如 0,1")
    selection.add_argument("--gpu-id", action="append", help="平台 GPU ID，可重复以跨服务器选择")
    selection.add_argument("--auto", action="store_true", help="自动选择可预约 GPU，优先当前实际空闲的卡")
    p.add_argument("--count", type=int, default=1, help="--auto 的 GPU 数量，默认 1")
    p.add_argument("--project", required=True, help="项目名")
    p.add_argument("--purpose", required=True, help="用途")
    p.add_argument("--dry-run", action="store_true", help="只检查并输出预约请求，不提交")
    for name in ["cancel", "end"]:
        commands[name].add_argument("reservation_id", help="预约 ID")
        commands[name].add_argument("--dry-run", action="store_true", help="预览操作，不提交")
    return root


def schema():
    return {
        "version": __version__, "defaultUrl": DEFAULT_URL, "defaultTimezone": "Asia/Shanghai",
        "authentication": {
            "login": "gpudeck login --username USER（终端隐藏输入密码）",
            "noninteractive": ["GPUDECK_USERNAME", "GPUDECK_PASSWORD"],
            "passwordStdin": "--password-stdin", "cachedCookie": True, "storesPassword": False,
        },
        "commands": {
            "whoami": "当前身份；不输出 CSRF Token",
            "resources": "两层结构 nodes -> gpus；包含 serverTime",
            "gpus": "[--server NAME] [--model H200] [--idle]",
            "processes": "[--server NAME] [--mine]",
            "reservations": "[--mine] [--status scheduled] [--from TIME] [--to TIME]",
            "availability": "--start TIME (--end TIME | --hours N) [--server NAME] [--model H200]",
            "reserve": "(--server NAME --gpus 0,1 | --gpu-id ID ... | --auto [--count N]) --start TIME (--end TIME | --hours N) --project NAME --purpose TEXT [--dry-run]",
            "cancel": "RESERVATION_ID [--dry-run]", "end": "RESERVATION_ID [--dry-run]",
            "login": "[--username USER] [--password-stdin]", "logout": "注销 CLI Cookie 会话",
        },
        "output": {"success": {"ok": True, "data": {}},
                   "failure": {"ok": False, "error": {"code": "...", "message": "..."}}},
        "exitCodes": {"0": "成功", "1": "网络或平台错误", "2": "参数错误",
                      "3": "登录或权限错误", "4": "预约冲突或校验失败",
                      "5": "提交结果不确定或提交后无法验证"},
        "policy": {"maxHours": 48, "maxAdvanceDays": 14,
                   "concurrentGpuLimit": "查询 whoami 获取；最终以平台校验为准",
                   "reservationDoesNotStopProcesses": True,
                   "postRequestsRetried": False,
                   "identicalExplicitReservationReused": True},
    }


def credentials(args, interactive=False):
    username = getattr(args, "username", None) or os.environ.get("GPUDECK_USERNAME")
    password = os.environ.get("GPUDECK_PASSWORD")
    if getattr(args, "password_stdin", False):
        line = sys.stdin.readline()
        if not line:
            raise DeckError("credentials_missing", "标准输入未提供密码", 3)
        password = line.rstrip("\r\n")
    if interactive and sys.stdin.isatty():
        if not username:
            username = input("GPUDeck 用户名: ").strip()
        if password is None:
            password = getpass.getpass("GPUDeck 密码: ")
    if interactive and (not username or password is None):
        raise DeckError("credentials_missing", "非交互登录需要用户名及 GPUDECK_PASSWORD 或 --password-stdin", 3)
    return username, password


def action(client, args, user):
    try:
        reservation_id = str(uuid.UUID(args.reservation_id))
    except ValueError as exc:
        raise DeckError("invalid_id", "预约 ID 必须是 UUID", 2) from exc
    records = client.request("/reservations")
    record = next((r for r in records if r["id"] == reservation_id), None)
    if not record:
        raise DeckError("reservation_not_found", "预约不存在或已超出平台历史查询范围", 4)
    if record["ownerId"] != user["id"] and not (args.command == "cancel" and user["role"] == "admin"):
        raise DeckError("forbidden", "只能操作自己的预约；管理员可取消他人预约", 3)
    target = "cancelled" if args.command == "cancel" else "completed"
    if record["status"] == target:
        return {"reservation": record, "changed": False, "verified": True}
    if record["status"] not in {"scheduled", "active"}:
        raise DeckError("invalid_status", f"不能对 {record['status']} 状态的预约执行 {args.command}", 4)
    if args.dry_run:
        return {"dryRun": True, "action": args.command, "reservation": record}
    client.request(f"/reservations/{reservation_id}/{args.command}", "POST")
    try:
        after = next((r for r in client.request("/reservations") if r["id"] == reservation_id), None)
    except DeckError as exc:
        raise DeckError("verification_failed", "操作已提交，但无法查询最终状态", 5,
                        details={"reservationId": reservation_id, "action": args.command,
                                 "cause": exc.as_dict()}) from exc
    if not after or after["status"] != target:
        raise DeckError("verification_failed", "平台未显示预期的预约状态，请查询记录确认", 5,
                        details={"reservationId": reservation_id, "expectedStatus": target,
                                 "actual": after})
    return {"reservation": after, "changed": True, "verified": True}


def reserve(client, args, resources, records, user, tz):
    now = parse_time(resources["serverTime"], UTC)
    start, end = interval(args, tz, now)
    gpus = flatten(resources["nodes"], args.server, args.model)
    chosen = select(args, gpus, start, end, records, user, now)
    payload = {"gpuIds": [g["id"] for g in chosen], "startsAt": wire_time(start),
               "endsAt": wire_time(end), "projectName": args.project.strip(), "purpose": args.purpose.strip()}
    existing = duplicate(records, user, payload)
    if existing:
        return {"dryRun": args.dry_run, "created": False, "reused": True,
                "verified": True, "reservation": existing, "request": payload}
    plan = checks(chosen, start, end, records, user, now, project=args.project, purpose=args.purpose)
    preview = {"request": payload, "check": plan,
               "selectedGpus": [{k: g[k] for k in ("id", "nodeName", "index", "name")} for g in chosen],
               "localTime": {"timezone": str(tz), "start": start.astimezone(tz).isoformat(),
                             "end": end.astimezone(tz).isoformat()}}
    if not plan["valid"]:
        raise DeckError("validation_failed", "预约预检查未通过", 4, details=preview)
    if args.dry_run:
        return {"dryRun": True, **preview}
    try:
        created = client.request("/reservations", "POST", payload)
    except DeckError as exc:
        if exc.code == "outcome_unknown":
            exc.details = {"request": payload, "nextStep": "gpudeck reservations --mine"}
        raise
    if not isinstance(created, dict) or not created.get("id"):
        raise DeckError("outcome_unknown", "平台接受请求但未返回预约 ID；请先查询记录", 5, details=preview)
    reservation_id = created["id"]
    try:
        record = next((r for r in client.request("/reservations") if r["id"] == reservation_id), None)
    except DeckError as exc:
        raise DeckError("verification_failed", "预约已创建，但未能读取结果；请先查询记录", 5,
                        details={"reservationId": reservation_id, "request": payload,
                                 "cause": exc.as_dict()}) from exc
    if not record:
        raise DeckError("verification_failed", "已收到预约 ID，但预约列表中未找到记录", 5,
                        details={"reservationId": reservation_id, "request": payload})
    return {"created": True, "reused": False, "verified": True, "reservation": record, **preview}


def run(args):
    if args.command == "schema":
        return schema()
    client = Client(getattr(args, "url", None) or os.environ.get("GPUDECK_URL", DEFAULT_URL),
                    getattr(args, "state_dir", None), getattr(args, "timeout", 20))
    if args.command == "login":
        username, password = credentials(args, interactive=True)
        return {"user": client.login(username, password), "sessionFile": str(client.cookie_file)}
    if args.command == "logout":
        if any(client.jar):
            try:
                client.me()
                client.request("/auth/logout", "POST")
            except DeckError as exc:
                if exc.status != 401:
                    raise
        client.clear()
        return {"loggedOut": True}
    username, password = credentials(args)
    user = client.authenticate(username, password)
    if args.command == "whoami":
        return user
    tz = zone(getattr(args, "timezone", None) or os.environ.get("GPUDECK_TIMEZONE", "Asia/Shanghai"))
    if args.command in {"cancel", "end"}:
        return action(client, args, user)
    if args.command == "reservations":
        records = client.request("/reservations")
        start = parse_time(args.from_time, tz) if args.from_time else None
        end = parse_time(args.to_time, tz) if args.to_time else None
        if start and end and end <= start:
            raise DeckError("invalid_time", "--to 必须晚于 --from", 2)
        return [r for r in records if (not args.mine or r["ownerId"] == user["id"])
                and (not args.status or r["status"] in args.status)
                and (not start or parse_time(r["endsAt"], UTC) > start)
                and (not end or parse_time(r["startsAt"], UTC) < end)]
    resources = client.request("/resources")
    if args.command == "resources":
        return resources
    now = parse_time(resources["serverTime"], UTC)
    gpus = flatten(resources["nodes"], args.server, getattr(args, "model", None))
    if args.command == "gpus":
        return [{**g, **snapshot(g, now)} for g in gpus if not args.idle or snapshot(g, now)["physicallyIdle"]]
    if args.command == "processes":
        return [{"gpuId": g["id"], "nodeName": g["nodeName"], "gpuIndex": g["index"], **p}
                for g in gpus for p in g.get("processes", [])
                if not args.mine or p["username"] == user.get("linuxUsername", user["username"])]
    records = client.request("/reservations")
    if args.command == "availability":
        start, end = interval(args, tz, now)
        return {"startsAt": wire_time(start), "endsAt": wire_time(end),
                "gpus": availability(gpus, start, end, records, user, now)}
    return reserve(client, args, resources, records, user, tz)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    pretty = "--pretty" in argv
    try:
        args = parser().parse_args(argv)
        result = {"ok": True, "data": public_data(run(args))}
        exit_code = 0
    except DeckError as exc:
        result = {"ok": False, "error": public_data(exc.as_dict())}
        exit_code = exc.exit_code
    except KeyboardInterrupt:
        result = {"ok": False, "error": {"code": "interrupted", "message": "操作中断；若中断发生在提交期间，请先查询预约记录"}}
        exit_code = 130
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        # Do not print tracebacks or response bodies which may contain credentials.
        result = {"ok": False, "error": {"code": "client_error", "message": f"客户端无法处理数据（{type(exc).__name__}）；检查平台响应或本地配置"}}
        exit_code = 1
    print(json.dumps(result, ensure_ascii=False, indent=2 if pretty else None))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
