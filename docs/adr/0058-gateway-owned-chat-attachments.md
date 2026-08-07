---
status: accepted
---

# Gateway-owned Chat attachments

Chat Session messages and Chat Attachments remain Gateway-owned product state. Attachments are stored on the Gateway's existing persistent volume with metadata in `gateway.db`, and are accessed only through authenticated Gateway endpoints; assistant-ui Cloud, browser-direct object storage, and a second Chat persistence owner are deliberately excluded so the existing privacy, CSRF, retention, audit, and single-origin boundaries remain authoritative.

Attachments are validated and malware-checked before model use, treated as untrusted context, and copied as retained Human Input material during Investigation Handoff without becoming Evidence, Approval, or execution authority.
