from __future__ import annotations

import unittest

from fnos_media_import.classifiers.link_classifier import detect_link
from fnos_media_import.services.job_completion_info_service import (
    JobCompletionInfoService,
)
from tests._organizer_v1_fixtures import organizer_v1_plan


class JobApiV1ContractTests(unittest.TestCase):
    def test_job_view_uses_only_structured_source_and_frozen_destination(self) -> None:
        plan = organizer_v1_plan(41)
        service = JobCompletionInfoService(
            common_virtual_path=lambda paths: paths[0] if paths else "",
            path_health_checks=lambda **_kwargs: [],
            plan_loader=lambda _job: plan,
        )

        view = service.build(
            {
                "id": 41,
                "status": "waiting_transfer",
                "source_provider_kind": "quark",
                "source_form": "share",
                "destination_id": plan["destination"]["id"],
                "raw_data": {},
            }
        )

        self.assertEqual("quark", view["source_provider_kind"])
        self.assertEqual("share", view["source_form"])
        self.assertEqual(
            plan["source"]["search_source_id"],
            view["search_source_id"],
        )
        self.assertEqual(plan["transfer"]["strategy"], view["transfer_strategy"])
        self.assertEqual(plan["destination"]["id"], view["destination_id"])
        self.assertEqual(
            plan["destination"]["provider_kind"],
            view["destination_provider_kind"],
        )
        self.assertNotIn("route", view)
        self.assertNotIn("target_route", view)
        self.assertNotIn("source_type", view)

    def test_source_detection_has_no_execution_route_alias(self) -> None:
        detected = detect_link("https://pan.quark.cn/s/example").to_dict()

        self.assertEqual("quark", detected["provider_kind"])
        self.assertEqual("share", detected["source_form"])
        self.assertEqual("quark", detected["search_source_id"])
        self.assertNotIn("route", detected)
        self.assertNotIn("target_route", detected)
        self.assertNotIn("source_type", detected)


if __name__ == "__main__":
    unittest.main()
