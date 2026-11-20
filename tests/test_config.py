from pathlib import Path

from rcompass.config import (
    ENV_DATA_DIR,
    default_data_dir,
    is_inside,
    resolve_data_dir,
    source_checkout_root,
)


def test_cli_option_wins_over_env(tmp_path: Path) -> None:
    cli_dir = tmp_path / "cli"
    env = {ENV_DATA_DIR: str(tmp_path / "env")}
    path, source = resolve_data_dir(cli_dir, env=env)
    assert (path, source) == (cli_dir.resolve(), "cli")


def test_env_used_when_no_cli_option(tmp_path: Path) -> None:
    env = {ENV_DATA_DIR: str(tmp_path / "env")}
    path, source = resolve_data_dir(None, env=env)
    assert (path, source) == ((tmp_path / "env").resolve(), "env")


def test_default_when_nothing_given() -> None:
    path, source = resolve_data_dir(None, env={})
    assert source == "default"
    assert path == default_data_dir().resolve()
    assert path.name == "rcompass"


def test_blank_values_are_ignored() -> None:
    _, source = resolve_data_dir("  ", env={ENV_DATA_DIR: "   "})
    assert source == "default"


def test_cli_value_is_stripped(tmp_path: Path) -> None:
    path, source = resolve_data_dir(f"  {tmp_path}  ", env={})
    assert (path, source) == (tmp_path.resolve(), "cli")


def test_default_data_dir_is_outside_checkout() -> None:
    assert not is_inside(default_data_dir(), source_checkout_root())


def test_is_inside(tmp_path: Path) -> None:
    assert is_inside(tmp_path / "a" / "b", tmp_path)
    assert not is_inside(tmp_path.parent / "other", tmp_path)
    assert not is_inside(tmp_path, None)
