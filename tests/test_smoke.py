"""Smoke test so CI and the session hook can be validated before real code lands."""

import ingest
import score


def test_packages_import():
    assert ingest is not None
    assert score is not None
