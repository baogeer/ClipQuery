"""CGTN 列表页 / 详情页 HTML 解析。

对外主要接口：
    - crawl_transcript_page()       拉 https://www.cgtn.com/transcript 并返回所有详情页链接
    - parse_transcript_detail(url)  解析单篇详情，返回 {url, title, paragraphs, video_m3u8}
"""
from __future__ import annotations

import html as html_lib
import json
import re
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests
from lxml import etree

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

TRANSCRIPT_URL = "https://www.cgtn.com/transcript"


def get_request(url: str, timeout: int = 15) -> Optional[requests.Response]:
    try:
        resp = requests.get(url, headers={"User-Agent": UA}, timeout=timeout)
        resp.raise_for_status()
        return resp
    except requests.RequestException as e:
        print(f"[ERROR] 请求失败: {url}: {e}")
        return None


# ---------- 列表页 ----------
def extract_links_from_html(html: str, base_url: str) -> list[str]:
    root = etree.HTML(html, base_url=base_url)
    if root is None:
        return []

    links: set[str] = set()
    for href in root.xpath('//a/@href'):
        if not href:
            continue
        full_url = urljoin(base_url, href.strip())
        parsed = urlparse(full_url)

        if not parsed.netloc.endswith("cgtn.com"):
            continue
        if re.search(r'/(tag|category|author|video|podcast|live)/', parsed.path):
            continue
        if re.search(r'/(news|transcript|the-heat|world-today|radio)/', parsed.path, re.I):
            links.add(full_url)

    return sorted(links)


def crawl_transcript_page(url: str = TRANSCRIPT_URL) -> list[str]:
    print(f"[INFO] 请求列表页: {url}")
    resp = get_request(url)
    if not resp:
        return []

    html = resp.content.decode("utf-8", errors="ignore")
    links = extract_links_from_html(html, url)

    if not links:
        print("[WARN] 列表页 HTML 中未发现详情页链接")
    else:
        print(f"[INFO] 共发现 {len(links)} 个详情页链接")

    return links


# ---------- 详情页 ----------
def _html_fragment_to_text(fragment: str) -> str:
    """把可能内嵌 HTML 的文本片段（如含 <br>）转为纯文本。

    - <br> 会被转为空格，避免两句话粘连
    - 其它标签被剥离，只保留文本
    - 连续空白压缩为单个空格
    """
    if not fragment:
        return ""

    wrapper = etree.fromstring(
        f"<div>{fragment}</div>",
        parser=etree.HTMLParser(recover=True),
    )
    if wrapper is None:
        text = fragment
    else:
        parts = [t.strip() for t in wrapper.xpath(".//text()") if t and t.strip()]
        text = " ".join(parts) if parts else fragment

    text = text.replace("&nbsp;", " ")
    text = re.sub(r'<!--.*?-->\s*$', '', text).strip()
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def _extract_paragraphs_from_data_json(raw: str) -> list[str]:
    """解析 #cmsMainContent 的 data-json 属性。

    data-json 是 HTML 实体转义过的 JSON 字符串，结构为:
        [{"contentType":1,"content":"段落文本"}, ...]
    contentType == 1 视为文本段落；content 内可能含 <br> 等 HTML。
    """
    if not raw:
        return []

    decoded = html_lib.unescape(raw)

    try:
        items = json.loads(decoded)
    except json.JSONDecodeError:
        return [
            _html_fragment_to_text(m)
            for m in re.findall(r'"content"\s*:\s*"(.*?)"\s*(?:,|})', decoded, re.S)
            if m.strip()
        ]

    if not isinstance(items, list):
        return []

    paragraphs: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("contentType") != 1:
            continue
        text = item.get("content", "")
        if not isinstance(text, str) or not text.strip():
            continue

        plain = _html_fragment_to_text(text)
        if plain:
            paragraphs.append(plain)

    return paragraphs


def parse_transcript_detail(article_url: str) -> dict:
    """解析 CGTN transcript 详情页。

    返回:
        {
            "url": str,
            "title": str,
            "paragraphs": list[str],   # 一个段落一个元素
            "video_m3u8": str | None,
        }
    """
    result: dict = {
        "url": article_url,
        "title": "",
        "paragraphs": [],
        "video_m3u8": None,
    }

    resp = get_request(article_url)
    if not resp:
        return result

    html = resp.content.decode("utf-8", errors="ignore")
    root = etree.HTML(html, base_url=article_url)
    if root is None:
        return result

    # ---------- 1. 标题 ----------
    title_nodes = root.xpath('//meta[@property="og:title"]/@content')
    if title_nodes:
        result["title"] = title_nodes[0].strip()
    else:
        title_nodes = root.xpath('//title/text()')
        if title_nodes:
            result["title"] = title_nodes[0].strip()

    # ---------- 2. 正文：读 #cmsMainContent 的 data-json ----------
    data_json_nodes = root.xpath('//div[@id="cmsMainContent"]/@data-json')
    if data_json_nodes:
        result["paragraphs"] = _extract_paragraphs_from_data_json(data_json_nodes[0])

    # 兜底：若 data-json 没解析出正文，退回 <p>
    if not result["paragraphs"]:
        for p in root.xpath('//div[@id="cmsMainContent"]//p'):
            text = p.xpath('string(.)').strip()
            if text and len(text) > 10:
                result["paragraphs"].append(text)

    # ---------- 3. 视频：优先 #top-media 的 video-source ----------
    video_source_nodes = root.xpath('//div[@id="top-media"]/@video-source')
    if video_source_nodes and video_source_nodes[0].strip():
        result["video_m3u8"] = urljoin(article_url, video_source_nodes[0].strip())
    else:
        # 兜底 1: JSON-LD VideoObject.contentUrl
        for script in root.xpath('//script[@type="application/ld+json"]/text()'):
            try:
                data = json.loads(script)
            except Exception:
                continue
            objs = data if isinstance(data, list) else [data]
            for obj in objs:
                if isinstance(obj, dict) and obj.get("@type") == "VideoObject":
                    cu = obj.get("contentUrl")
                    if isinstance(cu, str) and cu:
                        result["video_m3u8"] = urljoin(article_url, cu)
                        break
            if result["video_m3u8"]:
                break

        # 兜底 2: 全文正则找 .m3u8
        if not result["video_m3u8"]:
            match = re.search(r'https?://[^\s"\'<>]+?\.m3u8(?:\?[^\s"\'<>]*)?', html)
            if match:
                result["video_m3u8"] = match.group(0)

    return result
