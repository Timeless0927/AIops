# V1 owns a minimal resource catalog

Status: accepted

AIOps V1 does not depend on a CMDB. Connector discovery is authoritative for the existence and runtime identity of Deployment Target candidates; authorized humans promote or bind those real candidates to AIOps-owned Services and Teams. Alert labels may suggest matches but never create permanent identities or bindings. Confirmed Resource Bindings are reused and are not silently overwritten by later hints. A future CMDB integration may import or validate catalog data through an optional adapter without becoming a V1 prerequisite.
