**Workstream:** WS<N> <name> (docs/workstreams.md)

**What changed**

**How it was checked** (tests added, live run, screenshot)

- [ ] `cd agent; python -m pytest tests -q -m "not live"` passes
- [ ] Only my workstream's paths changed (or the owner of a shared file agreed)
- [ ] No contract changes, or this PR is *only* a contract change and the team knows
- [ ] No secrets, account IDs or `.env` files
- [ ] Main still works with every switch on `local`
