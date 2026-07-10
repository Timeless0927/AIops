# Triage Labels

The engineering skills use these five canonical triage roles for the local Markdown tracker.

| Canonical role | Tracker value | Meaning |
| --- | --- | --- |
| `needs-triage` | `needs-triage` | Maintainer needs to evaluate |
| `needs-info` | `needs-info` | Waiting on reporter |
| `ready-for-agent` | `ready-for-agent` | Fully specified and agent-ready |
| `ready-for-human` | `ready-for-human` | Requires human implementation |
| `wontfix` | `wontfix` | Will not be actioned |

When a skill names a canonical role, write the corresponding tracker value in the file's `Status:` line.
