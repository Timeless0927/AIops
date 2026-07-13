"""SQLite migrations owned by Gateway evidence and recommendation decisions."""

from __future__ import annotations


MIGRATIONS = (
    (
        9,
        """
        CREATE TABLE evidence_steps (
            id TEXT NOT NULL,
            investigation_id TEXT NOT NULL,
            sequence INTEGER NOT NULL CHECK (sequence > 0),
            purpose TEXT NOT NULL CHECK (length(purpose) > 0),
            source TEXT NOT NULL CHECK (length(source) > 0),
            scope_json TEXT NOT NULL,
            state TEXT NOT NULL CHECK (state IN ('running', 'succeeded', 'partial', 'failed', 'skipped')),
            result TEXT,
            impact TEXT NOT NULL CHECK (length(impact) > 0),
            evidence_references_json TEXT NOT NULL,
            missing_guidance TEXT,
            observed_at REAL NOT NULL,
            expires_at REAL NOT NULL CHECK (expires_at >= observed_at),
            PRIMARY KEY (investigation_id, id),
            UNIQUE (investigation_id, sequence),
            FOREIGN KEY (investigation_id) REFERENCES investigations(id)
        );
        CREATE INDEX evidence_steps_by_investigation
            ON evidence_steps(investigation_id, sequence);

        CREATE TABLE investigation_judgments (
            investigation_id TEXT PRIMARY KEY,
            summary TEXT NOT NULL CHECK (length(summary) > 0),
            valid INTEGER NOT NULL DEFAULT 1 CHECK (valid IN (0, 1)),
            evidence_gate_status TEXT NOT NULL CHECK (evidence_gate_status IN ('complete', 'incomplete')),
            next_evidence_guidance_json TEXT NOT NULL,
            FOREIGN KEY (investigation_id) REFERENCES investigations(id)
        );

        CREATE TABLE recommended_actions (
            id TEXT NOT NULL,
            investigation_id TEXT NOT NULL,
            version INTEGER NOT NULL CHECK (version > 0),
            action_type TEXT NOT NULL CHECK (length(action_type) > 0),
            summary TEXT NOT NULL CHECK (length(summary) > 0),
            target_json TEXT NOT NULL,
            parameters_json TEXT NOT NULL,
            evidence_step_ids_json TEXT NOT NULL,
            safeguards_json TEXT NOT NULL,
            rollback_plan_json TEXT NOT NULL,
            gate_status TEXT NOT NULL CHECK (gate_status IN ('complete', 'incomplete')),
            gate_reasons_json TEXT NOT NULL,
            action_hash TEXT NOT NULL CHECK (length(action_hash) = 64),
            stale INTEGER NOT NULL DEFAULT 0 CHECK (stale IN (0, 1)),
            created_at REAL NOT NULL,
            PRIMARY KEY (investigation_id, id, version),
            FOREIGN KEY (investigation_id) REFERENCES investigations(id)
        );
        CREATE INDEX recommended_actions_by_investigation
            ON recommended_actions(investigation_id, version, id);
        """,
    ),
    (
        36,
        """
        ALTER TABLE recommended_actions RENAME TO recommended_actions_v35;
        DROP INDEX recommended_actions_by_investigation;
        CREATE TABLE recommended_actions (
            id TEXT NOT NULL,
            investigation_id TEXT NOT NULL,
            version INTEGER NOT NULL CHECK (version > 0),
            summary TEXT NOT NULL CHECK (length(summary) > 0),
            change_intent TEXT NOT NULL CHECK (change_intent IN ('generic', 'controlled_restart')),
            target_json TEXT NOT NULL CHECK (json_valid(target_json)),
            evidence_step_ids_json TEXT NOT NULL CHECK (json_valid(evidence_step_ids_json)),
            safeguards_json TEXT NOT NULL CHECK (json_valid(safeguards_json)),
            gate_status TEXT NOT NULL CHECK (gate_status IN ('complete', 'incomplete')),
            gate_reasons_json TEXT NOT NULL CHECK (json_valid(gate_reasons_json)),
            action_hash TEXT NOT NULL CHECK (length(action_hash) = 64),
            stale INTEGER NOT NULL DEFAULT 0 CHECK (stale IN (0, 1)),
            created_at REAL NOT NULL,
            PRIMARY KEY (investigation_id, id, version),
            FOREIGN KEY (investigation_id) REFERENCES investigations(id)
        );
        INSERT INTO recommended_actions (
            id, investigation_id, version, summary, change_intent, target_json,
            evidence_step_ids_json, safeguards_json, gate_status,
            gate_reasons_json, action_hash, stale, created_at
        )
        SELECT id, investigation_id, version, summary,
               CASE action_type
                   WHEN 'restart_deployment' THEN 'controlled_restart'
                   ELSE 'generic'
               END,
               target_json,
               evidence_step_ids_json, safeguards_json, gate_status,
               gate_reasons_json, action_hash, stale, created_at
        FROM recommended_actions_v35;
        CREATE INDEX recommended_actions_by_investigation
            ON recommended_actions(investigation_id, version, id);
        DROP TABLE recommended_actions_v35;
        """,
    ),
)
