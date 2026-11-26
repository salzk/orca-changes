#!/usr/bin/env python3
"""OrcaSlicer user-profile change analyzer.

Browse the .json files under ~/.config/OrcaSlicer/user/default/{filament,machine,process}
and list their key:value pairs together with the original value from the system
profile they inherit from (following the whole "inherits" chain).

The original system profiles are fetched on demand from the OrcaSlicer GitHub
repository (tag of your installed version) and cached in ~/.config/orca-src/<version>/.
Only the files actually needed are downloaded.

Usage:  python3 orca_changes.py [user_default_dir] [--version 2.5.2]

Output legend:   * key : value   (orig: original value)
  '*'            the value differs from the original
  (orig: same)   value equals the original
  (orig: -)      the key does not exist in the parent chain
  (orig: ?)      the parent profile could not be found

Version: The script reads it from ~/.config/OrcaSlicer/OrcaSlicer.conf and prints the result on startup, 
for example OrcaSlicer version: 2.5.2 (detected from OrcaSlicer.conf). 
If detection fails, it uses FALLBACK_VERSION = "2.5.2" near the top of the script. 
You can also force a version with orca-changes --version 2.5.2. 
I only tested the detection against a made-up .conf, so check that the printed version matches your real install.

What happens on the first run:

It makes a small clone of the OrcaSlicer repository at /tmp/orca-src. 
The clone has no checkout and no file contents, so it is small. It exists only to look files up.
It lists which profiles exist for your version and saves that list.
For each profile in the inherits chain, it downloads just that file and prints (fetching …). Your example needed three files.
The files are cached in ~/.config/orca-src/<version>/, in the same folder layout as the repository.

Later runs: If the profiles you look at are already cached, the script uses no network and no git. 
/tmp/orca-src is only recreated when a new file has to be downloaded, for example after a reboot clears /tmp.

Updating OrcaSlicer: A new version gets its own cache folder, so the original values always match your installed version. 
Old version folders can be deleted by hand.

If anything fails (no network, git missing, version tag not found), 
the script prints the reason and shows (orig: ?) instead of crashing.

Yours
Claude
20261126
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

DEFAULT_BASE = Path.home() / ".config" / "OrcaSlicer" / "user" / "default"
CATEGORIES = ["filament", "machine", "process"]

# Used only if the installed version can't be detected (or given with --version).
FALLBACK_VERSION = "2.5.2"

REPO_URL = "https://github.com/SoftFever/OrcaSlicer.git"
CLONE_DIR = Path("/tmp/orca-src")                       # throw-away blobless clone
CACHE_DIR = Path.home() / ".config" / "orca-src"        # permanent cache of needed files
PREFIX = "resources/profiles/"                          # profile folder inside the repo

MAX_LEN = 100  # truncate long values (e.g. G-code) in the display


# --------------------------------------------------------------------------- git / cache

def git(*args, cwd=None):
    try:
        r = subprocess.run(["git", *args], cwd=cwd, capture_output=True)
    except FileNotFoundError:
        raise RuntimeError("git is not installed")
    if r.returncode:
        err = r.stderr.decode(errors="replace").strip()
        raise RuntimeError(f"git {' '.join(args[:2])} failed: {err}")
    return r.stdout


def ensure_clone(version):
    """Make sure /tmp/orca-src is a blobless, checkout-less clone of the version's tag."""
    marker = CLONE_DIR / ".orca-version"
    if (CLONE_DIR / ".git").is_dir() and marker.exists() \
            and marker.read_text().strip() == version:
        return
    shutil.rmtree(CLONE_DIR, ignore_errors=True)
    print(f"(cloning OrcaSlicer v{version} metadata into {CLONE_DIR} ...)")
    git("clone", "--depth", "1", "--filter=blob:none", "--no-checkout",
        "--branch", f"v{version}", REPO_URL, str(CLONE_DIR))
    marker.write_text(version)


class ProfileStore:
    """System profiles of one OrcaSlicer version, fetched lazily and cached locally."""

    def __init__(self, version):
        self.version = version
        self.root = CACHE_DIR / version
        self._tree = None

    def _load_tree(self):
        if self._tree is not None:
            return
        tree_file = self.root / "_tree.txt"
        if tree_file.exists():
            paths = tree_file.read_text(encoding="utf-8").split("\n")
        else:
            ensure_clone(self.version)
            out = git("ls-tree", "-r", "-z", "--name-only", "HEAD", PREFIX.rstrip("/"),
                      cwd=CLONE_DIR).decode("utf-8")
            paths = [p[len(PREFIX):] for p in out.split("\0")
                     if p.startswith(PREFIX) and p.endswith(".json")]
            if not paths:
                raise RuntimeError(f"no profiles found for v{self.version}")
            self.root.mkdir(parents=True, exist_ok=True)
            tree_file.write_text("\n".join(paths), encoding="utf-8")
        index = {}
        for p in paths:
            parts = p.split("/")
            if len(parts) >= 3 and parts[1] in CATEGORIES:
                index.setdefault((parts[1], p[:-5].rsplit("/", 1)[-1]), []).append(p)
        self._tree = index

    def find(self, category, name, prefer_vendor=None):
        """Return the repo-relative path of the profile called `name`, or None."""
        self._load_tree()
        candidates = self._tree.get((category, name), [])
        if not candidates:
            return None
        return next((c for c in candidates if c.split("/")[0] == prefer_vendor),
                    candidates[0])

    def get(self, rel):
        local = self.root / rel
        if not local.exists():
            ensure_clone(self.version)
            print(f"(fetching {rel})")
            data = git("show", f"HEAD:{PREFIX}{rel}", cwd=CLONE_DIR)
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_bytes(data)
        return load_json(local)


def detect_version(orca_root):
    """Read the OrcaSlicer version from OrcaSlicer.conf; None if not possible."""
    try:
        text = (orca_root / "OrcaSlicer.conf").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    body = "\n".join(l for l in text.splitlines() if not l.startswith("#"))
    try:
        version = json.loads(body).get("app", {}).get("version")
        if version:
            return str(version)
    except (json.JSONDecodeError, AttributeError):
        pass
    m = re.search(r'"version"\s*:\s*"(\d+\.\d+\.\d+[^"]*)"', text)
    return m.group(1) if m else None


# --------------------------------------------------------------------------- profiles

def load_json(path):
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def resolve_chain(store, name, category, prefer_vendor=None, seen=frozenset()):
    """Return (merged_dict, found) for a system profile incl. all its ancestors."""
    if name in seen:  # guard against inheritance cycles
        return {}, True
    rel = store.find(category, name, prefer_vendor)
    if rel is None:
        return {}, False
    try:
        data = store.get(rel)
    except (OSError, json.JSONDecodeError):
        return {}, False

    base, found = {}, True
    parent = data.get("inherits")
    if parent:
        base, found = resolve_chain(store, parent, category, rel.split("/")[0],
                                    seen | {name})
    return {**base, **data}, found


# --------------------------------------------------------------------------- UI

def choose(title, options):
    """Show a numbered menu. Return the chosen index, or None for back/quit."""
    print(f"\n{title}")
    for i, opt in enumerate(options, 1):
        print(f"  ({i}) - {opt}")
    print("  (q) - back / quit")
    while True:
        answer = input("> ").strip().lower()
        if answer in ("q", ""):
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return int(answer) - 1
        print(f"Please enter a number from 1 to {len(options)}, or q.")


def fmt(value):
    text = json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) \
        else str(value)
    text = text.replace("\n", "\\n")
    return text if len(text) <= MAX_LEN else text[:MAX_LEN - 1] + "…"


def same(a, b):
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def show_file(path, store, category):
    try:
        data = load_json(path)
    except (OSError, json.JSONDecodeError) as e:
        print(f"Could not read {path.name}: {e}")
        return

    print(f"\n=== {path.name} ===")
    if not isinstance(data, dict):
        print(fmt(data))
        return

    original, found = {}, False
    parent = data.get("inherits")
    if not parent:
        print("(no 'inherits' entry: no original values available)")
    else:
        try:
            original, found = resolve_chain(store, parent, category)
            if not found:
                print(f"(parent profile '{parent}' not found in OrcaSlicer v{store.version})")
        except RuntimeError as e:
            print(f"(could not get system profiles: {e})")

    width = max((len(k) for k in data), default=0)
    changed = 0
    for key, value in data.items():
        if not found:
            orig_txt, mark = "?", " "
        elif key not in original:
            orig_txt, mark = "-", "*"
            changed += 1
        elif same(value, original[key]):
            orig_txt, mark = "same", " "
        else:
            orig_txt, mark = fmt(original[key]), "*"
            changed += 1
        print(f"{mark} {key:<{width}} : {fmt(value)}   (orig: {orig_txt})")

    summary = f"\n{len(data)} keys"
    if found:
        summary += f", {changed} differ from the original (marked *)"
    print(summary)


def main():
    ap = argparse.ArgumentParser(description="Analyze OrcaSlicer user profile changes.")
    ap.add_argument("base", nargs="?", type=Path, default=DEFAULT_BASE,
                    help="user profile dir (default: %(default)s)")
    ap.add_argument("--version", help="OrcaSlicer version, e.g. 2.5.2 (default: autodetect)")
    args = ap.parse_args()

    base = args.base.expanduser()
    if not base.is_dir():
        sys.exit(f"Directory not found: {base}")

    if args.version:
        version, how = args.version.lstrip("v"), "from --version"
    elif (detected := detect_version(base.parents[1])):
        version, how = detected, "detected from OrcaSlicer.conf"
    else:
        version, how = FALLBACK_VERSION, "fallback, could not detect"
    print(f"OrcaSlicer version: {version} ({how})")
    store = ProfileStore(version)

    while True:
        idx = choose("Select category:", CATEGORIES)
        if idx is None:
            break
        category = CATEGORIES[idx]
        folder = base / category
        files = sorted(folder.glob("*.json")) if folder.is_dir() else []
        if not files:
            print(f"No .json files in {folder}")
            continue

        while True:
            fidx = choose(f"Files in {category}:", [p.name for p in files])
            if fidx is None:
                break
            show_file(files[fidx], store, category)


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print()
