# Reports are Incident-scoped structured publications

Status: accepted

V1 has one Incident Report that aggregates every Investigation; individual Investigations retain summaries rather than separate reports. Once the Incident is resolved and all Investigations are terminal, Gateway creates a structured draft tied to the source Incident revision, included Investigation IDs, and evidence references. Users may edit only narrative impact, root-cause explanation, resolution summary, and follow-up fields, while recorded facts remain immutable. Publication requires an explicit human action, each published version is immutable, and a reopened then re-resolved Incident produces a new draft without altering earlier publications. Reports store structured content rather than arbitrary HTML and exclude model reasoning traces and internal run or session identifiers.
