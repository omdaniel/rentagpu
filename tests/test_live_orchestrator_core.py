from __future__ import annotations

import unittest

from live_orchestrator import (
    classify_failure,
    detect_quota_or_rate_limit,
    format_template,
    parse_allowed_files,
    parse_profiles,
    parse_validation_commands,
    within_allowed_files,
)


class LiveOrchestratorCoreTests(unittest.TestCase):
    def test_parse_profiles_accepts_reasoning_alias(self) -> None:
        profiles = parse_profiles("gpt-5.3-codex:extrahigh;gpt-5.3-codex:low")
        self.assertEqual(len(profiles), 2)
        self.assertEqual(profiles[0].reasoning, "xhigh")
        self.assertEqual(profiles[1].reasoning, "low")

    def test_parse_profiles_rejects_invalid_reasoning(self) -> None:
        with self.assertRaises(ValueError):
            parse_profiles("gpt-5.3-codex:ultra")

    def test_detect_quota_or_rate_limit(self) -> None:
        self.assertEqual(
            detect_quota_or_rate_limit("Request failed: 429 Too Many Requests"),
            "rate_limit",
        )
        self.assertIsNone(detect_quota_or_rate_limit("plain compiler failure"))

    def test_parse_allowed_files_strips_annotation_suffixes(self) -> None:
        lines = [
            "# Packet",
            "## Allowed Files",
            "- `src/a.py`",
            "- `src/b.py (must stay local)`",
            "## Validation Commands",
        ]
        allowed = parse_allowed_files(lines)
        self.assertEqual(allowed, {"src/a.py", "src/b.py"})

    def test_parse_validation_commands_supports_multiline(self) -> None:
        lines = [
            "# Packet",
            "## Validation Commands",
            "```bash",
            "# comment line",
            "python -m pytest \\",
            "  tests -q",
            "",
            "echo done",
            "```",
            "## Next",
        ]
        commands = parse_validation_commands(lines)
        self.assertEqual(commands, ["python -m pytest tests -q", "echo done"])

    def test_classify_failure_prioritizes_quota(self) -> None:
        text = "insufficient_quota and failed to compile module"
        self.assertEqual(classify_failure(text), "quota")

    def test_within_allowed_files(self) -> None:
        self.assertTrue(within_allowed_files(["a.py", "b.py"], {"a.py", "b.py"}))
        self.assertFalse(within_allowed_files(["a.py", "c.py"], {"a.py", "b.py"}))

    def test_format_template_missing_key_raises(self) -> None:
        with self.assertRaises(ValueError):
            format_template(
                "echo {missing}",
                {
                    "task_id": "W101",
                    "model": "gpt-5.3-codex",
                    "reasoning": "low",
                    "worktree": "/tmp/work",
                    "prompt_file": "/tmp/prompt.txt",
                    "log_file": "/tmp/log.txt",
                    "packet_path": "/tmp/packet.md",
                },
            )


if __name__ == "__main__":
    unittest.main()
