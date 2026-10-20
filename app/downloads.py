import csv
import hashlib
import io
import json
import time
import warnings
import zipfile
from urllib.parse import urljoin

import httpx
from PIL import Image

from app.links import validate_url

MAX_IMAGE_BYTES = 15 * 1024 * 1024
MAX_TOTAL_BYTES = 300 * 1024 * 1024
MAX_IMAGES = 300
Image.MAX_IMAGE_PIXELS = 50_000_000


def atomic_write(path, data):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


def fetch_image(client, url, referer):
    for _ in range(6):
        url = validate_url(url, resource=True, resolve=True)
        with client.stream("GET", url, headers={"Referer": referer}) as response:
            if response.is_redirect:
                url = urljoin(url, response.headers.get("location", ""))
                continue
            response.raise_for_status()
            if not response.headers.get("content-type", "").split(";")[0].startswith("image/"):
                raise ValueError("返回内容不是图片")
            chunks, total = [], 0
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > MAX_IMAGE_BYTES:
                    raise ValueError("单张图片超过 15 MB")
                chunks.append(chunk)
        data = b"".join(chunks)
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                extension = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp", "GIF": "gif"}.get(
                    image.format
                )
                if not extension:
                    raise ValueError("不支持的图片格式")
                width, height = image.size
                image.verify()
        return data, extension, width, height
    raise ValueError("图片重定向次数超过限制")


def download_images(product, directory, progress, stop, fetch=fetch_image):
    (directory / "images").mkdir(parents=True, exist_ok=True)
    total_bytes, by_hash = 0, {}
    deadline = time.monotonic() + 600
    with httpx.Client(timeout=20, follow_redirects=False, trust_env=False) as client:
        for index, image in enumerate(product["images"]):
            if stop.is_set():
                raise InterruptedError("服务正在停止")
            progress(f"正在下载图片 {index + 1}/{len(product['images'])}")
            try:
                if (
                    index >= MAX_IMAGES
                    or total_bytes >= MAX_TOTAL_BYTES
                    or time.monotonic() >= deadline
                ):
                    raise ValueError("已达到本次图片下载上限，未将任务标为完整")
                data, extension, width, height = fetch(client, image["url"], product["source_url"])
                if total_bytes + len(data) > MAX_TOTAL_BYTES:
                    raise ValueError("图片总大小超过 300 MB")
                sha = hashlib.sha256(data).hexdigest()
                filename = by_hash.get(sha, f"images/{sha}.{extension}")
                if sha not in by_hash:
                    atomic_write(directory / filename, data)
                    total_bytes += len(data)
                by_hash[sha] = filename
                image.update(
                    status="downloaded",
                    file=filename,
                    sha256=sha,
                    bytes=len(data),
                    width=width,
                    height=height,
                    error=None,
                )
            except Exception as exc:
                # Do not log URLs with query credentials or HTTP response bodies.
                reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
                image.update(status="failed", error=reason, file=None)
    count = sum(i["status"] == "downloaded" for i in product["images"])
    product["image_summary"] = {
        "discovered": len(product["images"]),
        "downloaded": count,
        "failed": len(product["images"]) - count,
        "unique_files": len(by_hash),
    }
    product["completeness"]["images"] = (
        "downloaded"
        if count and count == len(product["images"])
        else "partial"
        if count
        else "missing"
    )
    if count != len(product["images"]):
        product["warnings"].append("有图片下载失败；详情中保留失败原因和原始链接，可重新采集")


def display_price(value):
    if value["cents"] is not None:
        return f"¥{value['cents'] // 100}.{value['cents'] % 100:02d}"
    return f"{value['raw']}（单位待核实）" if value["raw"] is not None else "未取得"


def safe_cell(value):
    value = str(value)
    return (
        "'" + value
        if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n"))
        else value
    )


def make_bundle(product, directory):
    atomic_write(
        directory / "product.json", json.dumps(product, ensure_ascii=False, indent=2).encode()
    )
    rows = io.StringIO(newline="")
    writer = csv.writer(rows)
    writer.writerow(["SKU ID", "规格", "拼团价", "单买价", "其他价格", "价格条件", "库存原值"])
    for sku in product["skus"]:
        writer.writerow(
            [
                safe_cell(v)
                for v in [
                    sku["sku_id"],
                    sku["label"],
                    display_price(sku["group_price"]),
                    display_price(sku["normal_price"]),
                    display_price(sku["other_price"]),
                    sku["price_note"],
                    sku["stock"] if sku["stock"] is not None else "未取得",
                ]
            ]
        )
    atomic_write(directory / "skus.csv", rows.getvalue().encode("utf-8-sig"))
    # Plain text avoids treating untrusted page content as executable markup.
    lines = ["1. 标题", product["title"], "", "2. 商品规格价格"]
    for sku in product["skus"]:
        lines.append(
            f"{sku['label']} | 拼团 {display_price(sku['group_price'])} | 单买 {display_price(sku['normal_price'])} | 其他 {display_price(sku['other_price'])} | {sku['price_note']}"
        )
    if not product["skus"]:
        lines.append("未取得规格价格；请回原页面核对")
    lines += ["", "3. 商品图片", json.dumps(product["image_summary"], ensure_ascii=False)]
    for image in product["images"]:
        lines.append(
            f"{','.join(image['categories'])} | {image['status']} | {image['file'] or image.get('error', '')}"
        )
    lines += ["", "4. 商品注意事项"]
    lines += [f"{item['text']} [来源：{item['source']}]" for item in product["notices"]] or [
        "页面未提供可读取文字；不代表商品没有注意事项，请核对详情图"
    ]
    lines += ["", "商品参数"] + [f"{p['name']}：{p['value']}" for p in product["properties"]]
    lines += [
        "",
        "采集说明",
        *product["warnings"],
        "",
        "来源：" + product["source_url"],
        "采集时间：" + product["captured_at"],
    ]
    atomic_write(directory / "product.txt", "\n".join(lines).encode())
    files = {"product.json", "product.txt", "skus.csv"} | {
        i["file"] for i in product["images"] if i["status"] == "downloaded"
    }
    temporary = directory / "product.zip.tmp"
    with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as bundle:
        for filename in sorted(files):
            bundle.write(directory / filename, filename)
    temporary.replace(directory / "product.zip")
