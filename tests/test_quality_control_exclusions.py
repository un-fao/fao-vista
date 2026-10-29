from __future__ import annotations

import io
import unittest
from pathlib import Path
from unittest.mock import patch

from core.quality_control.exclusions import (
    affected_monthly_center_labels,
    exclusions_affecting_monthly_statistic,
    load_dekad_exclusions,
    normalise_dekad_label,
)


class QualityControlExclusionTests(unittest.TestCase):
    def test_repository_manifest_contains_rejected_202407d1(self) -> None:
        exclusions = load_dekad_exclusions()
        exclusion = exclusions["202407d1"]
        self.assertEqual(exclusion.status, "rejected")
        self.assertEqual(exclusion.provider_folder, "2024/07/06")

    def test_affected_monthly_centers_cross_month_boundary(self) -> None:
        self.assertEqual(
            affected_monthly_center_labels("202407d1"),
            ("202406d3", "202407d1", "202407d2"),
        )

    def test_exclusion_is_visible_to_each_affected_monthly_statistic(self) -> None:
        exclusions = load_dekad_exclusions()
        for center in ("06d3", "07d1", "07d2"):
            matched = exclusions_affecting_monthly_statistic(center, {2024}, exclusions)
            self.assertEqual(tuple(item.label for item in matched), ("202407d1",))
        self.assertEqual(
            exclusions_affecting_monthly_statistic("07d3", {2024}, exclusions),
            (),
        )

    def test_duplicate_labels_are_rejected(self) -> None:
        content = """
excluded_dekads:
  - label: 202407d1
    status: rejected
    reason: first
  - label: 202407d1
    status: rejected
    reason: duplicate
"""
        with patch.object(Path, "open", return_value=io.StringIO(content)):
            with self.assertRaisesRegex(ValueError, "Duplicate excluded dekad"):
                load_dekad_exclusions(Path("ignored.yaml"))

    def test_invalid_month_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "Invalid excluded dekad month"):
            normalise_dekad_label("202413d1")


if __name__ == "__main__":
    unittest.main()
