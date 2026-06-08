## What & why

<!-- What does this change do, and which requirement/task does it satisfy? -->
Epic / Task:
Module(s):

## Golden rule (reuse before build)

<!-- For any new module/library: 2–3 lines. What did you pick, why did it beat the
     alternatives, and what is its license? Recorded in docs/stack.md. N/A for pure
     refactors. -->

## Definition of Done

- [ ] Code + tests merged; CI green
- [ ] Tests follow the module's harness profile
- [ ] Module spec updated (a behavior change with no spec update is not done)
- [ ] Migrations have a verified rollback (if schema changed)
- [ ] `docs/mcp.md` updated (if an MCP tool changed)
- [ ] Security review requested (if touching protocol / gateway / grants)
