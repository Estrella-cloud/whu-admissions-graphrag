#!/usr/bin/env python3
"""补抓招生简章/专业目录的 PDF 附件并抽取文本

很多“招生简章”“专业目录”页面本身只有标题，正文在 PDF 附件里。
这里扫描正文很短的页面，抓取其 PDF 链接，抽取文本后追加到 pages.jsonl。
"""
import hashlib
import io
import json
import os
import re
import sys
import time
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader

DATA = os.environ.get("WHU_DATA", "/opt/whu/data")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
session = requests.Session()
session.headers.update({"User-Agent": UA})

MAX_PAGES = int(os.environ.get("WHU_MAX_PDF_PAGES", "220"))
MAX_PDFS = int(os.environ.get("WHU_MAX_PDFS", "160"))


def short_pages():
    out = []
    with open(os.path.join(DATA, "pages.jsonl"), encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            if r["source"] in ("wdyz", "aoff", "gs") and len(r["text"]) < 600:
                out.append(r)
    return out


def pdf_links(html, base):
    soup = BeautifulSoup(html, "lxml")
    links = []
    for a in soup.find_all("a", href=True):
        h = a["href"].lower()
        if h.endswith(".pdf") or ".pdf?" in h or "/pdf/" in h:
            links.append((urljoin(base, a["href"]), a.get_text(strip=True)))
    return links


def extract_pdf(content):
    try:
        reader = PdfReader(io.BytesIO(content))
        pages = []
        for p in reader.pages[:40]:
            try:
                pages.append(p.extract_text() or "")
            except Exception:
                continue
        return "\n".join(pages).strip()
    except Exception as e:
        print("  pdf parse error: %s" % e, file=sys.stderr)
        return ""


def main():
    pages = short_pages()
    print("candidate pages: %d" % len(pages), file=sys.stderr)

    seen_pdf = set()
    added = 0
    out_path = os.path.join(DATA, "pages.jsonl")
    with open(out_path, "a", encoding="utf-8") as fout:
        for i, page in enumerate(pages[:MAX_PAGES]):
            if added >= MAX_PDFS:
                break
            try:
                r = session.get(page["url"], timeout=25)
                r.encoding = r.apparent_encoding if r.encoding in (None, "ISO-8859-1") else r.encoding
                links = pdf_links(r.text, page["url"])
            except Exception as e:
                print("[ERR page] %s %s" % (page["url"], e), file=sys.stderr)
                continue
            if not links:
                continue
            for url, anchor in links[:3]:
                if url in seen_pdf or added >= MAX_PDFS:
                    continue
                seen_pdf.add(url)
                try:
                    resp = session.get(url, timeout=60)
                    resp.raise_for_status()
                except Exception as e:
                    print("[ERR pdf] %s %s" % (url, e), file=sys.stderr)
                    continue
                text = extract_pdf(resp.content)
                if len(text) < 200:
                    continue
                title = (anchor or os.path.basename(url))[:80]
                if len(title) < 6:
                    title = page["title"] + "（附件）"
                rec = {"url": url, "source": page["source"], "category": "招生简章PDF",
                       "title": title, "date": page.get("date", ""), "text": text,
                       "hash": hashlib.md5(text.encode("utf-8")).hexdigest()}
                fout.write(json.dumps(rec, ensure_ascii=False) + "\n")
                added += 1
                print("[%d] %s (%d chars)" % (added, title[:50], len(text)), file=sys.stderr)
                time.sleep(0.2)
            time.sleep(0.2)

    print("added %d pdf documents" % added, file=sys.stderr)


if __name__ == "__main__":
    main()
