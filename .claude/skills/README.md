# Claude Code skills for Ripster

These are **maintenance & development skills** for [Claude Code](https://claude.com/claude-code)
contributors working on Ripster. Each `*/SKILL.md` encodes hard-won knowledge about a
subsystem — the gotchas, the verification steps, the "this looks broken but isn't" traps —
so an agent (or a human) fixes a class of problem correctly the first time instead of
re-deriving it.

They load automatically when you run Claude Code inside this repo; invoke one directly with
`/<skill-name>` or let the agent pick the relevant one by its description.

**Scope:** these cover Ripster's own engines, player, radar, packaging and Windows/WebView2
quirks. Owner-private automation (personal bots, tunnels, unrelated side-projects) is **not**
included, and every skill here has been scrubbed of personal data — where a skill needs a
personal value (your email for a leak-check, your tunnel host, your bot owner-id) it uses a
`<placeholder>` for you to fill in locally.

Nothing here contains real credentials, tokens, accounts or personal identifiers.
