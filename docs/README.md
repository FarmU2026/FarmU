# docs/

FieldWatch was planned in detail before the hackathon and built against that plan during it. These documents are kept as they were written, so some describe the target rather than what shipped. **[`decisions.md`](decisions.md) records where the build deliberately differs, and overrides the rest.**

| Document | What it is |
| --- | --- |
| [`decisions.md`](decisions.md) | Build decisions made at the event (CDK instead of Terraform, region, models, approvals, cut order). Start here |
| [`system-design.md`](system-design.md) | The architecture: containers, agents, data, cost, security, observability |
| [`core-features.md`](core-features.md) | What the farmer sees and does, and the UI rules |
| [`data-pipelines.md`](data-pipelines.md) | Every dataset and pipeline: onboarding context, imagery, hazards, knowledge |
| [`implementation-specs.md`](implementation-specs.md) | Contracts, tool shapes, data model, error codes |
| [`build-plan.md`](build-plan.md) | The ticket plan the build followed |
| [`workstreams.md`](workstreams.md) | How the build was split between three people and parallel AI coding agents |
| [`spike-findings.md`](spike-findings.md) | What pre-event experiments proved about the AWS services and data sources |
| [`hero-scenes.md`](hero-scenes.md) | Demo scene selection notes |
| [`images/`](images/) | Screenshots used in the main README |

Not built from the plan (see the main README's limitations): the Bedrock knowledge base, photo intake, voice, Sentinel-1 radar, and AgentCore Policy (Cedar).
