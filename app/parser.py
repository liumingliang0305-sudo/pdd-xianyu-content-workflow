"""Conservative page-data adapter. No signing API or invented product facts."""

import json
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from html import unescape
from html.parser import HTMLParser

from app.links import goods_id_from_url, validate_url

PARSER_VERSION = "pdd-page-v1"
CONTAINERS = (
    "store",
    "initDataObj",
    "initData",
    "data",
    "result",
    "goods",
    "goodsInfo",
    "goodsData",
)
IMAGE_FIELDS = {
    "top_gallery": "main",
    "topGallery": "main",
    "banner": "main",
    "gallery": "main",
    "goods_gallery_urls": "main",
    "detail_gallery": "detail",
    "detailGallery": "detail",
    "detail_images": "detail",
}
NOTICE_FIELDS = (
    "goods_desc",
    "goodsDesc",
    "notice",
    "notices",
    "buying_tips",
    "warm_tips",
    "service_promise",
    "servicePromise",
)


def text(value):
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        return unescape(re.sub(r"<[^>]+>", " ", str(value))).strip()
    return ""


def first(node, *keys):
    for key in keys:
        if key in node and node[key] is not None and node[key] != "":
            return node[key]
    return None


def product_nodes(payload, path="root", depth=0):
    if not isinstance(payload, dict) or depth > 8:
        return
    if first(payload, "goods_name", "goodsName"):
        yield path, payload
    # Deliberately do not recurse through arbitrary arrays/recommendations.
    for key in CONTAINERS:
        if key in payload:
            yield from product_nodes(payload[key], path + "." + key, depth + 1)


def price(sku, *keys):
    for key in keys:
        if key not in sku or sku[key] is None:
            continue
        raw = str(sku[key])
        cents = None
        unit = "unknown"
        # Only explicitly supported web-page minor-unit fields are converted.
        # CamelCase app/third-party payloads vary by provider; keep raw values.
        if key in ("group_price", "normal_price", "price_in_cents"):
            try:
                amount = Decimal(raw)
                if amount.is_finite() and amount >= 0 and amount == int(amount):
                    cents, unit = int(amount), "fen"
            except (InvalidOperation, ValueError, OverflowError):
                pass
        return {"raw": raw, "cents": cents, "unit": unit, "source_field": key}
    return {"raw": None, "cents": None, "unit": "unknown", "source_field": None}


def image_urls(value):
    if isinstance(value, str):
        if value.startswith(("http://", "https://", "//")):
            yield value
    elif isinstance(value, list):
        for item in value:
            yield from image_urls(item)
    elif isinstance(value, dict):
        for key in ("url", "image_url", "imageUrl", "img_url", "imgUrl", "thumb_url", "thumbUrl"):
            if key in value:
                yield from image_urls(value[key])


def notice_texts(value):
    if isinstance(value, list):
        for item in value:
            yield from notice_texts(item)
    elif isinstance(value, dict):
        for key in ("text", "title", "desc", "content", "name"):
            if key in value:
                yield from notice_texts(value[key])
    elif text(value):
        yield text(value)[:10000]


class EmbeddedData(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_script = False
        self.scripts = []
        self.buffer = []

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.in_script = True
            self.buffer = []

    def handle_data(self, data):
        if self.in_script:
            self.buffer.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.in_script:
            self.scripts.append("".join(self.buffer))
            self.in_script = False


def embedded_payloads(html):
    parser = EmbeddedData()
    parser.feed(html)
    decoder = json.JSONDecoder()
    for script in parser.scripts:
        candidates = [script.strip()] if script.lstrip().startswith("{") else []
        for match in re.finditer(
            r"(?:window\.)?(?:rawData|__INITIAL_STATE__|__NEXT_DATA__)\s*=\s*", script
        ):
            candidates.append(script[match.end() :].lstrip())
        for candidate in candidates:
            try:
                data, _ = decoder.raw_decode(candidate)
                yield data
            except (ValueError, RecursionError):
                continue


def parse_product(payloads, source_url, visible_text=""):
    expected_id = goods_id_from_url(source_url)
    nodes = []
    for index, payload in enumerate(payloads):
        for path, node in product_nodes(payload, f"payload[{index}]"):
            item_id = text(first(node, "goods_id", "goodsId"))
            if expected_id and item_id and item_id != expected_id:
                continue
            nodes.append((path, node))
    if not nodes:
        return None
    # Use one coherent product. Prefer the most complete SKU/gallery payload.
    nodes.sort(
        key=lambda row: sum(
            k in row[1] for k in (*IMAGE_FIELDS, "sku", "skus", "skuList", "sku_list")
        ),
        reverse=True,
    )
    main_path, main = nodes[0]
    item_id = text(first(main, "goods_id", "goodsId")) or expected_id
    if not item_id or not re.fullmatch(r"\d{1,30}", item_id):
        return None
    nodes = [
        (p, n) for p, n in nodes if text(first(n, "goods_id", "goodsId")) == item_id or n is main
    ]
    result = {
        "goods_id": item_id,
        "source_url": source_url,
        "canonical_url": f"https://mobile.yangkeduo.com/goods.html?goods_id={item_id}",
        "title": text(first(main, "goods_name", "goodsName")),
        "title_source": main_path,
        "skus": [],
        "images": [],
        "notices": [],
        "properties": [],
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "parser_version": PARSER_VERSION,
        "warnings": [],
        "completeness": {},
    }
    images, sku_map, notices = {}, {}, set()
    incomplete_skus = False
    conflicting_skus = False

    def add_image(value, category, path, sku_id=None):
        for raw in image_urls(value):
            try:
                url = validate_url(raw, resource=True)
            except ValueError:
                result["warnings"].append("发现非允许域名图片，未下载")
                continue
            if url not in images:
                images[url] = {
                    "url": url,
                    "categories": [],
                    "sku_ids": [],
                    "source_fields": [],
                    "status": "pending",
                    "file": None,
                }
            image = images[url]
            for key, val in (
                ("categories", category),
                ("sku_ids", sku_id),
                ("source_fields", path),
            ):
                if val and val not in image[key]:
                    image[key].append(val)

    for path, node in nodes:
        for key, category in IMAGE_FIELDS.items():
            if key in node:
                add_image(node[key], category, path + "." + key)
        for key in ("hd_thumb_url", "thumb_url", "thumbUrl"):
            if key in node:
                add_image(node[key], "main", path + "." + key)
        entries = first(node, "sku", "skus", "sku_list", "skuList")
        if isinstance(entries, list):
            for i, sku in enumerate(entries):
                if not isinstance(sku, dict):
                    continue
                sku_id = text(first(sku, "sku_id", "skuId"))
                if not sku_id:
                    result["warnings"].append("部分规格缺少 SKU 标识，未将其当作完整规格")
                    incomplete_skus = True
                    sku_id = f"unknown-{path}-{i}"
                specs = first(sku, "specs", "spec_list", "specList")
                attributes = []
                if isinstance(specs, list):
                    for spec in specs:
                        if isinstance(spec, dict):
                            key = text(first(spec, "spec_key", "specKey", "key", "name"))
                            value = text(first(spec, "spec_value", "specValue", "value"))
                            if value:
                                attributes.append({"name": key, "value": value})
                data = {
                    "sku_id": sku_id,
                    "specs": attributes,
                    "label": " / ".join(f"{s['name']}：{s['value']}" for s in attributes)
                    or text(first(sku, "spec", "sku_name"))
                    or "规格名称未取得",
                    "group_price": price(sku, "group_price", "groupPrice"),
                    "normal_price": price(sku, "normal_price", "normalPrice"),
                    "other_price": price(sku, "price_in_cents", "price"),
                    "stock": first(sku, "quantity", "stock"),
                    "price_note": text(first(sku, "group_tip", "groupTip", "price_desc")),
                    "source_field": f"{path}.sku[{i}]",
                }
                if not attributes and not text(first(sku, "sku_name")):
                    incomplete_skus = True
                if sku_id in sku_map and any(
                    sku_map[sku_id][k] != data[k]
                    for k in ("specs", "group_price", "normal_price", "other_price")
                ):
                    conflicting_skus = True
                    result["warnings"].append(
                        f"SKU {sku_id} 存在不同快照，以先读取的完整数据为准，请核对"
                    )
                sku_map.setdefault(sku_id, data)
                for key in ("thumb_url", "thumbUrl", "image", "image_url"):
                    add_image(sku.get(key), "sku", f"{path}.sku[{i}].{key}", sku_id)
        for key in NOTICE_FIELDS:
            for value in notice_texts(node.get(key)):
                if value not in notices:
                    notices.add(value)
                    result["notices"].append(
                        {"text": value, "source": path + "." + key, "kind": "page_text"}
                    )
        properties = first(node, "goods_property", "goodsProperty", "properties")
        if isinstance(properties, list):
            for prop in properties:
                if not isinstance(prop, dict):
                    continue
                key = text(first(prop, "key", "name"))
                values = first(prop, "values", "value")
                val = " / ".join(map(text, values)) if isinstance(values, list) else text(values)
                record = {"name": key, "value": val}
                if key and val and record not in result["properties"]:
                    result["properties"].append(record)
    # Visible text is supplemental, explicitly marked for manual verification.
    product_text = re.split(r"猜你喜欢|为你推荐|推荐商品", visible_text)[0]
    for line in product_text.splitlines():
        line = line.strip()
        if (
            4 <= len(line) <= 500
            and re.search(
                r"注意事项|温馨提示|购买须知|使用须知|不支持.{0,8}退|过敏|禁忌|保存方法|色差|定制商品",
                line,
            )
            and line not in notices
        ):
            notices.add(line)
            result["notices"].append(
                {"text": line, "source": "visible_page_text", "kind": "needs_review"}
            )
    result["skus"] = list(sku_map.values())
    result["images"] = list(images.values())
    known_prices = bool(sku_map) and all(
        any(s[k]["cents"] is not None for k in ("group_price", "normal_price", "other_price"))
        for s in sku_map.values()
    )
    result["completeness"] = {
        "title": "captured",
        "skus": "needs_review" if incomplete_skus else "captured" if sku_map else "missing",
        "prices": "captured" if known_prices and not conflicting_skus else "needs_review",
        "images": "pending" if images else "missing",
        "detail_images": "captured"
        if any("detail" in i["categories"] for i in images.values())
        else "missing",
        "notices": "captured" if result["notices"] else "not_exposed",
    }
    if not sku_map:
        result["warnings"].append("未取得规格列表，不能用商品最低价代替各规格价格")
    elif incomplete_skus:
        result["warnings"].append("部分规格标识或名称缺失，请在原页面核对")
    if not known_prices:
        result["warnings"].append("部分价格未取得或单位不明确，请核对原页面")
    if result["completeness"]["detail_images"] == "missing":
        result["warnings"].append("未取得详情图；可能需要展开商品详情后重新采集")
    result["warnings"].append(
        "注意事项仅提取页面可读取文字；图片内文字尚未做 OCR，请查看下载的详情图"
    )
    result["warnings"].append(
        "图片范围为本次页面数据中识别到的主图、规格图、详情图；尚不能证明已覆盖平台隐藏或未加载的图片"
    )
    result["warnings"] = list(dict.fromkeys(result["warnings"]))
    return result
