# 武汉大学招生 GraphRAG 问答

一个完全离线运行的招生问答系统：抓取武大官网公开招生信息 → 建知识图谱与向量索引 → 网页对话，回答带原文出处。

访问地址：**http://localhost:8090**

---

## 一、系统构成

```
官网页面  ──► 爬虫 ──► pages.jsonl ──► 分块 ──► BM25 索引
                                    │              └─► bge-small-zh 向量
                                    └──► 实体/关系抽取 ──► SQLite 知识图谱
                                                              │
用户提问 ──► 图谱定候选文档 ──► BM25+向量打分 ──► 取最相关段落 ──► Qwen2.5-1.5B ──► 流式回答 + 出处
```

| 组件 | 说明 |
|---|---|
| 数据源 | `aoff.whu.edu.cn` 本科招生、`wdyz.whu.edu.cn` 研究生招生、各学院官网简介 |
| 语料 | 796 篇文档（本科招生 172 / 学院简介 86 / 研究生招生 59 / 博士简章 45 / 强基 23 …） |
| 分块 | 3457 个文本块（450 字，重叠 80） |
| 词法索引 | jieba 分词 + BM25Okapi |
| 语义索引 | `BAAI/bge-small-zh-v1.5`（512 维，CPU 推理） |
| 知识图谱 | 3066 实体 / 4815 关系（学院、专业、招生类型、年份、分数；共现关系） |
| 生成模型 | Qwen2.5-1.5B-Instruct Q4_K_M（llama.cpp，本地 CPU） |
| Web | FastAPI + 原生 HTML/JS，SSE 流式输出 |

---

## 二、怎么用

浏览器打开 **http://localhost:8090** ，直接提问。示例：

- 武汉大学2026年强基计划有哪些招生专业？
- 武汉大学计算机学院的简介是什么？
- 保送生的报考条件是什么？
- 博士研究生的报名流程？
- 艺术学院有哪些专业？

回答下方可展开「依据 N 条资料」，每条都给了官网原文链接，点进去可核对。

### 服务管理

```powershell
# 启动（WSL 常驻 + 全部服务）
schtasks /run /tn "GPUServer-WSL"

# 查看状态
wsl -d Ubuntu2204 -u root -e systemctl status whu-app whu-llm

# 重启应用（改完代码后）
wsl -d Ubuntu2204 -u root -e systemctl restart whu-app

# 停止
wsl -d Ubuntu2204 -u root -e systemctl stop whu-app whu-llm
```

端口：`8090` 网页、`8082` 模型接口（仅本机）。

---

## 三、重新抓取与重建索引

```bash
# 1) 抓取主站（约 15 分钟，900 页）
wsl -d Ubuntu2204 -u root -e bash -c "cd /opt/whu && WHU_DATA=/opt/whu/data WHU_MAX_PAGES=900 /opt/dl/venv/bin/python app/crawl.py"

# 2) 补抓各学院“学院简介”页
wsl -d Ubuntu2204 -u root -e bash -c "cd /opt/whu/app && WHU_DATA=/opt/whu/data /opt/dl/venv/bin/python crawl_colleges.py"

# 3) 补抓 PDF 附件（可选）
wsl -d Ubuntu2204 -u root -e bash -c "cd /opt/whu/app && WHU_DATA=/opt/whu/data /opt/dl/venv/bin/python crawl_pdfs.py"

# 4) 重建索引（约 6 分钟，含向量化）
wsl -d Ubuntu2204 -u root -e bash -c "cd /opt/whu && HF_ENDPOINT=https://hf-mirror.com WHU_DATA=/opt/whu/data /opt/dl/venv/bin/python app/build_index.py"

# 5) 重启应用
wsl -d Ubuntu2204 -u root -e systemctl restart whu-app
```

---

## 四、检索是怎么做的

1. **图谱定候选**：从问题里识别实体（学院 / 专业 / 招生类型）。若点名了具体学院，只在该学院被提及的文档里检索，避免串到别的学院。
2. **本科 / 研究生范围先验**：问题含“本科、高考、保送、强基、艺术类”等词时，本科招生站文档 ×1.8、研究生站 ×0.55；含“硕士、博士、推免、复试”等词时反之。避免把「保送生」答成研究生复试政策。
3. **双路打分**：BM25（词法，jieba 分词）+ bge-small 向量余弦相似度，各占 0.65 权重归一化后相加。
4. **文档级重排**：
   - 落地页/栏目页降权 0.30（正文页在 `/info/` 或 `content.jsp` 下）
   - 学院官网首页降权 0.45（导航和新闻为主）
   - 标题命中问题词加权（最多 +1.5 倍）
   - 问「简介/概况」时，优先标题含「简介」或 URL 含 `xyjj`/`jj.htm` 的文档（×3）
   - 问「有哪些专业」时，招生简章/专业目录加权 1.7，通知/方案降权 0.8
   - 问题带年份（如 2026）时，同年度文档 ×1.5，其它年度 ×0.6
   - 招生类型实体（如「强基计划」）命中的文档 ×1.6，未命中 ×0.5
4. **取段落**：首篇文档取最相关的 5 段拼接（最多 1900 字），其余各取 600 字。

---

## 五、性能与限制（重要）

- **速度**：CPU 只有 2 核，首字延迟 **20–60 秒**，完整回答通常 30–90 秒。这是本地小模型的硬限制，不是故障。
- **模型能力**：1.5B 模型在长清单场景会漏项（例如强基计划 9 个专业可能只列出 7–8 个），也会偶发把相近概念混淆。**请以下方链接的官网原文为准。**
- **语料边界**：
  - 本科招生、学院简介覆盖较好；
  - 部分研究生「招生简章 / 专业目录」页面正文由 JS 动态加载，静态抓取拿不到，这类问题的回答可能不完整；
  - 分数线、招生计划等以 `zsdata.whu.edu.cn` 单页应用形式提供，未抓取。
- **不联网**：模型与索引都在本机，断网也能用；重建索引时需要联网。

---

## 六、文件位置

| 路径 | 内容 |
|---|---|
| `/opt/whu/app/app.py` | Web 服务与检索逻辑 |
| `/opt/whu/app/index.html` | 前端页面 |
| `/opt/whu/app/crawl.py` | 主爬虫 |
| `/opt/whu/app/crawl_colleges.py` | 学院简介补抓 |
| `/opt/whu/app/crawl_pdfs.py` | PDF 附件补抓 |
| `/opt/whu/app/build_index.py` | 索引构建 |
| `/opt/whu/data/pages.jsonl` | 原始语料 |
| `/opt/whu/data/chunks.jsonl` | 分块 |
| `/opt/whu/data/bm25.pkl` | BM25 索引 |
| `/opt/whu/data/embeddings.npy` | 向量矩阵 |
| `/opt/whu/data/graph.db` | 知识图谱（SQLite） |
| `/opt/models/qwen2.5-1.5b-instruct-q4_k_m.gguf` | 本地模型 |
| `/var/log/gpuserver/` | 各阶段日志 |

Git 工作副本：`C:\Users\wanyo\Documents\deepseek-harness\default-workspace\whu\`

---

## 七、换更强的模型

CPU 上跑 1.5B 是权衡结果。若以后有 GPU 或 API：

```bash
# 方式一：换更大的 GGUF（把模型文件换掉，改 whu-llm.service 的 -m 路径）
# 方式二：接 API，改 whu-app.service 的环境变量
Environment=WHU_LLM=https://api.deepseek.com/v1/chat/completions
Environment=WHU_LLM_MODEL=deepseek-chat
```

改完 `systemctl daemon-reload && systemctl restart whu-app` 即可，检索层不用动。
