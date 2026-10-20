import json
from pathlib import Path

import pytest


@pytest.fixture
def payload():
    return json.loads((Path(__file__).parent / "fixtures/product.json").read_text())


@pytest.fixture
def source_url():
    return "https://mobile.yangkeduo.com/goods.html?goods_id=100000000001"
