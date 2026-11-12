#!/usr/bin/env python3
"""武汉大学招生 GraphRAG 问答服务

检索：知识图谱定候选文档 -> BM25 + 向量打分 -> 文档级重排 -> 取最相关段落
生成：llama.cpp server（OpenAI 兼容，本地 Qwen2.5-1.5B）
"""
import json
import os
import pickle
import re
import sqlite3
import sys
from typing import List
from urllib.parse import urlparse

import numpy as np
import requests
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

DATA = os.environ.get("WHU_DATA", "/opt/whu/data")
LLM_URL = os.environ.get("WHU_LLM", "http://127.0.0.1:8082/v1/chat/completions")
LLM_MODEL = os.environ.get("WHU_LLM_MODEL", "qwen2.5-1.5b-instruct")
TOP_DOCS = 3
CHUNK_CHARS = 600

# 太泛的词不做图谱实体匹配
ENTITY_STOP = {"招生", "计划", "专业", "学院", "大学", "武汉大学", "简章", "目录",
               "通知", "公告", "信息", "研究生", "本科", "博士", "硕士",
               "简介", "优势", "概况", "介绍", "特色"}

LANDING_TITLES = {"师资队伍", "学校概况", "机构设置", "人才培养", "科学研究",
                  "招生信息", "首页", "通知公告", "招生简章", "专业目录", "招生动态",
                  "信息公开", "历年信息", "联系我们", "学校资源", "院系设置"}

INTRO_WORDS = ("简介", "概况", "介绍")

app = FastAPI(title="WHU Admissions GraphRAG")

chunks: List[dict] = []
bm25 = None
embeddings = None
emb_tokenizer = None
emb_model = None
doc_meta = {}
doc_indices = {}


def is_listing(url, title, category, total_len):
    """落地页/栏目页判定：正文页在 /info/ 或 content.jsp 下。"""
    if "/info/" in url or "content.jsp" in url:
        return title in LANDING_TITLES
    if category == "学院简介" and total_len >= 300:
        return False
    return True


def load_resources():
    global chunks, bm25, embeddings, emb_tokenizer, emb_model, doc_meta
    with open(os.path.join(DATA, "chunks.jsonl"), encoding="utf-8") as f:
        chunks = [json.loads(line) for line in f]
    with open(os.path.join(DATA, "bm25.pkl"), "rb") as f:
        bm25 = pickle.load(f)["bm25"]

    by_doc = {}
    for i, ch in enumerate(chunks):
        by_doc.setdefault(ch["doc_id"], []).append(i)
    doc_indices.update(by_doc)

    by_doc_ch = {}
    for ch in chunks:
        by_doc_ch.setdefault(ch["doc_id"], []).append(ch)
    for doc_id, chs in by_doc_ch.items():
        total_len = sum(len(c["text"]) for c in chs)
        doc_meta[doc_id] = {
            "title": chs[0]["title"],
            "category": chs[0]["category"],
            "date": chs[0]["date"],
            "url": chs[0]["url"],
            "source": chs[0]["source"],
            "is_listing": is_listing(chs[0]["url"], chs[0]["title"], chs[0]["category"], total_len),
        }

    npy = os.path.join(DATA, "embeddings.npy")
    if os.path.exists(npy):
        embeddings = np.load(npy)
        try:
            import torch
            from transformers import AutoModel, AutoTokenizer
            os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
            emb_tokenizer = AutoTokenizer.from_pretrained("BAAI/bge-small-zh-v1.5")
            emb_model = AutoModel.from_pretrained("BAAI/bge-small-zh-v1.5")
            emb_model.eval()
        except Exception as e:
            print("warn: embedding model unavailable: %s" % e, file=sys.stderr)
            emb_tokenizer = emb_model = None

    print("loaded %d chunks / %d docs, bm25=%s, vectors=%s" % (
        len(chunks), len(doc_meta), bm25 is not None,
        None if embeddings is None else embeddings.shape), file=sys.stderr)


@app.on_event("startup")
def _startup():
    load_resources()


def tokenize(text):
    import jieba
    jieba.setLogLevel(60)
    return [t for t in jieba.cut(text) if len(t.strip()) > 1 or t.isdigit()]


def embed_query(q):
    if embeddings is None or emb_model is None:
        return None
    import torch
    with torch.no_grad():
        enc = emb_tokenizer([q], padding=True, truncation=True, max_length=256, return_tensors="pt")
        h = emb_model(**enc).last_hidden_state[:, 0]
        return torch.nn.functional.normalize(h, dim=-1).numpy()[0]


def graph_match(query):
    db = os.path.join(DATA, "graph.db")
    if not os.path.exists(db):
        return [], {}
    conn = sqlite3.connect(db)
    cur = conn.cursor()
    ents = [r[0] for r in cur.execute(
        "SELECT name FROM entities WHERE type IN ('学院','专业','招生类型') ORDER BY cnt DESC LIMIT 8000")]
    hit = sorted([e for e in ents if e in query and e not in ENTITY_STOP],
                 key=len, reverse=True)[:8]
    docs = {}
    for e in hit:
        docs[e] = {r[0] for r in cur.execute(
            "SELECT DISTINCT doc_id FROM mentions WHERE entity=? LIMIT 80", (e,))}
    conn.close()
    return hit, docs


def title_bonus(q_tokens, title):
    if not title or not q_tokens:
        return 0.0
    return sum(1 for t in q_tokens if t in title) / len(q_tokens)


def retrieve(query):
    q_tokens = set(tokenize(query))
    intro_query = any(w in query for w in INTRO_WORDS)
    wants_list = any(w in query for w in ("哪些", "什么专业", "专业", "名单", "目录"))
    years = re.findall(r"20\d{2}", query)

    # 本科 / 研究生 范围先验：招生问答里最有效的一个判断
    undergrad_words = ("本科", "高考", "保送", "强基", "艺术类", "高水平", "自主招生",
                       "招生章程", "大类", "新高考", "志愿")
    grad_words = ("硕士", "博士", "研究生", "推免", "复试", "调剂", "博导", "硕导",
                  "学术学位", "专业学位", "夏令营")
    wants_ug = any(w in query for w in undergrad_words)
    wants_grad = any(w in query for w in grad_words)

    ents, ents_docs = graph_match(query)
    strong = sorted([e for e in ents if len(e) >= 4 and e.endswith(("学院", "学部", "研究院", "系"))],
                    key=len, reverse=True)

    # 图谱定候选：问题点名了具体学院，就只在该学院被提及的文档里找
    cand = None
    if strong:
        key = strong[0]
        short = key.replace("武汉大学", "")
        if intro_query:
            # “XX学院简介”：直接在标题里找该学院的介绍页，最稳
            cand = {d for d, m in doc_meta.items()
                    if (key in m["title"] or short in m["title"])
                    and ("简介" in m["title"] or "概况" in m["title"]
                         or "xyjj" in m["url"] or "jj.htm" in m["url"])}
            if not cand:
                cand = ents_docs.get(key) or None
        else:
            cand = ents_docs.get(key) or None

    bm = bm25.get_scores(list(q_tokens) or ["_"]) if bm25 is not None else np.zeros(len(chunks))
    qv = embed_query(query)
    sims = (embeddings @ qv) if qv is not None else np.zeros(len(chunks))
    bmax = float(bm.max()) or 1.0
    smax = float(sims.max()) or 1.0

    gdocs = set()
    for s in ents_docs.values():
        gdocs |= s

    doc_best, doc_chunk, via_map = {}, {}, {}
    key_ent = strong[0] if strong else None
    key_short = key_ent.replace("武汉大学", "") if key_ent else None
    for idx, ch in enumerate(chunks):
        d = ch["doc_id"]
        if cand is not None and d not in cand:
            continue
        nbm, nsm = bm[idx] / bmax, sims[idx] / smax
        score = 0.65 * nbm + 0.65 * nsm
        # 同一页面可能混排多个学院，优先取真正提到目标实体的那一段
        if key_ent and key_ent in ch["text"]:
            score += 0.45
        elif key_short and key_short in ch["text"]:
            score += 0.25
        if score > doc_best.get(d, -1.0):
            v = []
            if nbm > 0.20:
                v.append("bm25")
            if nsm > 0.50:
                v.append("vector")
            if d in gdocs:
                v.append("graph")
            doc_best[d] = score
            doc_chunk[d] = idx
            via_map[d] = v

    for d in list(doc_best):
        m = doc_meta.get(d, {})
        s = doc_best[d]
        if m.get("is_listing"):
            s *= 0.30
        if urlparse(m.get("url", "")).path in ("", "/"):
            s *= 0.45
        s *= (1.0 + 1.5 * title_bonus(q_tokens, m.get("title", "")))
        # 本科 / 研究生 范围先验
        src = m.get("source", "")
        if wants_ug and not wants_grad:
            s *= 1.8 if src == "aoff" else (0.55 if src in ("wdyz", "gs") else 1.0)
        elif wants_grad and not wants_ug:
            s *= 1.8 if src in ("wdyz", "gs") else (0.55 if src == "aoff" else 1.0)

        # 问“有哪些专业”时，招生简章/专业目录 比通知类文档更可能是答案来源
        if wants_list:
            t = m.get("title", "")
            if "简章" in t or "专业目录" in t or "专业" in t:
                s *= 1.7
            elif "通知" in t or "方案" in t:
                s *= 0.8
        # 招生类型/专业实体（如“强基计划”）命中的文档优先，避免混进别的专项简章
        type_ents = [e for e in ents if e not in strong]
        if type_ents:
            members = ents_docs.get(type_ents[0], set())
            if members:
                s *= 1.6 if d in members else 0.5
        # 问题带年份时优先同年度的文档
        if years:
            t = m.get("title", "")
            if any(y in t for y in years):
                s *= 1.5
            elif re.search(r"20\d{2}", t):
                s *= 0.6
        if d in gdocs:
            s += 0.05
        if intro_query:
            t, u = m.get("title", ""), m.get("url", "")
            if "简介" in t or "概况" in t or "xyjj" in u or "jj.htm" in u or "/about/" in u:
                s *= 3.0
        doc_best[d] = s

    # 简介类问题只保留真正的介绍页
    if intro_query:
        intro = {d: s for d, s in doc_best.items()
                 if ("简介" in doc_meta[d]["title"] or "概况" in doc_meta[d]["title"]
                     or "xyjj" in doc_meta[d]["url"] or "jj.htm" in doc_meta[d]["url"]
                     or "/about/" in doc_meta[d]["url"])}
        if intro:
            doc_best = intro

    ranked = sorted(doc_best.items(), key=lambda kv: kv[1], reverse=True)[:TOP_DOCS]
    out = []
    for rank, (d, sc) in enumerate(ranked):
        idx = doc_chunk[d]
        c = dict(chunks[idx])
        if rank == 0:
            # 首篇文档取“最相关的 3 段”拼起来，清单类内容不会因为只取开头而漏项
            idxs = doc_indices.get(d, [idx])
            top = sorted(idxs, key=lambda i: (bm[i] / bmax + sims[i] / smax), reverse=True)[:3]
            joined = "\n".join(chunks[i]["text"] for i in sorted(top))
            c["text"] = joined[:1600]
        else:
            c["text"] = c["text"][:CHUNK_CHARS]
        c["score"] = round(sc, 4)
        c["via"] = via_map.get(d, [])
        out.append(c)
    return out, ents


SYSTEM = ("你是武汉大学招生信息助手。只能依据给定资料回答，不得编造。"
          "资料不足时直接说“现有资料未提及”。回答用中文，条理清晰，"
          "关键数字原样引用，句末用 [1][2] 标注出处编号。不要输出资料之外的链接。"
          "资料里如果是清单（专业、学院、时间节点），要逐条列全，不要遗漏或合并。"
          "不要重复同一句话，不要罗列与问题无关的联系方式。")


def build_prompt(query, ctx):
    parts = []
    for i, ch in enumerate(ctx, 1):
        parts.append("【资料%d】%s（%s，%s）\n%s" % (
            i, ch["title"] or "无标题", ch["category"], ch["date"] or "无日期", ch["text"]))
    return "资料：\n\n" + "\n\n".join(parts) + "\n\n问题：" + query


class ChatReq(BaseModel):
    query: str
    history: list = []


@app.post("/api/chat")
def chat(req: ChatReq):
    ctx, ents = retrieve(req.query)

    def gen():
        yield "event: meta\ndata: " + json.dumps({
            "entities": ents,
            "sources": [{"n": i + 1, "title": c["title"], "url": c["url"],
                         "category": c["category"], "date": c["date"],
                         "via": c["via"], "score": c["score"]}
                        for i, c in enumerate(ctx)],
        }, ensure_ascii=False) + "\n\n"

        messages = [{"role": "system", "content": SYSTEM}]
        for h in req.history[-4:]:
            if h.get("role") in ("user", "assistant") and h.get("content"):
                messages.append({"role": h["role"], "content": h["content"][:400]})
        messages.append({"role": "user", "content": build_prompt(req.query, ctx)})

        # 注意：不要用 repeat_penalty —— llama.cpp 会把它算到提示词上，
        # 资料本身重复度高时会把模型逼成拒答（实测会输出“现有资料未提及”）。
        payload = {"model": LLM_MODEL, "messages": messages, "stream": True,
                   "temperature": 0.25, "max_tokens": 420,
                   "frequency_penalty": 0.3,
                   "presence_penalty": 0.2,
                   "stop": ["【资料", "\n问题：", "\n\n资料："]}
        try:
            with requests.post(LLM_URL, json=payload, stream=True, timeout=900) as r:
                r.raise_for_status()
                for line in r.iter_lines(decode_unicode=False):
                    if not line or not line.startswith(b"data:"):
                        continue
                    data = line[5:].strip().decode("utf-8", "ignore")
                    if data == "[DONE]":
                        break
                    try:
                        delta = json.loads(data)["choices"][0]["delta"].get("content", "")
                    except Exception:
                        continue
                    if delta:
                        yield "event: token\ndata: " + json.dumps({"t": delta}, ensure_ascii=False) + "\n\n"
        except Exception as e:
            yield "event: token\ndata: " + json.dumps(
                {"t": "\n\n[模型服务出错：%s]" % e}, ensure_ascii=False) + "\n\n"
        yield "event: done\ndata: {}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream; charset=utf-8",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/stats")
def stats():
    conn = sqlite3.connect(os.path.join(DATA, "graph.db"))
    cur = conn.cursor()
    e = cur.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    ed = cur.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    conn.close()
    return {"pages": len(doc_meta), "chunks": len(chunks), "entities": e, "edges": ed,
            "vectors": 0 if embeddings is None else int(embeddings.shape[0])}


@app.get("/api/graph")
def graph(q: str = "", limit: int = 40):
    conn = sqlite3.connect(os.path.join(DATA, "graph.db"))
    cur = conn.cursor()
    if q:
        rows = cur.execute("SELECT name,type,cnt FROM entities WHERE name LIKE ? ORDER BY cnt DESC LIMIT ?",
                           ("%" + q + "%", limit)).fetchall()
    else:
        rows = cur.execute("SELECT name,type,cnt FROM entities ORDER BY cnt DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return {"entities": [{"name": r[0], "type": r[1], "count": r[2]} for r in rows]}


@app.get("/", response_class=HTMLResponse)
def index():
    return open(os.path.join(os.path.dirname(__file__), "index.html"), encoding="utf-8").read()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("WHU_PORT", "8090")), log_level="info")
