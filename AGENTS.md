# TeamUp

Before making changes, developers and AI agents must read this file and the [development protocol](docs/protocols/README.md), then use the [documentation index](docs/README.md) to find the affected contracts. The following invariants take precedence over other design and implementation guidance.

1. **Use public interfaces.** Components communicate only through published entry points and protocols. Do not import another component's internals, manipulate its data directly, or bypass its authorization checks. Keep dependencies acyclic.
2. **Give each piece of state one owner and one authorized write path.** Caches, projections, and connection state cannot determine business authorization. The state owner enforces constraints that must hold together within the same boundary.
3. **Update the contract before changing code.** Before changing fields, permissions, failure semantics, or compatibility, revise the public contract and get agreement from the provider and the owners of affected callers. Callers may not extend the contract unilaterally.
4. **Keep the composition root focused on wiring.** It loads configuration, constructs production components, and connects public interfaces. Business logic belongs in the responsible component, and test doubles belong in tests.
5. **Make boundaries verifiable.** Use dependency checks to enforce allowed dependency directions and contract tests to verify authorization, persistence, and concurrent behavior. Preserve these boundaries when fixing defects.

Keep enduring rules here. Document the repository layout, technology choices, operating procedures, and progress under `docs/`.

## Documentation languages

Write Markdown documentation under `docs/` in clear, natural English. Publish tutorials, hands-on guides, introductions, and explainers for human readers in both English (`<name>.md`) and Simplified Chinese (`<name>.zh-CN.md`). Keep all architecture documentation under `docs/architecture/` in English only, along with technical references such as contracts, specifications, and implementation plans.

Add or update both editions of a bilingual document in the same change. Link the editions near the top of each page. In the Chinese edition, link to Chinese editions where available. Keep commands, API names, and identifiers the same in both editions.
