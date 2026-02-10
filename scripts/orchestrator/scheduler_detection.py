from __future__ import annotations

from typing import Optional


def is_model_unsupported(output: str) -> bool:
    lower = output.lower()
    return (
        "is not supported when using codex with a chatgpt account" in lower
        or "model is not supported" in lower
    )


def detect_quota_or_rate_limit(text: str) -> Optional[str]:
    lower = text.lower()
    indicators: list[tuple[str, str]] = [
        ("insufficient_quota", "insufficient_quota"),
        ("quota exceeded", "quota_exceeded"),
        ("exceeded your current quota", "quota_exceeded"),
        ("billing hard limit has been reached", "billing_limit"),
        ("usage limit reached", "usage_limit"),
        ("you have reached your usage limit", "usage_limit"),
        ("you've reached your usage limit", "usage_limit"),
        ("rate limit reached", "rate_limit"),
        ("too many requests", "rate_limit"),
        ("status code 429", "rate_limit"),
        ("429 too many requests", "rate_limit"),
        ("chatgpt account", "account_plan_limit"),
        ("monthly limit reached", "account_plan_limit"),
        ("daily limit reached", "account_plan_limit"),
        ("request was rejected due to rate limiting", "rate_limit"),
    ]
    for needle, kind in indicators:
        if needle in lower:
            return kind
    return None


def timeout_stream_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def classify_failure(text: str) -> str:
    quota_reason = detect_quota_or_rate_limit(text)
    if quota_reason:
        return "quota"

    lower = text.lower()
    compile_markers = [
        "failed to compile",
        "compilation failed",
        "could not compile",
        "cargo check",
        "cargo build",
        "swift build",
        "error[e",
        "no such module",
    ]
    runtime_markers = [
        "thread 'main' panicked",
        "panic",
        "segmentation fault",
        "fatal error",
        "traceback",
        "assertion failed",
        "runtime error",
    ]
    test_markers = [
        "test failed",
        "failures:",
        "assertion",
        "0 passed; 1 failed",
        "failed in",
    ]
    infra_markers = [
        "timed out",
        "timeout",
        "permission denied",
        "network is unreachable",
        "temporary failure",
        "killed",
    ]
    if any(marker in lower for marker in compile_markers):
        return "compile"
    if any(marker in lower for marker in runtime_markers):
        return "runtime"
    if any(marker in lower for marker in test_markers):
        return "test"
    if any(marker in lower for marker in infra_markers):
        return "infra"
    return "unknown"
