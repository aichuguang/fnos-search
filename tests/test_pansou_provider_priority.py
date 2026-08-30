from __future__ import annotations

import unittest

from fnos_media_import.search.pansou_provider import PanSouProvider


class _Client:
    base_url = "http://pansou.internal"
    cloud_types = ["quark", "mobile", "tianyi", "magnet"]
    result_provider_priority = ["mobile", "quark", "tianyi", "magnet"]
    async_poll_enabled = False
    result_type = "merge"
    source_scope = "all"

    def search(self, *_args, **_kwargs):
        return {
            "items": [
                {
                    "title": "移动资源",
                    "provider_kind": "mobile",
                    "source_form": "share",
                    "source_type": "cloud139",
                },
                {
                    "title": "夸克资源",
                    "provider_kind": "quark",
                    "source_form": "share",
                    "source_type": "quark",
                },
                {
                    "title": "磁链资源",
                    "provider_kind": "sixpan",
                    "source_form": "magnet",
                    "source_type": "magnet",
                },
                {
                    "title": "未知资源",
                    "provider_kind": "unknown",
                    "source_form": "share",
                    "source_type": "unknown",
                },
            ],
            "raw": {},
        }


class PanSouProviderPriorityTests(unittest.TestCase):
    def test_provider_marks_results_with_limited_priority_bonus(self) -> None:
        result = PanSouProvider(_Client()).search("测试")
        items = {item["title"]: item for item in result["items"]}

        self.assertEqual(items["移动资源"]["result_provider_priority_bonus"], 10)
        self.assertEqual(items["夸克资源"]["result_provider_priority_bonus"], 5)
        self.assertEqual(items["磁链资源"]["result_provider_priority_bonus"], 0)
        self.assertEqual(items["未知资源"]["result_provider_priority"], 999)
        self.assertEqual(items["未知资源"]["result_provider_priority_bonus"], 0)

    def test_provider_description_exposes_current_result_priority(self) -> None:
        description = PanSouProvider(_Client()).describe()

        self.assertEqual(
            description["capabilities"]["result_provider_priority"],
            ["mobile", "quark", "tianyi", "magnet"],
        )


if __name__ == "__main__":
    unittest.main()
