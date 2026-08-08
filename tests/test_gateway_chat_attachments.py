from __future__ import annotations

import base64
import hashlib
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from apps.aiops_k8s_gateway.chat_attachments import ChatAttachmentError, ChatAttachments
from apps.aiops_k8s_gateway.chat_sessions import ChatError, ChatSessions


# 1x1 RGB PNG; the attachment boundary performs a real decoder check.
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")
PDF = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n%%EOF\n"


def _reserve(store: ChatAttachments, session: str, *, key: str, filename: str, size: int | None = None):
    return store.reserve(
        "user-1", session, filename=filename, content_type="image/png" if filename.endswith(".png") else "text/plain",
        declared_size=len(PNG) if size is None else size, idempotency_key=key,
    )


def test_supported_attachment_is_scanned_persisted_and_hash_deduplicated(tmp_path: Path, monkeypatch) -> None:
    sessions = ChatSessions(tmp_path / "gateway.db")
    session = str(sessions.create("user-1", idempotency_key="create")['id'])
    store = ChatAttachments(tmp_path / "gateway.db", scanner=lambda _: True)
    first = _reserve(store, session, key="one", filename="logs/incident.png")
    ready = store.upload("user-1", session, str(first["id"]), PNG, idempotency_key="upload-one")
    second = _reserve(store, session, key="two", filename="copy.png")
    duplicate = store.upload("user-1", session, str(second["id"]), PNG, idempotency_key="upload-two")

    assert ready["status"] == duplicate["status"] == "ready"
    assert ready["filename"] == "incident.png"
    assert ready["sha256"] == hashlib.sha256(PNG).hexdigest()
    blobs = list((tmp_path / "chat-attachments" / "blobs").iterdir())
    assert len(blobs) == 1 and blobs[0].name == ready["sha256"]
    with pytest.raises(ChatAttachmentError) as immutable:
        store.upload("user-1", session, str(first["id"]), b"different", idempotency_key="replace")
    assert immutable.value.code == "attachment_immutable"
    monkeypatch.setattr("apps.aiops_k8s_gateway.chat_attachments.MAX_FILE_BYTES", 3)
    with pytest.raises(ChatAttachmentError) as oversized_immutable:
        store.upload("user-1", session, str(first["id"]), b"long", idempotency_key="replace-large")
    assert oversized_immutable.value.code == "attachment_immutable"


def test_validation_rejects_spoofing_sensitive_text_and_limits(tmp_path: Path) -> None:
    sessions = ChatSessions(tmp_path / "gateway.db")
    session = str(sessions.create("user-1", idempotency_key="create")['id'])
    store = ChatAttachments(tmp_path / "gateway.db", scanner=lambda _: True)
    with pytest.raises(ChatAttachmentError) as unsupported:
        store.reserve("user-1", session, filename="secret.pem", content_type="application/x-pem-file", declared_size=10, idempotency_key="bad")
    assert unsupported.value.code == "unsupported_type"
    item = store.reserve("user-1", session, filename="notes.txt", content_type="text/plain", declared_size=64, idempotency_key="text")
    rejected = store.upload("user-1", session, str(item["id"]), b"password = hunter2\n", idempotency_key="text-upload")
    assert rejected["status"] == "rejected"
    assert rejected["rejection_code"] == "sensitive_content"
    json_item = store.reserve("user-1", session, filename="settings.json", content_type="application/json", declared_size=16, idempotency_key="json")
    json_rejected = store.upload("user-1", session, str(json_item["id"]), b'{"token":"abc"}', idempotency_key="json-upload")
    assert json_rejected["status"] == "rejected" and json_rejected["rejection_code"] == "sensitive_content"
    with pytest.raises(ChatAttachmentError) as too_large:
        store.reserve("user-1", session, filename="large.txt", content_type="text/plain", declared_size=20 * 1024 * 1024 + 1, idempotency_key="large")
    assert too_large.value.code == "attachment_too_large"
    spoof = store.reserve("user-1", session, filename="spoof.png", content_type="image/png", declared_size=7, idempotency_key="spoof")
    spoofed = store.upload("user-1", session, str(spoof["id"]), b"not png", idempotency_key="spoof-upload")
    assert spoofed["status"] == "rejected" and spoofed["rejection_code"] == "mime_mismatch"
    oversized_text = store.reserve("user-1", session, filename="huge.txt", content_type="text/plain", declared_size=1 * 1024 * 1024 + 1, idempotency_key="huge")
    parsed = store.upload("user-1", session, str(oversized_text["id"]), b"a" * (1 * 1024 * 1024 + 1), idempotency_key="huge-upload")
    assert parsed["status"] == "rejected" and parsed["rejection_code"] == "parse_limit"


def test_pdf_extracted_sensitive_text_is_rejected(tmp_path: Path) -> None:
    writer = PdfWriter()
    page = writer.add_blank_page(width=200, height=200)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 20 100 Td (password = hunter2) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)

    sessions = ChatSessions(tmp_path / "gateway.db")
    session = str(sessions.create("user-1", idempotency_key="create")["id"])
    store = ChatAttachments(tmp_path / "gateway.db", scanner=lambda _: True)
    item = store.reserve("user-1", session, filename="report.pdf", content_type="application/pdf", declared_size=len(output.getvalue()), idempotency_key="reserve")
    rejected = store.upload("user-1", session, str(item["id"]), output.getvalue(), idempotency_key="upload")
    assert rejected["status"] == "rejected" and rejected["rejection_code"] == "sensitive_content"


def test_scanner_failure_is_fail_closed_and_only_ready_can_bind(tmp_path: Path) -> None:
    sessions = ChatSessions(tmp_path / "gateway.db")
    session = str(sessions.create("user-1", idempotency_key="create")['id'])
    store = ChatAttachments(tmp_path / "gateway.db", scanner=lambda _: (_ for _ in ()).throw(TimeoutError()))
    item = store.reserve("user-1", session, filename="log.txt", content_type="text/plain", declared_size=3, idempotency_key="one")
    failed = store.upload("user-1", session, str(item["id"]), b"ok\n", idempotency_key="upload")
    assert failed["status"] == "failed"
    assert failed["rejection_code"] == "scanner_unavailable"
    message = sessions.send("user-1", session, content="问题", idempotency_key="message", respond=lambda _: {"answer": "回答"})
    with pytest.raises(ChatAttachmentError, match="已通过安全检查"):
        store.bind("user-1", session, str(message["messages"][0]["id"]), [str(item["id"])])


def test_cross_user_access_is_hidden_and_session_delete_collects_blob(tmp_path: Path) -> None:
    sessions = ChatSessions(tmp_path / "gateway.db")
    session = str(sessions.create("user-1", idempotency_key="create")['id'])
    store = ChatAttachments(tmp_path / "gateway.db", scanner=lambda _: True)
    item = store.reserve("user-1", session, filename="log.txt", content_type="text/plain", declared_size=3, idempotency_key="one")
    item = store.upload("user-1", session, str(item["id"]), b"ok\n", idempotency_key="upload")
    with pytest.raises(ChatAttachmentError) as hidden:
        store.get("user-2", session, str(item["id"]))
    assert hidden.value.code == "attachment_not_found"
    blob = tmp_path / "chat-attachments" / "blobs" / str(item["sha256"])
    assert blob.exists()
    sessions.delete("user-1", session, idempotency_key="delete")
    store.collect_garbage()
    assert not blob.exists()


def test_count_total_and_malware_limits_are_enforced_at_reserve_and_scan(tmp_path: Path) -> None:
    sessions = ChatSessions(tmp_path / "gateway.db")
    session = str(sessions.create("user-1", idempotency_key="create")['id'])
    store = ChatAttachments(tmp_path / "gateway.db", scanner=lambda _: False)
    for index in range(5):
        store.reserve("user-1", session, filename=f"{index}.txt", content_type="text/plain", declared_size=1, idempotency_key=f"reserve-{index}")
    with pytest.raises(ChatAttachmentError) as count:
        store.reserve("user-1", session, filename="six.txt", content_type="text/plain", declared_size=1, idempotency_key="reserve-six")
    assert count.value.code == "attachment_count_limit"
    other = str(sessions.create("user-1", idempotency_key="other")['id'])
    store.reserve("user-1", other, filename="a.txt", content_type="text/plain", declared_size=20 * 1024 * 1024, idempotency_key="a")
    store.reserve("user-1", other, filename="b.txt", content_type="text/plain", declared_size=20 * 1024 * 1024, idempotency_key="b")
    with pytest.raises(ChatAttachmentError) as total:
        store.reserve("user-1", other, filename="c.txt", content_type="text/plain", declared_size=10 * 1024 * 1024 + 1, idempotency_key="c")
    assert total.value.code == "attachment_total_too_large"
    item = store.reserve("user-1", other, filename="malware.txt", content_type="text/plain", declared_size=4, idempotency_key="malware")
    rejected = store.upload("user-1", other, str(item["id"]), b"evil", idempotency_key="malware-upload")
    assert rejected["status"] == "rejected" and rejected["rejection_code"] == "malware_detected"

    concurrent_session = str(sessions.create("user-1", idempotency_key="concurrent")["id"])

    def reserve_concurrently(index: int) -> str:
        try:
            return str(store.reserve("user-1", concurrent_session, filename=f"c-{index}.txt", content_type="text/plain", declared_size=1, idempotency_key=f"concurrent-{index}")["status"])
        except ChatAttachmentError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=6) as executor:
        results = list(executor.map(reserve_concurrently, range(6)))
    assert results.count("pending") == 5 and results.count("attachment_count_limit") == 1


def test_failed_scanner_retry_reprocesses_quarantined_bytes(tmp_path: Path) -> None:
    sessions = ChatSessions(tmp_path / "gateway.db")
    session = str(sessions.create("user-1", idempotency_key="create")['id'])
    state = {"available": False}
    def scan(_: bytes) -> bool:
        if not state["available"]:
            raise TimeoutError()
        return True
    store = ChatAttachments(tmp_path / "gateway.db", scanner=scan)
    item = store.reserve("user-1", session, filename="log.txt", content_type="text/plain", declared_size=3, idempotency_key="reserve")
    failed = store.upload("user-1", session, str(item["id"]), b"ok\n", idempotency_key="upload")
    assert failed["status"] == "failed"
    state["available"] = True
    ready = store.retry("user-1", session, str(item["id"]), idempotency_key="retry")
    assert ready["status"] == "ready"
    assert store.retry("user-1", session, str(item["id"]), idempotency_key="retry") == ready


def test_attachment_ids_are_part_of_send_idempotency_and_bound_once(tmp_path: Path) -> None:
    sessions = ChatSessions(tmp_path / "gateway.db")
    session = str(sessions.create("user-1", idempotency_key="create")["id"])
    store = ChatAttachments(tmp_path / "gateway.db", scanner=lambda _: True)
    first = store.reserve("user-1", session, filename="one.txt", content_type="text/plain", declared_size=3, idempotency_key="one")
    second = store.reserve("user-1", session, filename="two.txt", content_type="text/plain", declared_size=3, idempotency_key="two")
    first = store.upload("user-1", session, str(first["id"]), b"one", idempotency_key="upload-one")
    second = store.upload("user-1", session, str(second["id"]), b"two", idempotency_key="upload-two")
    result = sessions.send(
        "user-1", session, content="问题", attachment_ids=[str(first["id"])],
        idempotency_key="message", bind_attachments=store.bind_in,
        respond=lambda _: {"mode": "knowledge", "answer": "回答", "scope": None, "tool_activity": [], "evidence_references": [], "uncertainty": None, "next_step": None, "completion": {"status": "completed", "stopping_reason": "knowledge_answered"}},
    )
    with pytest.raises(ChatError, match="conflicts") as conflict:
        sessions.send(
            "user-1", session, content="问题", attachment_ids=[str(second["id"])],
            idempotency_key="message", bind_attachments=store.bind_in,
            respond=lambda _: pytest.fail("idempotency conflict must not call the model"),
        )
    assert conflict.value.code == "idempotency_conflict"
    with pytest.raises(ChatAttachmentError) as bound:
        store.bind("user-1", session, str(result["messages"][0]["id"]), [str(first["id"])])
    assert bound.value.code == "attachment_bound"


def test_model_projection_uses_only_ready_attachments_on_the_current_branch(tmp_path: Path) -> None:
    database = tmp_path / "gateway.db"
    store = ChatAttachments(database, scanner=lambda _: True)
    sessions = ChatSessions(database, attachment_source=store)
    session = str(sessions.create("user-1", idempotency_key="create")["id"])
    item = store.reserve(
        "user-1", session, filename="incident.log", content_type="text/plain",
        declared_size=4, idempotency_key="reserve",
    )
    item = store.upload("user-1", session, str(item["id"]), b"boom", idempotency_key="upload")
    assert [candidate["id"] for candidate in sessions.list(
        "user-1", query="boom",
        attachment_session_ids=store.matching_session_ids("user-1", "boom"),
    )] == [session]
    assert store.matching_session_ids("user-2", "boom") == set()
    requests: list[dict[str, object]] = []

    def answer(request: dict[str, object]) -> dict[str, object]:
        requests.append(request)
        return {
            "mode": "knowledge", "answer": "回答", "scope": None, "tool_activity": [],
            "evidence_references": [], "uncertainty": None, "next_step": None,
            "completion": {"status": "completed", "stopping_reason": "knowledge_answered"},
        }

    sent = sessions.send(
        "user-1", session, content="分析", attachment_ids=[str(item["id"])],
        idempotency_key="message", bind_attachments=store.bind_in, respond=answer,
    )
    projected = requests[0]["attachments"][0]  # type: ignore[index]
    assert projected["attachment_id"] == item["id"]
    assert projected["extracted_text"] == "boom"
    assert sent["messages"][0]["attachments"][0]["model_use_status"] == "included"

    sessions.reload(
        "user-1", session, str(sent["messages"][-1]["id"]),
        idempotency_key="reload", respond=answer,
    )
    assert requests[-1]["attachments"][0]["attachment_id"] == item["id"]  # type: ignore[index]

    sessions.edit(
        "user-1", session, str(sent["messages"][0]["id"]), content="新分支",
        idempotency_key="edit", respond=answer,
    )
    assert "attachments" not in requests[-1]
