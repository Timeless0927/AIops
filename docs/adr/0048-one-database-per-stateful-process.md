# Each stateful process owns one database

Status: accepted

V1 uses one SQLite database file per stateful process: `gateway.db` for Gateway-owned Incidents, Investigations, identity, policy, Approval, Connector Command, audit, catalog, report, and Notification Request state; `diagnosis.db` for Diagnosis Jobs; `notification.db` for Notification Engine configuration, encrypted credentials, routing, templates, requests, and deliveries; and a local `connector.db` for each Connector's execution journal and unreported results. Modules within a process share its transaction boundary, while processes never read or write each other's tables.

This replaces the current module-per-file pattern so operations such as approve-and-create-command can commit atomically and each process has one backup and migration unit. Each process owns explicit forward schema migrations, foreign keys, constraints, and application-generated identifiers; code avoids cross-file `ATTACH`, implicit `rowid` identity, and other unnecessary SQLite-specific schema dependencies. No generic dual-database repository layer is added in V1. A later PostgreSQL migration replaces and migrates one process store at a time without first untangling module databases or cross-service transactions.
