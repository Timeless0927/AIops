# V1 owns local human identities and teams

Status: accepted

AIOps V1 stores Users, Teams, Team Memberships, and Role Bindings in its SQLite control-plane state and does not require LDAP or OIDC. A bootstrap administrator credential is injected through a Kubernetes Secret for first use; passwords are stored only as Argon2id hashes, and browser authentication remains an HttpOnly cookie session protected by CSRF. Plaintext users or passwords in ConfigMaps are forbidden. LDAP or OIDC may later become an identity source without changing the User, Team Membership, or Role Binding domain model.
