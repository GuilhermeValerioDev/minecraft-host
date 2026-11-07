from __future__ import annotations

import pytest

from mchost.errors import MchostError
from mchost.files import (
    delete_file,
    is_mod_disabled,
    read_text_file,
    toggle_mod,
    write_text_file,
)


def test_toggle_mod(tmp_path):
    mod = tmp_path / "example.jar"
    mod.write_bytes(b"jar")

    disabled = toggle_mod(mod)
    assert disabled.name == "example.jar.disabled"
    assert disabled.is_file()
    assert is_mod_disabled(disabled)

    enabled = toggle_mod(disabled)
    assert enabled == mod
    assert enabled.is_file()
    assert not is_mod_disabled(enabled)


def test_toggle_mod_refuses_to_clobber(tmp_path):
    mod = tmp_path / "example.jar"
    mod.write_bytes(b"jar")
    (tmp_path / "example.jar.disabled").write_bytes(b"other")
    with pytest.raises(MchostError, match="already exists"):
        toggle_mod(mod)


def test_delete_mod(tmp_path):
    mod = tmp_path / "example.jar"
    mod.write_bytes(b"jar")
    delete_file(mod)
    assert not mod.exists()


def test_read_and_write_text(tmp_path):
    path = tmp_path / "example.toml"
    path.write_text("a = 1\n", encoding="utf-8")
    assert read_text_file(path) == "a = 1\n"

    write_text_file(path, "a = 2\n")
    assert path.read_text(encoding="utf-8") == "a = 2\n"


def test_read_text_rejects_binary(tmp_path):
    path = tmp_path / "binary.dat"
    path.write_bytes(b"\xff\xfe\x00")
    with pytest.raises(MchostError, match="not a UTF-8"):
        read_text_file(path)


def test_read_text_rejects_large_files(tmp_path):
    path = tmp_path / "large.toml"
    path.write_text("x" * 1024, encoding="utf-8")
    with pytest.raises(MchostError, match="too large"):
        read_text_file(path, max_bytes=100)
