# Security policy

## Reporting a vulnerability

Please use a private vulnerability report through the repository host. Include
the affected version, a concise reproduction, and the impact. Do not include
API keys or other secrets in an issue or pull request.

This project treats screening as a filter, not a security boundary. Keep
authorization, secret handling, and consequential actions in the surrounding
application.

Requests are bounded by the serialized state-and-questions budget, including
the full candidate input and each candidate window. Validation failures return
at most ten detail entries, each message is truncated to 160 characters, and
each `loc` part is truncated to 64 characters. A detail `loc` can contain
caller-supplied mapping keys, so do not put secrets in state, label, proposition,
candidate, or question names.
