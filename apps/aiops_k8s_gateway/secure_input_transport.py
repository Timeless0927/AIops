"""Redaction of encrypted Secure Input refs from retained Gateway transport records."""

from __future__ import annotations

import json
import sqlite3

from aiops.security import public_secure_input_facts

def redact_command_secure_inputs_in(conn: sqlite3.Connection, command_id: str) -> None:
    row = conn.execute(
        "SELECT parameters_json FROM connector_commands WHERE id = ?",
        (command_id,),
    ).fetchone()
    if row is None:
        return
    parameters = json.loads(str(row["parameters_json"]))
    refs = parameters.get("secure_inputs") if isinstance(parameters, dict) else None
    if not isinstance(refs, list):
        return
    parameters["secure_inputs"] = public_secure_input_facts(refs)
    conn.execute(
        "UPDATE connector_commands SET parameters_json = ? WHERE id = ?",
        (_json(parameters), command_id),
    )


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
