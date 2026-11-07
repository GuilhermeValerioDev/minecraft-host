"""Command line interface for mchost."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from . import __version__
from .backups import (
    create_backup,
    delete_backup,
    get_backup,
    human_size,
    list_backups,
    prune_backups,
    restore_backup,
)
from .compose import render_compose, write_compose
from .config import (
    LOADER_LABELS,
    LOADERS,
    Instance,
    default_tz,
    instance_slugs,
    java_for_mc,
    new_rcon_password,
    resolve_instance,
    save_instance,
    scan_instances,
    slugify,
    utc_now,
)
from .doctor import FAIL, OK, WARN, host_memory, parse_memory, run_checks
from .engine import Engine, EngineError, detect_engine, restore_ownership
from .errors import ConfigError, MchostError
from .instances import delete_instance
from .localpack import inspect_local_pack, install_local_pack
from .migrate import already_migrated, maybe_auto_migrate, migrate
from .paths import Layout, find_root
from .modrinth import (
    check_modpack_update,
    get_project,
    get_versions,
    install_modpack,
    resolve_artifact,
    search_modpacks,
    select_version,
    update_modpack,
)
from .versions import (
    loader_builds,
    recommended_build,
    supported_mc_versions,
)

console = Console()

STATE_STYLE = {
    "running": "green",
    "restarting": "red",
    "exited": "yellow",
    "created": "yellow",
    "paused": "yellow",
    "missing": "dim",
}


def _layout(args: argparse.Namespace) -> Layout:
    root_value = getattr(args, "root", None)
    root = Path(root_value).expanduser().resolve() if root_value else find_root()
    layout = Layout(root)
    layout.ensure()
    return layout


def _engine(layout: Layout) -> Engine:
    return Engine(layout, detect_engine())


def _ask(label: str, default: str | None = None, *, required: bool = False) -> str:
    if not sys.stdin.isatty():
        raise MchostError(f"{label} is required; pass the matching option")
    suffix = f" [{default}]" if default else ""
    value = console.input(f"[bold]{label}[/]{suffix}: ").strip()
    if not value and default is not None:
        return default
    if not value and required:
        raise MchostError(f"{label} is required")
    return value


def _auto_migrate(layout: Layout) -> None:
    try:
        result = maybe_auto_migrate(layout)
    except MchostError as exc:
        console.print(f"[red]Legacy import failed:[/] {exc}")
        console.print("[dim]Fix .env or run 'mchost migrate' after correcting it.[/]")
        return
    if result is None:
        return
    console.print(
        f"[green]Imported legacy instance[/] {result.instance.name} "
        f"[dim]→ {result.instance.slug}[/]"
    )
    for warning in result.warnings:
        console.print(f"[yellow]![/] {warning}")


def _state(engine: Engine | None, slug: str) -> str:
    if engine is None:
        return "unknown"
    try:
        return engine.state(slug)
    except MchostError:
        return "unknown"


def _instances_table(layout: Layout, engine: Engine | None) -> Table:
    table = Table(title="Instances", title_justify="left", expand=False)
    table.add_column("Slug", style="bold")
    table.add_column("Name")
    table.add_column("Loader / version")
    table.add_column("Memory", justify="right")
    table.add_column("State")
    table.add_column("Ports", style="dim")

    instances, errors = scan_instances(layout)
    for instance in instances:
        state = _state(engine, instance.slug)
        style = STATE_STYLE.get(state, "white")
        ports = f"{instance.port} / rcon {instance.rcon_port}"
        if instance.bluemap_port is not None:
            ports += f" / map {instance.bluemap_port}"
        table.add_row(
            instance.slug,
            instance.name,
            instance.version_label,
            instance.memory,
            f"[{style}]{state}[/]",
            ports,
        )

    for slug, message in errors:
        table.add_row(slug, "[red]unreadable config[/]", message, "", "[red]error[/]", "")

    return table


def cmd_list(layout: Layout) -> int:
    try:
        engine: Engine | None = _engine(layout)
    except EngineError as exc:
        engine = None
        console.print(f"[yellow]![/] {exc}")

    instances, _ = scan_instances(layout)
    if not instances and not instance_slugs(layout):
        console.print("No instances yet.")
        console.print("[dim]Create one with:[/] mchost new MyServer --loader neoforge --mc-version 1.21.1")
        return 0

    console.print(_instances_table(layout, engine))
    console.print(
        "[dim]start · stop · restart · switch · status · logs · console · "
        "cmd · versions · tui[/]"
    )
    return 0


def cmd_status(layout: Layout, reference: str | None) -> int:
    if not reference:
        return cmd_list(layout)

    instance = resolve_instance(layout, reference)
    try:
        engine: Engine | None = _engine(layout)
    except EngineError:
        engine = None

    state = _state(engine, instance.slug)
    table = Table.grid(padding=(0, 2))
    table.add_column(style="dim", no_wrap=True)
    table.add_column()

    table.add_row("Name", instance.name)
    table.add_row("Slug", instance.slug)
    table.add_row("Kind", instance.kind)
    table.add_row("Loader", LOADER_LABELS.get(instance.loader, instance.loader))
    table.add_row("Minecraft", instance.mc_version)
    table.add_row("Loader build", instance.loader_version or "[yellow]not pinned[/]")
    table.add_row("Java", str(instance.java))
    table.add_row("Memory", instance.memory)
    table.add_row("Timezone", instance.tz)
    table.add_row("MOTD", instance.motd or "[dim]—[/]")
    table.add_row("State", f"[{STATE_STYLE.get(state, 'white')}]{state}[/]")
    table.add_row("Game port", str(instance.port))
    table.add_row("RCON", f"127.0.0.1:{instance.rcon_port}")
    table.add_row(
        "BlueMap", str(instance.bluemap_port) if instance.bluemap_port else "[dim]disabled[/]"
    )
    if instance.modpack:
        table.add_row(
            "Modpack",
            f"{instance.modpack.project} {instance.modpack.version_number} "
            f"({instance.modpack.file_kind or 'mrpack'})",
        )
    table.add_row("Config", str(layout.config_file(instance.slug)))
    table.add_row("Data", str(layout.server_dir_for(instance)))
    table.add_row("Compose", str(layout.compose_file(instance.slug)))
    table.add_row("Backups", str(layout.backups_for(instance.slug)))

    console.print(Panel(table, title=instance.name, border_style="cyan"))
    return 0


def cmd_new(layout: Layout, args: argparse.Namespace) -> int:
    name = args.name.strip()
    if not name:
        raise ConfigError("name cannot be empty")
    slug = slugify(name)
    if layout.config_file(slug).is_file():
        raise ConfigError(f"instance {slug!r} already exists")

    loader = args.loader.lower()
    mc_version = args.mc_version
    if not mc_version:
        mc_version = _ask("Minecraft version (e.g. 1.21.1)", required=True)

    loader_version = args.loader_version
    if loader_version is None and loader != "vanilla" and not args.no_pin:
        try:
            build = recommended_build(loader, mc_version)
        except MchostError as exc:
            build = None
            console.print(f"[yellow]![/] could not resolve {loader} builds: {exc}")
        if build is not None:
            loader_version = build.version
            console.print(
                f"[green]Pinned[/] {loader} {loader_version} for MC {mc_version}"
            )
    if loader_version is None and loader not in ("vanilla",):
        console.print(
            f"[yellow]![/] {loader} build is not pinned; "
            "the container will choose the latest at startup"
        )

    instance = Instance(
        name=name,
        slug=slug,
        kind="server",
        loader=loader,
        mc_version=mc_version,
        loader_version=loader_version,
        memory=(args.memory or "4G").upper(),
        tz=args.tz or default_tz(),
        java_version=args.java or java_for_mc(mc_version),
        eula=True,
        rcon_password=args.rcon_password or new_rcon_password(),
        port=args.port,
        rcon_port=args.rcon_port,
        bluemap_port=None if args.no_bluemap else args.bluemap_port,
        motd=args.motd,
        created_at=utc_now(),
        restore_ownership=not args.no_restore_ownership,
    )
    instance.validate()

    layout.instance_dir(slug).mkdir(parents=True, exist_ok=True)
    layout.server_dir(slug).mkdir(parents=True, exist_ok=True)
    layout.backups_for(slug).mkdir(parents=True, exist_ok=True)
    save_instance(layout, instance)
    write_compose(layout, instance)

    console.print(
        Panel(
            f"[green]Created[/] {instance.name}\n"
            f"[dim]Config:[/] {layout.config_file(slug)}\n"
            f"[dim]Data:[/]   {layout.server_dir(slug)}\n\n"
            f"Start it with: [bold]mchost start {slug}[/]",
            title="Instance created",
            border_style="green",
        )
    )
    return 0


def _running_others(layout: Layout, engine: Engine, slug: str) -> list[str]:
    return [other for other in engine.running_slugs() if other != slug]


def _stop_instance(
    layout: Layout, engine: Engine, instance: Instance, *, quiet: bool = False
) -> None:
    state = engine.state(instance.slug)
    if state == "missing":
        if not quiet:
            console.print(f"[dim]{instance.slug} has no container yet.[/]")
        return
    engine.down(instance.slug)
    if state == "running":
        if not quiet:
            console.print(f"[green]Stopped[/] {instance.name}")
    elif not quiet:
        console.print(f"[dim]Removed the {state} container for {instance.slug}.[/]")
    warning = restore_ownership(layout, instance)
    if warning:
        console.print(f"[yellow]![/] {warning}")


def _start_instance(
    layout: Layout,
    engine: Engine,
    instance: Instance,
    *,
    switch: bool,
    quiet: bool = False,
) -> None:
    if engine.is_running(instance.slug):
        if not quiet:
            console.print(f"[green]{instance.slug} is already running.[/]")
        return

    others = _running_others(layout, engine, instance.slug)
    if others:
        if not switch:
            names = ", ".join(others)
            raise MchostError(
                f"{names} is already running. mchost runs one server at a time; "
                f"use 'mchost switch {instance.slug}' to replace it."
            )
        for slug in others:
            other = resolve_instance(layout, slug)
            _stop_instance(layout, engine, other)

    total = host_memory()
    if total and parse_memory(instance.memory) > total * 0.85:
        console.print(
            f"[yellow]![/] {instance.memory} is more than 85% of host RAM "
            f"({total / 1024**3:.1f}G)"
        )

    write_compose(layout, instance)
    engine.up(instance.slug)
    if not quiet:
        console.print(f"[green]Started[/] {instance.name}")
        console.print(
            f"[dim]Game port {instance.port} · RCON 127.0.0.1:{instance.rcon_port} · "
            f"logs: mchost logs {instance.slug}[/]"
        )


def cmd_start(layout: Layout, args: argparse.Namespace) -> int:
    instance = resolve_instance(layout, args.name)
    engine = _engine(layout)
    _start_instance(layout, engine, instance, switch=args.switch)
    return 0


def cmd_stop(layout: Layout, args: argparse.Namespace) -> int:
    instance = resolve_instance(layout, args.name)
    engine = _engine(layout)
    _stop_instance(layout, engine, instance)
    return 0


def cmd_restart(layout: Layout, args: argparse.Namespace) -> int:
    instance = resolve_instance(layout, args.name)
    engine = _engine(layout)
    _stop_instance(layout, engine, instance)
    _start_instance(layout, engine, instance, switch=args.switch)
    return 0


def cmd_switch(layout: Layout, args: argparse.Namespace) -> int:
    instance = resolve_instance(layout, args.name)
    engine = _engine(layout)
    others = _running_others(layout, engine, instance.slug)
    for slug in others:
        other = resolve_instance(layout, slug)
        _stop_instance(layout, engine, other)
    if engine.is_running(instance.slug):
        console.print(f"[green]{instance.slug} is already running.[/]")
        return 0
    _start_instance(layout, engine, instance, switch=True)
    return 0


def cmd_logs(layout: Layout, args: argparse.Namespace) -> int:
    instance = resolve_instance(layout, args.name)
    engine = _engine(layout)
    if engine.state(instance.slug) == "missing":
        raise MchostError(
            f"{instance.slug} has no container yet; start it with 'mchost start {instance.slug}'"
        )
    try:
        engine.logs(instance.slug, tail=args.tail, follow=not args.no_follow)
    except KeyboardInterrupt:
        pass
    return 0


def cmd_console(layout: Layout, args: argparse.Namespace) -> int:
    instance = resolve_instance(layout, args.name)
    engine = _engine(layout)
    if not engine.is_running(instance.slug):
        raise MchostError(f"{instance.slug} is not running")
    if not sys.stdin.isatty():
        raise MchostError("the interactive console needs a TTY")
    engine.exec(instance.slug, ["rcon-cli"], tty=True)
    return 0


def cmd_cmd(layout: Layout, args: argparse.Namespace) -> int:
    instance = resolve_instance(layout, args.name)
    engine = _engine(layout)
    if not engine.is_running(instance.slug):
        raise MchostError(f"{instance.slug} is not running")
    command = " ".join(args.rcon_command).strip()
    if not command:
        raise ConfigError('nothing to send; usage: mchost cmd <instance> "say hello"')
    engine.exec(instance.slug, ["rcon-cli", command], tty=False)
    return 0


def cmd_path(layout: Layout, args: argparse.Namespace) -> int:
    instance = resolve_instance(layout, args.name)
    if args.json:
        console.print_json(json.dumps(instance.to_dict()))
        return 0
    table = Table.grid(padding=(0, 2))
    table.add_column(style="dim", no_wrap=True)
    table.add_column()
    table.add_row("config", str(layout.config_file(instance.slug)))
    table.add_row("compose", str(layout.compose_file(instance.slug)))
    table.add_row("server", str(layout.server_dir_for(instance)))
    table.add_row("pack", str(layout.pack_dir(instance.slug)))
    table.add_row("backups", str(layout.backups_for(instance.slug)))
    console.print(table)
    return 0


def cmd_compose(layout: Layout, args: argparse.Namespace) -> int:
    instance = resolve_instance(layout, args.name)
    path, changed = write_compose(layout, instance)
    if args.path:
        console.print(str(path))
        return 0
    if args.write:
        console.print(f"[green]{'Updated' if changed else 'Up to date'}:[/] {path}")
        return 0
    console.print(Syntax(render_compose(instance, layout.compose_data_source(instance)), "yaml", theme="ansi_dark"))
    return 0


def _infer_loader(source: Path) -> tuple[str, str | None, str | None]:
    """Make a best effort to identify an existing server directory."""
    libraries = source / "libraries"
    if (libraries / "net" / "neoforged" / "neoforge").is_dir():
        version = None
        for entry in sorted((libraries / "net" / "neoforged" / "neoforge").iterdir()):
            if entry.is_dir():
                version = entry.name
        return "neoforge", version, None
    forge = libraries / "net" / "minecraftforge" / "forge"
    if forge.is_dir():
        version = None
        mc_version = None
        for entry in sorted(forge.iterdir()):
            if entry.is_dir():
                version = entry.name
                mc_version = entry.name.split("-", 1)[0]
        return "forge", version, mc_version
    if (source / "fabric-server-launch.jar").is_file() or (
        libraries / "net" / "fabricmc"
    ).is_dir():
        return "fabric", None, None
    if (source / "quilt-server-launch.jar").is_file() or (
        libraries / "org" / "quiltmc"
    ).is_dir():
        return "quilt", None, None
    if any(source.glob("paper-*.jar")):
        return "paper", None, None
    return "vanilla", None, None


def cmd_import(layout: Layout, args: argparse.Namespace) -> int:
    source = Path(args.path).expanduser().resolve()
    if not source.is_dir():
        raise ConfigError(f"{source} is not a directory")
    if not any(source.iterdir()):
        raise ConfigError(f"{source} is empty")

    name = args.name or source.name
    slug = slugify(name)
    if layout.config_file(slug).is_file():
        raise ConfigError(f"instance {slug!r} already exists")

    inferred_loader, inferred_loader_version, inferred_mc = _infer_loader(source)
    loader = (args.loader or inferred_loader).lower()
    mc_version = args.mc_version or inferred_mc or "LATEST"
    loader_version = args.loader_version or inferred_loader_version
    if mc_version == "LATEST":
        console.print("[yellow]![/] Minecraft version could not be inferred; set it in instance.toml")

    target = layout.server_dir(slug)
    layout.instance_dir(slug).mkdir(parents=True, exist_ok=True)
    if source == target.resolve():
        console.print("[dim]Directory is already in place.[/]")
    elif args.copy:
        shutil.copytree(source, target)
    else:
        if target.exists():
            raise ConfigError(f"{target} already exists")
        shutil.move(str(source), str(target))

    instance = Instance(
        name=name,
        slug=slug,
        kind="server",
        loader=loader,
        mc_version=mc_version,
        loader_version=loader_version,
        memory=(args.memory or "4G").upper(),
        tz=args.tz or default_tz(),
        java_version=args.java or java_for_mc(mc_version),
        eula=True,
        rcon_password=args.rcon_password or new_rcon_password(),
        port=args.port,
        rcon_port=args.rcon_port,
        bluemap_port=None if args.no_bluemap else args.bluemap_port,
        created_at=utc_now(),
        restore_ownership=not args.no_restore_ownership,
    )
    instance.validate()
    layout.backups_for(slug).mkdir(parents=True, exist_ok=True)
    save_instance(layout, instance)
    write_compose(layout, instance)
    console.print(
        f"[green]Imported[/] {name} as [bold]{slug}[/] "
        f"({loader} · MC {mc_version})"
    )
    return 0


def cmd_migrate(layout: Layout, args: argparse.Namespace) -> int:
    if already_migrated(layout) and not args.force:
        console.print(
            "[dim]Already migrated. Use 'mchost migrate --force' to re-import.[/]"
        )
        return 0
    result = migrate(layout)
    console.print(
        f"[green]Imported[/] {result.instance.name} as "
        f"[bold]{result.instance.slug}[/]"
    )
    for warning in result.warnings:
        console.print(f"[yellow]![/] {warning}")
    return 0


def cmd_modpack_search(layout: Layout, args: argparse.Namespace) -> int:
    hits = search_modpacks(args.query, limit=args.limit, offset=args.offset)
    if args.json:
        console.print_json(
            json.dumps(
                [
                    {
                        "slug": hit.slug,
                        "title": hit.title,
                        "description": hit.description,
                        "downloads": hit.downloads,
                        "loaders": list(hit.loaders),
                        "game_versions": list(hit.game_versions),
                    }
                    for hit in hits
                ]
            )
        )
        return 0
    if not hits:
        console.print("No modpacks found.")
        return 0
    table = Table(title=f"Modrinth modpacks · {args.query}", title_justify="left")
    table.add_column("Title", style="bold")
    table.add_column("Slug", style="dim")
    table.add_column("Loaders")
    table.add_column("MC versions", style="dim")
    table.add_column("Downloads", justify="right")
    for hit in hits:
        mc = ", ".join(hit.game_versions[:4])
        if len(hit.game_versions) > 4:
            mc += "…"
        table.add_row(
            hit.title,
            hit.slug,
            ", ".join(hit.loaders),
            mc,
            f"{hit.downloads:,}",
        )
    console.print(table)
    return 0


def cmd_modpack_versions(layout: Layout, args: argparse.Namespace) -> int:
    project = get_project(args.slug)
    versions = get_versions(project.slug)
    if args.json:
        console.print_json(
            json.dumps(
                [
                    {
                        "id": version.id,
                        "version_number": version.version_number,
                        "name": version.name,
                        "type": version.version_type,
                        "game_versions": list(version.game_versions),
                        "loaders": list(version.loaders),
                        "environment": version.environment,
                        "published": version.date_published,
                        "files": [
                            {
                                "filename": file.filename,
                                "primary": file.primary,
                                "size": file.size,
                            }
                            for file in version.files
                        ],
                    }
                    for version in versions
                ]
            )
        )
        return 0
    table = Table(
        title=f"{project.title} versions", title_justify="left"
    )
    table.add_column("Version", style="bold")
    table.add_column("Type")
    table.add_column("MC")
    table.add_column("Loaders")
    table.add_column("Published", style="dim")
    for version in versions[: args.limit if args.limit > 0 else None]:
        table.add_row(
            version.version_number,
            version.version_type,
            ", ".join(version.game_versions[:3]),
            ", ".join(version.loaders),
            version.date_published[:10],
        )
    console.print(table)
    return 0


def cmd_modpack_resolve(layout: Layout, args: argparse.Namespace) -> int:
    project = get_project(args.slug)
    versions = get_versions(project.slug)
    version = select_version(versions, args.version)
    artifact = resolve_artifact(version, versions, mode=args.mode)
    payload = {
        "project": project.title,
        "slug": project.slug,
        "version_id": version.id,
        "version_number": version.version_number,
        "version_type": version.version_type,
        "reason": artifact.reason,
        "mode": artifact.mode,
        "file": artifact.file.filename,
        "kind": artifact.kind,
        "size": artifact.file.size,
        "sha512": artifact.file.sha512,
        "url": artifact.file.url,
    }
    if args.json:
        console.print_json(json.dumps(payload))
        return 0
    table = Table.grid(padding=(0, 2))
    table.add_column(style="dim", no_wrap=True)
    table.add_column()
    table.add_row("Project", f"{project.title} ({project.slug})")
    table.add_row("Version", version.version_number)
    table.add_row("Type", version.version_type)
    table.add_row("Pack mode", artifact.mode)
    table.add_row("Reason", artifact.reason)
    table.add_row("File", artifact.file.filename)
    table.add_row("Kind", artifact.kind)
    table.add_row("Size", f"{artifact.file.size / 1024 / 1024:.1f} MB")
    table.add_row("SHA-512", artifact.file.sha512)
    console.print(Panel(table, title="Resolved modpack", border_style="cyan"))
    console.print(
        f"[dim]Install it with: mchost modpack install {project.slug} "
        f"--version {version.version_number} --mode {artifact.mode}[/]"
    )
    return 0


def cmd_modpack_install(layout: Layout, args: argparse.Namespace) -> int:
    with console.status("Resolving and downloading modpack…"):
        result = install_modpack(
            layout,
            args.slug,
            version_reference=args.version,
            name=args.name,
            mode=args.mode,
            memory=args.memory,
        )
    if args.json:
        console.print_json(
            json.dumps(
                {
                    "slug": result.instance.slug,
                    "name": result.instance.name,
                    "project": result.project.slug,
                    "version": result.version.version_number,
                    "mode": result.artifact.mode,
                    "file": result.artifact.file.filename,
                    "sha512": result.artifact.file.sha512,
                    "path": str(layout.instance_dir(result.instance.slug)),
                }
            )
        )
        return 0
    console.print(
        Panel(
            f"[green]Installed[/] {result.instance.name}\n"
            f"[dim]Slug:[/] {result.instance.slug}\n"
            f"[dim]Pack:[/] {result.project.title} {result.version.version_number}\n"
            f"[dim]Mode:[/] {result.artifact.mode} ({result.artifact.reason})\n"
            f"[dim]File:[/] {result.pack_path}\n\n"
            f"Start it with: [bold]mchost start {result.instance.slug}[/]",
            title="Modpack installed",
            border_style="green",
        )
    )
    return 0


def cmd_modpack_local(layout: Layout, args: argparse.Namespace) -> int:
    path = Path(args.path).expanduser().resolve()
    try:
        info = inspect_local_pack(path)
    except MchostError as exc:
        console.print(f"[red]error:[/] {exc}")
        return 1
    loader = args.loader or info.loader
    mc_version = args.mc_version or info.mc_version
    loader_version = args.loader_version or info.loader_version
    name = args.name or info.name
    if not loader or not mc_version:
        console.print(f"[yellow]Could not infer the pack details.[/] {info.notes}")
        console.print(
            "Pass --loader and --mc-version explicitly, for example:\n"
            f"  mchost modpack local {path} --loader forge --mc-version 1.20.1"
        )
        return 1
    with console.status("Extracting and installing the local server pack…"):
        instance = install_local_pack(
            layout,
            path,
            name=name,
            loader=loader,
            mc_version=mc_version,
            loader_version=loader_version,
            memory=args.memory,
        )
    if args.json:
        console.print_json(
            json.dumps(
                {
                    "slug": instance.slug,
                    "name": instance.name,
                    "loader": instance.loader,
                    "mc_version": instance.mc_version,
                    "loader_version": instance.loader_version,
                    "file": instance.modpack.file_name if instance.modpack else None,
                }
            )
        )
        return 0
    console.print(
        Panel(
            f"[green]Installed[/] {instance.name}\n"
            f"[dim]Slug:[/] {instance.slug}\n"
            f"[dim]Pack:[/] {info.name} {info.pack_version}\n"
            f"[dim]Loader:[/] {LOADER_LABELS.get(instance.loader, instance.loader)} "
            f"{instance.loader_version or ''} · MC {instance.mc_version}\n\n"
            f"Start it with: [bold]mchost start {instance.slug}[/]",
            title="Local CurseForge pack installed",
            border_style="green",
        )
    )
    return 0


def cmd_modpack(layout: Layout, args: argparse.Namespace) -> int:
    if args.modpack_command == "search":
        return cmd_modpack_search(layout, args)
    if args.modpack_command == "versions":
        return cmd_modpack_versions(layout, args)
    if args.modpack_command == "resolve":
        return cmd_modpack_resolve(layout, args)
    if args.modpack_command == "install":
        return cmd_modpack_install(layout, args)
    if args.modpack_command == "local":
        return cmd_modpack_local(layout, args)
    if args.modpack_command == "check":
        return cmd_modpack_check(layout, args)
    if args.modpack_command == "update":
        return cmd_modpack_update(layout, args)
    raise MchostError("unknown modpack command")


def cmd_modpack_check(layout: Layout, args: argparse.Namespace) -> int:
    instance = resolve_instance(layout, args.name)
    try:
        update = check_modpack_update(instance)
    except MchostError as exc:
        console.print(f"[red]error:[/] {exc}")
        return 1
    payload = {
        "instance": instance.slug,
        "project": update.project.slug,
        "current": instance.modpack.version_number if instance.modpack else None,
        "latest": update.latest.version_number,
        "available": update.available,
        "mode": update.artifact.mode,
        "file": update.artifact.file.filename,
        "reason": update.reason,
    }
    if args.json:
        console.print_json(json.dumps(payload))
        return 0
    table = Table.grid(padding=(0, 2))
    table.add_column(style="dim", no_wrap=True)
    table.add_column()
    table.add_row("Instance", instance.slug)
    table.add_row("Project", update.project.title)
    table.add_row("Current", str(payload["current"]))
    table.add_row("Latest", update.latest.version_number)
    table.add_row("Available", "yes" if update.available else "no")
    table.add_row("Mode", update.artifact.mode)
    table.add_row("File", update.artifact.file.filename)
    table.add_row("Reason", update.reason)
    console.print(Panel(table, title="Modpack update", border_style="cyan"))
    if update.available:
        console.print(
            f"[dim]Update with: mchost modpack update {instance.slug}[/]"
        )
    return 0


def cmd_modpack_update(layout: Layout, args: argparse.Namespace) -> int:
    instance = resolve_instance(layout, args.name)
    try:
        engine = _engine(layout)
    except MchostError:
        engine = None
    with console.status("Checking and downloading the modpack update…"):
        result = update_modpack(
            layout,
            instance,
            version_reference=args.version,
            mode=args.mode,
            engine=engine,
            restart=not args.no_restart,
        )
    if args.json:
        console.print_json(
            json.dumps(
                {
                    "instance": result.instance.slug,
                    "project": result.project.slug,
                    "old_version": result.old_version,
                    "new_version": result.new_version,
                    "updated": result.updated,
                    "mode": result.artifact.mode,
                    "file": result.artifact.file.filename,
                }
            )
        )
        return 0
    if not result.updated:
        console.print(
            f"[green]{result.instance.slug}[/] is already up to date "
            f"({result.old_version})."
        )
        return 0
    console.print(
        f"[green]Updated[/] {result.instance.name}: "
        f"{result.old_version} → {result.new_version} "
        f"({result.artifact.mode} pack, {result.artifact.file.filename})"
    )
    if not args.no_restart and engine is not None and engine.is_running(result.instance.slug):
        console.print(f"[green]Restarted[/] {result.instance.name}")
    return 0


def _confirm(message: str) -> bool:
    if not sys.stdin.isatty():
        raise MchostError("pass --yes to confirm in a non-interactive session")
    answer = console.input(f"[yellow]{message}[/] [y/N]: ").strip().lower()
    return answer in ("y", "yes")


def cmd_memory(layout: Layout, args: argparse.Namespace) -> int:
    instance = resolve_instance(layout, args.name)
    value = args.value.strip().upper()
    old = instance.memory
    instance.memory = value
    instance.validate()
    save_instance(layout, instance)
    write_compose(layout, instance)
    console.print(f"[green]Memory updated[/] {instance.slug}: {old} → {value}")
    try:
        engine = _engine(layout)
        if engine.is_running(instance.slug):
            console.print("[dim]Restart the instance to apply the new heap.[/]")
    except MchostError:
        pass
    return 0


def cmd_delete(layout: Layout, args: argparse.Namespace) -> int:
    instance = resolve_instance(layout, args.name)
    if not args.yes and not _confirm(
        f"Delete {instance.name}? This removes "
        f"{layout.instance_dir(instance.slug)}, "
        f"{layout.server_dir_for(instance)}, and "
        f"{layout.backups_for(instance.slug)}. This cannot be undone."
    ):
        console.print("Cancelled.")
        return 0
    try:
        engine = _engine(layout)
    except MchostError:
        engine = None
    removed = delete_instance(layout, instance, engine=engine)
    console.print(f"[green]Deleted[/] {instance.name}")
    for path in removed:
        console.print(f"[dim]removed {path}[/]")
    return 0


def cmd_backup(layout: Layout, args: argparse.Namespace) -> int:
    instance = resolve_instance(layout, args.name)
    try:
        engine = _engine(layout)
    except MchostError:
        engine = None
    backup = create_backup(
        layout,
        instance,
        kind=args.kind,
        keep=args.keep,
        engine=engine,
    )
    if args.json:
        console.print_json(json.dumps(backup.manifest))
        return 0
    console.print(
        f"[green]Created {backup.kind} backup[/] {backup.backup_id} "
        f"({human_size(backup.size)})"
    )
    console.print(f"[dim]{backup.path}[/]")
    return 0


def cmd_backups(layout: Layout, args: argparse.Namespace) -> int:
    instance = resolve_instance(layout, args.name)
    backups = list_backups(layout, instance.slug)
    if args.json:
        console.print_json(
            json.dumps([backup.manifest for backup in backups])
        )
        return 0
    if not backups:
        console.print(f"No backups for {instance.slug}.")
        return 0
    table = Table(
        title=f"Backups · {instance.name}", title_justify="left"
    )
    table.add_column("ID", style="bold")
    table.add_column("Created")
    table.add_column("Kind")
    table.add_column("Size", justify="right")
    table.add_column("Archive", style="dim")
    for backup in backups:
        table.add_row(
            backup.backup_id,
            backup.created_at,
            backup.kind,
            human_size(backup.size),
            backup.archive_path.name,
        )
    console.print(table)
    return 0


def cmd_restore(layout: Layout, args: argparse.Namespace) -> int:
    instance = resolve_instance(layout, args.name)
    backup = get_backup(layout, instance.slug, args.backup)
    if not args.yes and not _confirm(
        f"Restore {backup.backup_id} over {instance.slug}? "
        "Existing world/config files will be replaced."
    ):
        console.print("Cancelled.")
        return 0
    engine = _engine(layout)
    restore_backup(
        layout,
        instance,
        backup,
        engine=engine,
        start_after=args.start,
    )
    console.print(
        f"[green]Restored[/] {backup.backup_id} into {instance.slug}"
    )
    if args.start:
        console.print(f"[green]Started[/] {instance.name}")
    return 0


def cmd_prune(layout: Layout, args: argparse.Namespace) -> int:
    instance = resolve_instance(layout, args.name)
    keep = args.keep if args.keep is not None else instance.backup_keep
    removed = prune_backups(layout, instance.slug, keep)
    if removed:
        console.print(
            f"[green]Pruned[/] {len(removed)} backup(s), keeping {keep}: "
            + ", ".join(removed)
        )
    else:
        console.print(f"Nothing to prune; keeping {keep} backups.")
    return 0


def cmd_delete_backup(layout: Layout, args: argparse.Namespace) -> int:
    instance = resolve_instance(layout, args.name)
    backup = get_backup(layout, instance.slug, args.backup)
    if not args.yes and not _confirm(
        f"Delete backup {backup.backup_id} for {instance.slug}?"
    ):
        console.print("Cancelled.")
        return 0
    delete_backup(layout, instance.slug, backup.backup_id)
    console.print(f"[green]Deleted[/] {backup.backup_id}")
    return 0


def cmd_versions(layout: Layout, args: argparse.Namespace) -> int:
    loader = args.loader
    try:
        if args.mc_version:
            builds = loader_builds(loader, args.mc_version)
            if args.json:
                console.print_json(
                    json.dumps(
                        {
                            "loader": loader,
                            "mc_version": args.mc_version,
                            "builds": [
                                {
                                    "version": build.version,
                                    "stable": build.stable,
                                    "latest": build.latest,
                                    "recommended": build.recommended,
                                }
                                for build in builds
                            ],
                        }
                    )
                )
                return 0
            if loader == "vanilla":
                console.print(
                    f"Vanilla has no loader builds; use --mc-version {args.mc_version}."
                )
                return 0
            if not builds:
                console.print(
                    f"[yellow]No {loader} builds found for Minecraft {args.mc_version}.[/]"
                )
                return 1
            table = Table(
                title=f"{loader} builds for Minecraft {args.mc_version}",
                title_justify="left",
            )
            table.add_column("Build", style="bold")
            table.add_column("Channel")
            table.add_column("Notes")
            for build in builds[: args.limit if args.limit > 0 else None]:
                notes = []
                if build.recommended:
                    notes.append("recommended")
                if build.latest:
                    notes.append("latest")
                table.add_row(
                    build.version,
                    "stable" if build.stable else "prerelease",
                    ", ".join(notes) or "[dim]—[/]",
                )
            console.print(table)
            return 0

        versions = supported_mc_versions(
            loader, include_snapshots=args.snapshots
        )
        if args.limit > 0:
            versions = versions[: args.limit]
        if args.json:
            console.print_json(
                json.dumps({"loader": loader, "versions": versions})
            )
            return 0
        table = Table(
            title=f"Minecraft versions supported by {loader}",
            title_justify="left",
        )
        table.add_column("#", justify="right", style="dim")
        table.add_column("Minecraft", style="bold")
        for index, version in enumerate(versions, start=1):
            table.add_row(str(index), version)
        console.print(table)
        return 0
    except MchostError as exc:
        console.print(f"[red]error:[/] {exc}")
        return 1


def cmd_doctor(layout: Layout) -> int:
    checks = run_checks(layout)
    table = Table(title="mchost doctor", title_justify="left")
    table.add_column("Status", no_wrap=True)
    table.add_column("Check", style="bold")
    table.add_column("Detail")

    styles = {OK: "green", WARN: "yellow", FAIL: "red"}
    labels = {OK: "ok", WARN: "warn", FAIL: "fail"}
    for check in checks:
        table.add_row(
            f"[{styles[check.status]}]{labels[check.status]}[/]",
            check.title,
            check.detail,
        )
    console.print(table)
    return 1 if any(check.status == FAIL for check in checks) else 0


def _common_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        "--root",
        default=argparse.SUPPRESS,
        help="host directory (default: nearest parent containing instances/)",
    )
    return parser


def build_parser() -> argparse.ArgumentParser:
    common = _common_parser()
    parser = argparse.ArgumentParser(
        prog="mchost",
        description="Host and manage containerized Minecraft servers.",
        parents=[common],
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    sub = parser.add_subparsers(dest="command")

    sub.add_parser("list", aliases=["ls"], parents=[common], help="list instances")
    sub.add_parser("tui", parents=[common], help="open the interactive dashboard")
    sub.add_parser("doctor", parents=[common], help="check engine, ports, and instances")
    migrate_parser = sub.add_parser(
        "migrate", parents=[common], help="import the legacy .env configuration"
    )
    migrate_parser.add_argument("--force", action="store_true", help="re-import")

    versions_parser = sub.add_parser(
        "versions", parents=[common], help="list Minecraft and loader versions"
    )
    versions_parser.add_argument("loader", choices=LOADERS)
    versions_parser.add_argument("--mc-version", dest="mc_version")
    versions_parser.add_argument("--snapshots", action="store_true")
    versions_parser.add_argument(
        "--limit", type=int, default=25, help="maximum rows (0 = all)"
    )
    versions_parser.add_argument("--json", action="store_true")

    backup_parser = sub.add_parser(
        "backup", parents=[common], help="create a world or full backup"
    )
    backup_parser.add_argument("name")
    backup_parser.add_argument(
        "--kind", choices=("world", "full"), default="world"
    )
    backup_parser.add_argument(
        "--keep", type=int, help="retention count (default: instance setting)"
    )
    backup_parser.add_argument("--json", action="store_true")

    backups_parser = sub.add_parser(
        "backups", parents=[common], help="list backups for an instance"
    )
    backups_parser.add_argument("name")
    backups_parser.add_argument("--json", action="store_true")

    restore_parser = sub.add_parser(
        "restore", parents=[common], help="restore a backup over an instance"
    )
    restore_parser.add_argument("name")
    restore_parser.add_argument("backup", help="backup ID or 'latest'")
    restore_parser.add_argument("--start", action="store_true")
    restore_parser.add_argument("--yes", action="store_true")

    prune_parser = sub.add_parser(
        "prune", parents=[common], help="delete old backups"
    )
    prune_parser.add_argument("name")
    prune_parser.add_argument("--keep", type=int)

    delete_backup_parser = sub.add_parser(
        "delete-backup", parents=[common], help="delete one backup"
    )
    delete_backup_parser.add_argument("name")
    delete_backup_parser.add_argument("backup")
    delete_backup_parser.add_argument("--yes", action="store_true")

    delete_parser = sub.add_parser(
        "delete", parents=[common], help="delete an instance and all its files"
    )
    delete_parser.add_argument("name")
    delete_parser.add_argument("--yes", action="store_true")

    memory_parser = sub.add_parser(
        "memory", parents=[common], help="change an instance's Java heap"
    )
    memory_parser.add_argument("name")
    memory_parser.add_argument("value", help="e.g. 4G, 6G, 8G, or 4096M")

    modpack_parser = sub.add_parser(
        "modpack", parents=[common], help="search and install Modrinth modpacks"
    )
    modpack_sub = modpack_parser.add_subparsers(
        dest="modpack_command", required=True
    )

    pack_search = modpack_sub.add_parser(
        "search", parents=[common], help="search Modrinth modpacks"
    )
    pack_search.add_argument("query")
    pack_search.add_argument("--limit", type=int, default=20)
    pack_search.add_argument("--offset", type=int, default=0)
    pack_search.add_argument("--json", action="store_true")

    pack_versions = modpack_sub.add_parser(
        "versions", parents=[common], help="list modpack versions"
    )
    pack_versions.add_argument("slug")
    pack_versions.add_argument("--limit", type=int, default=50)
    pack_versions.add_argument("--json", action="store_true")

    pack_resolve = modpack_sub.add_parser(
        "resolve", parents=[common], help="show which file would be installed"
    )
    pack_resolve.add_argument("slug")
    pack_resolve.add_argument("--version", help="version ID or number")
    pack_resolve.add_argument(
        "--mode", choices=("auto", "server", "full"), default="auto"
    )
    pack_resolve.add_argument("--json", action="store_true")

    pack_install = modpack_sub.add_parser(
        "install", parents=[common], help="download and create an instance"
    )
    pack_install.add_argument("slug")
    pack_install.add_argument("--version", help="version ID or number")
    pack_install.add_argument("--name", help="instance display name")
    pack_install.add_argument(
        "--mode", choices=("auto", "server", "full"), default="auto"
    )
    pack_install.add_argument("--memory", default="6G")
    pack_install.add_argument("--json", action="store_true")

    pack_local = modpack_sub.add_parser(
        "local", parents=[common], help="install a local CurseForge server pack"
    )
    pack_local.add_argument("path")
    pack_local.add_argument("--name")
    pack_local.add_argument("--loader", choices=LOADERS)
    pack_local.add_argument("--mc-version", dest="mc_version")
    pack_local.add_argument("--loader-version", dest="loader_version")
    pack_local.add_argument("--memory", default="6G")
    pack_local.add_argument("--json", action="store_true")

    pack_check = modpack_sub.add_parser(
        "check", parents=[common], help="check an instance for a newer pack"
    )
    pack_check.add_argument("name")
    pack_check.add_argument("--json", action="store_true")

    pack_update = modpack_sub.add_parser(
        "update", parents=[common], help="update a modpack instance"
    )
    pack_update.add_argument("name")
    pack_update.add_argument("--version", help="version ID or number")
    pack_update.add_argument(
        "--mode", choices=("auto", "server", "full"), default="auto"
    )
    pack_update.add_argument(
        "--no-restart",
        action="store_true",
        help="leave the server stopped if it was running",
    )
    pack_update.add_argument("--json", action="store_true")

    new = sub.add_parser("new", parents=[common], help="create an instance")
    new.add_argument("name", help="display name (used to derive the slug)")
    new.add_argument("--loader", choices=LOADERS, default="vanilla")
    new.add_argument("--mc-version", dest="mc_version")
    new.add_argument("--loader-version", dest="loader_version")
    new.add_argument(
        "--no-pin",
        action="store_true",
        help="do not resolve a loader build; let the container choose",
    )
    new.add_argument("--memory", help="Java heap, e.g. 4G or 2048M")
    new.add_argument("--tz", help="timezone (default: system timezone)")
    new.add_argument("--java", type=int, help="Java major version (default: inferred)")
    new.add_argument("--port", type=int, default=25565, help="game port (default 25565)")
    new.add_argument("--rcon-port", type=int, default=25575)
    new.add_argument("--bluemap-port", type=int, default=8100)
    new.add_argument("--no-bluemap", action="store_true", help="do not publish 8100")
    new.add_argument("--motd")
    new.add_argument("--rcon-password")
    new.add_argument("--no-restore-ownership", action="store_true")

    start = sub.add_parser("start", parents=[common], help="start an instance")
    start.add_argument("name")
    start.add_argument(
        "--switch",
        action="store_true",
        help="stop another running instance first",
    )

    stop = sub.add_parser("stop", parents=[common], help="stop an instance")
    stop.add_argument("name")

    restart = sub.add_parser("restart", parents=[common], help="restart an instance")
    restart.add_argument("name")
    restart.add_argument("--switch", action="store_true")

    switch = sub.add_parser(
        "switch", parents=[common], help="stop whatever is running and start this one"
    )
    switch.add_argument("name")

    status = sub.add_parser("status", parents=[common], help="show instance details")
    status.add_argument("name", nargs="?")

    logs = sub.add_parser("logs", parents=[common], help="follow container logs")
    logs.add_argument("name")
    logs.add_argument("--tail", type=int, default=200)
    logs.add_argument("--no-follow", action="store_true")

    console_parser = sub.add_parser(
        "console", parents=[common], help="open an interactive RCON console"
    )
    console_parser.add_argument("name")

    cmd = sub.add_parser("cmd", parents=[common], help="send one RCON command")
    cmd.add_argument("name")
    cmd.add_argument("rcon_command", nargs=argparse.REMAINDER)

    path = sub.add_parser("path", parents=[common], help="print instance paths")
    path.add_argument("name")
    path.add_argument("--json", action="store_true", help="print the config as JSON")

    compose = sub.add_parser(
        "compose", parents=[common], help="show or regenerate compose.yaml"
    )
    compose.add_argument("name")
    compose.add_argument("--write", action="store_true", help="write the file only")
    compose.add_argument("--path", action="store_true", help="print the file path only")

    imp = sub.add_parser("import", parents=[common], help="adopt an existing server dir")
    imp.add_argument("path")
    imp.add_argument("--name")
    imp.add_argument("--loader", choices=LOADERS)
    imp.add_argument("--mc-version", dest="mc_version")
    imp.add_argument("--loader-version", dest="loader_version")
    imp.add_argument("--memory")
    imp.add_argument("--tz")
    imp.add_argument("--java", type=int)
    imp.add_argument("--port", type=int, default=25565)
    imp.add_argument("--rcon-port", type=int, default=25575)
    imp.add_argument("--bluemap-port", type=int, default=8100)
    imp.add_argument("--no-bluemap", action="store_true")
    imp.add_argument("--rcon-password")
    imp.add_argument("--no-restore-ownership", action="store_true")
    imp.add_argument("--copy", action="store_true", help="copy instead of moving")

    return parser


def _dispatch(layout: Layout, args: argparse.Namespace) -> int:
    command = args.command
    if command == "tui":
        return run_tui(layout)
    if command in ("list", "ls"):
        return cmd_list(layout)
    if command == "status":
        return cmd_status(layout, args.name)
    if command == "new":
        return cmd_new(layout, args)
    if command == "start":
        return cmd_start(layout, args)
    if command == "stop":
        return cmd_stop(layout, args)
    if command == "restart":
        return cmd_restart(layout, args)
    if command == "switch":
        return cmd_switch(layout, args)
    if command == "logs":
        return cmd_logs(layout, args)
    if command == "console":
        return cmd_console(layout, args)
    if command == "cmd":
        return cmd_cmd(layout, args)
    if command == "path":
        return cmd_path(layout, args)
    if command == "compose":
        return cmd_compose(layout, args)
    if command == "import":
        return cmd_import(layout, args)
    if command == "migrate":
        return cmd_migrate(layout, args)
    if command == "versions":
        return cmd_versions(layout, args)
    if command == "backup":
        return cmd_backup(layout, args)
    if command == "backups":
        return cmd_backups(layout, args)
    if command == "restore":
        return cmd_restore(layout, args)
    if command == "prune":
        return cmd_prune(layout, args)
    if command == "delete-backup":
        return cmd_delete_backup(layout, args)
    if command == "delete":
        return cmd_delete(layout, args)
    if command == "memory":
        return cmd_memory(layout, args)
    if command == "modpack":
        return cmd_modpack(layout, args)
    if command == "doctor":
        return cmd_doctor(layout)

    if sys.stdin.isatty() and sys.stdout.isatty():
        return run_tui(layout)

    console.print(f"[bold]mchost {__version__}[/] — containerized Minecraft hosting")
    console.print("[dim]The interactive dashboard needs a TTY; showing the instance list.[/]")
    console.print()
    return cmd_list(layout)


def run_tui(layout: Layout) -> int:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        console.print(
            "[red]error:[/] the dashboard needs a TTY; use 'mchost list' "
            "or the CLI commands."
        )
        return 1
    from .tui.app import MchostApp

    MchostApp(layout).run()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        layout = _layout(args)
    except OSError as exc:
        console.print(f"[red]error:[/] {exc}")
        return 1

    if args.command not in ("doctor", "migrate", "versions"):
        _auto_migrate(layout)

    try:
        return _dispatch(layout, args)
    except MchostError as exc:
        console.print(f"[red]error:[/] {exc}")
        return 1
    except KeyboardInterrupt:
        console.print("\n[dim]Interrupted.[/]")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
