from __future__ import annotations

import pytest

from modelmux.runtime.hardening import hardening_problems

HARDENED = "Name:\tpython\nCapEff:\t0000000000000000\nNoNewPrivs:\t1\n"


def test_hardened_container_has_no_problems() -> None:
    assert hardening_problems(euid=10001, status=HARDENED, root_read_only=True) == []


@pytest.mark.parametrize(
    ("kwargs", "problem"),
    [
        ({"euid": 0}, "runs as root"),
        ({"status": HARDENED.replace("0000000000000000", "00000000a80425fb")}, "capabilities"),
        ({"status": HARDENED.replace("NoNewPrivs:\t1", "NoNewPrivs:\t0")}, "gain privileges"),
        ({"status": "Name:\tpython\n"}, "capabilities"),  # fields missing: fail closed
        ({"root_read_only": False}, "root filesystem is writable"),
    ],
)
def test_each_missing_setting_is_named(kwargs: dict[str, object], problem: str) -> None:
    values: dict[str, object] = {"euid": 10001, "status": HARDENED, "root_read_only": True}
    values.update(kwargs)
    problems = hardening_problems(**values)  # type: ignore[arg-type]
    assert any(problem in p for p in problems), problems


def test_reads_the_real_process() -> None:
    # The test process itself: not hardened like the container, but readable.
    problems = hardening_problems()
    assert isinstance(problems, list)
