"""Time parsing, selection and preflight checks matching the Hub policy."""

from datetime import datetime, timedelta, timezone
import math
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .client import DeckError

UTC = timezone.utc
TERMINAL = {"cancelled", "completed"}


def zone(name):
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise DeckError("invalid_timezone", f"未知时区：{name}", 2) from exc


def parse_time(value, tz, now=None):
    if value.lower() == "now":
        return now or datetime.now(UTC)
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DeckError("invalid_time", f"时间格式无效：{value}；请使用 ISO 8601 或 YYYY-MM-DD HH:MM", 2) from exc
    if result.tzinfo is None:
        result = result.replace(tzinfo=tz)
        # Do not silently reinterpret nonexistent or ambiguous DST wall times.
        if (result.astimezone(UTC).astimezone(tz).replace(tzinfo=None)
                != result.replace(tzinfo=None)):
            raise DeckError("invalid_time", "此本地时间因夏令时切换不存在；请提供带 UTC 偏移的时间", 2)
        if result.utcoffset() != result.replace(fold=1).utcoffset():
            raise DeckError("invalid_time", "此本地时间有夏令时歧义；请提供带 UTC 偏移的时间", 2)
    return result.astimezone(UTC)


def wire_time(value):
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def interval(args, tz, now):
    start = parse_time(args.start, tz, now)
    if args.end:
        end = parse_time(args.end, tz, now)
    else:
        if not math.isfinite(args.hours) or not 0 < args.hours <= 48:
            raise DeckError("invalid_duration", "--hours 必须大于 0 且不超过 48", 2)
        end = start + timedelta(hours=args.hours)
    # The exact millisecond values checked here are also sent to the server.
    start = parse_time(wire_time(start), tz)
    end = parse_time(wire_time(end), tz)
    return start, end


def overlaps(reservation, start, end):
    return (reservation.get("status") not in TERMINAL
            and parse_time(reservation["startsAt"], UTC) < end
            and parse_time(reservation["endsAt"], UTC) > start)


def find_nodes(nodes, server):
    if not server:
        return nodes
    def matches(node):
        return [str(node.get(k, "")).casefold() for k in ("id", "name", "hostname")]
    exact = [n for n in nodes if server.casefold() in matches(n)]
    candidates = exact or [n for n in nodes if any(server.casefold() in s for s in matches(n))]
    if not candidates:
        raise DeckError("server_not_found", f"未找到服务器：{server}", 2)
    if len(candidates) > 1:
        raise DeckError("ambiguous_server", "服务器名称匹配多台节点；请使用完整名称或 ID", 2,
                        details={"candidates": [n["name"] for n in candidates]})
    return candidates


def flatten(nodes, server=None, model=None):
    return [dict(g, nodeId=n["id"], nodeName=n["name"], lastSeenAt=n.get("lastSeenAt"))
            for n in find_nodes(nodes, server) for g in n["gpus"]
            if not model or model.casefold() in g["name"].casefold()]


def snapshot(gpu, now):
    last = gpu.get("lastSeenAt")
    fresh = bool(last and now - parse_time(last, UTC) < timedelta(seconds=30))
    memory = gpu.get("memoryUsedMb")
    busy = bool(gpu.get("processes")) or (memory is not None and memory > 256)
    known = fresh and memory is not None
    return {"online": fresh, "physicallyIdle": known and not busy,
            "occupancyKnown": known, "currentlyBusy": busy if known else None}


def peak_gpu_count(reservations, owner_id, start, end, new_count):
    events = []
    for reservation in reservations:
        if reservation["ownerId"] != owner_id or not overlaps(reservation, start, end):
            continue
        left = max(start, parse_time(reservation["startsAt"], UTC))
        right = min(end, parse_time(reservation["endsAt"], UTC))
        count = len(reservation["gpuIds"])
        events.extend([(left, count), (right, -count)])
    current = peak = 0
    # An ending reservation frees its allocations before an adjacent one starts.
    for _, delta in sorted(events):
        current += delta
        peak = max(peak, current)
    return peak + new_count


def checks(selected, start, end, reservations, user, now, *, project=None, purpose=None):
    errors, warnings, conflicts = [], [], []
    window = datetime.fromtimestamp(math.floor(now.timestamp() / 1800) * 1800, UTC)
    if start < window or start > now + timedelta(days=14):
        errors.append("开始时间必须在当前半小时窗口或未来 14 天内")
    if end <= now:
        errors.append("结束时间必须晚于当前时间")
    if end <= start or end - start > timedelta(hours=48):
        errors.append("预约时长必须大于 0 且不超过 48 小时")
    if not selected:
        errors.append("请至少选择一张 GPU")
    if len({g["id"] for g in selected}) != len(selected):
        errors.append("GPU 不可重复")
    for text, limit, label in [(project, 120, "项目名"), (purpose, 500, "用途")]:
        if text is not None and (not text.strip() or len(text.encode("utf-8")) > limit):
            errors.append(f"{label}不能为空，且 UTF-8 长度不能超过 {limit} 字节")
    for gpu in selected:
        label = f"{gpu['nodeName']} GPU {gpu['index']}"
        if gpu.get("maintenance") or gpu.get("missing"):
            errors.append(f"{label} 不可预约（维护或不可用）")
        for r in reservations:
            if gpu["id"] in r["gpuIds"] and overlaps(r, start, end):
                errors.append(f"{label} 与 {r['ownerName']} 的“{r['projectName']}”冲突")
                if r["id"] not in conflicts:
                    conflicts.append(r["id"])
        state = snapshot(gpu, now)
        if not state["online"]:
            warnings.append(f"{label} 节点离线或采样过期，需确认实际占用")
        elif not state["occupancyKnown"]:
            warnings.append(f"{label} 缺少显存采样，无法确认实际空闲")
        elif state["currentlyBusy"]:
            warnings.append(f"{label} 当前有进程或显存占用；预约不会停止现有任务")
    peak = peak_gpu_count(reservations, user["id"], start, end, len(selected))
    if peak > user["concurrentGpuLimit"]:
        errors.append(f"并发预约将达到 {peak} 张 GPU，超过 {user['concurrentGpuLimit']} 张上限")
    return {"valid": not errors, "errors": list(dict.fromkeys(errors)),
            "warnings": warnings, "conflictIds": conflicts, "peakGpuCount": peak,
            "concurrentGpuLimit": user["concurrentGpuLimit"]}


def availability(gpus, start, end, reservations, user, now):
    result = []
    for gpu in gpus:
        check = checks([gpu], start, end, reservations, user, now)
        result.append({"id": gpu["id"], "nodeName": gpu["nodeName"], "index": gpu["index"],
                       "name": gpu["name"], **snapshot(gpu, now),
                       "reservable": check["valid"], "errors": check["errors"],
                       "conflictIds": check["conflictIds"], "warnings": check["warnings"]})
    return result


def select(args, gpus, start, end, reservations, user, now):
    if args.auto:
        if args.count < 1:
            raise DeckError("invalid_count", "--count 必须大于 0", 2)
        candidates = [g for g in gpus if checks([g], start, end, reservations, user, now)["valid"]]
        # Prefer fresh, physically idle devices; booking availability is separate.
        candidates.sort(key=lambda g: (not snapshot(g, now)["physicallyIdle"],
                                       not snapshot(g, now)["online"], g["nodeName"], g["index"]))
        if len(candidates) < args.count:
            raise DeckError("no_available_gpus", "此时段没有足够的可预约 GPU", 4,
                            details={"requested": args.count, "available": len(candidates)})
        return candidates[:args.count]
    if args.count != 1:
        raise DeckError("invalid_selection", "--count 仅用于 --auto", 2)
    if args.gpus:
        if not args.server:
            raise DeckError("invalid_selection", "使用 --gpus 时必须指定 --server", 2)
        try:
            indices = [int(v.strip()) for v in args.gpus.split(",")]
        except ValueError as exc:
            raise DeckError("invalid_selection", "--gpus 应为 GPU 编号，例如 0,1", 2) from exc
        lookup = {g["index"]: g for g in gpus}
        missing = [i for i in indices if i not in lookup]
        if missing:
            raise DeckError("gpu_not_found", f"未找到 GPU 编号：{missing}", 2)
        return [lookup[i] for i in indices]
    lookup = {g["id"]: g for g in gpus}
    missing = [g for g in args.gpu_id if g not in lookup]
    if missing:
        raise DeckError("gpu_not_found", "未找到指定 GPU ID", 2, details={"ids": missing})
    return [lookup[g] for g in args.gpu_id]


def duplicate(reservations, user, payload):
    for r in reservations:
        if (r["ownerId"] == user["id"] and r["status"] not in TERMINAL
                and sorted(r["gpuIds"]) == sorted(payload["gpuIds"])
                and parse_time(r["startsAt"], UTC) == parse_time(payload["startsAt"], UTC)
                and parse_time(r["endsAt"], UTC) == parse_time(payload["endsAt"], UTC)
                and r["projectName"] == payload["projectName"] and r["purpose"] == payload["purpose"]):
            return r
    return None
