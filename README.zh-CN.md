# CCH — Cognitive Context Hub

**为长期使用的 AI Agent 提供可信、可追溯的项目知识和历史素材。**

[English](README.md) · [快速开始](QUICKSTART.md) · [操作手册](USER-MANUAL.md) · [MIT 许可](LICENSE)

CCH 提供正式认知、来源、版本、候选素材和证据交接记录。
Agent、skills 和模型负责理解任务、推理、写作、排版以及最终 PPT／Word 生成。

这是独立的公开预览仓库，软件版本为 `0.5.0rc1`。仓库中的示例、图表和测试语料
均为虚构数据；真实项目试点、部署日志和早期 Git 历史未包含在内。

## 能解决什么问题

| 任务 | CCH 提供的能力 |
| --- | --- |
| 换一个 Agent 或开启新会话 | 按项目输出同一正式版本的上下文，保留不确定性并检查新鲜度 |
| 复用过去的图片与资料 | 检索候选素材，附带来源、定位、字节哈希和版本替代信息 |
| 把新发现纳入长期项目知识 | 形成提案，人工审查批准后原子发布，保留审计历史 |
| 将资料交给写作或 PPT skill | 提供证据包、引用关系、图文绑定、缺失项及交接记录 |

正式认知经过“候选 → 提案 → 人工审查 → 发布”。
人工批准不会自动把 `TENTATIVE` 或 `AI_INFERRED` 升级为已确认事实。
素材相关、格式合法、程序检查通过，都不能单独证明内容或选图在语义上正确。

## 安装与演示

环境：macOS 或 Linux、Python 3.11+、Git、uv。当前依赖 POSIX 文件锁，未验证原生 Windows。

```bash
git clone https://github.com/VanTam-CN/cch.git
cd cch
uv venv --python 3.11
uv pip install --python .venv/bin/python -e ".[test]"
.venv/bin/python tools/provision_tokenizer.py
.venv/bin/python tools/e2e_acceptance.py
.venv/bin/python tools/asset_demo.py
```

安装和显式准备词表需要联网。核心编译和这两个演示不需要模型密钥，也不调用模型 API。
第一个演示验证资料导入、候选审批发布、多次读取同一版本及备份恢复。
第二个演示验证虚构图表的来源追溯和已替代素材的检索排除。

演示使用预先编写的模型结果，输出到已忽略的 `output/` 目录。
已有输出目录不会被删除或覆盖；再次运行时使用新的 `--out` 目录。
脚本内自动批准只适用于隔离的虚构演示库，真实项目仍需你审查后批准。

详见 [快速开始](QUICKSTART.md) 和 [演示边界](DEMO.md)。

## 本地界面

```bash
.venv/bin/python webui/server.py --home ./demo-home --port 8765
```

访问 `http://127.0.0.1:8765`，操作资料导入、候选、提案、审批、发布及上下文查询。
界面主要覆盖知识治理；当前仅用于本机，审查人名称是审计标签，不是身份认证。

## Hindsight

Hindsight 是可选的实验适配器，默认关闭。已实现显式事件提交、本地归档、待发送队列、
重试和诊断召回；召回还不进入 Agent 上下文，也不能直接成为正式认知。
真实服务的运行、持久化和恢复需要独立验证。参见 [记忆接入说明](docs/MEMORY.md)。

## 范围与证据

- 文档导入支持 Markdown、UTF-8 文本/日志、JSONL、DOCX 正文与表格、文本型 PDF。
- 素材发现支持独立图片、OOXML 内嵌图片和 PDF 候选；语义描述由外部模型 exchange 提供。
- `--wps-render` 是显式启用的输入资料预览转换，会上传文档到已认证的 WPS 账号；默认不调用。
- 最终成稿由外部 skill 与模型完成，CCH 不建设内置 PPT／Word 成稿引擎。
- 当前权限约束是协作式流程，没有同一 OS 用户进程之间的系统级强隔离。
- [验证报告](TEST-REPORT.md) 仅说明这个公开快照的受控验证，不构成生产验收或广泛使用证明。

## 参与和许可

欢迎提交问题、虚构复现和小范围改进。参见 [贡献说明](CONTRIBUTING.md)、
[安全说明](SECURITY.md) 和 [路线图](docs/ROADMAP.md)。
代码、文档和虚构示例采用 [MIT 许可](LICENSE)。第三方依赖保留自己的许可证；
你导入或分享的业务资料需要相应授权。
