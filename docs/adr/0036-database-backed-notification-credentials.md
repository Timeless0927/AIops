# Notification credentials are encrypted in the service database

Status: accepted

Notification Destination configuration and credentials live in Notification Engine's SQLite database; webhook URLs, signing secrets, and SMTP passwords are encrypted with authenticated encryption before storage. The encryption key is supplied only by a dedicated Kubernetes Secret mounted read-only into Notification Engine, separate from its database PVC; no API or environment-variable fallback exists. APIs expose only masked configuration state, and logs, audit records, rendered configuration, and the database exclude the plaintext key. Recoverable backups require both the database and Secret; losing either requires administrators to re-enter destination credentials until production recovery is defined.
