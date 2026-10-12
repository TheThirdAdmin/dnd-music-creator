import pytest

from dndmusic.library import Library


@pytest.fixture
def lib(tmp_path):
    return Library(tmp_path)
