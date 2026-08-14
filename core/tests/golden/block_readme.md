<!-- from: <ROOT>/home/.tstdesk/AGENTS.md (user global) -->
[GLOBAL-MAIN]
<!-- from: <ROOT>/workspace/CLAUDE.md (workspace, claude fallback) -->
[WS-FALLBACK]
<!-- from: <ROOT>/workspace/docs/imported.md (imported) -->
---
title: pinned literal — imported files keep frontmatter
---
[IMPORTED-DOC]
```
@phantom.md
```
<!-- from: <ROOT>/workspace/docs/nested/deep.md (imported) -->
[IMPORTED-DEEP]
<!-- from: <ROOT>/workspace/chain/a.md (imported) -->
[CHAIN-A]
<!-- from: <ROOT>/workspace/chain/b.md (imported) -->
[CHAIN-B]
<!-- from: <ROOT>/workspace/chain/c.md (imported) -->
[CHAIN-C]
<!-- from: <ROOT>/workspace/chain/d.md (imported) -->
[CHAIN-D]
<!-- max import depth 4 exceeded: a.md -> b.md -> c.md -> d.md -> e.md -->
<!-- from: <ROOT>/workspace/cycle/x.md (imported) -->
[CYCLE-X]
<!-- from: <ROOT>/workspace/cycle/y.md (imported) -->
[CYCLE-Y]
<!-- import cycle detected: x.md -> y.md -> x.md -->
<!-- from: <ROOT>/workspace/.tst/rules/always.md (rules) -->
[RULE-ALWAYS]
<!-- from: <ROOT>/workspace/.tst/rules/standards-code.md (rules) -->
[RULE-STANDARDS]
<!-- from: <ROOT>/workspace/src/AGENTS.md (nested: src) -->
[NESTED-SRC]
<!-- from: <ROOT>/workspace/tests/CLAUDE.md (nested, claude fallback: tests) -->
[NESTED-TESTS-FALLBACK]