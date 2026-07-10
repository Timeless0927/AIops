# Notification routing uses the first matching rule

Status: accepted

Notification Routes are evaluated by explicit priority and the first enabled match wins. V1 matches exact event types, severities, Environments, Teams, and Services; a route either fans out to one or more configured destinations or explicitly suppresses the request with an audited reason. A final default route is required, duplicate destinations are collapsed, and administrators can simulate a request and test a destination before activation. Regexes, scripts, and user-supplied code conditions are excluded from V1.
