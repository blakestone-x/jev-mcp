# Changelog

## 0.2.1 - Safer boundaries and bounded matching

- Kept malformed and invalid tool calls inside the safe 400 envelope, with bounded error details; no input values are
  echoed, and mapping keys that appear in `loc` are truncated.
- Added a bounded serialized request budget across state and questions, with a one-release alias for the previous
  environment variable name; matching also caps each candidate window and the full candidate input.
- Bounded matching fan-out and window size, added a wall-clock deadline, abstention probability, per-window existence scores, and honest no-match bands.
- Validated provider choices against submitted labels, score legend keys, and question criteria; out-of-set answers
  now use a safe 502 response.
- Hardened registration helpers so apply mode requires `TYPESAFE_API_KEY`, rejects unsafe key characters, backs up
  existing configuration, and writes it with restrictive permissions.
- Added read-only CI permissions, pinned dependency upper bounds, and a history-aware vocabulary check that is
  opt-in for local development and enabled in CI.
- Documented the two-call ensemble timeout and the security boundary around plaintext key registration.
- Corrected the batching guidance, removed the obsolete question alias, refreshed packaging metadata, and expanded offline, concurrency, ensemble, matching, vocabulary, and wheel tests.

## 0.2.0 - Initial public release

- Published the seven-tool MCP surface.
- Added typed classification, scoring, checks, matching, screening, and health.
- Added offline tests, examples, recipes, registration helpers, and CI.

## Known limitations

- Framework-level argument type mismatches, such as a string where an integer is expected or a missing required
  argument, use the framework's error text rather than the Jev envelope; echoed values are truncated by the input
  validator. Unknown extra arguments are ignored.
