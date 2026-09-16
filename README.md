# jev-mcp

Jev provides typed judgments over a closed answer set in about 130 ms, with no text generation, through [TypeSafe](https://typesafe.ai).

## Install

Set `TYPESAFE_API_KEY` in the environment used by your MCP client, then run:

```powershell
uvx --from git+https://github.com/blakestone-x/jev-mcp@v0.2.1 jev-mcp
```

Claude Code:

```text
claude mcp add --scope user jev -e TYPESAFE_API_KEY=... -- uvx --from git+https://github.com/blakestone-x/jev-mcp@v0.2.1 jev-mcp
```

Codex (`~/.codex/config.toml`):

```toml
[mcp_servers.jev]
command = "uvx"
args = ["--from", "git+https://github.com/blakestone-x/jev-mcp@v0.2.1", "jev-mcp"]
env = { TYPESAFE_API_KEY = "..." }
```

Cursor or another MCP client:

```json
{
  "mcpServers": {
    "jev": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/blakestone-x/jev-mcp@v0.2.1", "jev-mcp"],
      "env": { "TYPESAFE_API_KEY": "..." }
    }
  }
}
```

Replace `...` with the key value. Passing it through the client environment keeps it out of tool arguments and request state.

The `scripts/register.sh` helper requires Bash 4+; do not run it with `sh`. Apply mode reads
`TYPESAFE_API_KEY` from the environment and refuses to change client configuration when it is unset. `--apply` writes
the plaintext key into `~/.codex/config.toml` and the Claude Code user config. The key is briefly visible in the process
list while `claude mcp add` runs because that command receives it as an argument. On Windows, use
`scripts/register.ps1`; it restricts the resulting configuration files to the current user and keeps a `.bak` copy
before rewriting an existing Codex configuration.

The install examples pin the public `v0.2.1` tag. Omitting the tag tracks the repository's default branch instead.

## Worked example

This call classifies one state containing five support-ticket texts against one label map. It returns one label
for the combined state; send one request per ticket when you need an individual label for each ticket:

```json
{
  "state": [
    "The password reset link expired before I could use it.",
    "I do not recognize the charge on my statement.",
    "My order arrived two days late.",
    "The export button shows a blank screen.",
    "Please add a dark theme."
  ],
  "labels": {
    "account_access": "Login, password, or sign-in problems.",
    "billing": "Charges, invoices, or payment questions.",
    "delivery": "Late, missing, or damaged deliveries.",
    "bug": "Unexpected product behavior.",
    "feedback": "Suggestions or general comments."
  },
  "question": "Which category best fits the combined state of these five support tickets?",
  "add_other": true
}
```

The response is an ordinary MCP structured result with a stable envelope:

```json
{
  "ok": true,
  "model": "jev-latest",
  "answers": {
    "choice": "account_access",
    "confidence": 0.91,
    "probabilities": {
      "account_access": 0.91,
      "billing": 0.03,
      "delivery": 0.02,
      "bug": 0.02,
      "feedback": 0.01,
      "other": 0.01
    },
    "band": "act"
  },
  "usage": {
    "input_tokens": 420,
    "output_tokens": 0,
    "est_cost_usd": 0.00001764
  },
  "latency_ms": 130.0
}
```

## Tools

| tool | use it for | not for |
| --- | --- | --- |
| `jev_ask` | Your own Choice, Score, and Noul question map | Text generation or numeric extraction |
| `jev_classify` | Selecting one label from a closed list | Open-ended writing or ranking without labels |
| `jev_score` | Placing state on an ordered rubric | Returning a number extracted from text |
| `jev_check` | Independent yes/no propositions | Treating a probability as proof |
| `jev_match` | Matching a query to candidates with abstention | Assuming the closest candidate is genuine |
| `jev_screen` | Filtering instruction-like or untrusted text | A security boundary or authorization decision |
| `jev_health` | Listing served models and measuring round-trip latency | Evaluating application content |

## Design principles

- The key stays in the environment and never in the agent's context.
- All policy thresholds live in one file: `jev_mcp/questions.py`.
- Confidence is distribution concentration, not correctness.
- Screening is a filter, not a security boundary.
- Gate consequential decisions on the Score.
- Order-ensemble turns disagreement into a cheap review signal.
- Matching includes abstention through the `exists` Noul.

## Profiles and recipes

`examples/triage.questions.json` and `examples/route.questions.json` are generic, complete `jev_ask` request examples. They contain a `state` object and a `questions` map in the wire shape accepted by `jev_ask`; replace `state` with your own data and send the whole object as the tool arguments. Jev intentionally does not load profiles for you.

```python
import json

with open("examples/triage.questions.json", encoding="utf-8") as file:
    request = json.load(file)

result = await session.call_tool("jev_ask", request)
```

See [RECIPES.md](RECIPES.md) for comparison, reference-in-state, descriptions, batching, calibration, and order-ensemble patterns.

## What we measured

The measurements below come from a field-service company's production data.

| measurement | result |
| --- | --- |
| Clean labels at confidence 0.8 to 1.0 | 95% agreement |
| One-line label descriptions | 81.0% agreement; 1,689 tokens/request |
| Rich label descriptions | 84.5% agreement; 4,745 tokens/request |
| Reversing criteria order | 32 of 200 choices flipped; flipped confidence averaged 0.42 |
| Candidate matching | 8 of 11 picks matched when a candidate existed; misses had low `exists` |
| Amount comparison | 100% on 64 cases where displayed amounts differed |
| Past-date comparison | 100% for “is it past”; 98% on a four-level overdue Score |

## Limits

The default serialized request budget is 120,000 characters for the state and questions sent to the service. Set
`JEV_MCP_MAX_REQUEST_CHARS` to use a different positive limit. `JEV_MCP_MAX_STATE_CHARS` remains accepted as a
compatibility alias for one release. For `jev_match`, the budget applies to the full candidate input and to each
serialized candidate window.
The service provider has not published rate limits. A Noul carries evidence strength, not confidence. Jev is early access:
validate thresholds and outputs against your own data, and keep consequential actions behind your normal review controls.

`jev_match` accepts a `window` from 20 through 254 and up to 2,000 candidates by default. Set
`JEV_MCP_MATCH_MAX_CANDIDATES` to change the candidate cap. For `n` candidates, the normal billed request count is
`ceil(n / window) + 1`: one request per window plus one finalist-selection request. A single-window call may need
only its initial request, and a call with no finalists may omit the extra request. `exists` is the maximum of the
per-window values, so more windows can inflate it; inspect `windows` and `exists_by_window` too. The match deadline is
45 seconds by default and can be changed with `JEV_MCP_MATCH_DEADLINE_S`.

Set `JEV_MCP_MODEL` to select the model; it defaults to `jev-latest`. The timeout contract is: a single tool call can take up to about 30 seconds when the service is degraded; `jev_classify(ensemble=true)` can take about 60 seconds because it makes two sequential evaluations; `jev_match` can take up to its deadline. SDK attempts use an 8-second HTTP timeout and a 20-second retry budget.

## License

MIT. See [LICENSE](LICENSE).
