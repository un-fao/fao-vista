from __future__ import annotations

import unittest
from unittest.mock import patch

from core.phase3_gee import ee_utils


class GcsProjectTests(unittest.TestCase):
    def test_export_listing_uses_the_configured_project(self) -> None:
        with patch.object(ee_utils.storage, "Client") as client:
            client.return_value.bucket.return_value.list_blobs.return_value = []
            ee_utils.submit_gcs_export(
                image=None,
                bucket="your-bucket",
                file_name_prefix="vista_products/dekadal/count/vista_count_01d1",
                crs="EPSG:4326",
                crs_transform=[1, 0, 0, 0, -1, 0],
                dry_run=True,
                project="your-gcp-project",
            )
        client.assert_called_once_with(project="your-gcp-project")


if __name__ == "__main__":
    unittest.main()
