# Approval eligibility follows explicit environment authority

Status: accepted

A user's eligibility to approve a frozen Recommended Action is determined by whether their explicit approval authority covers its target environment and resource. Requester identity is irrelevant: a requester with the required production approval authority may approve their own request, while a user without that authority may not. A platform role, risk label, Agent identity, Service Identity, or Connector identity does not implicitly grant approval authority. V1 therefore does not enforce requester-approver separation of duties.
