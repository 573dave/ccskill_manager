#!/usr/bin/env python3
"""
cc-skills: TUI to manage which Claude Code skills are active via symlinks.

Architecture:
  - Active dir:  <scope>/skills/         (symlinks only; Claude Code reads this)
  - Sources are SCANNED, not owned:
      * Library    — <scope>/skills-library/      (real skill folders you own)
      * Plugins    — <scope>/plugins/cache/*/     (read-only, owned by Claude Code)
      * Local      — arbitrary paths the user adds, tracked in .sources.json

Run with no arguments. Everything happens in the TUI.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Protocol, TypeVar

try:
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical
    from textual.screen import ModalScreen, Screen
    from textual.widgets import (
        Button,
        Collapsible,
        Footer,
        Header,
        Input,
        Label,
        ListItem,
        ListView,
        SelectionList,
        Static,
    )
    from textual.widgets.selection_list import Selection
except ImportError:
    sys.stderr.write(
        "Missing dependency: textual\nInstall with: pip install --user textual\n"
    )
    sys.exit(1)


# ---------- Constants ----------

LIBRARY_DIRNAME = "skills-library"
ACTIVE_DIRNAME = "skills"
PLUGINS_CACHE_DIRNAME = "plugins/cache"
SOURCES_FILE = ".sources.json"        # in library; tracks local-path sources only
PRESETS_FILE = ".presets.json"        # legacy; auto-migrated on first run
SNAPSHOTS_FILE = ".snapshots.json"    # current: presets + undo history
HISTORY_CAP = 20                      # auto-snapshots kept


# ---------- Data model ----------

@dataclass
class Skill:
    name: str             # skill directory name (also the symlink name)
    path: Path            # absolute path to the skill dir on disk
    description: str = ""
    tags: list[str] = field(default_factory=list)
    origin: str = "library"           # display label, e.g. "plugin: marketing@2.1"
    origin_kind: str = "library"      # library | plugin | local
    active: bool = False              # symlinked into active dir
    active_target: Path | None = None # what the symlink currently points at
    conflict_with: list[Skill] = field(default_factory=list)


@dataclass
class Snapshot:
    """Named selection state. Drives both presets and undo history.

    Stores BOTH row_ids (for exact source recovery) AND skill names (fallback
    when sources move). Loading prefers row_ids; falls back to name + kind_pref
    if a row_id no longer exists.
    """
    name: str
    created_at: str                       # ISO 8601
    is_auto: bool = False                 # True for undo history, False for user presets
    rows: list[str] = field(default_factory=list)   # ["library::cro", ...]
    names: list[str] = field(default_factory=list)  # ["cro", "copywriting", ...]

    @classmethod
    def now(cls, name: str, rows: Iterable[str], names: Iterable[str], is_auto: bool = False) -> Snapshot:
        # Local time with TZ offset; unambiguous across DST and machine moves.
        ts = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
        return cls(
            name=name,
            created_at=ts,
            is_auto=is_auto,
            rows=sorted(set(rows)),
            names=sorted(set(names)),
        )

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> Snapshot:
        return cls(
            name=d.get("name", ""),
            created_at=d.get("created_at", ""),
            is_auto=bool(d.get("is_auto", False)),
            rows=list(d.get("rows", [])),
            names=list(d.get("names", [])),
        )


@dataclass
class SnapshotLoadResult:
    """Result of loading a Snapshot into pending state."""
    resolved_count: int
    missing: list[str]                              # skills no longer available anywhere
    substituted: list[tuple[str, str, str]]         # (name, old_origin, new_origin)


class SnapshotStore:
    """Reads/writes .snapshots.json. Handles migration from .presets.json.

    Data is read once at __init__ and cached. All mutations go through _save()
    which rewrites the file. Migration is attempted once, idempotently.
    """

    def __init__(self, library: Path):
        self.library = library
        self.path = library / SNAPSHOTS_FILE
        self.legacy_path = library / PRESETS_FILE
        # Surfaced to the UI via consume_migration_warning() exactly once
        self._migration_warning: str | None = None
        self._data = self._init_data()

    def _init_data(self) -> dict:
        """Read or migrate. Called exactly once at __init__."""
        self._maybe_migrate_legacy()
        if not self.path.exists():
            return {"presets": {}, "history": []}
        try:
            data = json.loads(self.path.read_text())
        except json.JSONDecodeError:
            # Don't silently nuke a corrupt file — back it up so the user can recover
            local = datetime.now(timezone.utc).astimezone()
            stamp = local.strftime("%Y%m%d-%H%M%S")
            backup = self.path.with_suffix(f".json.corrupt-{stamp}")
            try:
                self.path.rename(backup)
                self._migration_warning = (
                    f"⚠ .snapshots.json was corrupt; backed up to {backup.name}. "
                    "Starting with empty presets/history."
                )
            except OSError:
                pass
            return {"presets": {}, "history": []}
        except OSError:
            return {"presets": {}, "history": []}
        data.setdefault("presets", {})
        data.setdefault("history", [])
        return data

    def _maybe_migrate_legacy(self) -> None:
        """One-shot migration. Idempotent: returns immediately if either:
          - .snapshots.json already exists (migration previously succeeded), or
          - .presets.json is absent (nothing to migrate).
        If rename of the legacy file fails after writing .snapshots.json, we
        surface a warning instead of silently leaving both files in place.
        """
        if self.path.exists() or not self.legacy_path.exists():
            return
        try:
            old = json.loads(self.legacy_path.read_text())
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(old, dict):
            return
        presets = {}
        for name, skill_names in old.items():
            if not isinstance(skill_names, list):
                continue
            snap = Snapshot.now(name=name, rows=[], names=skill_names, is_auto=False)
            presets[name] = snap.to_dict()
        # Write new file first (atomically); only then remove the legacy file.
        # We unlink rather than rename — the new file is durable on disk at this
        # point, so keeping a copy of the legacy file adds risk of orphan-state
        # confusion without any real recovery value.
        _atomic_write_text(
            self.path,
            json.dumps({"presets": presets, "history": []}, indent=2) + "\n",
        )
        try:
            self.legacy_path.unlink()
        except OSError as e:
            self._migration_warning = (
                f"⚠ migrated presets to .snapshots.json but couldn't remove "
                f".presets.json ({e}); please delete it manually to avoid confusion."
            )

    def consume_migration_warning(self) -> str | None:
        """Returns the migration warning once, then clears it."""
        w, self._migration_warning = self._migration_warning, None
        return w

    def _save(self) -> None:
        _atomic_write_text(self.path, json.dumps(self._data, indent=2) + "\n")

    # --- presets ---

    def list_presets(self) -> list[Snapshot]:
        return [Snapshot.from_dict(d) for d in self._data.get("presets", {}).values()]

    def get_preset(self, name: str) -> Snapshot | None:
        d = self._data.get("presets", {}).get(name)
        return Snapshot.from_dict(d) if d else None

    def save_preset(self, snap: Snapshot) -> None:
        self._data["presets"][snap.name] = snap.to_dict()
        self._save()

    def delete_preset(self, name: str) -> bool:
        if name in self._data["presets"]:
            del self._data["presets"][name]
            self._save()
            return True
        return False

    # --- history (undo) ---

    def push_history(self, snap: Snapshot) -> None:
        """Push to history, capped at HISTORY_CAP (FIFO)."""
        hist: list = self._data.get("history", [])
        if hist:
            last = Snapshot.from_dict(hist[-1])
            if set(last.rows) == set(snap.rows):
                return
        hist.append(snap.to_dict())
        if len(hist) > HISTORY_CAP:
            hist = hist[-HISTORY_CAP:]
        self._data["history"] = hist
        self._save()

    def peek_history(self) -> Snapshot | None:
        """Look at the most recent entry without removing it (#5)."""
        hist = self._data.get("history", [])
        return Snapshot.from_dict(hist[-1]) if hist else None

    def list_history(self) -> list[Snapshot]:
        return [Snapshot.from_dict(d) for d in self._data.get("history", [])]


def _atomic_write_text(path: Path, text: str) -> None:
    """Write text to a file atomically: write to a sibling tempfile, then rename.

    Uses tempfile.mkstemp so parallel processes don't collide on a fixed `.tmp`
    name. os.replace() is atomic on POSIX and near-atomic on Windows. Prevents
    truncated/corrupt files if the process is killed mid-write.
    """
    import tempfile
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


T = TypeVar("T")


def _sample_with_more(items: list[T], n: int = 3, render: Callable[[T], str] = str) -> str:
    """Render a short sample of items with a "(+N more)" suffix when truncated."""
    head = ", ".join(render(x) for x in items[:n])
    return head + (f" (+{len(items)-n} more)" if len(items) > n else "")


def _humanize_ts(iso: str) -> str:
    """Render an ISO 8601 timestamp as a humanized 'X ago' string. Falls back to
    the raw value if parsing fails."""
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
    secs = (now - dt).total_seconds()
    if secs < 0:
        return iso  # clock skew; don't lie
    if secs < 60: return "just now"
    if secs < 3600: return f"{int(secs/60)}m ago"
    if secs < 86400: return f"{int(secs/3600)}h ago"
    if secs < 86400 * 7: return f"{int(secs/86400)}d ago"
    return dt.strftime("%b %d")


def parse_skill_md(path: Path) -> tuple[str, list[str]]:
    """Extract description + tags from SKILL.md YAML frontmatter.
    Note: simple one-line parser. Multi-line YAML values (| or >) won't parse;
    descriptions will come back empty in that case rather than crashing."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            text = f.read(8192)   # frontmatter is always near the top
    except OSError:
        return "", []
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if not m:
        return "", []
    desc, tags = "", []
    for line in m.group(1).splitlines():
        if line.startswith("description:"):
            desc = line.split(":", 1)[1].strip().strip('"\'')
        elif line.startswith("tags:"):
            raw = line.split(":", 1)[1].strip()
            if raw.startswith("["):
                tags = [t.strip().strip('"\'') for t in raw.strip("[]").split(",") if t.strip()]
    return desc, tags


# ---------- Scanners ----------

class Scanner(Protocol):
    def scan(self) -> list[Skill]: ...


def _scan_skill_dir(root: Path, origin: str, origin_kind: str) -> list[Skill]:
    """Find SKILL.md anywhere one level deep in root."""
    if not root.exists():
        return []
    out: list[Skill] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        skill_md = entry / "SKILL.md"
        if not skill_md.exists():
            continue
        desc, tags = parse_skill_md(skill_md)
        out.append(Skill(
            name=entry.name,
            path=entry.resolve(),
            description=desc,
            tags=tags,
            origin=origin,
            origin_kind=origin_kind,
        ))
    return out


@dataclass
class LibraryScanner:
    library: Path
    def scan(self) -> list[Skill]:
        return _scan_skill_dir(self.library, origin="library", origin_kind="library")


@dataclass
class PluginCacheScanner:
    """Scans <scope>/plugins/cache/<plugin>@<version>/skills/<skill>/SKILL.md.

    Picks the latest version per plugin using a numeric tuple key (so 2.10 > 2.9).
    Falls back to lexicographic for non-numeric version strings.
    """
    cache_root: Path

    @staticmethod
    def _version_key(dir_name: str) -> tuple:
        """Numeric-aware sort key for plugin@version dir names.

        Key shapes by category (compared element-by-element):
          - Numeric SemVer-ish:  (1, 2, 10, 0)         for '1.2.10.0'
          - Non-numeric:         (0, 'v1.0-beta')      for arbitrary version strings
          - Unversioned:         (-1,)                  for just 'plugin' (no '@')
        Because tuple comparison goes left-to-right, numeric versions always
        beat non-numeric (1 > 0), which always beats unversioned (0 > -1).
        Within each category, comparison is meaningful.
        """
        _, _, ver = dir_name.partition("@")
        if not ver:
            return (-1,)  # unversioned sorts below everything
        try:
            parts = ver.lstrip("vV").split(".")
            # Note: leading 1 in the result so numeric beats non-numeric (which gets 0)
            return (1, *(int(p) for p in parts))
        except ValueError:
            return (0, ver)

    def _pick_latest_versions(self) -> dict[str, Path]:
        """Return {plugin_name: latest_version_dir}."""
        latest: dict[str, tuple[tuple, Path]] = {}
        if not self.cache_root.exists():
            return {}
        for entry in self.cache_root.iterdir():
            if not entry.is_dir():
                continue
            if entry.name.startswith(".") or "orphan" in entry.name.lower():
                continue
            plugin = entry.name.partition("@")[0] if "@" in entry.name else entry.name
            key = self._version_key(entry.name)
            cur = latest.get(plugin)
            if cur is None or key > cur[0]:
                latest[plugin] = (key, entry)
        return {p: d for p, (_, d) in latest.items()}

    def scan(self) -> list[Skill]:
        skills: list[Skill] = []
        for plugin_name, version_dir in self._pick_latest_versions().items():
            # Plugin manifest (optional)
            manifest_path = version_dir / ".claude-plugin" / "plugin.json"
            label_ver = ""
            if "@" in version_dir.name:
                label_ver = "@" + version_dir.name.partition("@")[2]
            origin_label = f"plugin: {plugin_name}{label_ver}"

            # Standard layout: skills/<name>/SKILL.md
            skills_dir = version_dir / "skills"
            found = _scan_skill_dir(skills_dir, origin=origin_label, origin_kind="plugin")

            # Some plugins (rare) put SKILL.md at root or under a custom path declared in manifest.
            # TODO: if a plugin has BOTH a standard skills/ dir AND manifest-declared skills,
            # we currently only return the standard ones. Merge both if/when we see this in the wild.
            if not found and manifest_path.exists():
                try:
                    manifest = json.loads(manifest_path.read_text())
                    custom = manifest.get("skills")
                    if isinstance(custom, list):
                        for item in custom:
                            sub = version_dir / item
                            if sub.is_dir():
                                found.extend(_scan_skill_dir(sub, origin=origin_label, origin_kind="plugin"))
                except (OSError, json.JSONDecodeError):
                    pass

            skills.extend(found)
        return skills


@dataclass
class LocalPathScanner:
    """Scans user-added directories. Each may itself be a skill or contain skills."""
    path: Path

    def scan(self) -> list[Skill]:
        if not self.path.exists():
            return []
        origin = f"local: {self.path}"
        # If the path itself has SKILL.md, treat it as a single skill
        if (self.path / "SKILL.md").exists():
            desc, tags = parse_skill_md(self.path / "SKILL.md")
            return [Skill(
                name=self.path.name, path=self.path.resolve(),
                description=desc, tags=tags,
                origin=origin, origin_kind="local",
            )]
        # Otherwise scan one level deep
        return _scan_skill_dir(self.path, origin=origin, origin_kind="local")


# ---------- Scope (the data hub) ----------

@dataclass
class SkillsRoot:
    base: Path  # ~/.claude or ./.claude
    # Cache for the SnapshotStore so we don't re-init (and re-run migration)
    # on every property access. init=False/repr=False keeps it invisible to users.
    _snapshots_cache: SnapshotStore | None = field(default=None, init=False, repr=False, compare=False)

    @property
    def library(self) -> Path: return self.base / LIBRARY_DIRNAME
    @property
    def active(self) -> Path: return self.base / ACTIVE_DIRNAME
    @property
    def plugins_cache(self) -> Path: return self.base / PLUGINS_CACHE_DIRNAME
    @property
    def presets_file(self) -> Path: return self.library / PRESETS_FILE
    @property
    def sources_file(self) -> Path: return self.library / SOURCES_FILE

    def exists(self) -> bool:
        return self.library.exists() and self.active.exists()

    def init(self) -> None:
        self.library.mkdir(parents=True, exist_ok=True)
        self.active.mkdir(parents=True, exist_ok=True)
        if not self.sources_file.exists():
            _atomic_write_text(self.sources_file, json.dumps({"local_paths": []}, indent=2) + "\n")
        # .snapshots.json is created lazily on first write by SnapshotStore

    # --- migration of pre-existing real dirs in active/ ---

    def find_real_dirs_in_active(self) -> list[Path]:
        if not self.active.exists():
            return []
        return [p for p in self.active.iterdir() if p.is_dir() and not p.is_symlink()]

    def migrate_real_dirs(self) -> tuple[int, list[str]]:
        """Returns (moved_count, collision_names) — collisions are folder names
        we couldn't migrate because the library already has something by that name."""
        moved = 0
        collisions: list[str] = []
        for src in self.find_real_dirs_in_active():
            dest = self.library / src.name
            if dest.exists():
                collisions.append(src.name)
                continue
            src.rename(dest)
            os.symlink(dest, self.active / src.name, target_is_directory=True)
            moved += 1
        return moved, collisions

    # --- sources config (local paths only) ---

    def load_local_sources(self) -> list[Path]:
        if not self.sources_file.exists():
            return []
        try:
            data = json.loads(self.sources_file.read_text())
            return [Path(p).expanduser() for p in data.get("local_paths", [])]
        except (OSError, json.JSONDecodeError):
            return []

    def save_local_sources(self, paths: list[Path]) -> None:
        existing = {}
        if self.sources_file.exists():
            try:
                existing = json.loads(self.sources_file.read_text())
            except json.JSONDecodeError:
                pass
        existing["local_paths"] = [str(p) for p in paths]
        _atomic_write_text(self.sources_file, json.dumps(existing, indent=2) + "\n")

    # --- snapshots (presets + undo history) ---

    @property
    def snapshots(self) -> SnapshotStore:
        if self._snapshots_cache is None:
            self._snapshots_cache = SnapshotStore(self.library)
        return self._snapshots_cache

    # --- scanning all sources, merged ---

    def all_scanners(self) -> list[Scanner]:
        scanners: list[Scanner] = [LibraryScanner(self.library)]
        if self.plugins_cache.exists():
            scanners.append(PluginCacheScanner(self.plugins_cache))
        for p in self.load_local_sources():
            scanners.append(LocalPathScanner(p))
        return scanners

    def scan_all(self) -> list[Skill]:
        """Scan every source, then annotate with active state + conflicts."""
        all_skills: list[Skill] = []
        for sc in self.all_scanners():
            all_skills.extend(sc.scan())

        # Active state: read current symlinks in active dir
        active_links: dict[str, Path] = {}
        if self.active.exists():
            for entry in self.active.iterdir():
                if entry.is_symlink():
                    try:
                        active_links[entry.name] = Path(os.readlink(entry))
                    except OSError:
                        continue
        for s in all_skills:
            if s.name in active_links:
                target = active_links[s.name]
                s.active = (target.resolve() == s.path.resolve()) if target.exists() else False
                s.active_target = target

        # Conflict detection: same name from multiple origins
        by_name: dict[str, list[Skill]] = {}
        for s in all_skills:
            by_name.setdefault(s.name, []).append(s)
        for name, group in by_name.items():
            if len(group) > 1:
                for s in group:
                    s.conflict_with = [other for other in group if other is not s]

        return sorted(all_skills, key=lambda s: (s.origin_kind, s.name))

    # --- symlink health ---

    def find_broken_symlinks(self) -> list[Path]:
        """Symlinks where the target no longer exists."""
        if not self.active.exists():
            return []
        # Path.exists() on a symlink follows it: False == dangling
        return [
            entry for entry in self.active.iterdir()
            if entry.is_symlink() and not entry.exists()
        ]

    def repair_or_drop(self, all_skills: list[Skill]) -> tuple[int, int, list[tuple[str, str]]]:
        """For broken symlinks, try to re-point to a same-named skill in any source; else drop.

        Returns (repaired, dropped, kind_mismatched) where kind_mismatched is
        [(skill_name, new_origin), ...] for repairs that came from a different
        origin kind than the original target (so the caller can warn).
        """
        repaired = dropped = 0
        kind_mismatched: list[tuple[str, str]] = []
        skills_by_name = {s.name: s for s in all_skills}
        for link in self.find_broken_symlinks():
            # Figure out what kind the old target was from its (resolved) path
            old_kind = "unknown"
            try:
                old_target = Path(os.readlink(link))
                # readlink may return a relative path — resolve relative to the link's parent
                if not old_target.is_absolute():
                    old_target = (link.parent / old_target).resolve(strict=False)
                if old_target.is_relative_to(self.plugins_cache):
                    old_kind = "plugin"
                elif old_target.is_relative_to(self.library):
                    old_kind = "library"
                else:
                    old_kind = "local"
            except (OSError, ValueError):
                pass

            replacement = skills_by_name.get(link.name)
            if replacement is not None:
                # Belt-and-suspenders: confirm it's still a symlink before unlinking
                if link.is_symlink():
                    link.unlink()
                os.symlink(replacement.path, link, target_is_directory=True)
                repaired += 1
                if old_kind != "unknown" and replacement.origin_kind != old_kind:
                    kind_mismatched.append((link.name, replacement.origin))
            else:
                if link.is_symlink():
                    link.unlink()
                dropped += 1
        return repaired, dropped, kind_mismatched

    # --- apply selection ---

    def snapshot_current_state(self, all_skills: list[Skill], auto_name: str | None = None) -> Snapshot:
        """Build a Snapshot of currently-active skills (post-scan state).
        Caller passes the full scan result; we read .active off each Skill."""
        if auto_name is None:
            local = datetime.now(timezone.utc).astimezone()
            auto_name = f"auto-{local.strftime('%Y-%m-%d-%H-%M-%S')}"
        active = [s for s in all_skills if s.active]
        return Snapshot.now(
            name=auto_name,
            rows=[f"{s.origin}::{s.name}" for s in active],
            names=[s.name for s in active],
            is_auto=True,
        )

    def apply_selection(self, selected: dict[str, Path]) -> tuple[int, int, int, list[str]]:
        """selected = {skill_name: source_path}. Make active match exactly.
        Returns (added, removed, repointed, skipped) where skipped is a list of
        skill names we refused to clobber because a real file/dir occupies the slot.

        Note: this method itself does NOT push to history — the caller should
        snapshot current state and push it before invoking this, because the
        caller has the full Skill list with origin info needed for row_ids."""
        added = removed = repointed = 0
        skipped: list[str] = []
        # Pass 1: collect names of existing symlinks (targets unused now)
        existing_names: set[str] = set()
        if self.active.exists():
            for entry in self.active.iterdir():
                if entry.is_symlink():
                    existing_names.add(entry.name)

        # Remove unwanted (re-check is_symlink to avoid TOCTOU clobbering of real files)
        for name in existing_names:
            if name not in selected:
                link = self.active / name
                if link.is_symlink():
                    link.unlink()
                    removed += 1

        # Add or repoint wanted
        for name, want_path in selected.items():
            link = self.active / name
            if link.is_symlink():
                cur = Path(os.readlink(link))
                if cur.resolve() == want_path.resolve():
                    continue
                link.unlink()
                os.symlink(want_path, link, target_is_directory=True)
                repointed += 1
            else:
                if link.exists():
                    skipped.append(name)  # real dir or file; refuse to clobber
                    continue
                os.symlink(want_path, link, target_is_directory=True)
                added += 1
        return added, removed, repointed, skipped


# ---------- Screens ----------

class ScopeScreen(Screen):
    CSS = """
    Screen { align: center middle; }
    #scope-box { width: 72; height: auto; padding: 1 2; border: thick $primary; background: $surface; }
    #scope-box Label { margin-bottom: 1; }
    #scope-list { height: 6; margin-top: 1; }
    #scope-buttons { height: auto; align: center middle; margin-top: 1; }
    #scope-buttons Button { margin: 0 1; }
    """
    BINDINGS = [Binding("q", "quit", "Quit")]

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="scope-box"):
            yield Label("cc-skills — manage Claude Code skills")
            yield Label("Choose a scope:")
            yield ListView(
                ListItem(Label(f"  User-global    ~{os.sep}.claude"), id="scope-user"),
                ListItem(Label(f"  Project        .{os.sep}.claude   ({Path.cwd()})"), id="scope-project"),
                id="scope-list",
            )
            with Horizontal(id="scope-buttons"):
                yield Button("Open", variant="primary", id="open-btn")
                yield Button("Quit", id="quit-btn")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#scope-list", ListView).focus()

    def _open(self) -> None:
        lv = self.query_one("#scope-list", ListView)
        choice = lv.highlighted_child.id if lv.highlighted_child else "scope-user"
        base = (Path.cwd() / ".claude") if choice == "scope-project" else (Path.home() / ".claude")
        self.app.open_root(SkillsRoot(base=base))

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self._open()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "open-btn":
            self._open()
        else:
            self.app.exit()


class InitScreen(ModalScreen[bool]):
    CSS = """
    InitScreen { align: center middle; }
    #init-box { width: 74; height: auto; padding: 1 2; border: thick $primary; background: $surface; }
    #init-box Label { margin-bottom: 1; }
    #init-buttons { height: auto; align: center middle; }
    #init-buttons Button { margin: 0 1; }
    """
    def __init__(self, base: Path):
        super().__init__(); self.base = base

    def compose(self) -> ComposeResult:
        with Vertical(id="init-box"):
            yield Label(f"No skill library found at:\n  {self.base}")
            yield Label("Initialize? This creates:")
            yield Label(f"  • {self.base}/{LIBRARY_DIRNAME}/   (your skill files)")
            yield Label(f"  • {self.base}/{ACTIVE_DIRNAME}/    (symlinks; Claude Code reads this)")
            yield Label(f"  Plugin cache at {self.base}/{PLUGINS_CACHE_DIRNAME}/ scanned if present.")
            with Horizontal(id="init-buttons"):
                yield Button("Initialize", variant="primary", id="init-yes")
                yield Button("Back", id="init-no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "init-yes")


class MigrateScreen(ModalScreen[bool]):
    CSS = """
    MigrateScreen { align: center middle; }
    #mig-box { width: 75; height: auto; padding: 1 2; border: thick $warning; background: $surface; }
    #mig-box Label { margin-bottom: 1; }
    #mig-buttons { height: auto; align: center middle; }
    #mig-buttons Button { margin: 0 1; }
    """
    def __init__(self, dirs: list[Path]):
        super().__init__(); self.dirs = dirs

    def compose(self) -> ComposeResult:
        with Vertical(id="mig-box"):
            yield Label(f"⚠ Found {len(self.dirs)} real skill folder(s) in skills/")
            yield Label(f"  {_sample_with_more([d.name for d in self.dirs], n=5)}")
            yield Label("Move them into skills-library/ and replace with symlinks? (Non-destructive.)")
            with Horizontal(id="mig-buttons"):
                yield Button("Migrate", variant="primary", id="mig-yes")
                yield Button("Skip", id="mig-no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "mig-yes")


class PresetSaveScreen(ModalScreen[str | None]):
    CSS = """
    PresetSaveScreen { align: center middle; }
    #save-box { width: 50; padding: 1 2; border: thick $primary; background: $surface; }
    #save-buttons { height: auto; align: center middle; margin-top: 1; }
    #save-buttons Button { margin: 0 1; }
    """
    def compose(self) -> ComposeResult:
        with Vertical(id="save-box"):
            yield Label("Save current selection as preset:")
            yield Input(placeholder="preset name (e.g. web-dev)", id="preset-name")
            with Horizontal(id="save-buttons"):
                yield Button("Save", variant="primary", id="save-yes")
                yield Button("Cancel", id="save-no")

    def on_mount(self) -> None:
        self.query_one("#preset-name", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save-yes":
            name = self.query_one("#preset-name", Input).value.strip()
            self.dismiss(name or None)
        else:
            self.dismiss(None)


class PresetLoadScreen(ModalScreen["tuple[str | None, str | None]"]):
    """Returns (action, name): action in {"load", "delete"}; name or None."""
    CSS = """
    PresetLoadScreen { align: center middle; }
    #load-box { width: 76; height: 22; padding: 1 2; border: thick $primary; background: $surface; }
    #load-buttons { height: auto; align: center middle; margin-top: 1; }
    #load-buttons Button { margin: 0 1; }
    """
    def __init__(self, presets: list[Snapshot]):
        super().__init__()
        self.presets = sorted(presets, key=lambda s: s.name)

    def compose(self) -> ComposeResult:
        with Vertical(id="load-box"):
            yield Label("Presets:")
            items: list[ListItem] = []
            for i, snap in enumerate(self.presets):
                label = f"{snap.name}  ({len(snap.names)} skills, saved {_humanize_ts(snap.created_at)})"
                items.append(ListItem(Label(label), id=f"p-{i}"))
            if not items:
                items.append(ListItem(Label("(no presets saved yet)")))
            yield ListView(*items, id="preset-list")
            with Horizontal(id="load-buttons"):
                yield Button("Load", variant="primary", id="load-yes")
                yield Button("Delete", variant="error", id="load-delete")
                yield Button("Cancel", id="load-no")

    def _selected_name(self) -> str | None:
        lv = self.query_one("#preset-list", ListView)
        if lv.highlighted_child and lv.highlighted_child.id and lv.highlighted_child.id.startswith("p-"):
            try:
                idx = int(lv.highlighted_child.id[2:])
                if 0 <= idx < len(self.presets):
                    return self.presets[idx].name
            except ValueError:
                pass
        return None

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "load-yes":
            name = self._selected_name()
            self.dismiss(("load", name) if name else (None, None))
        elif event.button.id == "load-delete":
            name = self._selected_name()
            self.dismiss(("delete", name) if name else (None, None))
        else:
            self.dismiss((None, None))


class HistoryScreen(ModalScreen["Snapshot | None"]):
    """Browse auto-snapshots. Returns the selected Snapshot or None."""
    CSS = """
    HistoryScreen { align: center middle; }
    #hist-box { width: 80; height: 26; padding: 1 2; border: thick $primary; background: $surface; }
    #hist-buttons { height: auto; align: center middle; margin-top: 1; }
    #hist-buttons Button { margin: 0 1; }
    """
    BINDINGS = [Binding("escape", "close", "Close")]

    def __init__(self, snapshots: list[Snapshot]):
        super().__init__()
        self.snapshots = snapshots  # caller passes in display order (newest first)

    def compose(self) -> ComposeResult:
        with Vertical(id="hist-box"):
            yield Label(f"History — {len(self.snapshots)} snapshot(s), newest first:")
            items: list[ListItem] = []
            for i, snap in enumerate(self.snapshots):
                label = f"{_humanize_ts(snap.created_at)}  ({len(snap.names)} skills)"
                items.append(ListItem(Label(label), id=f"h-{i}"))
            if not items:
                items.append(ListItem(Label("(no history yet — apply something first)")))
            yield ListView(*items, id="hist-list")
            with Horizontal(id="hist-buttons"):
                yield Button("Load", variant="primary", id="hist-load")
                yield Button("Cancel", id="hist-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "hist-load":
            lv = self.query_one("#hist-list", ListView)
            if lv.highlighted_child and lv.highlighted_child.id:
                idx = int(lv.highlighted_child.id[2:])
                if 0 <= idx < len(self.snapshots):
                    self.dismiss(self.snapshots[idx])
                    return
        self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)


class LocalPathAddScreen(ModalScreen[Path | None]):
    CSS = """
    LocalPathAddScreen { align: center middle; }
    #add-box { width: 70; padding: 1 2; border: thick $primary; background: $surface; }
    #add-buttons { height: auto; align: center middle; margin-top: 1; }
    #add-buttons Button { margin: 0 1; }
    """
    def compose(self) -> ComposeResult:
        with Vertical(id="add-box"):
            yield Label("Add a local path to scan for skills:")
            yield Label("(Either a single skill folder, or a folder containing skill folders.)")
            yield Input(placeholder="/path/to/skills or ~/work/my-skills", id="path-input")
            with Horizontal(id="add-buttons"):
                yield Button("Add", variant="primary", id="add-yes")
                yield Button("Cancel", id="add-no")

    def on_mount(self) -> None:
        self.query_one("#path-input", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "add-yes":
            raw = self.query_one("#path-input", Input).value.strip()
            if raw:
                p = Path(raw).expanduser().resolve()
                if p.exists() and p.is_dir():
                    self.dismiss(p); return
        self.dismiss(None)


class SourcesScreen(ModalScreen[bool]):
    """Manage local sources + show what's being scanned."""
    CSS = """
    SourcesScreen { align: center middle; }
    #src-box { width: 90; height: 28; padding: 1 2; border: thick $primary; background: $surface; }
    #src-list { height: 1fr; margin-top: 1; }
    #src-buttons { height: auto; align: center middle; margin-top: 1; }
    #src-buttons Button { margin: 0 1; }
    """
    BINDINGS = [Binding("escape", "close", "Close")]

    def __init__(self, root: SkillsRoot):
        super().__init__(); self.root = root; self.dirty = False

    def compose(self) -> ComposeResult:
        with Vertical(id="src-box"):
            yield Label("Sources scanned for skills:")
            yield ListView(id="src-list")
            with Horizontal(id="src-buttons"):
                yield Button("Add local path", variant="primary", id="add-local")
                yield Button("Remove selected", id="remove-local")
                yield Button("Done", id="done")

    def on_mount(self) -> None:
        self._refresh()

    def _refresh(self) -> None:
        lv = self.query_one("#src-list", ListView)
        lv.clear()
        # Library
        lv.append(ListItem(Label(f"  [library]  {self.root.library}  (always on)"), id="x-lib"))
        # Plugin cache
        if self.root.plugins_cache.exists():
            plugins = PluginCacheScanner(self.root.plugins_cache)._pick_latest_versions()
            lv.append(ListItem(Label(
                f"  [plugins]  {self.root.plugins_cache}  ({len(plugins)} plugin(s))"
            ), id="x-plugins"))
        else:
            lv.append(ListItem(Label(
                f"  [plugins]  {self.root.plugins_cache}  (not present — skipped)"
            ), id="x-plugins-missing"))
        # Local paths
        for p in self.root.load_local_sources():
            mark = "" if p.exists() else "  (missing)"
            lv.append(ListItem(Label(f"  [local]    {p}{mark}"), id=f"local::{p}"))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "add-local":
            def _added(p: Path | None) -> None:
                if p:
                    paths = self.root.load_local_sources()
                    if p not in paths:
                        paths.append(p)
                        self.root.save_local_sources(paths)
                        self.dirty = True
                        self._refresh()
            self.app.push_screen(LocalPathAddScreen(), _added)
        elif event.button.id == "remove-local":
            lv = self.query_one("#src-list", ListView)
            item = lv.highlighted_child
            if item and item.id and item.id.startswith("local::"):
                target = item.id[len("local::"):]
                paths = [p for p in self.root.load_local_sources() if str(p) != target]
                self.root.save_local_sources(paths)
                self.dirty = True
                self._refresh()
        else:
            self.dismiss(self.dirty)

    def action_close(self) -> None:
        self.dismiss(self.dirty)


class MainScreen(Screen):
    BINDINGS = [
        Binding("a", "apply", "Apply"),
        Binding("u", "undo", "Undo"),
        Binding("s", "save_preset", "Save preset"),
        Binding("l", "load_preset", "Load preset"),
        Binding("H", "open_history", "History"),
        Binding("/", "focus_filter", "Filter"),
        Binding("o", "open_sources", "Sources"),
        Binding("r", "refresh", "Refresh"),
        Binding("h", "repair", "Repair links"),
        Binding("e", "expand_all", "Expand all"),
        Binding("c", "collapse_all", "Collapse all"),
        Binding("b", "back_to_scope", "Back"),
        Binding("q", "quit", "Quit"),
    ]
    CSS = """
    #header-info { padding: 0 1; background: $boost; color: $text; }
    #filter { margin: 0 1; }
    #warning { padding: 0 1; background: $warning 30%; color: $text; }
    #warning.-hidden { display: none; }
    #status { padding: 0 1; background: $boost; color: $text-muted; }
    #groups { height: 1fr; margin: 0 1; }
    .group-list { height: auto; max-height: 20; }
    Collapsible { margin-bottom: 0; }
    """

    def __init__(self, root: SkillsRoot):
        super().__init__()
        self.root = root
        self.skills: list[Skill] = []
        self.filter_text = ""
        # Map row_id -> Skill for cross-list aggregation
        self._row_index: dict[str, Skill] = {}
        # Pending selection — TRI-STATE (load-bearing, do not collapse to bool):
        #   None       → "no user input yet; mirror disk state (s.active)"
        #   set()      → "user has interacted and explicitly wants nothing selected"
        #   set(rids)  → "user wants exactly these row_ids selected"
        # See has_pending_changes() to check if there are unapplied differences vs. disk.
        self._pending_active: set[str] | None = None
        # row_ids currently rendered in the DOM; populated by _rebuild_groups.
        # Used by _snapshot_pending_from_ui so we don't have to read Textual internals.
        # Consistency: this is in sync with the DOM as of the last _rebuild_groups call.
        # Callers should not assume it reflects the DOM mid-rebuild (it doesn't matter
        # in practice because filter→snapshot→rebuild is synchronous).
        self._visible_rids: set[str] = set()

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(self._header_text(), id="header-info")
        yield Input(placeholder="filter by name, tag, origin, or description...", id="filter")
        yield Static("", id="warning", classes="-hidden")
        yield Vertical(id="groups")
        yield Static("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_all()
        # Surface any one-time warning from store initialization (e.g. failed rename)
        warn = self.root.snapshots.consume_migration_warning()
        if warn:
            self.update_status(warn)

    def _header_text(self) -> str:
        return f"Scope: {self.root.base}"

    def refresh_all(self, msg: str = "") -> None:
        # Capture current toggles before discarding the DOM
        self._snapshot_pending_from_ui()
        self.skills = self.root.scan_all()
        self._rebuild_groups()
        self._update_warning()
        self.update_status(msg)

    def _snapshot_pending_from_ui(self) -> None:
        """Merge visible SelectionList state into _pending_active.

        Critical: when a filter is active, off-screen pending toggles must NOT
        be lost. We only update the row_ids that are currently visible (which
        we track in self._visible_rids during _rebuild_groups, so we don't have
        to use Textual private APIs).
        """
        try:
            lists = list(self.query("#groups SelectionList").results(SelectionList))
        except Exception:
            return
        if not lists:
            return
        selected_visible: set[str] = set()
        for sl in lists:
            selected_visible.update(sl.selected)

        # Start from existing pending (or disk state if first time)
        if self._pending_active is None:
            base = {self._row_id(s) for s in self.skills if s.active}
        else:
            base = set(self._pending_active)

        # For each visible row (tracked by us, not via Textual internals):
        # the on-screen state is authoritative; off-screen rows are untouched.
        for rid in self._visible_rids:
            if rid in selected_visible:
                base.add(rid)
            else:
                base.discard(rid)
        self._pending_active = base

    def _initial_state_for(self, s: Skill) -> bool:
        """What checkbox state should this skill have right now?"""
        if self._pending_active is not None:
            return self._row_id(s) in self._pending_active
        return s.active

    def _update_warning(self) -> None:
        """Persistent banner: broken symlinks, unapplied pending changes."""
        warning = self.query_one("#warning", Static)
        bits: list[str] = []
        broken = self.root.find_broken_symlinks()
        if broken:
            bits.append(f"⚠ {len(broken)} broken symlink(s) — press 'h' to repair")
        # Pending unapplied differs from disk?
        if self._pending_active is not None:
            disk_active = {self._row_id(s) for s in self.skills if s.active}
            if self._pending_active != disk_active:
                diff = len(self._pending_active.symmetric_difference(disk_active))
                bits.append(f"● {diff} unapplied change(s) — press 'a' to apply")
        if bits:
            warning.update("  ·  ".join(bits))
            warning.remove_class("-hidden")
        else:
            warning.update("")
            warning.add_class("-hidden")

    def _row_id(self, s: Skill) -> str:
        return f"{s.origin}::{s.name}"

    def _label_for(self, s: Skill) -> str:
        tag_str = f"  [{', '.join(s.tags)}]" if s.tags else ""
        desc_short = (s.description[:55] + "…") if len(s.description) > 55 else s.description
        tag = ""
        if s.conflict_with:
            # Determine which version (if any) is the one currently active on disk
            if s.active:
                active_origin = s.origin
            else:
                active_origin = next((o.origin for o in s.conflict_with if o.active), None)
            tag = f"  ⚠CONFLICT (active: {active_origin})" if active_origin else "  ⚠CONFLICT"
        return f"{s.name}{tag_str}  —  {desc_short}{tag}"

    def _group_key(self, s: Skill) -> str:
        # One group per distinct origin (so each plugin is its own group)
        return s.origin

    def _rebuild_groups(self) -> None:
        container = self.query_one("#groups", Vertical)
        container.remove_children()
        self._row_index.clear()
        self._visible_rids.clear()

        q = self.filter_text.lower()
        groups: dict[str, list[Skill]] = {}
        for s in self.skills:
            hay = f"{s.name} {s.description} {' '.join(s.tags)} {s.origin}".lower()
            if q and q not in hay:
                continue
            groups.setdefault(self._group_key(s), []).append(s)

        def group_sort_key(origin: str) -> tuple[int, str]:
            if origin == "library":
                return (0, origin)
            if origin.startswith("plugin"):
                return (1, origin)
            return (2, origin)

        for origin in sorted(groups.keys(), key=group_sort_key):
            items = groups[origin]
            active_in_group = sum(1 for s in items if self._initial_state_for(s))
            conflict_count = sum(1 for s in items if s.conflict_with)
            title_bits = [origin, f"{active_in_group}/{len(items)} active"]
            if conflict_count:
                title_bits.append(f"⚠{conflict_count} conflict")
            title = "  ·  ".join(title_bits)
            collapsed = not (q or active_in_group or origin == "library")

            sl = SelectionList[str](classes="group-list")
            for s in items:
                rid = self._row_id(s)
                self._row_index[rid] = s
                self._visible_rids.add(rid)
                sl.add_option(Selection(self._label_for(s), rid, initial_state=self._initial_state_for(s)))
            collapsible = Collapsible(sl, title=title, collapsed=collapsed)
            container.mount(collapsible)

    def on_selection_list_selected_changed(self, event: SelectionList.SelectedChanged) -> None:
        """Keep _pending_active in sync as the user toggles."""
        self._snapshot_pending_from_ui()
        self._update_warning()

    def update_status(self, msg: str = "") -> None:
        disk_active = sum(1 for s in self.skills if s.active)
        by_origin: dict[str, int] = {}
        for s in self.skills:
            by_origin[s.origin_kind] = by_origin.get(s.origin_kind, 0) + 1
        origins = ", ".join(f"{k}:{v}" for k, v in sorted(by_origin.items()))
        if self._pending_active is not None:
            pending = len(self._pending_active)
            text = f"{pending} selected ({disk_active} on disk)  |  discovered: {len(self.skills)} ({origins})"
        else:
            text = f"{disk_active} active  |  discovered: {len(self.skills)} ({origins})"
        if msg:
            text += f"  |  {msg}"
        self.query_one("#status", Static).update(text)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "filter":
            self._snapshot_pending_from_ui()
            self.filter_text = event.value
            self._rebuild_groups()
            self._update_warning()
            self.update_status()

    def action_focus_filter(self) -> None:
        self.query_one("#filter", Input).focus()

    def action_refresh(self) -> None:
        self.refresh_all("refreshed")

    def action_repair(self) -> None:
        repaired, dropped, mismatched = self.root.repair_or_drop(self.skills)
        msg = f"repair: re-pointed {repaired}, dropped {dropped}"
        if mismatched:
            sample = _sample_with_more(mismatched, render=lambda t: f"{t[0]}→{t[1]}")
            msg += f"  ⚠ {len(mismatched)} now from different source kind: {sample}"
        self.refresh_all(msg)

    def action_back_to_scope(self) -> None:
        self.app.pop_screen()

    def action_expand_all(self) -> None:
        for c in self.query("#groups Collapsible").results(Collapsible):
            c.collapsed = False

    def action_collapse_all(self) -> None:
        for c in self.query("#groups Collapsible").results(Collapsible):
            c.collapsed = True

    def action_open_sources(self) -> None:
        def _done(dirty: bool | None) -> None:
            if dirty:
                self.refresh_all("sources updated")
        self.app.push_screen(SourcesScreen(self.root), _done)

    def _resolve_selection(self) -> tuple[dict[str, Path], list[tuple[str, list[Skill]]]]:
        # Snapshot the visible UI back into pending so we use the latest user input
        self._snapshot_pending_from_ui()
        # Source of truth: _pending_active (or disk state if user hasn't touched anything)
        if self._pending_active is not None:
            chosen_rows = self._pending_active
        else:
            chosen_rows = {self._row_id(s) for s in self.skills if s.active}

        # Build name -> [skills selected for that name] from a fresh lookup
        skills_by_rid = {self._row_id(s): s for s in self.skills}
        chosen: dict[str, list[Skill]] = {}
        for rid in chosen_rows:
            s = skills_by_rid.get(rid)
            if s is None:
                continue
            chosen.setdefault(s.name, []).append(s)
        resolved: dict[str, Path] = {}
        conflicts: list[tuple[str, list[Skill]]] = []
        for name, candidates in chosen.items():
            if len(candidates) == 1:
                resolved[name] = candidates[0].path
            else:
                conflicts.append((name, candidates))
        return resolved, conflicts

    def action_apply(self) -> None:
        resolved, conflicts = self._resolve_selection()
        if conflicts:
            sample = _sample_with_more([c[0] for c in conflicts])
            self.update_status(
                f"⚠ same name selected from multiple sources: {sample}. Uncheck duplicates."
            )
            return
        # Auto-snapshot the CURRENT (pre-mutation) state for undo
        pre_snap = self.root.snapshot_current_state(self.skills)
        self.root.snapshots.push_history(pre_snap)
        # Mutate
        added, removed, repointed, skipped = self.root.apply_selection(resolved)
        self.skills = self.root.scan_all()
        self._pending_active = None
        self._rebuild_groups()
        self._update_warning()
        msg = f"applied: +{added} −{removed} ↻{repointed}"
        if skipped:
            msg += f"  ⚠ skipped (real file/dir in slot): {_sample_with_more(skipped)}"
        msg += "  ⚠ restart Claude Code  ·  press 'u' to undo"
        self.update_status(msg)

    def _load_snapshot_as_pending(self, snap: Snapshot) -> SnapshotLoadResult:
        """Restore a Snapshot into _pending_active.

        Strategy:
          1. Exact row_id match → use it.
          2. Else fall back to kind_pref-best same-name candidate, recording
             the substitution so the UI can warn.
          3. No candidates → missing.
        """
        kind_pref = {"library": 0, "plugin": 1, "local": 2}
        skills_by_rid = {self._row_id(s): s for s in self.skills}
        by_name: dict[str, list[Skill]] = {}
        for s in self.skills:
            by_name.setdefault(s.name, []).append(s)

        pending: set[str] = set()
        resolved_names: set[str] = set()
        substituted: list[tuple[str, str, str]] = []

        # Pass 1: exact row_id matches
        for rid in snap.rows:
            if rid in skills_by_rid:
                pending.add(rid)
                resolved_names.add(skills_by_rid[rid].name)

        # Pass 2: for snap entries whose row_id no longer exists, try name fallback.
        # Track the ORIGINAL origin so we can report substitutions.
        original_origin_by_name: dict[str, str] = {}
        for rid in snap.rows:
            if rid in skills_by_rid:
                continue
            if "::" in rid:
                orig, _, nm = rid.rpartition("::")
                original_origin_by_name.setdefault(nm, orig)

        unresolved_names = set(snap.names) - resolved_names
        missing: list[str] = []
        for name in unresolved_names:
            candidates = by_name.get(name, [])
            if not candidates:
                missing.append(name)
                continue
            best = min(candidates, key=lambda s: kind_pref.get(s.origin_kind, 9))
            pending.add(self._row_id(best))
            orig = original_origin_by_name.get(name)
            if orig is not None and orig != best.origin:
                substituted.append((name, orig, best.origin))

        self._pending_active = pending
        self.filter_text = ""
        try:
            self.query_one("#filter", Input).value = ""
        except Exception:
            pass
        self._rebuild_groups()
        self._update_warning()
        return SnapshotLoadResult(
            resolved_count=len(pending),
            missing=missing,
            substituted=substituted,
        )

    def action_undo(self) -> None:
        snap = self.root.snapshots.peek_history()
        if snap is None:
            self.update_status("nothing to undo")
            return
        r = self._load_snapshot_as_pending(snap)
        msg = f"undo: loaded prior state ({r.resolved_count} skill(s)) — review and press 'a' to commit"
        if r.missing:
            msg += f"  ⚠ {len(r.missing)} no longer available: {_sample_with_more(r.missing)}"
        if r.substituted:
            sub = _sample_with_more(r.substituted, render=lambda t: f"{t[0]} ({t[1]}→{t[2]})")
            msg += f"  ⚠ {len(r.substituted)} from different source: {sub}"
        self.update_status(msg)

    def action_save_preset(self) -> None:
        def _done(name: str | None) -> None:
            if not name:
                return
            resolved, conflicts = self._resolve_selection()
            if conflicts:
                self.update_status("⚠ resolve conflicts before saving preset")
                return
            self._snapshot_pending_from_ui()
            row_ids = self._pending_active or {self._row_id(s) for s in self.skills if s.active}
            snap = Snapshot.now(
                name=name,
                rows=row_ids,
                names=list(resolved),
                is_auto=False,
            )
            self.root.snapshots.save_preset(snap)
            self.update_status(f"preset '{name}' saved ({len(snap.names)} skills)")
        self.app.push_screen(PresetSaveScreen(), _done)

    def action_load_preset(self) -> None:
        presets = {p.name: p for p in self.root.snapshots.list_presets()}
        def _done(result: tuple[str | None, str | None]) -> None:
            action, name = result
            if not name or name not in presets:
                return
            if action == "delete":
                self.root.snapshots.delete_preset(name)
                self.update_status(f"preset '{name}' deleted")
                return
            snap = presets[name]
            r = self._load_snapshot_as_pending(snap)
            msg = f"preset '{name}' loaded — review and press 'a' to apply"
            if r.missing:
                msg += f"  ⚠ {len(r.missing)} not found: {_sample_with_more(sorted(r.missing))}"
            if r.substituted:
                sub = _sample_with_more(r.substituted, render=lambda t: f"{t[0]} ({t[1]}→{t[2]})")
                msg += f"  ⚠ {len(r.substituted)} from different source: {sub}"
            self.update_status(msg)
        self.app.push_screen(PresetLoadScreen(list(presets.values())), _done)

    def action_open_history(self) -> None:
        history = self.root.snapshots.list_history()
        if not history:
            self.update_status("history is empty")
            return
        def _done(snap: Snapshot | None) -> None:
            if snap is None:
                return
            r = self._load_snapshot_as_pending(snap)
            msg = f"history: loaded {_humanize_ts(snap.created_at)} ({r.resolved_count} skill(s)) — review and press 'a'"
            if r.missing:
                msg += f"  ⚠ {len(r.missing)} no longer available"
            if r.substituted:
                msg += f"  ⚠ {len(r.substituted)} from different source"
            self.update_status(msg)
        self.app.push_screen(HistoryScreen(list(reversed(history))), _done)


# ---------- App ----------

class CCSkillsApp(App):
    TITLE = "cc-skills"
    SUB_TITLE = "Claude Code skill manager"

    def on_mount(self) -> None:
        self.push_screen(ScopeScreen())

    def open_root(self, root: SkillsRoot) -> None:
        if not root.exists():
            def _initted(yes: bool | None) -> None:
                if yes:
                    root.init()
                    self._check_migrate_then_open(root)
            self.push_screen(InitScreen(root.base), _initted)
            return
        self._check_migrate_then_open(root)

    def _check_migrate_then_open(self, root: SkillsRoot) -> None:
        real = root.find_real_dirs_in_active()
        if real:
            def _migrated(yes: bool | None) -> None:
                msg = ""
                if yes:
                    moved, collisions = root.migrate_real_dirs()
                    if collisions:
                        msg = f"migrated {moved}; ⚠ skipped (already in library): {_sample_with_more(collisions)}"
                    else:
                        msg = f"migrated {moved} skill folder(s) into the library"
                screen = MainScreen(root)
                self.push_screen(screen)
                if msg:
                    # status update happens after mount
                    self.call_later(lambda: screen.update_status(msg))
            self.push_screen(MigrateScreen(real), _migrated)
            return
        self.push_screen(MainScreen(root))


def main() -> None:
    CCSkillsApp().run()


if __name__ == "__main__":
    main()
