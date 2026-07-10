# V1 supports one delivery mode per notification provider

Status: accepted

V1 provides Feishu group-bot webhooks with signing secrets, DingTalk group-robot webhooks with signing secrets, and authenticated SMTP email with TLS. Each provider validates configuration, supports a test delivery, and renders the same channel-neutral Notification Request into its native card or email format. Feishu and DingTalk application modes, personal messages, SMS, voice, and generic webhooks are deferred so V1 does not maintain parallel authentication paths or inbound callbacks.
