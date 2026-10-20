"""Opt-in real Chrome tests using synthetic pages; no PDD network calls."""

import io
import json
import os
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from PIL import Image
from playwright.sync_api import BrowserType, expect, sync_playwright

from app import collector
from app.collector import Worker
from app.links import validate_url
from app.main import create_app
from app.storage import Store

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_BROWSER_TESTS") != "1", reason="Set RUN_BROWSER_TESTS=1 to launch local Chrome"
)
TOKEN = "fixture-only-token-not-a-real-secret"


def test_collector_and_browser_ui(monkeypatch, tmp_path, payload, source_url):
    monkeypatch.setenv("PDD_HEADLESS", "1")
    monkeypatch.setattr(
        collector,
        "validate_url",
        lambda url, **kwargs: validate_url(url, resource=kwargs.get("resource", False)),
    )
    original_launch = BrowserType.launch_persistent_context
    image = io.BytesIO()
    Image.new("RGB", (320, 240), "#89a37b").save(image, format="PNG")
    data = image.getvalue()
    html = (
        "<html><body><h1>合成测试商品页面</h1><p>温馨提示：远离火源</p><script>window.rawData="
        + json.dumps(payload)
        + ";</script></body></html>"
    )

    def launch(browser_type, *args, **kwargs):
        context = original_launch(browser_type, *args, **kwargs)
        route = context.route
        # Replace only this test collector's network with fixture responses.
        context.route = lambda pattern, guard: route(
            pattern,
            lambda request_route: request_route.fulfill(
                status=200, content_type="text/html", body=html
            ),
        )
        return context

    monkeypatch.setattr(BrowserType, "launch_persistent_context", launch)
    original_download = collector.download_images
    monkeypatch.setattr(
        collector,
        "download_images",
        lambda product, directory, progress, stop: original_download(
            product, directory, progress, stop, fetch=lambda *a: (data, "png", 320, 240)
        ),
    )
    store = Store(tmp_path)
    job = store.create("fixture", source_url)
    worker = Worker(store)
    worker.collect(store.claim())
    result = store.get(job["id"])
    assert result["status"] == "completed"
    assert result["product"]["image_summary"]["downloaded"] == 3
    assert result["download_ready"]

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    app = create_app(tmp_path, TOKEN, start_worker=False)
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    server_thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    server_thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="chrome", headless=True)
            page = browser.new_page(viewport={"width": 1365, "height": 1000}, accept_downloads=True)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{port}")
            page.locator("#token").fill(TOKEN)
            page.get_by_role("button", name="进入工作台").click()
            expect(page.locator(".product-title")).to_have_text(result["product"]["title"])
            expect(page.locator("tbody tr")).to_have_count(2)
            expect(page.locator(".image-card img")).to_have_count(3)
            expect(page.locator("#detail")).to_contain_text("商品注意事项")
            with page.expect_download() as download:
                page.get_by_role("link", name="下载资料与全部已取得图片").click()
            assert download.value.suggested_filename == "pdd-100000000001.zip"
            preview = Path(".local/test-preview.png")
            preview.parent.mkdir(exist_ok=True)
            page.screenshot(path=str(preview), full_page=True)
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.locator("#share").fill("这不是商品链接")
            page.locator("#collect").click()
            expect(page.locator("#message")).to_contain_text("拼多多商品链接")
            page.locator("#share").fill(source_url)
            page.locator("#collect").click()
            expect(page.locator("#detail .badge")).to_have_text("等待采集")
            assert len(store.list()) == 2
            assert not errors
            page.get_by_role("button", name="退出", exact=True).click()
            expect(page.locator("#login-panel")).to_be_visible()
            browser.close()
    finally:
        server.should_exit = True
        server_thread.join(10)
        listener.close()
