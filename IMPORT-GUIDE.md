# IMPORT-GUIDE — 旧文档与日志经 LLM 变成候选认知

链路：登记原文件 → 提取可定位文本 → 按边界分块 → LLM 输出候选 → 机器校验 →
去重/冲突提示 → Proposal → 用户审核 → 发布。导入与日常更新共用同一审批通道。

## 当前资料提取范围

| 输入 | 支持 | 定位方式 | 明确不支持/需 OCR |
| --- | --- | --- | --- |
| Markdown | 标题/段落/代码块（代码块仅作数据保留边界） | markdown_section{headings,line_start,line_end}、code_fence | HTML 块按原文保留，不解析结构 |
| TXT / 普通日志 | UTF-8 文本按固定行段 | text_line_range | 非 UTF-8、二进制附件 |
| JSONL | 每条记录一块；role/time 仅取记录内声明值 | jsonl_record | 非 JSON 行保留并标 NOT_JSON |
| DOCX | 正文段落+表格按文档顺序；单元格带表头上下文 | docx_paragraph{index}、docx_table_cell{table,row,column} | 批注、修订、文本框、页眉页脚、嵌入对象（报告如实列出） |
| 文本型 PDF | 逐页文本块 | pdf_page_block{page,block} | 无文本层页面标 NEEDS_OCR，全页无文本则拒绝入库而非伪成功 |
| XLSX/PPTX/图片 | — | — | UNSUPPORTED（source add 直接拒绝，不静默跳过） |

CRLF 在提取时统一为 LF 后冻结；此后任何人不得再改文本，否则 offset 作废。

## 登记与身份

```bash
cch --home H source add <file> --domain D --project P [--declared-date YYYY-MM-DD]
```

- 原文件按字节 SHA-256 存入 `sources/sha256/`，永不覆盖。
- 同字节重复导入：复用 blob，追加一条 scope 绑定；同名不同字节：新 source，两者都可追溯。
- `--declared-date` 是**资料声明的事实日期**，不传则 null 待核；导入时间永远不是事实时间。
- scope 必须显式 domain/project；不接受 `*`。全局规则需用户单独提案并带 GLOBAL_SCOPE 标记。

## Exchange 操作步骤

```bash
cch --home H ingest prepare SRC-...            # 提取+分块（目标 2000 Token/任务，1500–2500）
cch --home H ingest export-job SRC-... --chunk 0
# 把 jobs/JOB-.../job.json 交给你的 LLM（内含任务说明与输出 Schema）
# 回答保存为 jobs/JOB-.../result.json（模型给不出 offset 时可只给 quote，程序做唯一匹配）
cch --home H ingest import-result JOB-...
cch --home H ingest status SRC-...             # 覆盖统计
```

- job 的 manifest_hash 绑定输入；回传 job_id/hash 不匹配即拒绝。
- 结果里的 `approved`、命令、路径等未知字段一律拒绝——候选只是候选。
- 每块必须有状态；`NO_CANDIDATE`（含原因）是合法结果，不算漏处理。
- quote 必须逐字等于冻结块的 `text[start:end]`（Unicode code point，0 基，左闭右开）；
  程序复核，错位/多处匹配/零匹配 → NEEDS_EVIDENCE_ALIGNMENT，不得进提案。
- 候选默认 AI_INFERRED + TENTATIVE；批准入库不升级（升级需在 Review 中看明确目标值后另批）。

## 去重、冲突与分批

- 同 job 内同内容+同 span：判 DUPLICATE_OF，不重复建对象。
- 与既有认知的 lexical 匹配只作提示（matched_existing_ids），不自动合并；
  ADD_EVIDENCE / SUGGEST_SUPERSEDE / CONFLICT / POSSIBLE_DUPLICATE 意图需要人工构造提案。
- 首批建议：一个项目 3–5 份可核实资料、15–30 条候选，先边界/术语/决定，后事实/未决。

## 失败重试与覆盖检查

- 某块 NEEDS_RETRY 或未报告：该 chunk 状态保持可重试，`ingest status` 的 blocks_pending
  不会归零，覆盖率不显示 100%。
- store 前进后旧 job 作废（STALE_BASE）：对同一 source 重新 `export-job`，把候选重导入
  （块是内容寻址的，quote 会重新对齐），再建提案。
- 扫描 PDF 全页无文本：`ingest prepare` 直接报 NEEDS_OCR；先 OCR 再作为新 source 登记。

## 虚构 exchange 的演示留痕

运行 `.venv/bin/python tools/e2e_acceptance.py` 后，
`output/e2e-acceptance/cch-home/jobs/<JOB>/` 保存输入 job.json 与虚构 result.json。
其中 model_meta 明确标注 recorded-fixture。该演示不调用模型，不代表真实模型质量、
独立用户验收或生产效果。真实项目中的模型回传和人工决定需要另行保存、审查。
