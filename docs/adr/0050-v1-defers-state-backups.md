# V1 defers state backups until PostgreSQL

Status: accepted

The internal V1 trial does not add scheduled SQLite backups, CSI VolumeSnapshots, object-storage replication, or an application backup service. Its PVCs provide restart durability only; loss or corruption of a Gateway, Diagnosis, Notification Engine, or Connector volume may permanently lose that process's local state. Notification Destination credentials must be re-entered if either `notification.db` or its installation key is lost, as described by ADR-0036.

This risk is accepted only for the trial stage. Moving a process to PostgreSQL is the trigger to define and verify its production backup policy using standard PostgreSQL facilities such as logical or physical backups and point-in-time recovery. PostgreSQL availability alone is not considered a backup: production promotion requires scheduled backups, declared RPO/RTO, and a successful restore exercise.
