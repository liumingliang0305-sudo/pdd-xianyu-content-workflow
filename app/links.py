"""URL validation shared by link intake, browser requests and image downloads."""

import ipaddress
import re
import socket
from urllib.parse import parse_qs, urlsplit, urlunsplit

PLATFORM_DOMAINS = ("pinduoduo.com", "yangkeduo.com")
RESOURCE_DOMAINS = PLATFORM_DOMAINS + ("pddpic.com", "pddstatic.com", "pinduoduo.net")


def validate_url(url, *, resource=False, resolve=False):
    if url.startswith("//"):
        url = "https:" + url
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    domains = RESOURCE_DOMAINS if resource else PLATFORM_DOMAINS
    if (
        parsed.scheme not in ("http", "https")
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 80, 443)
        or not any(host == domain or host.endswith("." + domain) for domain in domains)
    ):
        raise ValueError("仅支持拼多多商品链接及其图片域名")
    if resolve:
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise ValueError("拒绝访问非公网地址")
    return urlunsplit(("https", host, parsed.path or "/", parsed.query, ""))


def parse_share(text):
    urls = re.findall(r"https?://[^\s<>\"'，。；【】]+", text)
    valid = []
    for url in urls:
        try:
            cleaned = validate_url(url.rstrip(").,;!?）】》"))
        except ValueError:
            continue
        if cleaned not in valid:
            valid.append(cleaned)
    if len(valid) != 1:
        raise ValueError("请一次粘贴一个拼多多商品链接或包含单个链接的分享文本")
    url = valid[0]
    return {"url": url, "goods_id": goods_id_from_url(url)}


def goods_id_from_url(url):
    values = parse_qs(urlsplit(url).query).get("goods_id", [])
    return values[0] if values and re.fullmatch(r"\d{1,30}", values[0]) else None
