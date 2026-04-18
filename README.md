# Code Mapper

Parse any Python codebase into a structured map of nodes, edges, and logic blocks. Deterministic AST analysis, not LLM. Optional AI tiers for targeted bug verification.

## Install

```bash
pip install -e .                # core (AST + linter + xref)
pip install -e ".[clustering]"  # + networkx for Louvain logic-block clustering
pip install -e ".[ai]"          # + httpx for Ollama-backed Tier 2 questioner
pip install -e ".[claude]"      # + Claude API Tier 3 deep review
pip install -e ".[all]"         # everything above
```

## Usage

```bash
# Tier 1 (fast, free, deterministic)
python -m code_mapper /path/to/project --lint --xref

# Tier 2 (targeted yes/no verification via Ollama 7B — needs local ollama running)
python -m code_mapper /path/to/project --lint --xref --verify --ollama-url http://localhost:11434

# Tier 3 (Claude API synthesis on top of Tier 2)
ANTHROPIC_API_KEY=... python -m code_mapper /path --lint --xref --verify --claude

# Open-ended AI review (older, kept for exploration)
python -m code_mapper /path --ai --ollama-url http://localhost:11434
```

Output: `repo-map.json` in the project root. Contains nodes (files/classes/functions), edges (imports, calls, routes), logic blocks (clustered), and findings per tier.

## Lint rules

See `src/code_mapper/linter.py`. 13 rules as of 2026-04-18, including:

- `DEAD_IMPORT`, `UNUSED_PARAM`, `UNUSED_CONSTANT`
- `FUNCTION_SCOPED_IMPORT_LEAK` — function A imports X locally, function B uses X → runtime NameError
- `UNPACK_SIZE_MISMATCH` — `a, b = func()` where func returns N-tuple with N≠2
- `SWALLOWED_EXCEPTION`, `SELF_ASSIGN_IN_EXCEPT`, `UNREACHABLE_CODE`
- `GOD_FUNCTION`, `GOD_FILE`, `GOD_FUNCTION_COMPLEXITY`, `CIRCULAR_DEPENDENCY`
- `LIST_POP_ZERO`, `UNGUARDED_JSON`, `UNGUARDED_FILE_OPEN`, `MAGIC_NUMBER_VS_CONSTANT`

All lint rules respect `# noqa` and `# noqa: RULENAME,F401` line comments for suppression.

## Why

Every existing static analysis tool trades off between precision and breadth. CM's take: deterministic structural analysis (AST) finds the easy stuff fast and free, then uses that as input to targeted yes/no questions for an LLM (rather than open-ended "review this"). The structural signals tell the LLM exactly what to look at and why.

## Tiers

| Tier | Engine | Cost | Speed | What it catches |
|------|--------|------|-------|-----------------|
| 1 | AST parser + linter | Free | Instant | Dead imports, unreachable code, god functions, routes, tables, stubs |
| 1.5 | Cross-reference | Free | Instant | Cross-file unused symbols, duplicate functions |
| 2 | Questioner + 7B Ollama | Free (local GPU) | ~30s | Targeted verification: temporal ordering, constraints, doc-vs-code |
| 3 | Claude API | $$ | ~15s | Cross-file synthesis (usually adds 0 beyond Tier 2) |

## License

MIT
