#!/usr/bin/env python3
"""补抓各学院官网的“学院简介/学院概况”页面

从 www.whu.edu.cn/jgsz/yxsz.htm 拿到学院列表，逐站首页找“简介/概况”入口，
抓取 1-2 个页面追加到 data/pages.jsonl（category=学院简介）。
"""
import json
import os
import re
import sys
import time
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

DATA = os.environ.get("WHU_DATA", "/opt/whu/data")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
DELAY = float(os.environ.get("WHU_DELAY", "0.3"))

sys.path.insert(0, os.path.dirname(__file__))
from crawl import extract, get_title, get_date, clean_text  # noqa: E402

session = requests.Session()
session.headers.update({"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"})

KEY = ("学院简介", "学院概况", "学院介绍", "本院概况", "学部简介", "中心简介",
       "研究院简介", "学校简介", "学院概述")


def fetch(url):
    r = session.get(url, timeout=25, allow_redirects=True)
    r.raise_for_status()
    r.encoding = r.apparent_encoding if r.encoding in (None, "ISO-8859-1") else r.encoding
    return BeautifulSoup(r.text, "lxml")


def colleges():
    soup = fetch("https://www.whu.edu.cn/jgsz/yxsz.htm")
    out, seen_host = [], set()
    for a in soup.find_all("a", href=True):
        text = a.get_text(strip=True)
        href = a["href"]
        if not href.startswith("http"):
            continue
        host = urlparse(href).netloc
        if not host.endswith("whu.edu.cn"):
            continue
        if host in seen_host:
            continue
        if re.search(r"(学院|学部|中心|研究院)$", text) and len(text) >= 4:
            seen_host.add(host)
            out.append((text, href))
    return out


def main():
    out_path = os.path.join(DATA, "pages.jsonl")
    rows = colleges()
    print("colleges found: %d" % len(rows), file=sys.stderr)

    added = 0
    with open(out_path, "a", encoding="utf-8") as fout:
        for name, home in rows:
            try:
                soup = fetch(home)
            except Exception as e:
                print("[ERR home] %s %s" % (home, e), file=sys.stderr)
                continue

            cands = []
            for a in soup.find_all("a", href=True):
                t = a.get_text(strip=True)
                if any(k in t for k in KEY) and len(t) <= 12:
                    cands.append(urljoin(home, a["href"]))
            # 去重保序
            seen, picked = set(), []
            for u in cands:
                if u not in seen and u.startswith("http"):
                    seen.add(u)
                    picked.append(u)

            if not picked:
                picked = [home]

            for u in picked[:2]:
                try:
                    s = fetch(u)
                except Exception as e:
                    print("[ERR page] %s %s" % (u, e), file=sys.stderr)
                    continue
                text = extract(s)
                if len(text) < 150:
                    continue
                title = get_title(s) or (name + "学院简介")
                rec = {"url": u, "source": "college", "category": "学院简介",
                       "title": title if len(title) > 6 else name + "简介",
                       "date": get_date(text, s), "text": text,
                       "hash": __import__("hashlib").md5(text.encode("utf-8")).hexdigest()}
                fout.write(json.dumps(rec, ensure_ascii=False) + "\n")
                added += 1
                print("[%d] %s -> %s (%d chars)" % (added, name, u, len(text)), file=sys.stderr)
                time.sleep(DELAY)
            time.sleep(DELAY)

    print("added %d college intro pages" % added, file=sys.stderr)


if __name__ == "__main__":
    main()
