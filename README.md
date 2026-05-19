# cc-skills

A TUI for managing which [Claude Code](https://code.claude.com) skills are active, without uninstalling them.

Claude Code loads every skill in `~/.claude/skills/` (or `./.claude/skills/`) at session start — paying context tokens for each one whether you use it or not. There's [no native per-skill toggle yet](https://github.com/anthropics/claude-code/issues/39749). `cc-skills` adds that toggle by treating `skills/` as a directory of **symlinks** pointing at a library you keep elsewhere.

## Install

```bash
pipx install cc-skills          # recommended
# or
pip install --user cc-skills
```

Requires Python 3.10+.

### Local development install

```bash
git clone https://github.com/yourname/cc-skills
cd cc-skills
pip install -e .        # editable install — picks up source changes immediately
```

After the editable install the `cc-skills` command is available in your shell just like a normal install.

If you only want to run it without installing, install the single dependency and invoke `main.py` directly:

```bash
pip install textual
python main.py
```

## Run

```bash
cc-skills           # installed via pipx / pip
# or, from the cloned repository without installing:
python main.py
```

No flags. Everything happens in the TUI.

## Quick start: your first skill

1. **Create a skill folder** in `~/.claude/skills-library/`:

   ```bash
   mkdir -p ~/.claude/skills-library/my-first-skill
   ```

2. **Add a `SKILL.md`** describing what the skill does. Claude Code looks for this file:

   ```bash
   cat > ~/.claude/skills-library/my-first-skill/SKILL.md << 'EOF'
   # My First Skill

   Describe what this skill teaches Claude here.
   EOF
   ```

3. **Launch `cc-skills`**:

   ```bash
   cc-skills
   ```

4. **Choose a scope.** Select `~/.claude` (user-global) to make the skill available in every Claude Code session.

5. **Enable the skill.** Your skill appears under the **library** group. Use the arrow keys to move to it, then press `Space` to check it.

6. **Apply.** Press `a`. `cc-skills` writes a symlink `~/.claude/skills/my-first-skill → ../skills-library/my-first-skill`.

7. **Restart Claude Code.** It re-reads `~/.claude/skills/` at session start and your skill is now active.

> **Tip:** Repeat steps 1–2 for each additional skill you want to add to the library, then toggle them on or off any time from `cc-skills` without touching the actual files.

## What it does

1. You pick a **scope**: `~/.claude` (user-global) or `./.claude` (project).
2. On first run, it sets up:
   - `<scope>/skills-library/` — your own skill folders go here
   - `<scope>/skills/` — symlinks only; this is what Claude Code reads
3. It scans **three source types** and shows them grouped:
   - **library** — `<scope>/skills-library/` (skills you own)
   - **plugins** — `<scope>/plugins/cache/*/` (read-only; Claude Code installs plugins here via `/plugin install`)
   - **local** — any directories you've added via the Sources screen
4. You check which skills to activate. Press `a` to apply — `cc-skills` writes the right symlinks.
5. Restart Claude Code to pick up the change.

## Source types in detail

### Library (`<scope>/skills-library/`)

Real skill folders you put here directly. Best for skills you author or copy in standalone. Each folder must contain `SKILL.md`. **Library skills should be self-contained** — they have no shared `tools/` or `references/` directory above them, so relative `../` references won't work. If you're copying a skill out of a plugin, prefer symlinking instead (see "Shared references" below).

### Plugins (read-only)

`cc-skills` scans `<scope>/plugins/cache/` for installed Claude Code plugins. It automatically picks the latest version per plugin (e.g. `marketing@2.1` over `marketing@1.0`). For each plugin, it discovers skills under `skills/<name>/SKILL.md`, or wherever the plugin's `.claude-plugin/plugin.json` declares.

**`cc-skills` does not call `/plugin` itself.** Install/update/uninstall stays with Claude Code. We just read what's in the cache and offer to expose its skills via our symlink toggle.

A side effect: if you have a plugin enabled in Claude Code AND symlinked through `cc-skills`, its skills load twice. Disable the plugin (`/plugin disable <name>` in Claude Code) so `cc-skills` is the source of truth for which of its skills are active.

### Local paths

Add arbitrary directories from the Sources screen (`o`). Useful for:
- Skills you're developing locally
- A git checkout of someone else's skill repo (e.g. `git clone some-repo ~/skill-repos/marketing`, then add `~/skill-repos/marketing/skills/` as a local source)

For the git-clone case, you get the same "shared references work" benefit as plugins: symlinks point at the real files, so `../tools/foo.md` resolves correctly. Update by `git pull` in the source directory.

## Shared references

Skills often reference sibling files: `../tools/something.md`, `../../references/style-guide.md`. **How this works depends on the source:**

| Source | Shared refs work? |
|---|---|
| Plugin cache | ✅ Symlink points at real plugin tree; relative paths resolve |
| Local path (git clone) | ✅ Same — symlink to real tree |
| Library (you put it there) | ⚠️ Only if you brought the shared files along; library is flat |

**Recommendation**: don't copy skills with cross-references into the library. Add their parent directory as a local source instead, and let `cc-skills` symlink them in.

## Conflicts

If two sources ship a skill with the same name (e.g. library has `cro` and a plugin has `cro`), both appear in the list flagged `⚠CONFLICT`. The active symlink can only point at one. Pick one before applying — `cc-skills` will refuse to apply if you've selected the same name from multiple sources.

## Presets and undo

`cc-skills` automatically snapshots the active set before every apply, giving you **undo** (`u`). The snapshot history is capped at 20 entries.

| Key | Action |
|---|---|
| `a` | Apply selection (rewrite symlinks). Snapshots current state first. |
| `u` | Undo — load the previous applied state into pending. Press `a` to commit. |
| `H` | Browse history — pick any past snapshot to load into pending |
| `s` | Save current selection as a named preset |
| `l` | Load a saved preset into pending |

**Undo doesn't auto-apply.** It loads the prior state into the checkbox grid as a pending change. You see what's about to happen, then press `a` to commit. Same flow as load-preset — one consistent model.

Snapshots are stored in `<scope>/skills-library/.snapshots.json`. Both presets and history live there, distinguished by the `is_auto` flag. They record both row_ids (so you get the exact same source restored when possible) and skill names (fallback when a source has been removed).

Old `.presets.json` files are migrated automatically on first run (renamed to `.presets.json.migrated`).

## Keybindings

| Key | Action |
|---|---|
| `a` | Apply selection (snapshots current state first) |
| `u` | Undo last apply (loads prior state as pending) |
| `s` | Save current selection as preset |
| `l` | Load a saved preset |
| `H` | Open history (browse past snapshots) |
| `o` | Open Sources screen |
| `h` | Repair broken symlinks |
| `/` | Focus filter input |
| `e` / `c` | Expand all / collapse all groups |
| `r` | Refresh (rescan sources) |
| `b` | Back to scope picker |
| `q` | Quit |

## Layout on disk

```
~/.claude/
├── skills-library/              # real skill folders (Claude Code does NOT read this)
│   ├── my-custom-skill/SKILL.md
│   ├── .sources.json            # list of local-path sources
│   └── .snapshots.json          # presets + auto-history (replaces .presets.json)
├── skills/                      # symlinks; Claude Code reads this
│   ├── my-custom-skill -> ../skills-library/my-custom-skill
│   ├── cro -> ../plugins/cache/marketing@2.1/skills/cro
│   └── react-patterns -> /home/me/work/some-repo/skills/react-patterns
└── plugins/cache/               # owned by Claude Code; cc-skills reads, never writes
    └── marketing@2.1/...
```

## Safety

- `cc-skills` only ever writes symlinks. It never modifies real skill files.
- On startup, if it finds real directories in `skills/` (from a previous non-`cc-skills` install), it offers to migrate them into `skills-library/` and replace with symlinks. Non-destructive.
- After a plugin version bump, symlinks pointing at the old version path may dangle. `cc-skills` detects this and offers to repair (`h`) — re-pointing at the current version, or dropping the symlink if no replacement exists.

## Why not just use the plugin manager?

Claude Code's `/plugin enable/disable` works at the **plugin** level. A plugin like [`marketingskills`](https://github.com/coreyhaines31/marketingskills) ships ~35 skills — `cc-skills` lets you turn on 3 of them without enabling all 35.

## License

MIT — see [LICENSE](LICENSE).
