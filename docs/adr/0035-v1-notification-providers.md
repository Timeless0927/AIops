# V1 supports one delivery mode per notification provider

Status: accepted

V1 uses the in-process BSD-2-Clause Apprise library as its only Provider Adapter for Feishu group-bot webhooks, DingTalk group-robot webhooks, and authenticated SMTP email with TLS. Notification Engine validates configuration, performs test delivery, and passes its safe rendered presentation to Apprise while retaining routing, delivery state, retries, and audit. It does not deploy Apprise API or maintain parallel provider-specific clients. Feishu and DingTalk application modes, personal messages, SMS, voice, and generic webhooks are deferred so V1 does not maintain parallel authentication paths or inbound callbacks.
