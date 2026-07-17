"""Tests for terravault.process_api."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from terravault.auth import SentinelHubAuthConfig
from terravault.process_api import (
    ProcessPatchConfig,
    fetch_sentinel2_patch,
    output_size_for_bbox,
)


class TestProcessAPI(unittest.TestCase):
    def test_fetch_patch_writes_response_bytes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            session = MagicMock()
            token_response = MagicMock()
            token_response.json.return_value = {"access_token": "token-123", "expires_in": 3600}
            token_response.raise_for_status.return_value = None
            process_response = MagicMock()
            process_response.content = b"geotiff-bytes"
            process_response.raise_for_status.return_value = None
            session.post.side_effect = [token_response, process_response]

            out = Path(tmpdir) / "patch.tif"
            path = fetch_sentinel2_patch(
                ProcessPatchConfig(
                    bbox=[8.47, 47.33, 8.62, 47.44],
                    time_from="2024-06-01T00:00:00Z",
                    time_to="2024-06-30T23:59:59Z",
                    bands=["B04", "B08"],
                ),
                output_path=out,
                auth=SentinelHubAuthConfig(client_id="cid", client_secret="secret"),
                session_factory=lambda: session,
            )

            self.assertEqual(path, out)
            self.assertEqual(out.read_bytes(), b"geotiff-bytes")
            process_call = session.post.call_args_list[1]
            payload = process_call.kwargs["json"]
            self.assertEqual(payload["input"]["data"][0]["type"], "sentinel-2-l2a")
            self.assertIn('"B04"', payload["evalscript"])
            self.assertIs(payload["input"]["data"][0]["processing"]["harmonizeValues"], False)
            self.assertEqual(
                process_call.kwargs["headers"]["Authorization"],
                "Bearer token-123",
            )

    def test_gain_is_applied_to_evalscript(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            session = MagicMock()
            token_response = MagicMock()
            token_response.json.return_value = {"access_token": "token-123", "expires_in": 3600}
            token_response.raise_for_status.return_value = None
            process_response = MagicMock()
            process_response.content = b"png-bytes"
            process_response.raise_for_status.return_value = None
            session.post.side_effect = [token_response, process_response]

            out = Path(tmpdir) / "patch.png"
            fetch_sentinel2_patch(
                ProcessPatchConfig(
                    bbox=[8.47, 47.33, 8.62, 47.44],
                    time_from="2024-06-01T00:00:00Z",
                    time_to="2024-06-30T23:59:59Z",
                    bands=["B04", "B03", "B02"],
                    output_format="image/png",
                    units="REFLECTANCE",
                    sample_type="AUTO",
                    gain=2.5,
                ),
                output_path=out,
                auth=SentinelHubAuthConfig(client_id="cid", client_secret="secret"),
                session_factory=lambda: session,
            )

            payload = session.post.call_args_list[1].kwargs["json"]
            self.assertIn("sample.B04 * 2.5", payload["evalscript"])

    def test_per_band_units_are_applied_to_evalscript(self):
        from terravault.process_api import build_process_request

        config = ProcessPatchConfig(
            bbox=[5.96, 45.82, 10.49, 47.81],
            time_from="2026-07-04T00:00:00Z",
            time_to="2026-07-05T00:00:00Z",
            bands=["SCL", "CLD"],
            units=["DN", "PERCENT"],
        )
        payload = build_process_request(config)
        self.assertIn('units: ["DN", "PERCENT"]', payload["evalscript"])

    def test_rejects_dimensions_above_process_api_limit(self):
        with self.assertRaisesRegex(ValueError, "width"):
            ProcessPatchConfig(
                bbox=[5.96, 45.82, 10.49, 47.81],
                time_from="2026-07-04T00:00:00Z",
                time_to="2026-07-05T00:00:00Z",
                width=2501,
            )

    def test_country_output_size_preserves_approximate_aspect(self):
        width, height = output_size_for_bbox([5.96, 45.82, 10.49, 47.81], 2000)
        self.assertEqual(width, 2000)
        self.assertGreater(height, 1000)
        self.assertLess(height, width)


if __name__ == "__main__":
    unittest.main()
