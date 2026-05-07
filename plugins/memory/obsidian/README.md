# Obsidian Memory Provider

Read-only local Obsidian vault recall for Hermes.

This provider treats an Obsidian vault as human-readable long-term storage. It can automatically prefetch compact relevant snippets and exposes read-only search/read/recent tools. It does not write, move, rename, or delete files.

## Configure

```bash
hermes config set memory.provider obsidian
# optional if not using ~/vaults/vault-one
export OBSIDIAN_VAULT_PATH="$HOME/vaults/vault-one"
```

Optional environment variables:

- `OBSIDIAN_VAULT_PATH` — vault path. Defaults to `~/vaults/vault-one` if present, then `~/Documents/Obsidian Vault`.
- `OBSIDIAN_MEMORY_MAX_CHARS` — max auto-prefetch context characters, default `1800`.
- `OBSIDIAN_MEMORY_SEARCH_LIMIT` — default auto/search result count, default `5`.

Restart Hermes or `/reset` after changing memory provider config.

## Behavior

Search priority:

1. `mocs/`
2. `3-resources/wiki/`
3. `3-resources/raw/`
4. `daily/`
5. projects/areas/resources

Always excluded:

- `.claude/`
- `.obsidian/`
- `.git/`
- `_attachments/`
- `.trash/`
- `node_modules/`

## Tools

- `obsidian_search` — search markdown notes by keyword/path/title with snippets.
- `obsidian_read` — read a vault-relative markdown note, capped and truncated safely.
- `obsidian_recent` — list recently modified markdown notes.
