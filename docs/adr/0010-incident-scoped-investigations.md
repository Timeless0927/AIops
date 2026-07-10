# Incidents own sequential Investigations

Status: accepted

Each Incident owns an ordered history of Investigations and has at most one active Investigation. Alert ingress automatically creates the first Investigation; subsequent questions, Agent responses, tool activity, and evidence updates belong to that same Investigation. After completion or termination, only an explicit reinvestigate action creates the next one. Diagnosis Jobs and legacy Agent Runs are internal execution details: Console and the public Gateway contract expose Incident identity, Investigation state, and Investigation events without exposing `run_id`, `session_id`, or job identity.
