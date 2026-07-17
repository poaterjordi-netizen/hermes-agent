"""Opt-in CLI auto fresh-session handoff.

This module is deliberately CLI-only and deterministic. It never calls an LLM,
never changes compression thresholds, and fails open: callers should treat any
``skipped`` or exception as a no-op for the just-finished turn.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import logging
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional

logger = logging.getLogger(__name__)

DEFAULT_CONFIG: Dict[str, Any] = {
    "enabled": False,
    "low_watermark": 0.80,
    "high_watermark": 0.90,
    "max_handoff_chars": 24_000,
    "auto_continue": True,
    "once_per_session": True,
    "persist_to_session_db": True,
}

_CONTINUATION_INSTRUCTION = (
    "Continue from this handoff. Do not repeat completed work. "
    "If no active work remains, say refresh completed and wait."
)


@dataclass
class AutoHandoffResult:
    triggered: bool = False
    skipped: bool = False
    reason: str = ""
    old_session_id: Optional[str] = None
    new_session_id: Optional[str] = None
    handoff_text: str = ""
    continuation_prompt: str = ""
    goal_migrated: bool = False


def normalize_config(config: Any) -> Dict[str, Any]:
    """Return a small, typed config dict with feature defaults applied."""
    cfg = dict(DEFAULT_CONFIG)
    if isinstance(config, Mapping):
        cfg.update({k: v for k, v in config.items() if k in DEFAULT_CONFIG})

    cfg["enabled"] = bool(cfg.get("enabled", False))
    cfg["auto_continue"] = bool(cfg.get("auto_continue", True))
    cfg["once_per_session"] = bool(cfg.get("once_per_session", True))
    cfg["persist_to_session_db"] = bool(cfg.get("persist_to_session_db", True))
    cfg["low_watermark"] = _as_float(cfg.get("low_watermark"), DEFAULT_CONFIG["low_watermark"])
    cfg["high_watermark"] = _as_float(cfg.get("high_watermark"), DEFAULT_CONFIG["high_watermark"])
    cfg["max_handoff_chars"] = max(1_000, int(_as_float(cfg.get("max_handoff_chars"), DEFAULT_CONFIG["max_handoff_chars"])))
    return cfg


def evaluate_auto_handoff_gate(
    cli: Any,
    *,
    response: Optional[str],
    result: Optional[Mapping[str, Any]],
    config: Optional[Mapping[str, Any]] = None,
) -> AutoHandoffResult:
    """Decide whether the just-finished CLI turn may auto-handoff."""
    cfg = normalize_config(config if config is not None else getattr(cli, "_auto_handoff_config", None))
    old_session_id = getattr(cli, "session_id", None)

    if not cfg["enabled"]:
        return AutoHandoffResult(skipped=True, reason="disabled", old_session_id=old_session_id)
    if not str(response or "").strip():
        return AutoHandoffResult(skipped=True, reason="empty_response", old_session_id=old_session_id)

    result = result or {}
    if result.get("failed"):
        return AutoHandoffResult(skipped=True, reason="failed", old_session_id=old_session_id)
    if result.get("partial"):
        return AutoHandoffResult(skipped=True, reason="partial", old_session_id=old_session_id)
    if result.get("interrupted"):
        return AutoHandoffResult(skipped=True, reason="interrupted", old_session_id=old_session_id)
    if result.get("completed") is False:
        return AutoHandoffResult(skipped=True, reason="not_completed", old_session_id=old_session_id)

    if bool(getattr(cli, "_should_exit", False)):
        return AutoHandoffResult(skipped=True, reason="exiting", old_session_id=old_session_id)
    if bool(getattr(cli, "_command_running", False)):
        return AutoHandoffResult(skipped=True, reason="command_running", old_session_id=old_session_id)
    if _queue_has_items(getattr(cli, "_pending_input", None)):
        return AutoHandoffResult(skipped=True, reason="pending_input", old_session_id=old_session_id)
    if _queue_has_items(getattr(cli, "_interrupt_queue", None)):
        return AutoHandoffResult(skipped=True, reason="pending_interrupt", old_session_id=old_session_id)
    if result.get("pending_steer"):
        return AutoHandoffResult(skipped=True, reason="pending_steer", old_session_id=old_session_id)

    if cfg["once_per_session"]:
        seen = getattr(cli, "_auto_handoff_triggered_sessions", None)
        if isinstance(seen, set) and old_session_id in seen:
            return AutoHandoffResult(skipped=True, reason="once_per_session", old_session_id=old_session_id)

    agent = getattr(cli, "agent", None)
    if agent is None:
        return AutoHandoffResult(skipped=True, reason="missing_agent", old_session_id=old_session_id)
    compressor = getattr(agent, "context_compressor", None)
    if compressor is None:
        return AutoHandoffResult(skipped=True, reason="missing_compressor", old_session_id=old_session_id)

    try:
        context_length = int(getattr(compressor, "context_length", 0) or 0)
        last_prompt_tokens = int(getattr(compressor, "last_prompt_tokens", 0) or 0)
    except (TypeError, ValueError):
        return AutoHandoffResult(skipped=True, reason="invalid_context_usage", old_session_id=old_session_id)

    if context_length <= 0:
        return AutoHandoffResult(skipped=True, reason="missing_context_length", old_session_id=old_session_id)
    if last_prompt_tokens < 0:
        return AutoHandoffResult(skipped=True, reason="negative_prompt_tokens", old_session_id=old_session_id)

    ratio = last_prompt_tokens / context_length
    if ratio < cfg["low_watermark"]:
        return AutoHandoffResult(skipped=True, reason="below_low_watermark", old_session_id=old_session_id)
    if ratio >= cfg["high_watermark"]:
        return AutoHandoffResult(skipped=True, reason="at_or_above_high_watermark", old_session_id=old_session_id)

    return AutoHandoffResult(triggered=True, reason="triggered", old_session_id=old_session_id)


def perform_cli_auto_handoff(
    cli: Any,
    *,
    response: Optional[str],
    result: Optional[Mapping[str, Any]],
) -> AutoHandoffResult:
    """Run the full CLI auto-handoff sequence if the gate triggers.

    Safety order: generate deterministic handoff -> append to old session DB ->
    rotate with ``new_session(silent=True)`` -> migrate active /goal -> enqueue
    continuation prompt. Any failure returns a skipped result and must not affect
    the just-finished turn.
    """
    cfg = normalize_config(getattr(cli, "_auto_handoff_config", None))
    gate = evaluate_auto_handoff_gate(cli, response=response, result=result, config=cfg)
    if not gate.triggered:
        return gate

    old_session_id = gate.old_session_id
    handoff_text = build_deterministic_handoff(
        cli,
        response=response or "",
        result=result or {},
        old_session_id=old_session_id,
        max_chars=int(cfg["max_handoff_chars"]),
    )
    continuation_prompt = format_continuation_prompt(handoff_text)

    gate.handoff_text = handoff_text
    gate.continuation_prompt = continuation_prompt

    try:
        if cfg["persist_to_session_db"]:
            if not persist_handoff_to_session_db(cli, old_session_id, handoff_text):
                return AutoHandoffResult(skipped=True, reason="persist_failed", old_session_id=old_session_id)

        try:
            cli.new_session(silent=True, title="Auto handoff continuation")
        except Exception:
            logger.debug("auto handoff: new_session failed", exc_info=True)
            return AutoHandoffResult(skipped=True, reason="new_session_failed", old_session_id=old_session_id)

        new_session_id = getattr(cli, "session_id", None)
        gate.new_session_id = new_session_id
        try:
            from hermes_cli.goals import migrate_goal_to_session
            gate.goal_migrated = bool(
                migrate_goal_to_session(old_session_id, new_session_id, reason="auto_handoff")
            )
        except Exception:
            logger.debug("auto handoff: goal migration failed", exc_info=True)
            gate.goal_migrated = False
        try:
            cli._goal_manager = None
        except Exception:
            pass

        if cfg["auto_continue"]:
            try:
                getattr(cli, "_pending_input").put(continuation_prompt)
            except Exception:
                logger.debug("auto handoff: enqueue continuation failed", exc_info=True)
                return AutoHandoffResult(
                    skipped=True,
                    reason="enqueue_failed",
                    old_session_id=old_session_id,
                    new_session_id=new_session_id,
                    handoff_text=handoff_text,
                    continuation_prompt=continuation_prompt,
                    goal_migrated=gate.goal_migrated,
                )

        if cfg["once_per_session"]:
            seen = getattr(cli, "_auto_handoff_triggered_sessions", None)
            if not isinstance(seen, set):
                seen = set()
                try:
                    cli._auto_handoff_triggered_sessions = seen
                except Exception:
                    pass
            seen.add(old_session_id)

        gate.triggered = True
        gate.skipped = False
        gate.reason = "triggered"
        return gate
    except Exception:
        logger.debug("auto handoff failed open", exc_info=True)
        return AutoHandoffResult(skipped=True, reason="exception", old_session_id=old_session_id)


def build_deterministic_handoff(
    cli: Any,
    *,
    response: str,
    result: Mapping[str, Any],
    old_session_id: Optional[str],
    max_chars: int,
) -> str:
    agent = getattr(cli, "agent", None)
    compressor = getattr(agent, "context_compressor", None) if agent is not None else None
    context_tokens = int(getattr(compressor, "last_prompt_tokens", 0) or 0) if compressor is not None else 0
    context_length = int(getattr(compressor, "context_length", 0) or 0) if compressor is not None else 0
    ratio = (context_tokens / context_length) if context_length else 0.0
    history = list(getattr(cli, "conversation_history", None) or [])
    latest_user = _latest_content(history, "user")
    latest_assistant = response or _latest_content(history, "assistant")
    paths = _extract_path_like_strings("\n".join([latest_user, latest_assistant]))

    sections: List[str] = [
        "[AUTO-HANDOFF]",
        "# Auto Fresh Session Handoff",
        "",
        "## Why refreshed",
        f"- old_session_id: {old_session_id or 'unknown'}",
        "- new_session_id: assigned after this handoff is persisted",
        f"- context: {context_tokens} / {context_length} tokens ({ratio:.1%})",
        "- trigger: CLI-only opt-in deterministic auto handoff",
        "",
        "## Latest user ask",
        _truncate(latest_user or "(not found)", 3_000),
        "",
        "## Latest assistant result",
        _truncate(latest_assistant or "(not found)", 5_000),
        "",
        "## Current state",
        f"- cwd: {Path.cwd()}",
        f"- session_id_before_refresh: {old_session_id or 'unknown'}",
        f"- active_goal: {_goal_status(cli)}",
        f"- todo_summary: {_todo_summary(agent)}",
        "",
        "## Files / artifacts mentioned",
    ]
    if paths:
        sections.extend(f"- {path}" for path in paths[:20])
    else:
        sections.append("- (none detected)")
    sections.extend([
        "",
        "## Verification already mentioned",
        _extract_verification_lines(latest_assistant) or "- (none detected)",
        "",
        "## Risks / boundaries",
        "- This auto handoff is recovery context, not a new user instruction.",
        "- Do not repeat completed work.",
        "- Auto handoff would not trigger with pending user input, interrupt, or steer queued.",
        "",
        "## Exact continuation instruction",
        _CONTINUATION_INSTRUCTION,
    ])
    text = "\n".join(sections).strip()
    return _truncate(text, max_chars)


def format_continuation_prompt(handoff_text: str) -> str:
    return f"{handoff_text}\n\n{_CONTINUATION_INSTRUCTION}"


def persist_handoff_to_session_db(cli: Any, session_id: Optional[str], handoff_text: str) -> bool:
    db = getattr(cli, "_session_db", None)
    if db is None or not session_id:
        return False
    try:
        db.append_message(
            session_id=session_id,
            role="assistant",
            content=handoff_text,
        )
        return True
    except Exception:
        logger.debug("auto handoff: append_message failed", exc_info=True)
        return False


def _queue_has_items(value: Any) -> bool:
    if value is None:
        return False
    try:
        return not value.empty()
    except Exception:
        pass
    try:
        return bool(value.qsize())
    except Exception:
        return False


def _latest_content(history: Iterable[Mapping[str, Any]], role: str) -> str:
    for msg in reversed(list(history)):
        if not isinstance(msg, Mapping) or msg.get("role") != role:
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, Mapping) and item.get("type") == "text":
                    parts.append(str(item.get("text", "")))
            return "\n".join(parts)
        if content is not None:
            return str(content)
    return ""


def _goal_status(cli: Any) -> str:
    mgr = getattr(cli, "_goal_manager", None)
    state = getattr(mgr, "state", None)
    if state is not None:
        return str(getattr(state, "status", "unknown"))
    try:
        from hermes_cli.goals import load_goal
        loaded = load_goal(getattr(cli, "session_id", ""))
        if loaded is not None:
            return str(getattr(loaded, "status", "unknown"))
    except Exception:
        pass
    return "none"


def _todo_summary(agent: Any) -> str:
    store = getattr(agent, "_todo_store", None) if agent is not None else None
    todos = getattr(store, "todos", None)
    if not todos:
        return "none"
    try:
        counts: Dict[str, int] = {}
        for item in todos:
            status = getattr(item, "status", None) or (item.get("status") if isinstance(item, Mapping) else "unknown")
            counts[str(status)] = counts.get(str(status), 0) + 1
        return ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    except Exception:
        return "available"


def _extract_path_like_strings(text: str) -> List[str]:
    seen = set()
    out: List[str] = []
    patterns = [
        r"/(?:Users|tmp|var|opt|home|Volumes)/[^\s`'\"<>]+",
        r"~/(?:[^\s`'\"<>]+)",
        r"(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+",
    ]
    for pattern in patterns:
        for match in re.findall(pattern, text or ""):
            cleaned = match.rstrip(".,;:)]}")
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                out.append(cleaned)
    return out


def _extract_verification_lines(text: str) -> str:
    lines = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if re.search(r"\b(pytest|python -m py_compile|git |uv |npm |pnpm |make |tox |ruff |mypy)\b", stripped):
            lines.append(f"- {stripped[:500]}")
    return "\n".join(lines[:20])


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 16)] + "… [truncated]"
