import pytest

from app.services.evening_report_rules import all_of, any_of, compare


@pytest.mark.parametrize(("conditions", "all_pass", "any_pass"), [
    ((True, None), None, True), ((False, None), False, None), ((True, False), False, True),
])
def test_unknown_only_affects_the_dependent_branch(conditions, all_pass, any_pass):
    assert all_of(*conditions) is all_pass
    assert any_of(*conditions) is any_pass


def test_return_boundaries_preserve_inclusive_and_exclusive_thresholds():
    assert compare(11.2 / 10 - 1, .12)
    assert not compare(15.5 / 10 - 1, .55, "lt")
