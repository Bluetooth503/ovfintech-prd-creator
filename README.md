# ovfintech-prd-creator · 需求文档标准化助手

> 把零散材料或旧需求文档整理成符合标准模板的需求文档（PRD / 产品需求规格说明书），或对现有文档做结构体检。

一个遵循 [Agent Skills](https://agentskills.io) 开放标准（`SKILL.md`）的技能，兼容 Claude Code / Codex / Cursor / OpenCode 等支持 `SKILL.md` 的 agent。

## 三种模式

| 模式 | 什么时候用 | 产出 |
|:--|:--|:--|
| **创建** | 给的是零散材料（聊天记录、会议纪要、旧片段、原型说明），要写一份需求文档 | 标准文档 + 未决缺口说明 |
| **优化** | 给的是一份现有需求文档（`.docx` / `.md`），要按模板整理 / 规范化 / 重排 | 标准版 + 变更说明 |
| **审查** | 要检查 / 体检 / 评审前自查，且明说「先不要改」 | 体检报告（问题清单 + 建议追问），**不改写文档** |

粒度上另分**标准 / 轻量**：小需求、优化项、配置变更走轻量；新模块、新功能、跨系统需求走标准。

## 设计取向

- **结构稳定优先**：agent 在需求文档上的角色是「整理与查漏」，不是「创作」。改写越流畅，越容易把脑补写成事实——所以先问清、事实保真、缺口经对话确认。
- **术语一个叫法、规则一条一事**：这既是评审读得懂的前提，也是后续能被系统正确解析的前提。
- **缺口不进正文**：不确定的实现与用户确认前不写入文档，也不留未决标记。
- **提问批量 ≤ 4 题、一次问完**；自检以脚本 + 定向抽查为主，不做全文复读。
- 事实、数值、状态名、提示原话**永不压缩、永不改写**。

## 目录结构

```
ovfintech-prd-creator/
├── SKILL.md
├── references/   # 标准模板 / 写作规范 / 术语与规则写法 / 旧文档处理 / 表达规范
├── assets/       # 空白模板
├── scripts/      # docx→md、md→docx、结构体检
└── evals/        # 示例材料与评测用例
```

## 安装

技能是 `SKILL.md` + 附带资源的自包含目录，放到任意 Agent Skills 兼容位置即可。

```bash
# 跨 agent 共享目录（Codex / Cursor / OpenCode 等原生读取）
git clone <本仓库地址> ~/.agents/skills/ovfintech-prd-creator

# Claude Code 只认自己的目录，软链过去即可
mkdir -p ~/.claude/skills
ln -sfn ../../.agents/skills/ovfintech-prd-creator ~/.claude/skills/ovfintech-prd-creator
```

## 依赖

| 用途 | 依赖 |
|:--|:--|
| 体检脚本 `check_prd.py`、`.docx` 转换 `docx2md.py` | python3（仅标准库） |
| 导出 Word（`md2docx.sh`） | pandoc |
| 原型图缩略图预览 | macOS 的 `sips` |

## 使用

```text
帮我把这份会议纪要整理成需求文档
按模板把这份旧 PRD 规范化一下
评审前帮我体检一下这份需求文档，先别改
```
