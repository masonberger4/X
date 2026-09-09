import pytest

from db import Database


@pytest.fixture
def db():
    d = Database(":memory:")
    yield d
    d.close()


@pytest.fixture
def dedup_cfg():
    return {"title_similarity": 0.92, "near_dup_window_days": 14}
