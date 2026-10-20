import io
import json
import threading
import zipfile

import httpx
import pytest
from PIL import Image

from app import downloads
from app.downloads import download_images, fetch_image, make_bundle
from app.parser import parse_product


def png():
    buffer = io.BytesIO()
    Image.new("RGB", (20, 30), "#678f65").save(buffer, format="PNG")
    return buffer.getvalue()


def public_dns(monkeypatch):
    monkeypatch.setattr(
        "app.links.socket.getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("8.8.8.8", 443))]
    )


def test_real_image_validation_and_redirect(monkeypatch):
    public_dns(monkeypatch)
    calls = []

    def handle(request):
        calls.append(str(request.url))
        if request.url.path == "/redirect":
            return httpx.Response(302, headers={"location": "/image.png"})
        return httpx.Response(200, headers={"content-type": "image/png"}, content=png())

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        data, extension, width, height = fetch_image(
            client, "https://img.pddpic.com/redirect", "https://mobile.yangkeduo.com/"
        )
    assert (data, extension, width, height) == (png(), "png", 20, 30)
    assert len(calls) == 2


@pytest.mark.parametrize("location", ["http://127.0.0.1/secret", "http://evil.test/image"])
def test_redirect_outside_allowlist_never_fetched(monkeypatch, location):
    public_dns(monkeypatch)
    calls = []

    def handle(request):
        calls.append(request.url)
        return httpx.Response(302, headers={"location": location})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ValueError):
            fetch_image(client, "https://img.pddpic.com/redirect", "")
    assert len(calls) == 1


@pytest.mark.parametrize(
    "mime,body", [("text/html", b"<html>login</html>"), ("image/png", b"not-an-image")]
)
def test_bad_image_rejected(monkeypatch, mime, body):
    public_dns(monkeypatch)
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, headers={"content-type": mime}, content=body)
        )
    ) as client:
        with pytest.raises((ValueError, OSError)):
            fetch_image(client, "https://img.pddpic.com/bad", "")


def test_download_partial_dedup_and_zip(payload, source_url, tmp_path):
    product = parse_product([payload], source_url)

    def fetch(client, url, referer):
        if url.endswith("detail.png"):
            raise ValueError("fixture download failure")
        return png(), "png", 20, 30

    download_images(product, tmp_path, lambda _: None, threading.Event(), fetch=fetch)
    assert product["image_summary"] == {
        "discovered": 3,
        "downloaded": 2,
        "failed": 1,
        "unique_files": 1,
    }
    make_bundle(product, tmp_path)
    with zipfile.ZipFile(tmp_path / "product.zip") as bundle:
        assert bundle.testzip() is None
        assert len(bundle.namelist()) == 4
        result = json.loads(bundle.read("product.json"))
        assert result["images"][1]["status"] == "failed"
        assert "1. 标题" in bundle.read("product.txt").decode()
        assert "4. 商品注意事项" in bundle.read("product.txt").decode()
        assert "¥12.90" in bundle.read("skus.csv").decode("utf-8-sig")
        assert not any("cookie" in f or "token" in f for f in bundle.namelist())


def test_size_limit_and_csv_formula(monkeypatch, payload, source_url, tmp_path):
    public_dns(monkeypatch)
    monkeypatch.setattr(downloads, "MAX_IMAGE_BYTES", 5)
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"content-type": "image/png"}, content=png()
            )
        )
    ) as client:
        with pytest.raises(ValueError, match="15 MB"):
            fetch_image(client, "https://img.pddpic.com/big", "")
    assert downloads.safe_cell('=HYPERLINK("x")').startswith("'=")
    product = parse_product([payload], source_url)
    monkeypatch.setattr(downloads, "MAX_IMAGES", 1)
    download_images(
        product,
        tmp_path,
        lambda _: None,
        threading.Event(),
        fetch=lambda *a: (png(), "png", 20, 30),
    )
    assert product["image_summary"]["failed"] == 2
