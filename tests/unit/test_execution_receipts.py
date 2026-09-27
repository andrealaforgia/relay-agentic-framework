import pytest


from relay import receipts


@pytest.mark.parametrize(
    "reports",
    [
        [],
        [{"id": "tests/test_rooms.py::test_free", "outcome": "skipped"}],
        [{"id": "tests/test_other.py::test_free", "outcome": "passed"}],
    ],
)
def test_missing_skipped_and_unrelated_checks_cannot_satisfy_an_expectation(reports):
    with pytest.raises(ValueError):
        receipts.coverage({"E1": ["tests/test_rooms.py::test_free"]}, reports)


def test_generated_cases_must_all_pass():
    binding = {"P1": ["tests/test_rooms.py::test_free"]}
    passed = [
        {"id": "tests/test_rooms.py::test_free[empty]", "outcome": "passed"},
        {"id": "tests/test_rooms.py::test_free[booked]", "outcome": "passed"},
    ]
    assert receipts.coverage(binding, passed) == {"P1": sorted(r["id"] for r in passed)}
    with pytest.raises(ValueError):
        receipts.coverage(
            binding,
            [
                *passed,
                {"id": "tests/test_rooms.py::test_free[full]", "outcome": "failed"},
            ],
        )


def test_container_command_exposes_only_checkout_collector_and_results(tmp_path):
    from relay.workers.evidence_runner import container_command

    image = "python@sha256:" + "a" * 64
    command = container_command(
        image,
        tmp_path / "checkout",
        tmp_path / "collector",
        tmp_path / "results",
        ["python", "-m", "pytest"],
    )
    assert "--network" in command and command[command.index("--network") + 1] == "none"
    assert "--read-only" in command
    assert not any("RELAY_RECEIPT_KEY" in part for part in command)
    with pytest.raises(ValueError):
        container_command(
            "python:latest",
            tmp_path / "checkout",
            tmp_path / "collector",
            tmp_path / "results",
            ["pytest"],
        )
