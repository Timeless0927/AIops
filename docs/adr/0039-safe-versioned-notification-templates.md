# Notification templates are safe, versioned presentations

Status: accepted

Notification Engine provides a built-in template for every supported event and Provider combination. An administrator may copy a built-in template and edit its title, Markdown body, color, and button label; SMTP templates additionally define a subject, and their Markdown is rendered into sanitized HTML plus plain text. Templates accept only documented, event-specific `{{field}}` variables and do not support loops, conditions, functions, scripts, arbitrary HTML, recipients, destinations, or credentials.

A template draft must be previewed or sent as a test before it can be enabled. A Notification Route may select an enabled compatible template and otherwise uses the built-in template. Each Notification Delivery freezes the exact template version and final rendered content used for that attempt so later template edits cannot change delivery history, retries, or audit evidence.
