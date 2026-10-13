---
name: GitHub source-control authentication
description: GitHub integration metadata can report a healthy connection while repository creation through the CLI is unauthenticated.
---

Check real GitHub command/API access before creating or pushing a repository. An integration may appear active and `addIntegration` may report success while `gh auth status` still has no authenticated account.

**Why:** repository creation was blocked despite the connection metadata appearing healthy.

**How to apply:** do not ask the user for a token. Have them connect GitHub through Replit's Git interface/OAuth, then retry after access is confirmed.
