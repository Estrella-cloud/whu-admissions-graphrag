import json, sys, time, sqlite3
import requests

BASE = "http://127.0.0.1:8090"

if len(sys.argv) > 1 and sys.argv[1] == "coverage":
    conn = sqlite3.connect("/opt/whu/data/graph.db")
    cur = conn.cursor()
    print("=== docs by category ===")
    for cat, n in cur.execute("SELECT category, COUNT(*) FROM docs GROUP BY category ORDER BY n DESC"):
        print("  %-16s %d" % (cat, n))
    print("=== top entities ===")
    for name, typ, cnt in cur.execute("SELECT name,type,cnt FROM entities ORDER BY cnt DESC LIMIT 15"):
        print("  %-22s %-6s %d" % (name, typ, cnt))
    conn.close()
    sys.exit(0)

q = sys.argv[1] if len(sys.argv) > 1 else "武汉大学2026年强基计划有哪些专业？"
t0 = time.time()
with requests.post(BASE + "/api/chat", json={"query": q, "history": []}, stream=True, timeout=1800) as r:
    first = None
    for raw in r.iter_lines(decode_unicode=False):
        if not raw or not raw.startswith(b"data:"):
            continue
        data = raw[5:].strip().decode("utf-8", "ignore")
        if not data or data == "{}":
            continue
        try:
            obj = json.loads(data)
        except Exception:
            continue
        if "sources" in obj:
            print("=== retrieved %d docs in %.1fs ===" % (len(obj["sources"]), time.time() - t0))
            print("entities:", "、".join(obj.get("entities", [])) or "(none)")
            for s in obj["sources"]:
                print("  [%d] %s | %s | %s | %s" % (s["n"], (s["title"] or "")[:40], s["category"], s["date"], ",".join(s["via"])))
        elif "t" in obj:
            if first is None:
                first = time.time()
                print("=== first token after %.1fs ===" % (first - t0))
            sys.stdout.write(obj["t"])
            sys.stdout.flush()
print("\n=== total %.1fs ===" % (time.time() - t0))
