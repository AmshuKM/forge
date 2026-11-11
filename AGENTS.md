# Instructions for AI agents working in this repository

This project is Forge: a system that builds, tests and improves small apps behind gates. Changes from agents wait for the owner's approval.

If Forge started you to build an app, its prompt names an open change: work only on that change with the tools it lists, and do not call `start_change` or `submit_change`.

To build a new app or change an existing one, **use the `forge` MCP tools, not direct edits to this folder**:

1. Call `guide` once. It describes the app contract and file layout.
2. Call `status`, then `start_change` with the app name. It returns a private folder.
3. Edit files only inside that folder, and only `apps/<app>/...` and `prompts/<app>_*.md`.
4. Call `check_change` until tests and golden cases pass, then `submit_change`.
5. Stop. Tell the user the change is waiting for their approval in the dashboard (`python -m forge.cli serve`).

Do not edit `apps/`, `prompts/` or `config/` on the main branch directly, and do not merge, tag or approve anything: releases are the owner's decision.

Edit `forge/` or `tests/` directly only when the user asks you to change Forge itself. Then run `python -m unittest discover -s tests`.
