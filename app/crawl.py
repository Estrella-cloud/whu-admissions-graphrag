#!/usr/bin/env python3
"""武汉大学公开招生信息爬虫

抓取范围：
  1. aoff.whu.edu.cn   本科招生网
  2. wdyz.whu.edu.cn   研究生招生信息网
  3. www.whu.edu.cn    院系设置 + 学院简介（一跳）
输出：data/pages.jsonl  每行一个页面 {url, source, category, title, date, text}
"""
import json
import os
import re
import sys
import time
import hashlib
from collections import deque
from urllib.parse import urljoin, urlparse, urldefrag

import requests
from bs4 import BeautifulSoup

DATA_DIR = os.environ.get("WHU_DATA", "/opt/whu/data")
os.makedirs(DATA_DIR, exist_ok=True)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"}

DELAY = float(os.environ.get("WHU_DELAY", "0.35"))
TIMEOUT = 20
MAX_PAGES = int(os.environ.get("WHU_MAX_PAGES", "1500"))

# (起始 URL, 站点标识, 分类, 允许的前缀)
SEEDS = [
    ("https://aoff.whu.edu.cn/", "aoff", "本科招生", ["https://aoff.whu.edu.cn/"]),
    ("https://aoff.whu.edu.cn/zsxx1/zsz.htm", "aoff", "本科招生章程", ["https://aoff.whu.edu.cn/"]),
    ("https://aoff.whu.edu.cn/zsxx1/tzgg.htm", "aoff", "本科招生通知", ["https://aoff.whu.edu.cn/"]),
    ("https://aoff.whu.edu.cn/zsxx1/tslzs/bs.htm", "aoff", "保送生招生", ["https://aoff.whu.edu.cn/"]),
    ("https://aoff.whu.edu.cn/zsxx1/tslzs/gspyt.htm", "aoff", "高水平艺术团", ["https://aoff.whu.edu.cn/"]),
    ("https://aoff.whu.edu.cn/zsxx1/tslzs/gspydd_.htm", "aoff", "高水平运动队", ["https://aoff.whu.edu.cn/"]),
    ("https://aoff.whu.edu.cn/zsxx1/tslzs/qjjh.htm", "aoff", "强基计划", ["https://aoff.whu.edu.cn/"]),
    ("https://aoff.whu.edu.cn/zsxx1/tslzs/ysl.htm", "aoff", "艺术类招生", ["https://aoff.whu.edu.cn/"]),
    ("https://wdyz.whu.edu.cn/", "wdyz", "研究生招生", ["https://wdyz.whu.edu.cn/"]),
    ("https://wdyz.whu.edu.cn/sszs/zsdt.htm", "wdyz", "硕士招生动态", ["https://wdyz.whu.edu.cn/"]),
    ("https://wdyz.whu.edu.cn/sszs/ptss.htm", "wdyz", "硕士招生简章", ["https://wdyz.whu.edu.cn/"]),
    ("https://wdyz.whu.edu.cn/sszs/tms.htm", "wdyz", "推免生招生", ["https://wdyz.whu.edu.cn/"]),
    ("https://wdyz.whu.edu.cn/sszs/xly.htm", "wdyz", "夏令营", ["https://wdyz.whu.edu.cn/"]),
    ("https://wdyz.whu.edu.cn/bszs/zsdt.htm", "wdyz", "博士招生动态", ["https://wdyz.whu.edu.cn/"]),
    ("https://wdyz.whu.edu.cn/bszs/zsjz.htm", "wdyz", "博士招生简章", ["https://wdyz.whu.edu.cn/"]),
    ("https://wdyz.whu.edu.cn/bszs/zyml.htm", "wdyz", "博士专业目录", ["https://wdyz.whu.edu.cn/"]),
    ("https://wdyz.whu.edu.cn/gatzs/zsjz.htm", "wdyz", "港澳台招生简章", ["https://wdyz.whu.edu.cn/"]),
    ("https://wdyz.whu.edu.cn/gatzs/zyml.htm", "wdyz", "港澳台专业目录", ["https://wdyz.whu.edu.cn/"]),
    ("https://wdyz.whu.edu.cn/gatzs/zsdt.htm", "wdyz", "港澳台招生动态", ["https://wdyz.whu.edu.cn/"]),
    ("https://www.whu.edu.cn/jgsz/yxsz.htm", "whu", "院系设置", ["https://www.whu.edu.cn/"]),
]

# 学院官网首页，只抓一跳（用于学院简介）
COLLEGE_HOME_ONLY = True

SKIP_EXT = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".zip", ".rar", ".jpg",
            ".png", ".gif", ".jpeg", ".mp4", ".avi", ".css", ".js", ".ico", ".ppt", ".pptx")

session = requests.Session()
session.headers.update(HEADERS)


def norm(url, base=None):
    if base:
        url = urljoin(base, url)
    url, _ = urldefrag(url)
    if url.startswith("http://"):
        url = "https://" + url[len("http://"):]
    return url


def ok_url(url, prefixes):
    if any(url.lower().split("?")[0].endswith(e) for e in SKIP_EXT):
        return False
    return any(url.startswith(p) for p in prefixes)


def clean_text(t):
    t = re.sub(r"[ \t\u00a0]+", " ", t)
    t = re.sub(r"\n\s*\n+", "\n", t)
    return t.strip()


def extract(soup):
    # 注意：不要删除 <form> —— 博达(VSB)站点整页正文就包在 ASP.NET 的 form 里
    for tag in soup(["script", "style", "noscript", "iframe", "svg",
                     "nav", "footer", "header", "aside"]):
        tag.decompose()

    priority = ["#vsb_content", "div.v_news_content", "div.ar_article",
                "div.article-content", "div.article", "article"]
    for sel in priority:
        try:
            nodes = soup.select(sel)
        except Exception:
            nodes = []
        if not nodes:
            continue
        node = max(nodes, key=lambda n: len(n.get_text(strip=True)))
        txt = node.get_text("\n", strip=True)
        if len(txt) >= 60:
            return clean_text(txt)

    blocks = soup.find_all(["div", "article", "section"])
    if blocks:
        node = max(blocks, key=lambda n: len(n.get_text(strip=True)))
        txt = node.get_text("\n", strip=True)
        if len(txt) >= 60:
            return clean_text(txt)

    body = soup.body or soup
    return clean_text(body.get_text("\n", strip=True))


def get_title(soup):
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        return h1.get_text(strip=True)
    if soup.title and soup.title.string:
        return soup.title.string.strip()
    return ""


def get_date(text, soup):
    m = re.search(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})", text[:600])
    if m:
        return "%s-%02d-%02d" % (m.group(1), int(m.group(2)), int(m.group(3)))
    for meta in soup.find_all("meta"):
        if meta.get("name") in ("PubDate", "publishdate", "publish_date"):
            v = meta.get("content", "")
            m = re.search(r"(20\d{2})-(\d{2})-(\d{2})", v)
            if m:
                return m.group(0)
    return ""


def crawl():
    out_path = os.path.join(DATA_DIR, "pages.jsonl")
    seen = set()
    queue = deque((norm(u), src, cat, prefixes, 0) for u, src, cat, prefixes in SEEDS)
    fout = open(out_path, "w", encoding="utf-8")
    n, errors = 0, 0

    while queue and n < MAX_PAGES:
        url, src, cat, prefixes, depth = queue.popleft()
        if url in seen:
            continue
        seen.add(url)

        try:
            r = session.get(url, timeout=TIMEOUT, allow_redirects=True)
            r.raise_for_status()
            if "text/html" not in r.headers.get("Content-Type", ""):
                continue
            r.encoding = r.apparent_encoding if r.encoding in (None, "ISO-8859-1") else r.encoding
            soup = BeautifulSoup(r.text, "lxml")
        except Exception as e:
            errors += 1
            print("[ERR] %s -> %s" % (url, e), file=sys.stderr)
            continue

        text = extract(soup)
        title = get_title(soup)
        date = get_date(text, soup)

        if len(text) >= 80:
            rec = {"url": url, "source": src, "category": cat, "title": title,
                   "date": date, "text": text,
                   "hash": hashlib.md5(text.encode("utf-8")).hexdigest()}
            fout.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fout.flush()
            n += 1
            if n % 25 == 0:
                print("[%d] %s (%d chars)" % (n, url, len(text)), file=sys.stderr)

        # 入队
        if depth < 3 or src == "whu":
            for a in soup.find_all("a", href=True):
                nxt = norm(a["href"], url)
                if not nxt.startswith("http"):
                    continue
                if nxt in seen:
                    continue
                if ok_url(nxt, prefixes):
                    queue.append((nxt, src, cat, prefixes, depth + 1))
                elif COLLEGE_HOME_ONLY and depth == 0:
                    # 学院官网首页（不同域名），只抓这一页
                    host = urlparse(nxt).netloc
                    if host.endswith("whu.edu.cn") and host != urlparse(url).netloc:
                        queue.append((nxt, "college", "学院简介", [nxt], 3))

        time.sleep(DELAY)

    fout.close()
    print("done: %d pages, %d errors -> %s" % (n, errors, out_path), file=sys.stderr)


if __name__ == "__main__":
    crawl()
