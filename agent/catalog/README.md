# catalog/ (WS5, Aaron; agronomy reviewed by Caleb)

- `conditions.json`: one entry per `ConditionId` with prior odds by crop, stage and irrigation, and a
  weight per `weight_key` (the keys specialists put on `EvidenceItem`s), each with a one-line cited reason.
- `loss_tables.json`: yield-loss fractions by condition, growth stage and severity (hail defoliation by
  stage, water stress around silking), each row with its source.
Include a `version` field; `ScoreResult.catalog_version` records it.
