# Notification credentials are encrypted in the service database

Status: accepted

Notification Destination configuration and credentials live in Notification Engine's SQLite database; webhook URLs, signing secrets, and SMTP passwords are encrypted with authenticated encryption before storage. On first start the service generates one local installation key file with restricted filesystem permissions, independent of Kubernetes, and uses no alternate Secret or environment-variable credential backend. APIs expose only masked configuration state, and logs, audit records, and errors exclude secret material. Recoverable backups require both the database and installation key; losing the key requires administrators to re-enter destination credentials.
