# mcp/

MCP servers that live in this repository but are **not part of the TST Desk app**.

Everything here is standalone: its own project, its own dependencies, its own test
suite, usable from any MCP client. Nothing under `mcp/` is imported by `core/`,
`shell/` or `ui/`, and the app's CI does not build or test it — `ci.yml` scopes
each job to `core`, `shell` or `ui` with `working-directory`, so this directory is
outside all of them.

That detachment is deliberate. TST Desk loads MCP servers you list under `mcp.servers`
in `config.yaml` (Settings → MCP servers), and one of these can be listed there like any
other; none is bundled or started by the app.

| Server | Purpose | Platforms |
|--------|---------|-----------|
| [`tst-cu-mcp`](tst-cu-mcp/) | Computer use: screenshots (vision) plus mouse and keyboard control, over stdio. | macOS, Windows |

Each server has its own README covering setup, client configuration, and its
platform-specific limits.
