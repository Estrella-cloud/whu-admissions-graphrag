#!/usr/bin/env python3
"""构建 GraphRAG 索引

输入 data/pages.jsonl
输出
  data/chunks.jsonl        文本块
  data/bm25.pkl            BM25 索引（jieba 分词）
  data/embeddings.npy      向量（bge-small-zh，可选）
  data/graph.db            SQLite 知识图谱（实体 / 关系 / 文档提及）
"""
import json
import os
import pickle
import re
import sqlite3
import sys
import time
from collections import Counter, defaultdict

import numpy as np

DATA = os.environ.get("WHU_DATA", "/opt/whu/data")
CHUNK_SIZE = 450
OVERLAP = 80

ADMISSION_TYPES = ["强基计划", "保送生", "高水平艺术团", "高水平运动队", "艺术类",
                   "国家专项", "高校专项", "地方专项", "少数民族", "预科",
                   "推免", "统考", "申请考核", "硕博连读", "直博", "非全日制",
                   "港澳台", "留学生", "第二学士学位", "中外合作办学"]

COLLEGE_RE = re.compile(r"[\u4e00-\u9fa5]{2,10}(?:学院|学部|研究院|研究中心|附属医院|系)")
YEAR_RE = re.compile(r"(20\d{2})\s*年?")
SCORE_RE = re.compile(r"(\d{3})\s*分")
MAJOR_RE = re.compile(r"(?:专业名称|招生专业|专业)[:：\s]*([\u4e00-\u9fa5]{2,16}(?:专业|类|方向)?)")

# 页面装饰性文字，作为实体没有意义
STOP_ENTITIES = {
    "联系学院", "联系招办", "联系我们", "目录", "首页", "正文", "招生网", "搜索结果",
    "学院简介", "查看更多", "通知公告", "招生简章", "专业目录", "招生动态", "信息公开",
    "学校资源", "平台和项目", "奖助学金", "导师风采", "国际交流", "历年信息", "报录比",
    "复试分数线", "学校概况", "人才培养", "科学研究", "师资队伍", "公共服务", "校友",
}


def load_pages():
    path = os.path.join(DATA, "pages.jsonl")
    pages, seen_hash = [], set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            rec = json.loads(line)
            if rec["hash"] in seen_hash:
                continue
            seen_hash.add(rec["hash"])
            pages.append(rec)
    return pages


def split_chunks(text):
    paras = [p.strip() for p in text.split("\n") if p.strip()]
    chunks, buf = [], ""
    for p in paras:
        if len(buf) + len(p) + 1 <= CHUNK_SIZE:
            buf = (buf + "\n" + p).strip()
        else:
            if buf:
                chunks.append(buf)
            if len(p) <= CHUNK_SIZE:
                buf = p
            else:
                for i in range(0, len(p), CHUNK_SIZE - OVERLAP):
                    chunks.append(p[i:i + CHUNK_SIZE])
                buf = ""
    if buf:
        chunks.append(buf)
    return [c for c in chunks if len(c) >= 40]


def build_chunks(pages):
    chunks = []
    for p in pages:
        for i, c in enumerate(split_chunks(p["text"])):
            chunks.append({
                "id": len(chunks),
                "doc_id": p["hash"],
                "chunk_no": i,
                "url": p["url"],
                "source": p["source"],
                "category": p["category"],
                "title": p["title"],
                "date": p["date"],
                "text": c,
            })
    return chunks


def build_bm25(chunks):
    import jieba
    from rank_bm25 import BM25Okapi

    jieba.setLogLevel(60)
    corpus = []
    for c in chunks:
        toks = [t for t in jieba.cut(c["text"]) if len(t.strip()) > 1 or t.isdigit()]
        corpus.append(toks)
    bm25 = BM25Okapi(corpus)
    with open(os.path.join(DATA, "bm25.pkl"), "wb") as f:
        pickle.dump({"bm25": bm25, "corpus": corpus, "n": len(corpus)}, f)
    print("bm25: %d chunks, avg %.1f tokens" % (len(corpus), sum(map(len, corpus)) / max(1, len(corpus))), file=sys.stderr)


def build_embeddings(chunks):
    try:
        import torch
        from transformers import AutoModel, AutoTokenizer
    except Exception as e:
        print("embeddings: transformers unavailable (%s)" % e, file=sys.stderr)
        return False

    os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    name = "BAAI/bge-small-zh-v1.5"
    try:
        tok = AutoTokenizer.from_pretrained(name)
        model = AutoModel.from_pretrained(name)
        model.eval()
    except Exception as e:
        print("embeddings: model load failed (%s)" % e, file=sys.stderr)
        return False

    texts = [c["text"] for c in chunks]
    out = np.zeros((len(texts), model.config.hidden_size), dtype="float32")
    bs = 16
    t0 = time.time()
    with torch.no_grad():
        for i in range(0, len(texts), bs):
            batch = texts[i:i + bs]
            enc = tok(batch, padding=True, truncation=True, max_length=256, return_tensors="pt")
            h = model(**enc).last_hidden_state[:, 0]          # [CLS]
            h = torch.nn.functional.normalize(h, dim=-1)
            out[i:i + len(batch)] = h.numpy()
            if (i // bs) % 20 == 0:
                print("  embed %d/%d  %.1fs" % (i, len(texts), time.time() - t0), file=sys.stderr)
    np.save(os.path.join(DATA, "embeddings.npy"), out)
    print("embeddings: %s -> %s  (%.1fs)" % (out.shape, name, time.time() - t0), file=sys.stderr)
    return True


def build_graph(chunks):
    db = os.path.join(DATA, "graph.db")
    if os.path.exists(db):
        os.remove(db)
    conn = sqlite3.connect(db)
    c = conn.cursor()
    c.executescript("""
    CREATE TABLE entities(id INTEGER PRIMARY KEY, name TEXT UNIQUE, type TEXT, cnt INTEGER);
    CREATE TABLE edges(src TEXT, dst TEXT, rel TEXT, weight INTEGER, PRIMARY KEY(src,dst,rel));
    CREATE TABLE mentions(entity TEXT, chunk_id INTEGER, doc_id TEXT);
    CREATE TABLE docs(doc_id TEXT PRIMARY KEY, url TEXT, title TEXT, category TEXT, date TEXT);
    CREATE INDEX idx_mentions_entity ON mentions(entity);
    CREATE INDEX idx_mentions_doc ON mentions(doc_id);
    """)

    ent = Counter()
    ent_type = {}
    co = Counter()
    mentions = []
    docs = {}

    for ch in chunks:
        text = ch["text"]
        found = set()
        for m in COLLEGE_RE.finditer(text):
            name = m.group(0)
            if len(name) <= 12:
                found.add((name, "学院"))
        for t in ADMISSION_TYPES:
            if t in text:
                found.add((t, "招生类型"))
        for m in YEAR_RE.finditer(text):
            found.add((m.group(1), "年份"))
        for m in SCORE_RE.finditer(text):
            found.add((m.group(1) + "分", "分数"))
        for m in MAJOR_RE.finditer(text):
            name = m.group(1).strip()
            if 2 <= len(name) <= 16:
                found.add((name, "专业"))

        for name, typ in found:
            if name in STOP_ENTITIES or len(name) < 2:
                continue
            ent[(name, typ)] += 1
            ent_type[name] = typ
            mentions.append((name, ch["id"], ch["doc_id"]))

        names = sorted({n for n, _ in found})
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                co[(names[i], names[j])] += 1

        docs[ch["doc_id"]] = (ch["url"], ch["title"], ch["category"], ch["date"])

    c.executemany("INSERT OR REPLACE INTO entities(name,type,cnt) VALUES(?,?,?)",
                  [(n, ent_type[n], cnt) for (n, t), cnt in ent.items()])
    c.executemany("INSERT INTO mentions(entity,chunk_id,doc_id) VALUES(?,?,?)", mentions)
    c.executemany("INSERT OR REPLACE INTO docs(doc_id,url,title,category,date) VALUES(?,?,?,?,?)",
                  [(k,) + v for k, v in docs.items()])
    c.executemany("INSERT OR REPLACE INTO edges(src,dst,rel,weight) VALUES(?,?,?,?)",
                  [(a, b, "CO_OCCURS", w) for (a, b), w in co.items() if w >= 2])
    conn.commit()

    n_ent = c.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    n_edge = c.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    n_men = c.execute("SELECT COUNT(*) FROM mentions").fetchone()[0]
    conn.close()
    print("graph: %d entities, %d edges, %d mentions" % (n_ent, n_edge, n_men), file=sys.stderr)


def main():
    t0 = time.time()
    pages = load_pages()
    print("pages: %d" % len(pages), file=sys.stderr)

    chunks = build_chunks(pages)
    print("chunks: %d" % len(chunks), file=sys.stderr)
    with open(os.path.join(DATA, "chunks.jsonl"), "w", encoding="utf-8") as f:
        for c in chunks:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")

    build_bm25(chunks)
    if os.environ.get("WHU_SKIP_EMBED") != "1":
        build_embeddings(chunks)
    build_graph(chunks)
    print("done in %.1fs" % (time.time() - t0), file=sys.stderr)


if __name__ == "__main__":
    main()
