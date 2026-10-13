# GOVERNANCE — 受控发布、审批绑定、崩溃恢复与权限边界

## 角色

| 角色 | 能做 | 不能做 |
| --- | --- | --- |
| 工作 Agent | agent context/check、proposal create、查看自己提案 | review approve/reject、apply、改审批记录 |
| 提炼 LLM | 读 job、写 result.json 候选 | 自我批准、读库外资料、执行源文指令 |
| 用户 | review render/approve/reject、apply、store export/restore | 未看差异包就“盲批”（工具会强制先 render） |
| 发布程序 | 校验批准、原子发布、幂等重试 | 补事实、合并冲突、提升确定性 |

## 事实源与目录

- 唯一正式版本指针：Git `refs/heads/cch-approved`（cch-home/store.git 裸仓库）。
- 快照缓存（cache/snapshots）、runs、proposals/reviews 目录都不是事实源。
- 原始文件在 sources/sha256/（内容寻址，不覆盖）；仅备份 Git 不等于备份原始文件，
  `store export` 会带上全部被引用 sources 并校验 hash。

## 审批绑定（防覆盖/防篡改）

- Proposal 冻结后不可变：`proposal_id + version + proposal_hash`；同 ID 同版本不同内容 → 拒绝。
- 批准记录绑定 proposal_hash、review_bundle_hash、base_revision；apply 时逐一复核，
  任一不符 → APPROVAL_MISMATCH / STALE_BASE，正式库不动。
- 编辑任何影响结果的字段 = 新版本（refresh），旧批准自然失效；不自动重放。
- `--reviewer` 只是记录字符串；cooperative 模式下不是身份认证。
- 批准保存 ≠ 确认属实：TENTATIVE/AI_INFERRED 原样入库。确认属实走
  `--confirm-truth`（只记录用户另行确认的事实，不改字段）或一个明确升级字段的提案。

## 发布原子性与并发

- 发布全程持 flock（locks/publish.lock，默认 30s 超时 → LOCK_TIMEOUT）。
- 临时完整树 → 全库 Schema/关系/证据/路径校验 → commit-tree →
  `git update-ref refs/heads/cch-approved <new> <old>`（compare-and-swap）。
- 两个批准基于同一基线并发 apply：恰好一个 APPLIED，另一个 STALE_BASE；无丢失更新（有测试）。
- 读者每次解析一次 ref、读一个 commit 的完整快照；发布期间只会看到旧或新整版本，不混版（有测试）。

## 幂等与崩溃恢复

- 幂等键 = proposal_id+version+proposal_hash。已发布的重复 apply 返回 ALREADY_APPLIED + 原收据。
- 崩溃窗口：
  - commit 已建、ref 未切 → 视为未发布；重试会正常发布（孤儿 commit 无害）。
  - ref 已切、提案状态未标 → 正式历史里的 transactions/*.json 是恢复依据；重试返回原收据。
  - 队列/状态 JSON 永远不是发布成功的唯一依据。
- 事务收据不写自身 commit hash（避免循环 hash）；revision 从外层 commit 获得。

## 回滚

不做 reset 删除历史。回滚 = 新建一个 replace/transition 提案（恢复旧值或 supersedes 关系），
走同样的审批与基线检查；历史保留完整审计轨迹。

## STALE_BASE 与刷新

```bash
cch --home H proposal refresh PR-xxxx   # 基于当前 approved 重建差异 → 新版本
cch --home H review render PR-xxxx      # 重新渲染 bundle（版本号取最新）
cch --home H review approve PR-xxxx --reviewer "$USER" && cch --home H apply PR-xxxx
```

提炼 job 过期（store 已前进）同理：重新 export-job → 重导候选 → 再建提案。

## 权限模式（如实申报）

- **cooperative（当前交付）**：所有程序同一 OS 用户。流程约束来自工具（未批准无法 apply、
  hash 不符拒绝、CAS 防覆盖），但没有 OS 级“只有人类能批准”。`store status` 如实报告。
- **enforced（未交付）**：需宿主为 Agent 进程/身份配置 store.git、proposals/、reviews/ 的
  写权限隔离，并以“Agent 身份写入被拒”的可复现测试证明后才可声称。加 chmod 或
  `--role human` 之类开关不构成隔离。

## 空库与首发布

`store init` 后是空容器，不自动塞示例。首个提案用 base_revision =
`000…0`（40 个零）引导，仍走完整校验/审批/发布；库非空后零基线提案被拒绝。
