"""Chat response delta delivery and current-response cancellation."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .chat_sessions import ChatSessions


def stream_answer(sessions: ChatSessions, owner_id: str, session_id: str, message_id: str, result: dict[str, object]) -> None:
    answer = str(result["answer"])
    for delta in _answer_chunks(answer):
        now = sessions._clock()
        with sessions._database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            sessions._owned_in(conn, owner_id, session_id)
            updated = conn.execute(
                """UPDATE chat_messages SET content = content || ?, updated_at = ?
                   WHERE id = ? AND session_id = ? AND role = 'assistant' AND status = 'sending'""",
                (delta, now, message_id, session_id),
            ).rowcount
            if updated != 1:
                return
            sessions._append_event(
                conn, session_id, "message.delta", f"message:{message_id}:delta:{sessions._id_factory()}",
                {"message_id": message_id, "delta": delta}, now,
            )
        time.sleep(0.02)
    finish_response(
        sessions,
        owner_id, session_id, message_id, status="completed",
        content=answer, error_code=None, result=result,
    )


def complete_response(
    sessions: ChatSessions, owner_id: str, session_id: str, message_id: str,
    result: dict[str, object], *, stream: bool,
) -> None:
    if stream:
        stream_answer(sessions, owner_id, session_id, message_id, result)
    else:
        finish_response(sessions, owner_id, session_id, message_id, status="completed", content=str(result["answer"]), error_code=None, result=result)


def finish_response(
    sessions: ChatSessions, owner_id: str, session_id: str, message_id: str, *,
    status: str, content: str, error_code: str | None, result: dict[str, object] | None,
) -> None:
    from .chat_sessions import ChatError, _canonical

    now = sessions._clock()
    with sessions._database.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        sessions._owned_in(conn, owner_id, session_id)
        updated = conn.execute(
            """UPDATE chat_messages SET status = ?, content = ?, error_code = ?, result_json = ?, updated_at = ?
               WHERE id = ? AND session_id = ? AND status = 'sending'""",
            (status, content, error_code, _canonical(result), now, message_id, session_id),
        ).rowcount
        if updated != 1:
            current = conn.execute(
                "SELECT result_json FROM chat_messages WHERE id = ? AND session_id = ?",
                (message_id, session_id),
            ).fetchone()
            if current is not None and is_user_cancelled(current["result_json"]):
                return
            raise ChatError("message_conflict", "Chat message is no longer awaiting completion")
        conn.execute("UPDATE chat_sessions SET updated_at = ? WHERE id = ?", (now, session_id))
        sessions._append_event(
            conn, session_id, f"message.{status}", f"message:{message_id}:{status}:{sessions._id_factory()}",
            {"message_id": message_id}, now,
        )


def cancel_chat(sessions: ChatSessions, owner_id: str, session_id: str, *, idempotency_key: str) -> dict[str, object]:
    from .chat_sessions import ChatError, _canonical, _required

    owner_id = _required(owner_id, "owner_id", 200)
    session_id = _required(session_id, "session_id", 200)
    idempotency_key = _required(idempotency_key, "idempotency_key", 200)
    event_key = f"message:cancel:{idempotency_key}"
    now = sessions._clock()
    with sessions._database.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        session = sessions._owned_in(conn, owner_id, session_id)
        existing = conn.execute(
            "SELECT 1 FROM chat_events WHERE session_id = ? AND idempotency_key = ?",
            (session_id, event_key),
        ).fetchone()
        if existing is None:
            message_id = str(session["current_head_id"] or "")
            updated = conn.execute(
                """UPDATE chat_messages SET status = 'completed', result_json = ?, updated_at = ?
                   WHERE id = ? AND session_id = ? AND role = 'assistant' AND status = 'sending'""",
                (_canonical({"completion": {"status": "cancelled", "stopping_reason": "user_cancelled"}}), now, message_id, session_id),
            ).rowcount
            if updated != 1:
                raise ChatError("message_not_cancellable", "Chat message is not generating")
            conn.execute("UPDATE chat_sessions SET updated_at = ? WHERE id = ?", (now, session_id))
            sessions._append_event(conn, session_id, "message.cancelled", event_key, {"message_id": message_id}, now)
    return sessions.get(owner_id, session_id)


def is_user_cancelled(value: object) -> bool:
    try:
        decoded = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    completion = decoded.get("completion") if isinstance(decoded, dict) else None
    return isinstance(completion, dict) and completion.get("stopping_reason") == "user_cancelled"


def _answer_chunks(answer: str) -> list[str]:
    size = max(4, (len(answer) + 99) // 100)
    return [answer[index:index + size] for index in range(0, len(answer), size)]
