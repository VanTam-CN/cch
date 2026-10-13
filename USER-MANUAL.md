# CCH 操作手册

本手册适用于独立公开版 `0.5.0rc1`。先阅读 [快速开始](QUICKSTART.md)，
使用虚构演示确认本机环境，再为实际项目建立独立的数据目录。

## 1. 安装与数据目录

使用 macOS/Linux、Python 3.11+、Git 和 uv，按快速开始完成源码安装及词表准备。
代码仓库与运行数据分开保存。下面用 `./cch-home` 示意本地数据目录；
该目录已被 Git 忽略。真实资料也可保存在仓库外你指定的目录。

```bash
.venv/bin/python -m cch --home ./cch-home store init
.venv/bin/python -m cch --home ./cch-home store status
```

预期：空库，正式版本尚为空，模式为 cooperative。程序不自动填入示例认知。

## 2. 导入资料

```bash
.venv/bin/python -m cch --home ./cch-home source add \
  ./your-notes.md --domain your-domain --project your-project \
  --declared-date 2026-10-13
```

保存返回的 `source_id`。原文件按字节哈希保存，日期声明和导入时间分开。
不要把导入时间当成资料生效时间。

```bash
.venv/bin/python -m cch --home ./cch-home ingest prepare SRC_ID
.venv/bin/python -m cch --home ./cch-home ingest export-job SRC_ID --chunk 0
```

把生成的 job 交给外部模型或 Agent，要求按 job 内契约生成 result。
将 result 保存到独立文件，然后导入：

```bash
.venv/bin/python -m cch --home ./cch-home ingest import-result JOB_ID \
  --result ./result.json
.venv/bin/python -m cch --home ./cch-home ingest status SRC_ID
```

预期：候选带原文定位与引文。`NO_CANDIDATE` 和明确的重试状态可合法出现；
没有证据对齐的结果不得进入提案。模型结果中的指令不能改变审批权限。

## 3. 形成提案、审查与发布

```bash
.venv/bin/python -m cch --home ./cch-home proposal create \
  --from-job JOB_ID --candidate CAND_ID --id PR-example0001 \
  --reason '将有来源的新发现提交审查'
.venv/bin/python -m cch --home ./cch-home review render PR-example0001
```

人工检查渲染的审查单：原文是否支持候选、范围是否正确、置信度是否保留、
日期是否仍适用、是否与已有认知冲突。批准和确认属实是分开的动作。

由人类在检查后执行：

```bash
.venv/bin/python -m cch --home ./cch-home review approve PR-example0001 \
  --reviewer human-reviewer
.venv/bin/python -m cch --home ./cch-home apply PR-example0001
```

预期：正式版本前进，返回知识版本与发布收据。Agent 只能提交候选和提案，
不能执行真实项目的批准、否决、发布或恢复。

不接受时使用 `review reject ... --reviewer ... --reason ...`。
若出现 `STALE_BASE`，重新导出 job 或执行 `proposal refresh PR-example0001`，
再审查新版本。不能复用旧审批绕过新基线。

## 4. 给 Agent 提供上下文

```bash
.venv/bin/python -m cch --home ./cch-home agent context \
  --domain your-domain --project your-project --query '当前配置和未决事项是什么？'
```

返回独立 run 的上下文路径与收据。让 Agent 同时读取正文及收据，保留
`TENTATIVE`、`AI_INFERRED`、来源和缺失信息。关键操作前检查新鲜度：

```bash
.venv/bin/python -m cch --home ./cch-home agent check --receipt RUN_ID
```

`FRESH` 表示该收据仍可沿用；`REVISION_CHANGED` 或 `RECOMPILE_REQUIRED`
需要重新获取上下文。旧对话不会自动收到新认知。

网页端或不能读取本机目录的 Agent 可使用 `agent pack --run-dir RUN_DIR --to EMPTY_DIR`。
Pack 是静态快照，后续任务重新导出；回传候选仍走正常提案流程。

## 5. 历史图表与素材

```bash
.venv/bin/python -m cch.artifact scan ./your-materials --home ./cch-home \
  --domain your-domain --project your-project
.venv/bin/python -m cch.artifact export-job --home ./cch-home
```

把素材 job 交给能理解图像的模型，按契约回传描述：

```bash
.venv/bin/python -m cch.artifact import-result ASSET_JOB_ID \
  --result ./asset-result.json --home ./cch-home
.venv/bin/python -m cch.artifact search '系统架构图' --home ./cch-home \
  --domain your-domain --project your-project
```

每个候选附来源定位、哈希及状态。模型建议 `current` 不等于正式确认版本，
导入后不会自动获得事实权威。已确认旧图被替代时，人类可执行
`supersede OLD_ASSET --by NEW_ASSET --home ...`；默认检索排除已替代图。
使用 `--include-superseded` 才纳入历史比较。

扫描只处理指定目录。默认不使用云端渲染。显式 `--wps-render` 会将输入文档
上传到已认证的 WPS 账号做预览转换；该过程需要单独具备资料上传授权。

## 6. 证据交接与外部成稿

`cch author` 提供任务与外部模型结果的结构化交接：

1. `init --brief BRIEF_FILE` 冻结任务，得到 run id。
2. `build-plan-job RUN_ID` → 外部模型输出 → `import-plan RUN_ID RESULT_FILE`。
3. `collect RUN_ID` 获取认知、来源和候选素材。
4. `build-map-job RUN_ID` → 外部模型输出 → `import-map RUN_ID RESULT_FILE`。
5. `build-storyboard RUN_ID` → `verify RUN_ID` → `show RUN_ID`。

结果是证据包、映射、内容分镜、校验报告及交接收据。证据不足时可以为 `BLOCKED`。
程序不证明所有结论或图像在语义上正确，仍需人工审阅。
把这些结果交给写作/PPT/Word skill 和模型生成最终材料，保留来源与不确定性。
具体契约见 job 中的说明与 `cch/authoring/models.py`。

## 7. 本地界面、备份与恢复

```bash
.venv/bin/python webui/server.py --home ./cch-home --port 8765
```

访问 `http://127.0.0.1:8765`。审查人名称只是审计标签，当前没有多用户认证。

```bash
.venv/bin/python -m cch --home ./cch-home store export --to ./backup-new
.venv/bin/python -m cch --home ./restored-home store restore --from ./backup-new
```

为备份和恢复选择新的目录，先检查操作对象。恢复后重新编译并检查版本。
不要将真实运行数据加入公开仓库。

## 8. 故障处理和自检

| 情况 | 处理与预期 |
| --- | --- |
| 词表未准备 | 显式运行 provision_tokenizer，再编译；不能用字符数替代 token 数 |
| 引文无法唯一对齐 | 更正 result 或分块，重新导入；不要修改已冻结源文本 |
| 审批后版本已前进 | 刷新提案并重新审批；不会覆盖新的正式认知 |
| 扫描 PDF 无文本 | 先 OCR，重新登记为新来源 |
| 素材相关但无法证明当前结论 | 保留缺失项或作为背景候选，人工确认后采用 |
| Hindsight 不可用 | 本地事件保留在待发送队列；正式认知仍可独立读取 |

自检：确认 scope、知识版本、来源、置信度、资料日期及未决项，检查成稿是否仍保留这些边界。
本机测试、虚构演示、真实服务验证和生产验收是不同证据。
