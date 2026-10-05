from __future__ import annotations

import argparse

import pytest

from modelmux_cli import main as main_module
from modelmux_cli.errors import EXIT_DOCKER, EXIT_INTERRUPTED, EXIT_USAGE, DockerError
from modelmux_cli.main import main


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        main(["--version"])
    assert info.value.code == 0
    assert capsys.readouterr().out.startswith("modelmux ")


def test_no_command_is_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        main([])
    assert info.value.code == EXIT_USAGE


def test_unknown_provider(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        main(["login", "nope"])
    assert info.value.code == EXIT_USAGE
    assert "invalid choice: 'nope'" in capsys.readouterr().err


def patch_handler(monkeypatch: pytest.MonkeyPatch, exc: BaseException) -> None:
    original = main_module.build_parser

    def build() -> argparse.ArgumentParser:
        parser = original()

        def handler(_args: argparse.Namespace, _console: object) -> int:
            raise exc

        parser.set_defaults(handler=handler)
        for action in parser._subparsers._group_actions:  # type: ignore[union-attr]
            for sub in action.choices.values():  # type: ignore[union-attr]
                sub.set_defaults(handler=handler)
        return parser

    monkeypatch.setattr(main_module, "build_parser", build)


def test_cli_error_is_reported(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    patch_handler(monkeypatch, DockerError("Docker is not running.", hint="Start Docker Desktop."))
    code, out, err = run(capsys, "status")
    assert code == EXIT_DOCKER
    assert out == ""
    assert "Docker is not running." in err
    assert "Start Docker Desktop." in err


def test_ctrl_c(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    patch_handler(monkeypatch, KeyboardInterrupt())
    code, _, err = run(capsys, "status")
    assert code == EXIT_INTERRUPTED
    assert "Cancelled." in err


def test_unexpected_error_hides_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    patch_handler(monkeypatch, RuntimeError("internal detail"))
    code, _, err = run(capsys, "status")
    assert code == 1
    assert "Something unexpected went wrong." in err
    assert "internal detail" not in err
    assert "Traceback" not in err


def test_unexpected_error_verbose_shows_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    patch_handler(monkeypatch, RuntimeError("internal detail"))
    code, _, err = run(capsys, "--verbose", "status")
    assert code == 1
    assert "Traceback" in err


@pytest.mark.parametrize(
    "argv",
    [["up"], ["down"], ["logs"], ["status"], ["login", "claude"], ["logout", "codex"],
     ["config", "litellm"], ["doctor"], ["upgrade"], ["key", "show"]],
)  # fmt: skip
def test_every_command_parses(argv: list[str]) -> None:
    args = main_module.build_parser().parse_args(argv)
    assert callable(args.handler)
