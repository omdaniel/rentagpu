from __future__ import annotations

import unittest

from scripts.gpu_exec import _parse_result_json


class GpuExecParsingTests(unittest.TestCase):
    def test_parse_result_json_from_mixed_output(self) -> None:
        output = "\n".join(
            [
                "modal setup logs ...",
                "still working...",
                '{"foo":"bar"}',
                '{"run_id":"abc123","exit_code":0}',
            ]
        )
        parsed = _parse_result_json(output)
        self.assertEqual(parsed["run_id"], "abc123")
        self.assertEqual(parsed["exit_code"], 0)

    def test_parse_result_json_raises_when_missing(self) -> None:
        with self.assertRaises(ValueError):
            _parse_result_json("no json lines here")


if __name__ == "__main__":
    unittest.main()

