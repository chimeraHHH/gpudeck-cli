from argparse import Namespace
from datetime import timedelta
import unittest

from gpudeck_cli.client import DeckError
from gpudeck_cli.reservations import (
    UTC, availability, checks, duplicate, find_nodes, flatten, parse_time,
    peak_gpu_count, select, snapshot, wire_time, zone,
)


NOW = parse_time("2026-10-04T10:15:00Z", UTC)
USER = {"id": "owner", "concurrentGpuLimit": 2}


def gpu(index=0, **values):
    return {"id": f"gpu-{index}", "nodeId": "node", "nodeName": "WHUServer-H200",
            "index": index, "name": "NVIDIA H200", "maintenance": False, "missing": False,
            "lastSeenAt": wire_time(NOW), "memoryUsedMb": 0, "processes": [], **values}


def record(start, end, ids=None, **values):
    return {"id": "r", "ownerId": "owner", "ownerName": "Someone",
            "projectName": "研究", "purpose": "训练", "gpuIds": ids or ["gpu-1"],
            "startsAt": wire_time(start), "endsAt": wire_time(end), "status": "scheduled", **values}


class TimeAndPolicyTests(unittest.TestCase):
    def test_beijing_time_is_not_host_timezone(self):
        self.assertEqual(parse_time("2026-10-04 18:15", zone("Asia/Shanghai")), NOW)
        self.assertEqual(parse_time("2026-10-04T18:15:00+08:00", UTC), NOW)

    def test_dst_ambiguous_and_nonexistent_times_are_rejected(self):
        for value in ["2026-11-01 01:30", "2026-03-08 02:30"]:
            with self.assertRaises(DeckError):
                parse_time(value, zone("America/New_York"))

    def test_current_half_hour_and_future_limits(self):
        for start, duration, valid in [
            (NOW.replace(minute=0), 1, True),
            (NOW.replace(minute=0) - timedelta(seconds=1), 1, False),
            (NOW + timedelta(days=14), 48, True),
            (NOW + timedelta(days=14, seconds=1), 1, False),
            (NOW, 48.001, False), (NOW, 0, False),
        ]:
            with self.subTest(start=start, duration=duration):
                result = checks([gpu()], start, start + timedelta(hours=duration), [], USER, NOW)
                self.assertEqual(result["valid"], valid)

    def test_adjacent_booking_does_not_conflict(self):
        records = [record(NOW - timedelta(hours=1), NOW, ["gpu-0"])]
        self.assertTrue(checks([gpu()], NOW, NOW + timedelta(hours=1), records, USER, NOW)["valid"])
        records[0]["endsAt"] = wire_time(NOW + timedelta(milliseconds=1))
        self.assertFalse(checks([gpu()], NOW, NOW + timedelta(hours=1), records, USER, NOW)["valid"])

    def test_completed_and_cancelled_do_not_conflict(self):
        for status in ["completed", "cancelled"]:
            records = [record(NOW, NOW + timedelta(hours=1), ["gpu-0"], status=status)]
            self.assertTrue(checks([gpu()], NOW, NOW + timedelta(hours=1), records, USER, NOW)["valid"])

    def test_peak_limit_counts_overlapping_allocations(self):
        records = [record(NOW, NOW + timedelta(hours=1), ["gpu-1", "gpu-2"]),
                   record(NOW + timedelta(hours=1), NOW + timedelta(hours=2), ["gpu-3"])]
        self.assertEqual(peak_gpu_count(records, "owner", NOW, NOW + timedelta(hours=2), 1), 3)
        result = checks([gpu()], NOW, NOW + timedelta(hours=2), records, USER, NOW)
        self.assertFalse(result["valid"])
        self.assertEqual(result["peakGpuCount"], 3)

    def test_offline_and_missing_sample_never_mean_idle(self):
        for device in [gpu(lastSeenAt=None), gpu(lastSeenAt=wire_time(NOW - timedelta(seconds=31))),
                       gpu(memoryUsedMb=None)]:
            self.assertFalse(snapshot(device, NOW)["physicallyIdle"])
            self.assertTrue(checks([device], NOW, NOW + timedelta(hours=1), [], USER, NOW)["warnings"])

    def test_busy_gpu_is_a_warning_and_not_booking_conflict(self):
        device = gpu(memoryUsedMb=60000, processes=[{"pid": 123}])
        result = availability([device], NOW, NOW + timedelta(hours=1), [], USER, NOW)[0]
        self.assertTrue(result["reservable"])
        self.assertFalse(result["physicallyIdle"])
        self.assertTrue(result["warnings"])

    def test_maintenance_duplicate_ids_and_utf8_limits(self):
        for devices, project, purpose in [([gpu(maintenance=True)], "ok", "ok"),
                                          ([gpu(), gpu()], "ok", "ok"),
                                          ([gpu()], "中" * 41, "ok"),
                                          ([gpu()], "ok", " ")]:
            self.assertFalse(checks(devices, NOW, NOW + timedelta(hours=1), [], USER, NOW,
                                    project=project, purpose=purpose)["valid"])

    def test_auto_selection_prefers_fresh_idle_and_skips_conflicts(self):
        devices = [gpu(0, memoryUsedMb=1000), gpu(1), gpu(2)]
        args = Namespace(auto=True, count=1)
        records = [record(NOW, NOW + timedelta(hours=1), ["gpu-1"])]
        selected = select(args, devices, NOW, NOW + timedelta(hours=1), records, USER, NOW)
        self.assertEqual(selected[0]["index"], 2)
        args.count = 3
        with self.assertRaises(DeckError):
            select(args, devices, NOW, NOW + timedelta(hours=1), records, USER, NOW)

    def test_server_ambiguity_and_gpu_numbers_require_server(self):
        nodes = [{"id": "one", "name": "WHUServer-H200", "gpus": [gpu()]},
                 {"id": "two", "name": "WHUServer-L40S", "gpus": [gpu(1)]}]
        self.assertEqual(len(flatten(nodes, "H200")), 1)
        with self.assertRaises(DeckError):
            find_nodes(nodes, "WHUServer")
        args = Namespace(auto=False, count=1, gpus="0", server=None)
        with self.assertRaises(DeckError):
            select(args, [gpu()], NOW, NOW + timedelta(hours=1), [], USER, NOW)

    def test_duplicate_matches_owner_time_and_payload(self):
        r = record(NOW, NOW + timedelta(hours=1), ["gpu-0"])
        payload = {k: r[k] for k in ["gpuIds", "startsAt", "endsAt", "projectName", "purpose"]}
        self.assertEqual(duplicate([r], USER, payload), r)
        payload["purpose"] = "other"
        self.assertIsNone(duplicate([r], USER, payload))


if __name__ == "__main__":
    unittest.main()
