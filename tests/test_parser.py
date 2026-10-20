import copy
import json

import pytest

from app.links import parse_share, validate_url
from app.parser import embedded_payloads, parse_product, price


def test_share_and_short_link(source_url):
    parsed = parse_share(f"好物分享【收纳盒】 {source_url}。复制链接打开")
    assert parsed == {"url": source_url, "goods_id": "100000000001"}
    assert parse_share("https://mobile.yangkeduo.com/goods.html?ps=abc")["goods_id"] is None
    assert parse_share(source_url + " " + source_url)["url"] == source_url
    with pytest.raises(ValueError):
        parse_share(source_url + " https://mobile.yangkeduo.com/goods.html?goods_id=2")


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1/goods.html",
        "file:///etc/passwd",
        "https://yangkeduo.com.evil.test/",
        "https://evil.test/?u=yangkeduo.com",
        "https://user:pass@mobile.yangkeduo.com/",
        "https://mobile.yangkeduo.com:8080/",
    ],
)
def test_reject_non_platform_links(url):
    with pytest.raises(ValueError):
        validate_url(url)


def test_reject_private_dns(monkeypatch):
    monkeypatch.setattr(
        "app.links.socket.getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("127.0.0.1", 443))]
    )
    with pytest.raises(ValueError, match="非公网"):
        validate_url("https://mobile.yangkeduo.com", resolve=True)


def test_four_sections_and_image_dedup(payload, source_url):
    product = parse_product(
        [payload], source_url, "温馨提示：请核对尺寸\n猜你喜欢\n注意事项：推荐品说明"
    )
    assert product["title"].startswith("测试夹具")
    assert len(product["skus"]) == 2
    assert product["skus"][0]["group_price"]["cents"] == 1290
    assert product["skus"][0]["stock"] == 0
    assert len(product["images"]) == 3
    assert product["images"][0]["categories"] == ["main", "sku"]
    assert product["images"][0]["sku_ids"] == ["101"]
    assert all("recommendation" not in image["url"] for image in product["images"])
    assert len(product["notices"]) == 2
    assert not any("推荐品说明" in note["text"] for note in product["notices"])
    assert product["properties"] == [{"name": "材质", "value": "PP"}]


def test_wrong_product_and_invalid_id(payload, source_url):
    payload["store"]["initDataObj"]["goods"]["goods_id"] = "999"
    assert parse_product([payload], source_url) is None
    payload["store"]["initDataObj"]["goods"]["goods_id"] = "../../bad"
    assert parse_product([payload], "https://mobile.yangkeduo.com/goods.html?ps=abc") is None


def test_missing_data_not_invented(source_url):
    product = parse_product(
        [{"goods_name": "测试", "goods_id": "100000000001", "min_group_price": 1}], source_url
    )
    assert product["skus"] == []
    assert product["images"] == []
    assert product["completeness"]["notices"] == "not_exposed"
    assert product["completeness"]["prices"] == "needs_review"


def test_conflicting_and_incomplete_sku(payload, source_url):
    other = copy.deepcopy(payload)
    other["store"]["initDataObj"]["goods"]["sku"][0]["group_price"] = 990
    product = parse_product([payload, other], source_url)
    assert product["completeness"]["prices"] == "needs_review"
    assert product["skus"][0]["group_price"]["cents"] == 1290
    del payload["store"]["initDataObj"]["goods"]["sku"][0]["sku_id"]
    assert parse_product([payload], source_url)["completeness"]["skus"] == "needs_review"


@pytest.mark.parametrize("value", ["-1", "NaN", "Infinity", "1.5", True])
def test_price_invalid_never_converted(value):
    assert price({"group_price": value}, "group_price")["cents"] is None


def test_ambiguous_price_keeps_raw():
    assert price({"groupPrice": "99"}, "groupPrice") == {
        "raw": "99",
        "cents": None,
        "unit": "unknown",
        "source_field": "groupPrice",
    }
    assert price({"group_price": 0}, "group_price")["cents"] == 0


def test_inline_json_does_not_execute_scripts(payload, source_url):
    html = "<script>window.rawData = " + json.dumps(payload) + "; evil();</script>"
    values = list(embedded_payloads(html))
    assert len(values) == 1
    assert parse_product(values, source_url)["goods_id"] == "100000000001"
    assert list(embedded_payloads('<script>window.rawData = eval("bad")</script>')) == []
