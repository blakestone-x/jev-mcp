# Jev recipes

These patterns keep the decision in a small, typed answer set and leave any consequential action to the caller.

## Compare in state

Put both values in `state` and ask for the comparison. The service is good at deciding what is larger or later; it is not a replacement for a parser when the caller needs an exact number back.

```json
{
  "state": {
    "quoted_amount": "$1,500.00",
    "approved_limit": "1.50k",
    "today": "2026-09-16",
    "due_date": "09/10/2026"
  },
  "questions": {
    "over_limit": {
      "type": "noul",
      "instructions": "The quoted amount is greater than the approved limit.",
      "criteria": {
        "true": "The quote is greater than the limit.",
        "false": "The quote is equal to or below the limit."
      }
    },
    "past_due": {
      "type": "noul",
      "instructions": "The due date is before today.",
      "criteria": {
        "true": "The due date has passed.",
        "false": "The due date is today or in the future."
      }
    }
  }
}
```

Across 64 amount cases with visibly different values, the comparison was right 100% of the time. A four-level distance Score was exact 90% of the time, with misses limited to hidden sub-dollar ties. Dates were right 100% of the time for the simple “is it past” question and 98% for a four-level overdue Score.

## Put a reference table in state

Give the decision a small catalog in the same state and ask a second Noul whether it used the reference.

```json
{
  "state": {
    "item": {"maker": "Northwind", "part": "AX-410"},
    "reference": {
      "AX-": "air-moving equipment",
      "BX-": "water-moving equipment"
    }
  },
  "questions": {
    "equipment_type": {
      "type": "choice",
      "instructions": "Which type best fits the item, using the reference when it helps?",
      "criteria": {
        "air_moving": "Equipment that moves or conditions air.",
        "water_moving": "Equipment that moves or conditions water.",
        "other": "A different equipment type."
      }
    },
    "used_reference": {
      "type": "noul",
      "instructions": "The reference table materially informed the equipment type answer.",
      "criteria": {
        "true": "The answer used a relevant entry from the reference.",
        "false": "The answer did not use a relevant reference entry."
      }
    }
  }
}
```

On 300 items, adding a hand-written prefix catalog raised agreement from 30% to 39% and raised the number clearing a 0.9 confidence gate from 37 to 88. The `used_reference` Noul fired on 97 rows, and 70% of those calls were right. The reference needs to be real and maintained; a thin catalog can add the wrong confidence.

## Descriptions buy coverage, not precision

Richer label descriptions mostly help more answers clear the confidence gate:

| criteria | agreement | agreement at confidence >= 0.9 | tokens/request |
| --- | ---: | ---: | ---: |
| Bare label names | 64.5% | 91.5% inside the band | 735 |
| One-line `what` | 81.0% | 92.8% inside the band | 1,689 |
| `what`, `not_for`, and `examples` | 84.5% | 94.6% inside the band | 4,745 |

Start with one-line `what` descriptions. Add the other fields to the confusing pairs, where extra coverage pays for the tokens.

## Batch related questions

Batch questions that share a state. Adding two related Nouls changed 1 of 100 choices; adding 20 changed 3 of 100, moved the top probability by 0.016 on average, added 6% tokens, and added 26 ms. Batching is nearly free and nearly independent, but keep a decision that cannot drift in its own request.

## Read a flat calibration curve as a label warning

On clean labels, agreement was 95% at confidence 0.8 to 1.0, 71% at 0.7 to 0.8, and near a coin flip below 0.6. A vendor-line label curve was flat because the labels were wrong as often as the judgment. A flat calibration curve means the labels are bad or ambiguous; it is not automatically a model failure.

## Use order-ensemble as abstention

Set `ensemble: true` on a classification that gates a consequential action. Jev makes a second request with the labels in a different order. Reversing criteria flipped 32 of 200 choices; the flipped items averaged 0.42 confidence, while stable items averaged 0.81. If the two choices disagree, the result is always in the `review` band.

## Match with abstention

`jev_match` pairs a Choice over candidates with an `exists` Noul. The Choice supplies the best candidate; `exists` answers whether any candidate is genuinely a match rather than merely the closest. Use `band == "match"` only when `exists >= 0.70` and Choice confidence is at least 0.50. A middle `exists` value is `review`; a low value is `none`.

Candidates are evaluated in windows of 20 to 254 entries. The success answer includes `windows` and
`exists_by_window`; `exists` remains the maximum of those values. More windows create more chances for a high
window-level score, so the maximum can be inflated as the candidate count grows. The normal billed request count is
`ceil(n / window) + 1`, including the finalist-selection request when more than one window produces a finalist.
