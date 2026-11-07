from __future__ import annotations

from mchost.cli import build_parser, main
from mchost.config import load_instance
from mchost.paths import Layout


def test_new_list_and_path(tmp_path):
    root = str(tmp_path)
    code = main(
        [
            "--root",
            root,
            "new",
            "My Server",
            "--loader",
            "fabric",
            "--mc-version",
            "1.20.4",
            "--loader-version",
            "0.15.11",
            "--memory",
            "2G",
        ]
    )
    assert code == 0

    layout = Layout(tmp_path)
    instance = load_instance(layout, "my-server")
    assert instance.loader == "fabric"
    assert instance.loader_version == "0.15.11"
    assert instance.memory == "2G"
    assert layout.compose_file("my-server").is_file()

    assert main(["--root", root, "list"]) == 0
    assert main(["--root", root, "path", "my-server"]) == 0
    assert main(["--root", root, "compose", "my-server", "--path"]) == 0


def test_new_rejects_duplicate(tmp_path):
    root = str(tmp_path)
    args = ["--root", root, "new", "Server", "--mc-version", "1.21.1"]
    assert main(args) == 0
    assert main(args) == 1


def test_memory_command(tmp_path):
    root = str(tmp_path)
    assert (
        main(
            [
                "--root",
                root,
                "new",
                "Memory Test",
                "--mc-version",
                "1.21.1",
            ]
        )
        == 0
    )
    assert main(["--root", root, "memory", "memory-test", "8G"]) == 0
    layout = Layout(tmp_path)
    assert load_instance(layout, "memory-test").memory == "8G"
    assert "MEMORY: 8G" in layout.compose_file("memory-test").read_text(
        encoding="utf-8"
    )


def test_status_unknown_instance(tmp_path):
    assert main(["--root", str(tmp_path), "status", "nope"]) == 1


def test_cmd_parser_does_not_shadow_subcommand():
    args = build_parser().parse_args(["cmd", "my-server", "say", "hello"])
    assert args.command == "cmd"
    assert args.name == "my-server"
    assert args.rcon_command == ["say", "hello"]
