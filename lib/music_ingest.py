#!/usr/bin/env python3
"""User-facing launcher and XDG configuration for music-ingest."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tomllib
import time
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
WIZARD = APP_DIR / "bin" / "whipper-music-wizard"
CONFIG_PATH = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "music-ingest" / "config.toml"
DEFAULTS = {
    "incoming": str(Path.home() / "Music" / "Incoming"),
    "library": str(Path.home() / "Music"),
    "default_mode": "dry-run",
    "publication": "copy",
    "preserve_source": True,
    "staging": str(Path.home() / ".local" / "state" / "music-ingest" / "staging"),
    "musicbrainz_lookup": True,
    "multidisc_folders": False,
    "cover_mode": "file",
}

STAGING_PREFIXES = ("music-import-out-", "whipper-out-")


def load_config(path: Path = CONFIG_PATH) -> dict[str, object]:
    config = DEFAULTS.copy()
    if path.exists():
        with path.open("rb") as stream:
            values = tomllib.load(stream)
        unknown = set(values) - set(DEFAULTS)
        if unknown:
            raise ValueError("unknown config key(s): " + ", ".join(sorted(unknown)))
        config.update(values)
    for key in ("incoming", "library", "staging"):
        config[key] = str(Path(str(config[key])).expanduser())
    if config["publication"] != "copy" or config["preserve_source"] is not True:
        raise ValueError("only publication='copy' and preserve_source=true are supported")
    if config["default_mode"] not in {"dry-run", "apply"}:
        raise ValueError("default_mode must be 'dry-run' or 'apply'")
    return config


def show_config(config: dict[str, object], path: Path = CONFIG_PATH) -> None:
    print(f"Config: {path}")
    for key, value in config.items():
        print(f"{key} = {value}")


def owned_staging_dirs(staging: Path) -> list[Path]:
    """Only immediate directories with an application-created prefix are manageable."""
    if not staging.is_dir():
        return []
    return sorted(
        path for path in staging.iterdir()
        if path.is_dir() and path.name.startswith(STAGING_PREFIXES) and path.parent == staging
    )


def staging_size(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def staging_file_count(path: Path) -> int:
    return sum(1 for item in path.rglob("*") if item.is_file())


def managed_staging_dirs(config: dict[str, object]) -> list[Path]:
    """Return only immediate, tool-owned staging directories in known roots."""
    roots = [Path(str(config["staging"])).resolve()]
    incoming = Path(str(config["incoming"])).resolve()
    if incoming not in roots:
        roots.append(incoming)
    found: list[Path] = []
    for root in roots:
        found.extend(owned_staging_dirs(root))
    return sorted(set(found))


def staging_classification(entry: Path) -> str:
    # A directory alone cannot prove publication.  Keep all historical output
    # conservative until a future operation has an explicit retained marker.
    if not entry.exists():
        return "unknown"
    if entry.name.startswith("whipper-out-"):
        return "partial/unknown"
    return "unknown"


def staging_command(config: dict[str, object], action: str) -> int:
    staging = Path(str(config["staging"])).resolve()
    entries = managed_staging_dirs(config)
    if action == "status":
        print(f"Staging: {staging}")
        if not entries:
            print("No tool-owned staged operations found.")
            return 0
        for entry in entries:
            age = max(0, int(time.time() - entry.stat().st_mtime))
            print(
                f"{entry}: {staging_classification(entry)}; "
                f"age={age}s; files={staging_file_count(entry)}; bytes={staging_size(entry)}; "
                "report=not linked"
            )
        return 0
    if action == "cleanup":
        if not entries:
            print("No tool-owned staged operations found.")
            return 0
        print("The following tool-owned staging directories will be removed:")
        for entry in entries:
            print(f"  {entry} ({staging_classification(entry)}; {staging_file_count(entry)} files; {staging_size(entry)} bytes)")
        try:
            approved = input("Type CLEANUP to remove only these staged directories: ").strip() == "CLEANUP"
        except (EOFError, KeyboardInterrupt):
            approved = False
        if not approved:
            print("Staging cleanup cancelled.")
            return 0
        for entry in entries:
            # Re-check parent/name immediately before removal; never traverse arbitrary paths.
            if entry.parent in {staging, Path(str(config["incoming"])).resolve()} and entry.name.startswith(STAGING_PREFIXES):
                shutil.rmtree(entry)
        print("Tool-owned staging cleanup complete.")
        return 0
    raise ValueError(f"unknown staging action: {action}")


def run_action(
    action: str, config: dict[str, object], mode: str | None = None, device: Path | None = None,
    allow_cdr: bool = False, rip_profile: str = "secure",
) -> int:
    if action == "cd":
        if not shutil.which("whipper"):
            print("CD ripping is unavailable: Whipper is not installed. Digital import remains available.", file=sys.stderr)
            return 2
        if not (Path("/dev/cdrom").exists() or list(Path("/dev").glob("sr*"))):
            print("CD ripping is unavailable: no optical drive was detected. Digital import remains available.", file=sys.stderr)
            return 2
    incoming = Path(str(config["incoming"]))
    library = Path(str(config["library"]))
    staging = Path(str(config["staging"]))
    if not incoming.is_dir():
        print(f"Incoming directory is unavailable: {incoming}", file=sys.stderr)
        return 2
    if not library.is_dir():
        print(f"Production library directory is unavailable: {library}; refusing to create it", file=sys.stderr)
        return 2
    if not os.access(incoming, os.R_OK | os.X_OK) or not os.access(library, os.W_OK | os.X_OK):
        print("Incoming/library permissions do not allow the requested operation.", file=sys.stderr)
        return 2
    if incoming == library or incoming in library.parents or library in incoming.parents:
        print("Incoming and library paths must not overlap.", file=sys.stderr)
        return 2
    staging.mkdir(parents=True, exist_ok=True)
    if staging.stat().st_dev != library.stat().st_dev:
        print("Staging and production library must be on the same filesystem.", file=sys.stderr)
        return 2
    env = os.environ.copy()
    env.update({
        "MUSIC_INGEST_LIBRARY": str(library),
        "MUSIC_INGEST_STAGING": str(staging),
        "MUSIC_INGEST_INCOMING": str(incoming),
        "MUSIC_INGEST_MODE": mode or str(config["default_mode"]),
        "MUSIC_INGEST_LOOKUP": "yes" if config["musicbrainz_lookup"] else "no",
        "MUSIC_INGEST_MULTIDISC": "yes" if config["multidisc_folders"] else "no",
        "MUSIC_INGEST_COVER_MODE": str(config["cover_mode"]),
        "MUSIC_INGEST_ACTION": action,
    })
    if device is not None:
        # The device is an operator-selected hint for the CD-only wizard. It is
        # intentionally not persisted: a replacement drive needs recommissioning.
        env["MUSIC_INGEST_DEVICE"] = str(device)
    # Explicit one-session intent only.  Never persist permission for future discs.
    if allow_cdr:
        env["MUSIC_INGEST_ALLOW_CDR"] = "yes"
    if rip_profile != "secure":
        env["MUSIC_INGEST_RIP_PROFILE"] = rip_profile
    return subprocess.run([str(WIZARD)], env=env, check=False).returncode


def wizard(config: dict[str, object]) -> int:
    print("\nMusic Ingest\n")
    print("[1] Import digital music")
    print("[2] Rip audio CD")
    print("[3] Inspect configuration/status")
    print("[4] Exit")
    try:
        choice = input("Choose [1-4]: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.")
        return 0
    if choice == "1":
        return run_action("digital", config)
    if choice == "2":
        return run_action("cd", config, "apply")
    if choice == "3":
        show_config(config)
        print(f"Whipper: {'available' if shutil.which('whipper') else 'not installed'}")
        print(f"Optical device node: {'present' if Path('/dev/cdrom').exists() or list(Path('/dev').glob('sr*')) else 'not detected'}")
        return 0
    if choice in {"4", "q", "quit", ""}:
        return 0
    print("Choose 1, 2, 3, or 4.")
    return 2


def add_path_overrides(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--incoming", type=Path, default=argparse.SUPPRESS, help="override the incoming directory")
    parser.add_argument("--library", type=Path, default=argparse.SUPPRESS, help="override the production music root")
    parser.add_argument("--staging", type=Path, default=argparse.SUPPRESS, help="override the staging directory")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="music-ingest", description="Review, stage, and safely publish digital music or rip CDs.")
    parser.add_argument("--config-file", type=Path, default=CONFIG_PATH, help="use a different TOML config")
    add_path_overrides(parser)
    parser.add_argument("--mode", choices=("dry-run", "apply"), help="digital import mode")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("help", help="show this help")
    config = sub.add_parser("config", help="show effective configuration")
    add_path_overrides(config)
    digital = sub.add_parser("digital", help="review digital music for import")
    digital.add_argument("--mode", choices=("dry-run", "apply"), default=argparse.SUPPRESS, help="override configured import mode")
    add_path_overrides(digital)
    cd = sub.add_parser("cd", help="start the Whipper CD workflow")
    add_path_overrides(cd)
    cd.add_argument("--device", type=Path, help="use this optical device for this CD session only")
    cd.add_argument("--allow-cdr", action="store_true", help="explicitly permit Whipper's CD-R mode for this session")
    cd.add_argument("--rip-profile", choices=("secure", "bounded"), default="secure", help="secure uses five retries; bounded uses one while retaining verification")
    staging = sub.add_parser("staging", help="inspect or safely clean tool-owned staging")
    staging.add_argument("action", choices=("status", "cleanup"), nargs="?", default="status")
    add_path_overrides(staging)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config_file)
        for key in ("incoming", "library", "staging"):
            override = getattr(args, key, None)
            if override is not None:
                config[key] = str(override.expanduser())
        if args.command == "help":
            parser.print_help(); return 0
        if args.command == "config":
            show_config(config, args.config_file)
            return 0
        if args.command == "digital":
            return run_action("digital", config, args.mode)
        if args.command == "cd":
            return run_action("cd", config, "apply", args.device, args.allow_cdr, args.rip_profile)
        if args.command == "staging":
            return staging_command(config, args.action)
        return wizard(config)
    except (EOFError, KeyboardInterrupt):
        print("music-ingest: cancelled; nothing was published.")
        return 0
    except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
        print(f"music-ingest: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        if os.environ.get("MUSIC_INGEST_DEBUG") == "1":
            raise
        print(f"music-ingest: unexpected error: {exc} (set MUSIC_INGEST_DEBUG=1 for traceback)", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
