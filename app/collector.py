import json
import os
import re
import threading
import time
from urllib.parse import urlsplit

from playwright.sync_api import Error as BrowserError
from playwright.sync_api import sync_playwright

from app.downloads import download_images, make_bundle
from app.links import validate_url
from app.parser import embedded_payloads, parse_product, product_nodes


class CollectionPaused(Exception):
    pass


def needs_login(url, body):
    path = urlsplit(url).path.lower()
    return bool(
        re.search(r"login|captcha|verify|risk", path)
        or re.search(r"请完成.{0,8}验证|拖动滑块|访问过于频繁|验证码登录|请先登录", body[:10000])
    )


def snapshot(page, payloads):
    # Read data; never eval inline scripts as Python/JS code.
    serialized = page.evaluate("""() => {
      try { return JSON.stringify(window.rawData || window.__INITIAL_STATE__ || null); }
      catch { return null; }
    }""")
    if serialized and len(serialized) <= 5_000_000:
        try:
            payloads.append(json.loads(serialized))
        except ValueError:
            pass
    html = page.content()
    if len(html) <= 8_000_000:
        payloads.extend(embedded_payloads(html))
    return page.locator("body").inner_text(timeout=5000)[:60000]


class Worker:
    def __init__(self, store):
        self.store = store
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, name="pdd-collector", daemon=True)

    def start(self):
        self.store.recover()
        self.thread.start()

    def close(self):
        self.stop.set()
        self.thread.join(timeout=35)

    def run(self):
        while not self.stop.is_set():
            job = self.store.claim()
            if not job:
                self.stop.wait(0.5)
                continue
            try:
                self.collect(job)
            except CollectionPaused:
                pass
            except InterruptedError:
                self.store.update(job["id"], status="interrupted", message="采集已中断，可重新采集")
            except BrowserError:
                self.store.update(
                    job["id"],
                    status="failed",
                    error_code="BROWSER_ERROR",
                    message="浏览器启动失败、页面超时或窗口被关闭。请确认已安装 Chrome，关闭占用的采集窗口后重试。",
                )
            except Exception as exc:
                self.store.update(
                    job["id"],
                    status="failed",
                    error_code=type(exc).__name__,
                    message="本次采集未完成，请检查网络后重新采集；原页面结构也可能已变化。",
                )

    def wait_for_user(self, page, job):
        self.store.update(
            job["id"],
            status="needs_login",
            error_code="LOGIN_OR_VERIFICATION",
            message="请在采集浏览器中完成登录或验证，再点“继续采集”。窗口保留 10 分钟。",
        )
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline and not self.stop.is_set():
            current = self.store.get(job["id"])
            if current["status"] == "queued":
                self.store.update(
                    job["id"], status="running", message="继续读取商品页面", error_code=None
                )
                return
            page.wait_for_timeout(500)
        if self.stop.is_set():
            raise InterruptedError()
        self.store.update(
            job["id"], message="登录等待已超过 10 分钟，浏览器窗口已关闭；点击继续采集会重新打开。"
        )
        raise CollectionPaused()

    def collect(self, job):
        profile = self.store.directory / "browser-profile"
        profile.mkdir(exist_ok=True, mode=0o700)
        payloads = []
        dns_checked = set()
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                str(profile),
                channel=os.getenv("PDD_BROWSER_CHANNEL", "chrome") or None,
                headless=os.getenv("PDD_HEADLESS", "0") == "1",
                locale="zh-CN",
                viewport={"width": 430, "height": 900},
                service_workers="block",
                accept_downloads=False,
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.set_default_timeout(5000)

                def guard(route):
                    url = route.request.url
                    if url.startswith(("data:", "blob:")):
                        route.continue_()
                        return
                    try:
                        host = urlsplit(url).hostname
                        validate_url(url, resource=True, resolve=host not in dns_checked)
                        dns_checked.add(host)
                        route.continue_()
                    except (ValueError, OSError):
                        route.abort()

                context.route("**/*", guard)

                def capture(response):
                    if len(payloads) >= 60 or not 200 <= response.status < 300:
                        return
                    if "json" not in response.headers.get("content-type", ""):
                        return
                    if not re.search(r"goods|oak|detail|render", response.url, re.I):
                        return
                    try:
                        raw = response.body()
                        if len(raw) <= 5_000_000:
                            data = json.loads(raw)
                            if next(product_nodes(data), None):
                                payloads.append(data)
                    except (BrowserError, ValueError, RecursionError):
                        pass

                page.on("response", capture)
                source = validate_url(job["source_url"], resolve=True)
                page.goto(source, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(1500)
                for _ in range(3):
                    body = page.locator("body").inner_text(timeout=5000)
                    if not needs_login(page.url, body):
                        break
                    self.wait_for_user(page, job)
                    payloads.clear()
                    page.goto(source, wait_until="domcontentloaded", timeout=45000)
                    page.wait_for_timeout(1500)
                else:
                    self.store.update(
                        job["id"],
                        status="needs_login",
                        message="仍停留在登录或验证页，请处理后再继续",
                    )
                    raise CollectionPaused()
                self.store.update(job["id"], message="正在读取规格、详情图和页面说明")
                # Expands only explicit detail tabs, never purchase or payment controls.
                detail = page.get_by_text(re.compile(r"^(商品详情|图文详情|查看图文详情)$")).first
                if detail.count() and detail.is_visible():
                    try:
                        detail.click(timeout=2000)
                    except BrowserError:
                        pass
                stable, previous = 0, -1
                for _ in range(24):
                    if self.stop.is_set():
                        raise InterruptedError()
                    height = page.evaluate("document.body.scrollHeight")
                    page.evaluate("window.scrollBy(0, 800)")
                    page.wait_for_timeout(350)
                    at_bottom = page.evaluate(
                        "window.scrollY + window.innerHeight >= document.body.scrollHeight - 5"
                    )
                    stable = stable + 1 if at_bottom and height == previous else 0
                    previous = height
                    if stable >= 3:
                        break
                body = snapshot(page, payloads)
                if needs_login(page.url, body):
                    self.store.update(
                        job["id"],
                        status="needs_login",
                        error_code="LOGIN_OR_VERIFICATION",
                        message="读取详情时出现登录或验证，已停止采集；点击继续采集重新打开浏览器。",
                    )
                    raise CollectionPaused()
                resolved = validate_url(page.url)
                product = parse_product(payloads, resolved, body)
                if not product:
                    self.store.update(
                        job["id"],
                        status="failed",
                        error_code="PRODUCT_DATA_UNAVAILABLE",
                        message="页面未暴露可识别的商品数据。请确认链接是商品详情页；登录、App 专属页或页面结构变化都可能导致此情况。",
                    )
                    return
                product["input_url"] = job["source_url"]
                if stable < 3:
                    product["warnings"].append(
                        "页面滚动达到本次上限，可能仍有未加载内容，请核对详情图"
                    )
                    product["completeness"]["page_scan"] = "limited"
                else:
                    product["completeness"]["page_scan"] = "finished"
            finally:
                context.close()
        self.store.update(job["id"], product=product, message="商品信息已读取，正在下载图片")
        directory = self.store.directory / "products" / job["id"]
        directory.mkdir(parents=True, exist_ok=True)
        download_images(
            product,
            directory,
            lambda message: self.store.update(job["id"], message=message),
            self.stop,
        )
        make_bundle(product, directory)
        complete = all(
            product["completeness"][key] == expected
            for key, expected in (
                ("title", "captured"),
                ("skus", "captured"),
                ("prices", "captured"),
                ("images", "downloaded"),
                ("detail_images", "captured"),
                ("notices", "captured"),
                ("page_scan", "finished"),
            )
        )
        self.store.update(
            job["id"],
            product=product,
            status="completed" if complete else "partial",
            message="已整理商品资料与图片；请核对注意事项和价格条件"
            if complete
            else "已保存可取得的资料，部分字段或图片缺失，请查看提示",
            download_ready=True,
            error_code=None,
        )
