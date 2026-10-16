#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""需求文档体检脚本：对一份 Markdown 需求文档做确定性结构检查。

用法:
    python3 check_prd.py <文档.md>              # 打印体检报告（Markdown）
    python3 check_prd.py <文档.md> -o 报告.md   # 报告写入文件
    python3 check_prd.py <文档.md> --json r.json

输出: Markdown 体检报告；--json 附带结构化结果。
说明: 本脚本只做确定性检查（结构、占位、空转、可判定性启发式），
      语义问题（口径矛盾、业务事实）由人工/模型评审补充。
"""
import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

VAGUE_WORDS = ["快速", "及时", "合理", "尽量", "尽快", "友好", "灵活", "必要时",
               "视情况", "适当", "较大", "较多", "较少", "高效", "便捷", "顺畅"]

RE_MD_HEAD = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
# 支持：1.标题、1.1 标题、2.1.1. 标题；要求数字后有分隔符（.、．）或空格
RE_NUM_HEAD = re.compile(r"^\s*(\d+(?:[.．]\d+)*)([.、．]|\s+)\s*(.+?)\s*$")
RE_XXX = re.compile(r"[X×ｘ]{3,}")
RE_FILL = re.compile(r"填写提示|填写说明")
RE_TODO = re.compile(r"【待确认[^】]*】|待确认|待补充|待完善|TBD|TODO")
# 不用 \b：中文在 Unicode 下也算 \w，「功能F01」这类紧贴中文处 \b 会失效
RE_FEATURE_ID = re.compile(r"(?<![A-Za-z0-9])F\d{2,3}(?![A-Za-z0-9])")
RE_RULE_ID = re.compile(r"(?<![A-Za-z0-9])R\d{2,3}(?![A-Za-z0-9])")
RE_SEE_PROTO = re.compile(r"见原型|以原型为准")
RE_MATERIAL_SLOT = re.compile(r"【[^】]{1,12}(?:图|原型)】")
RE_IMAGE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
RE_MERMAID = re.compile(r"```\s*mermaid")
RE_MERMAID_BLOCK = re.compile(r"```\s*mermaid[^\n]*\n(.*?)```", re.S)
# 加粗字段标签行，如 `**处理逻辑**：……`
RE_BOLD_LABEL = re.compile(r"^\s*\*\*(.+?)\*\*\s*[：:]\s*(.*)$")
# mermaid 支持的图类型（小写）；用于基本合法性检查
MERMAID_TYPES = {
    "flowchart", "graph", "statediagram", "statediagram-v2", "erdiagram",
    "sequencediagram", "classdiagram", "journey", "gantt", "pie", "mindmap",
    "timeline", "gitgraph", "quadrantchart", "requirementdiagram", "c4context",
    "block-beta", "packet-beta",
}
# 模板里固定字段名（S2/S3 骨架）；用于检空壳字段
FIELD_LABELS = ["现状", "业务痛点", "本期目标", "方案取舍", "流程描述", "概念模型",
                "数据流图", "触发入口", "前置条件", "操作步骤", "处理逻辑",
                "后置结果", "异常与提示", "交互细节"]

SEV_ORDER = {"高": 0, "中": 1, "低": 2}


class Doc:
    def __init__(self, path: Path):
        self.path = path
        text = path.read_text(encoding="utf-8", errors="ignore")
        # 清除 Word 转换残留的控制符（如 \x15），保留制表符 \t
        text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
        # 剥离 HTML 注释：模板注释是填写提示/示例，不属于正文，不应参与体检。
        # 用等长空白替换并保留换行，既消除误报又不打乱行号与字数统计。
        self.text = re.sub(r"<!--.*?-->",
                           lambda m: re.sub(r"[^\n]", " ", m.group(0)),
                           text, flags=re.S)
        self.lines = self.text.splitlines()
        self.headings = []  # (idx, level, title)
        for i, line in enumerate(self.lines):
            m = RE_MD_HEAD.match(line)
            if m:
                self.headings.append((i, len(m.group(1)), m.group(2).strip()))
                continue
            if "\t" in line and re.search(r"\t\s*\d+\s*$", line):
                continue  # 目录行（标题 + 页码）
            m = RE_NUM_HEAD.match(line)
            if not m:
                continue
            rest = line.strip()
            title = m.group(3).strip()
            if len(rest) > 60 or len(title) < 2:
                continue
            if re.search(r"[，；。！？]", title):
                continue
            if rest.endswith("：") or rest.endswith(":"):
                continue
            # 目录行：标题以“空格+页码”结尾（如「1.2 术语定义及参考 3」）
            if re.search(r"\s\d{1,3}$", title):
                continue
            depth = len(re.split(r"[.．]", m.group(1)))
            self.headings.append((i, depth, title))

    # ---------- 基础工具 ----------
    def next_heading_idx(self, idx, level=None):
        best = len(self.lines)
        for (i, lvl, _t) in self.headings:
            if i > idx and (level is None or lvl <= level):
                return i
        return best

    def body_lines(self, idx, level=None):
        end = self.next_heading_idx(idx, level)
        return self.lines[idx + 1:end]

    def body_text(self, idx, level=None):
        return "\n".join(l for l in self.body_lines(idx, level) if l.strip())

    def find_heading(self, keyword):
        """返回含关键词的标题中层级最小的最后一个 (idx, level, title)。"""
        hits = [h for h in self.headings if keyword in h[2]]
        if not hits:
            return None
        min_level = min(h[1] for h in hits)
        same = [h for h in hits if h[1] == min_level]
        return same[-1]

    def all_headings_with(self, keyword):
        return [h for h in self.headings if keyword in h[2]]


def table_data_rows(lines):
    """从行列表里取第一个表格的数据行（非空）。"""
    rows = [l.strip() for l in lines if l.strip().startswith("|")]
    if not rows:
        return []
    if len(rows) >= 2 and re.search(r"^\|[\s:\-|]+\|?$", rows[1]):
        data = rows[2:]
    else:
        data = rows[1:]
    out = []
    for r in data:
        cells = [c.strip() for c in r.strip("|").split("|")]
        if any(cells):
            out.append(cells)
    return out


class Report:
    def __init__(self, path):
        self.path = path
        self.issues = []
        self.passes = []
        self.skips = []   # 因章节缺失而未能执行的检查

    def add(self, level, cid, title, location, message):
        self.issues.append({"level": level, "id": cid, "title": title,
                            "location": location, "message": message})

    def ok(self, text):
        self.passes.append(text)

    def skip(self, text):
        self.skips.append(text)


# ---------------- 检查项 ----------------

def check_header(doc, rep):
    head = "\n".join(doc.lines[:60])
    fields = ["需求编号", "需求名称", "产品线", "作者"]
    missing = [f for f in fields if f not in head]
    if "版本" not in head:
        missing.append("版本")
    if missing:
        rep.add("低", "HDR-01", "文档头字段不全", "文档头",
                f"缺少：{'、'.join(missing)}")
    else:
        rep.ok("文档头字段完整")


def check_sections(doc, rep):
    spec = [
        ("背景与目标", ["背景"], "中", "建议补「背景与目标」（现状/痛点/目标/本期做与不做）"),
        ("术语定义", ["术语"], "中", "建议补「术语定义」（无新术语时写「本需求不涉及新术语」）"),
        ("业务流程", ["业务流程", "整体流程"], "低",
         "建议补「业务流程」主干描述与业务流程图"),
        ("数据视图", ["数据视图", "概念模型", "数据流图"], "低",
         "建议补「数据视图」（概念模型/数据流图；不涉及可声明不涉及）"),
        ("功能清单", ["功能清单"], "中", "建议补「功能清单」并与第 2 章功能一一对应"),
        ("功能说明", ["功能说明"], "中", "建议补「功能说明」章节"),
        ("业务规则", ["业务规则"], "中", "建议设「业务规则」章节，集中可判定的校验/枚举/计算口径"),
        ("非功能需求", ["非功能需求", "非功能性"], "低", "如涉及性能/安全/权限等，建议补「非功能需求」"),
        ("评审记录", ["评审"], "低", "建议补评审记录"),
        ("变更记录", ["变更"], "低", "建议补变更记录"),
    ]
    for name, kws, sev, msg in spec:
        if any(any(k in t for k in kws) for (_i, _l, t) in doc.headings):
            rep.ok(f"章节存在：{name}")
        else:
            rep.add(sev, "SEC-01", f"缺少章节：{name}", "-", msg)


def _looks_like_format_mask(line):
    """判断 XXX 是否为合法格式掩码/占位模板（应保留），而不是待清理的残留占位。"""
    if re.search(r"[X×ｘ]{2,}\s*[-—－]\s*[X×ｘ]{2,}", line):      # 如 XXXXXXXX-XXXXXXXX
        return True
    if re.search(r"格式|掩码|示例|模板|占位|代入|占位符", line):
        return True
    return False


# 未决标记的「状态名语境」豁免：含状态/枚举类词汇的行通常是在描述状态取值，不是未决残留
TODO_STATUS_CTX = re.compile(r"状态|枚举|取值|∈")


def _find_todo_markers(doc):
    """返回 [(行号, 匹配词)]；跳过状态/枚举语境行，降低业务状态名（如「订单状态：待确认」）的误报。"""
    hits = []
    for i, line in enumerate(doc.lines):
        if TODO_STATUS_CTX.search(line):
            continue
        for m in re.finditer(RE_TODO, line):
            hits.append((i + 1, m.group(0)))
    return hits


def check_placeholders(doc, rep):
    xxx = [(i + 1, l.strip()) for i, l in enumerate(doc.lines) if RE_XXX.search(l)]
    if not xxx:
        rep.ok("无 XXX 占位")
    else:
        masks = [(n, l) for n, l in xxx if _looks_like_format_mask(l)]
        leftovers = [(n, l) for n, l in xxx if not _looks_like_format_mask(l)]
        if leftovers:
            samples = "；".join(f"L{n}" for n, _ in leftovers[:8])
            extra = f"；另有 {len(masks)} 处疑似格式掩码（保留需登记）" if masks else ""
            rep.add("高", "PLACE-01", "存在 XXX 占位未清理", samples,
                    f"共 {len(leftovers)} 处需处理{extra}。例：{leftovers[0][1][:50]}")
        else:
            rep.add("低", "PLACE-01", "XXX 疑似格式掩码/占位模板（非残留）",
                    "；".join(f"L{n}" for n, _ in masks[:8]),
                    f"共 {len(masks)} 处，如批次号掩码「XXXXXXXX-XXXXXXXX-XXXXXXXX」；"
                    f"确认保留后在交付说明中登记即可，不必改写")

    fill = [(i + 1, l.strip()) for i, l in enumerate(doc.lines) if RE_FILL.search(l)]
    if fill:
        rep.add("高", "PLACE-02", "存在「填写提示」残留", "；".join(f"L{n}" for n, _ in fill[:8]),
                f"共 {len(fill)} 处。模板提示应在定稿时删除")
    else:
        rep.ok("无「填写提示」残留")

    todo = _find_todo_markers(doc)
    if todo:
        samples = "；".join(f"L{n}" for n, _ in todo[:8])
        rep.add("中", "PLACE-03", "存在未决标记（待确认/待补充类）", samples,
                f"共 {len(todo)} 处；未决事项应与相关方确认后落实，正式文档不留未决标记；"
                f"业务状态名（如「订单状态：待确认」）属正常用法，不计入")
    else:
        rep.ok("无未决标记")


def check_terms(doc, rep):
    h = doc.find_heading("术语")
    if h is None:
        rep.skip("术语定义章节缺失（见 SEC-01），未执行术语检查")
        return
    idx, level, title = h
    body = [l.strip() for l in doc.body_lines(idx) if l.strip()]
    btxt = "\n".join(body)
    if not btxt:
        rep.add("中", "TERM-01", "术语章节为空", title,
                "无新术语请写「本需求不涉及新术语」，不要留空")
        return
    if re.sub(r"[\s。.、，]", "", btxt) in ("无", "暂无", "没有", "不涉及"):
        if "不涉及" in btxt:
            rep.ok("术语章节已声明不涉及")
        else:
            rep.add("中", "TERM-01", "术语章节写「无」/「暂无」", title,
                    "建议改为「本需求不涉及新术语」或补充术语定义")
        return
    if RE_FILL.search(btxt) and len(btxt) < 40:
        rep.add("中", "TERM-01", "术语章节仅剩填写提示", title,
                "补术语定义或声明不涉及")
        return
    rows = table_data_rows(body)
    if rows:
        rep.ok(f"术语表有 {len(rows)} 条")
        header_cells = [c.strip() for c in body[0].strip("|").split("|")] if body else []
        if header_cells and ("术语" not in "".join(header_cells) or "定义" not in "".join(header_cells)):
            rep.add("低", "TERM-02", "术语表缺列", title,
                    "建议列表头为：术语｜定义｜别称/简称")
        empty_def = [r for r in rows if len(r) >= 2 and not r[1].strip()]
        if empty_def:
            rep.add("中", "TERM-03", "术语定义为空", title,
                    f"{len(empty_def)} 条术语没有定义")
    else:
        rep.ok("术语章节有内容")


def check_rules(doc, rep):
    h = doc.find_heading("业务规则")
    if h is None:
        rep.add("中", "RULE-01", "未设业务规则章节", "-",
                "建议集中登记：非空与存在性、取值范围、条件约束、枚举取值、计算口径、唯一性")
        return
    idx, level, title = h
    body = doc.body_lines(idx)
    rows = table_data_rows(body)
    if not rows:
        numbered = [l for l in body if re.match(r"^\s*(\d+[.、]|[①-⑳]|[Rr]\d+)", l.strip())]
        if not numbered:
            bodytxt = "\n".join(body)
            if "不涉及" in bodytxt or "无需登记" in bodytxt:
                rep.ok("业务规则章节声明不涉及（宁缺毋滥，不收交互层规则）")
                return
            rep.add("低", "RULE-01", "业务规则章节没有条目", title,
                    "章节存在但未登记规则；按宁缺毋滥原则，确无数据层规则时写「本次需求不涉及业务规则」")
            return
        rep.ok(f"业务规则非表格式，约 {len(numbered)} 条（建议用表）")
        return

    rep.ok(f"业务规则 {len(rows)} 条")
    vague_hits, compound_hits, missing_action = [], [], []
    for r in rows:
        rowtxt = "｜".join(r)
        vw = [w for w in VAGUE_WORDS if w in rowtxt]
        if vw:
            vague_hits.append((rowtxt[:40], vw))
        if re.search(r"且|并且|以及", rowtxt) and "∈" not in rowtxt:
            compound_hits.append(rowtxt[:40])
        if len(r) >= 4 and not r[-1].strip():
            missing_action.append(rowtxt[:40])
    if vague_hits:
        rep.add("中", "RULE-02", "规则含模糊表述（不可判定）", "业务规则表",
                f"{len(vague_hits)} 条，例：{vague_hits[0][0]}（{','.join(vague_hits[0][1])}）")
    if compound_hits:
        rep.add("低", "RULE-03", "规则疑似复合（含且/并且/以及）", "业务规则表",
                f"{len(compound_hits)} 条，例：{compound_hits[0]}；建议一条一事拆开")
    if missing_action:
        rep.add("低", "RULE-04", "规则缺触发动作/提示", "业务规则表",
                f"{len(missing_action)} 条，例：{missing_action[0]}")
    if not (vague_hits or compound_hits or missing_action):
        rep.ok("业务规则表述检查通过")


def check_functions(doc, rep):
    ch = doc.find_heading("功能说明")
    if ch is None:
        rep.skip("功能说明章节缺失（见 SEC-01），未执行功能说明相关检查")
        return
    ci, clvl, ctitle = ch
    end = doc.next_heading_idx(ci, level=clvl)
    feats = [(i, l, t) for (i, l, t) in doc.headings if ci < i < end and l == clvl + 1]

    # 功能清单条目数：优先按 F 编号计数，无编号时按表格数据行计数
    cl = doc.find_heading("功能清单")
    fcount, list_count = 0, 0
    if cl is None:
        rep.skip("功能清单章节缺失（见 SEC-01），清单相关检查不完整")
    if cl:
        cl_body = doc.body_lines(cl[0])
        fids = set()
        for line in cl_body:
            fids.update(RE_FEATURE_ID.findall(line))
        fcount = len(fids)
        if not fids:
            list_count = len(table_data_rows(cl_body))

    if feats:
        rep.ok(f"功能说明章节含 {len(feats)} 个功能小节")
        # 六要素检查
        labels = ["触发入口", "前置条件", "操作步骤", "处理逻辑", "后置结果", "异常与提示"]
        none_cnt, partial_cnt, complete_cnt = 0, 0, 0
        examples = []
        for (i, lvl, title) in feats:
            fend = doc.next_heading_idx(i, level=lvl)
            region = "\n".join(doc.lines[i:fend])
            present = [lab for lab in labels if lab in region]
            if len(present) == len(labels):
                complete_cnt += 1
            elif present:
                partial_cnt += 1
                if len(examples) < 4:
                    missing = [lab for lab in labels if lab not in present]
                    examples.append(f"{title}（缺 {'、'.join(missing[:3])}）")
            else:
                none_cnt += 1
        if none_cnt or partial_cnt:
            rep.add("中", "FUNC-02", "功能说明缺「六要素」骨架", ctitle,
                    f"完全缺失 {none_cnt} 个、部分缺失 {partial_cnt} 个、完整 {complete_cnt} 个。"
                    + ("例：" + "；".join(examples) if examples else ""))
        else:
            rep.ok("功能说明六要素齐全")

        # 清单与说明对应
        expected = fcount or list_count
        if expected and expected != len(feats):
            rep.add("中", "FUNC-01", "功能清单与功能说明数量不一致", ctitle,
                    f"功能清单 {expected} 项 vs 功能说明 {len(feats)} 节，请核对对应关系")
        elif expected:
            rep.ok("功能清单与功能说明对应一致")

    # 功能清单存在但没有任何 F 编号
    if cl and not fcount:
        rep.add("低", "FUNC-03", "功能清单未使用 F 编号", "功能清单",
                "建议给功能点编号（F01、F02…），便于规则与评审引用")

    # 清单编号与第 2 章功能小节一一对应（比只比数量更严）
    list_ids = set()
    if cl:
        for line in doc.body_lines(cl[0]):
            list_ids.update(RE_FEATURE_ID.findall(line))
    if feats or list_ids:
        detail_ids = set()
        for (i, lvl, _t) in feats:
            fend = doc.next_heading_idx(i, level=lvl)
            detail_ids.update(RE_FEATURE_ID.findall("\n".join(doc.lines[i:fend])))
        only_list = sorted(list_ids - detail_ids)
        only_detail = sorted(detail_ids - list_ids)
        if only_list or only_detail:
            parts = []
            if only_list:
                parts.append("清单有、第 2 章无：" + "、".join(only_list))
            if only_detail:
                parts.append("第 2 章有、清单无：" + "、".join(only_detail))
            rep.add("中", "FUNC-04", "功能编号未一一对应", ctitle,
                    "；".join(parts) + "；请在 2.x 小节首行注明「对应功能清单：Fxx」并核对")
        elif list_ids:
            rep.ok(f"功能编号一一对应（{len(list_ids)} 个）")


def _is_table_row(s):
    return s.startswith("|") and s.count("|") >= 2


def _is_table_sep(s):
    # GFM 分隔行：只由 | - : 与空白组成，且至少含一个 -
    return "-" in s and re.fullmatch(r"\|[\s:\-|]*\|?", s) is not None


def check_tables(doc, rep):
    """检出疑似 GFM 表格缺分隔行（导出 Word 时会塌成普通文本）。"""
    groups, cur = [], []
    in_fence = False
    for i, raw in enumerate(doc.lines):
        s = raw.strip()
        if s.startswith("```"):
            in_fence = not in_fence
            if cur:
                groups.append(cur)
                cur = []
            continue
        if not in_fence and _is_table_row(s):
            cur.append((i + 1, s))
        elif cur:
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)

    bad = [g for g in groups if len(g) >= 2 and not any(_is_table_sep(l) for _n, l in g)]
    valid = [g for g in groups if len(g) >= 2 and any(_is_table_sep(l) for _n, l in g)]
    if bad:
        locs = "；".join(f"L{g[0][0]}" for g in bad[:8])
        rep.add("中", "TBL-01", "疑似表格缺分隔行（导出 Word 会塌成一行）", locs,
                f"共 {len(bad)} 个表格块连续多行以 | 开头却没有分隔行"
                f"（首行后应有由 - 和 : 组成的行）；例：{bad[0][0][1][:50]}")
    else:
        rep.ok(f"表格格式正常（{len(valid)} 个表格均含分隔行）" if valid
               else "未发现缺分隔行的表格")


def check_vague(doc, rep):
    hits = {}
    for i, line in enumerate(doc.lines):
        for w in VAGUE_WORDS:
            if w in line:
                hits.setdefault(w, []).append(i + 1)
    total = sum(len(v) for v in hits.values())
    if not hits:
        rep.ok("未发现模糊表述")
        return
    top = sorted(hits.items(), key=lambda x: -len(x[1]))[:8]
    words = "、".join(f"{w}×{len(v)}（L{v[0]}）" for w, v in top)
    rep.add("中" if total > 5 else "低", "VAGUE-01", "模糊表述（形容词/副词）", "全文",
            f"共 {total} 处：{words}；建议替换为具体数值或条件")


def check_message_text(doc, rep):
    bad = []
    for i, line in enumerate(doc.lines):
        s = line.strip()
        if re.search(r"给出(?:相应(?:的)?)?提示|提示用户|进行提示|弹出提示", s) and not re.search(r"[「『“”]", s):
            bad.append((i + 1, s[:50]))
        elif re.search(r"提示[：:]\s*$", s):
            bad.append((i + 1, s[:50]))
    if bad:
        rep.add("低", "MSG-01", "提示未给原话", "；".join(f"L{n}" for n, _ in bad[:8]),
                f"共 {len(bad)} 处。例：{bad[0][1]}；提示应写原话（「……」）")
    else:
        rep.ok("提示文案检查通过")


def check_slots(doc, rep):
    slots = [(i, l) for i, l in enumerate(doc.lines) if RE_MATERIAL_SLOT.search(l)]
    no_detail = []
    for i, l in slots:
        after = l.split("】", 1)[1].strip() if "】" in l else ""
        ctx = " ".join(doc.lines[i:i + 3])
        if len(after) < 2 and not re.search(r"http|\.png|\.jpg|\.rp|地址|链接|文件|assets", ctx):
            no_detail.append(i + 1)
    for i, l in enumerate(doc.lines):
        if l.strip().startswith("<!--") or "不允许" in l:
            continue
        if RE_SEE_PROTO.search(l) and not re.search(r"http|地址|链接|文件|\.png|\.jpg|\.rp", l):
            no_detail.append(i + 1)
    if no_detail:
        rep.add("低", "SLOT-01", "图与素材占位未写清位置", "；".join(f"L{n}" for n in no_detail[:8]),
                f"共 {len(no_detail)} 处；不允许只写「见原型」，需给链接或文件位置")
    elif slots:
        rep.ok(f"图与素材占位 {len(slots)} 处，均有定位信息")
    else:
        rep.ok("未使用图与素材占位")


def check_empty_sections(doc, rep):
    empties = []
    for (i, lvl, t) in doc.headings:
        nxt = doc.next_heading_idx(i, level=lvl)
        has_child = any(l > lvl for (j, l, _t) in doc.headings if i < j < nxt)
        if has_child:
            continue
        btxt = "\n".join(x for x in doc.body_lines(i) if x.strip())
        btxt = re.sub(r"<!--.*?-->", "", btxt, flags=re.S).strip()
        if btxt and not re.fullmatch(r"[\|\-\s:]+", btxt):
            continue
        empties.append((i + 1, t))
    if empties:
        rep.add("低", "EMPTY-01", "空章节", "；".join(f"L{n} {t}" for n, t in empties[:10]),
                f"共 {len(empties)} 个；留空请声明「不涉及」或补充内容")


def check_images(doc, rep):
    refs = []
    for i, line in enumerate(doc.lines):
        for m in RE_IMAGE.finditer(line):
            refs.append((i + 1, m.group(1).strip()))
    if not refs:
        rep.ok("未嵌入图片")
        return
    missing = []
    for ln, ref in refs:
        if re.match(r"^https?://", ref):
            continue
        if not (doc.path.parent / ref).exists():
            missing.append((ln, ref[:50]))
    if missing:
        rep.add("低", "IMG-01", "图片引用文件不存在", "；".join(f"L{n}" for n, _ in missing[:8]),
                f"共 {len(missing)} 处；例：{missing[0][1]}；请检查相对路径或改回【材料名称】占位")
    else:
        rep.ok(f"图片嵌入 {len(refs)} 处，文件均存在")


def check_flow_diagram(doc, rep):
    h = doc.find_heading("业务流程") or doc.find_heading("整体流程")
    if h is None:
        rep.skip("业务流程章节缺失（见 SEC-01），未执行流程图检查")
        return
    idx, _lvl, _t = h
    body = "\n".join(doc.body_lines(idx))
    if RE_MERMAID.search(body) or RE_IMAGE.search(body) or RE_MATERIAL_SLOT.search(body):
        n = len(RE_MERMAID.findall(body))
        rep.ok(f"业务流程有图承载：mermaid {n} 个 / 嵌入图片 {len(RE_IMAGE.findall(body))} 处")
        return
    if re.search(r"不涉及", body):
        rep.ok("业务流程声明不涉及")
        return
    rep.add("低", "FLOW-01", "业务流程无图承载", "1.3",
            "业务流程图（原图嵌入或 mermaid）缺失；如本次确无流程变化，请写「本次需求不涉及流程变化」")


def check_data_view(doc, rep):
    h = doc.find_heading("数据视图")
    if h is None:
        rep.skip("数据视图章节缺失（见 SEC-01），未执行概念模型/数据流图检查")
        return
    idx, _lvl, _t = h
    body = "\n".join(doc.body_lines(idx))
    # 概念模型：erDiagram 或明确声明不涉及新业务实体
    if "erDiagram" in body:
        rep.ok("数据视图含概念模型（erDiagram）")
    elif re.search(r"不涉及(新)?业务(实体|对象)", body):
        rep.ok("数据视图声明不涉及新业务实体")
    else:
        rep.add("低", "DATA-01", "数据视图缺概念模型", "1.4",
                "有新增/变更业务对象时用 mermaid erDiagram 画；确无则写「本次需求不涉及新业务实体」")
    # 数据流图：不涉及声明，或含 mermaid flowchart 数据流图
    if re.search(r"不涉及数据流图", body):
        rep.ok("数据视图声明不涉及数据流图")
    elif re.search(r"```\s*mermaid[^\n]*\n\s*(?:flowchart|graph)\b", body):
        rep.ok("数据视图含数据流图（flowchart）")
    else:
        rep.add("低", "DATA-02", "数据视图缺数据流图", "1.4",
                "涉及数据在角色/系统/存储间流转时用 mermaid flowchart 画数据流图"
                "（外部实体方框 []、加工圆角 ()、数据存储圆柱 [()]、数据流箭头连线）；"
                "完全不涉及数据流转时才写「本次需求不涉及数据流图」并给理由")


def _is_placeholder_value(s: str) -> bool:
    """判断一行是不是模板提示/占位（括号提示、纯省略号），而非实质内容。"""
    t = s.strip()
    if not t:
        return True
    if re.fullmatch(r"[（(].*[）)]", t):          # 整行被括号包裹，通常是模板提示
        return True
    if re.fullmatch(r"[…\.．·\s、，,]+", t):       # 纯省略号/顿号占位
        return True
    return False


def check_hollow_fields(doc, rep):
    """检出「字段标签在、值空」的空壳——比缺章节更隐蔽的未填写。"""
    hollows = []
    lines = doc.lines
    for i, raw in enumerate(lines):
        m = RE_BOLD_LABEL.match(raw)
        if not m:
            continue
        label, value = m.group(1).strip(), m.group(2)
        if not any(k in label for k in FIELD_LABELS):
            continue
        if not _is_placeholder_value(value):
            continue
        # 标签行本身无值：看紧随其后的第一个非空行是否有实质内容
        j, nxt = i + 1, None
        while j < len(lines):
            s = lines[j].strip()
            if s:
                nxt = s
                break
            j += 1
        if (nxt is not None and not RE_MD_HEAD.match(nxt)
                and not RE_BOLD_LABEL.match(nxt) and not _is_placeholder_value(nxt)):
            continue
        hollows.append((i + 1, label))
    if hollows:
        rep.add("中", "HOLLOW-01", "字段标签无有效内容（空壳）",
                "；".join(f"L{n} {lb}" for n, lb in hollows[:10]),
                f"共 {len(hollows)} 处：字段标签下无实质内容，疑似未填写或仅剩模板提示；"
                f"请填写内容，或写「不适用」+理由")
    else:
        rep.ok("未发现空壳字段")


def check_empty_table_rows(doc, rep):
    """检出所有单元格皆空的表格数据行（模板未填写的典型症状）。"""
    groups, cur, in_fence = [], [], False
    for i, raw in enumerate(doc.lines):
        s = raw.strip()
        if s.startswith("```"):
            in_fence = not in_fence
            if cur:
                groups.append(cur)
                cur = []
            continue
        if not in_fence and s.startswith("|"):
            cur.append((i + 1, s))
        elif cur:
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)
    empties = []
    for g in groups:
        sep = next((k for k, (_n, s) in enumerate(g) if _is_table_sep(s)), None)
        rows = g[sep + 1:] if sep is not None else g[1:]
        for n, s in rows:
            cells = [c.strip() for c in s.strip("|").split("|")]
            if cells and not any(cells):
                empties.append(n)
    if empties:
        rep.add("低", "TBL-02", "表格存在空数据行（未填写）",
                "；".join(f"L{n}" for n in empties[:10]),
                f"共 {len(empties)} 行所有单元格为空，疑似模板未填写")
    else:
        rep.ok("无空表格行")


def check_mermaid(doc, rep):
    """mermaid 基本合法性：非空、类型受支持、主干有连接符。"""
    blocks = RE_MERMAID_BLOCK.findall(doc.text)
    if not blocks:
        return
    bad = []
    for k, body in enumerate(blocks, 1):
        stripped = body.strip()
        if not stripped:
            bad.append(f"#{k} 内容为空")
            continue
        first = stripped.splitlines()[0].strip()
        first = re.sub(r"^%%\{.*?\}%%\s*", "", first)     # 去掉 init 指令
        word = re.split(r"[\s{(\[]", first.strip())[0].lower()
        if word and word not in MERMAID_TYPES:
            bad.append(f"#{k} 未知图类型「{word}」")
        elif word in ("flowchart", "graph", "statediagram", "statediagram-v2") and "-->" not in body:
            bad.append(f"#{k} 缺连接符（-->）")
    if bad:
        rep.add("低", "MERMAID-01", "mermaid 图疑似有问题", "；".join(bad[:8]),
                "检查图类型是否受支持、主干是否有连接符；渲染失败会让评审看不到流程图")
    else:
        rep.ok(f"mermaid 图基本结构正常（{len(blocks)} 个）")


def check_rule_refs(doc, rep):
    """业务规则引用完整性与功能编号存在性。"""
    rh = doc.find_heading("业务规则")
    if rh is None:
        return
    ruledef = set(RE_RULE_ID.findall("\n".join(doc.body_lines(rh[0]))))
    cl = doc.find_heading("功能清单")
    list_ids = set()
    if cl:
        for line in doc.body_lines(cl[0]):
            list_ids.update(RE_FEATURE_ID.findall(line))

    # RULE-05：规则表引用了清单中不存在的功能编号
    ref_f = set(RE_FEATURE_ID.findall("\n".join(doc.body_lines(rh[0]))))
    dangling_f = sorted(ref_f - list_ids) if list_ids else []
    if dangling_f:
        rep.add("低", "RULE-05", "业务规则引用了功能清单中不存在的编号", "业务规则表",
                "编号：" + "、".join(dangling_f) + "；请与功能清单核对")

    # RULE-06：正文引用的规则编号未在业务规则表定义
    cited = set()
    for line in doc.lines:
        cited.update(re.findall(r"(?:见|校验见|规则)\s*(R\d{2,3})", line))
    dangling_r = sorted(cited - ruledef) if ruledef else []
    if dangling_r:
        rep.add("低", "RULE-06", "正文引用了未定义的规则编号", "全文",
                "编号：" + "、".join(dangling_r) + "；请在业务规则表补定义或改为正确编号")
    elif cited and ruledef:
        rep.ok("正文引用的规则编号均有定义")


def build_stats(doc):
    return {
        "chars": len(doc.text),
        "sections": len(doc.headings),
        "features": len(set(RE_FEATURE_ID.findall(doc.text))),
        "rules": len(set(RE_RULE_ID.findall(doc.text))),
        "todo": len(_find_todo_markers(doc)),
        "slots": len(RE_MATERIAL_SLOT.findall(doc.text)),
        "images": len(RE_IMAGE.findall(doc.text)),
        "mermaid": len(RE_MERMAID.findall(doc.text)),
    }


def render_report(doc, rep, stats, baseline=None):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    issues = sorted(rep.issues, key=lambda x: (SEV_ORDER.get(x["level"], 9), x["id"]))
    hi = sum(1 for x in issues if x["level"] == "高")
    mid = sum(1 for x in issues if x["level"] == "中")
    low = sum(1 for x in issues if x["level"] == "低")

    if hi:
        concl = f"存在 {hi} 个高优先级问题（占位残留、空转等），建议处理后再进入评审。"
    elif mid:
        concl = f"未发现高优先级问题；{mid} 个中级问题建议逐项处理。"
    else:
        concl = "结构检查通过。注意：本脚本不检查语义正确性，业务口径请人工复核。"

    out = []
    out.append(f"# 需求文档体检报告 · {doc.path.name}")
    out.append("")
    out.append(f"> 体检时间：{now} ｜ 工具：check_prd.py（确定性结构检查）")
    out.append(f"> 结论：**{concl}**")
    out.append("")
    out.append("## 总览")
    out.append("")
    out.append("| 级别 | 数量 |")
    out.append("|:--|--:|")
    out.append(f"| 高 | {hi} |")
    out.append(f"| 中 | {mid} |")
    out.append(f"| 低 | {low} |")
    out.append("")
    out.append(f"文档规模：约 {stats['chars']} 字，{stats['sections']} 个标题节；"
               f"功能编号 {stats['features']} 个，规则编号 {stats['rules']} 条，"
               f"未决标记 {stats['todo']} 处，图与素材占位 {stats['slots']} 处，"
               f"图片嵌入 {stats['images']} 处，mermaid 图 {stats['mermaid']} 个。")
    if baseline:
        out.append(f"篇幅比例：**{baseline['ratio']}×** 源文档（源：{baseline['name']}，预算 0.8～1.2 倍）。")
    out.append("")
    if issues:
        out.append("## 问题清单")
        out.append("")
        out.append("| # | 级别 | 检查项 | 位置 | 说明 |")
        out.append("|--:|:--|:--|:--|:--|")
        for n, x in enumerate(issues, 1):
            msg = x["message"].replace("|", "丨")
            out.append(f"| {n} | {x['level']} | {x['title']} | {x['location']} | {msg} |")
        out.append("")
    if rep.skips:
        out.append("## 未执行项（对应章节缺失）")
        out.append("")
        for s in rep.skips:
            out.append(f"- {s}")
        out.append("")
    out.append("## 通过项")
    out.append("")
    for p in rep.passes:
        out.append(f"- {p}")
    out.append("")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description="需求文档体检（确定性结构检查）")
    ap.add_argument("file", help="Markdown 文档路径")
    ap.add_argument("-o", "--output", help="报告输出文件（默认打印到 stdout）")
    ap.add_argument("--json", dest="json_path", help="另存 JSON 结果")
    ap.add_argument("--baseline", help="源文档路径（优化模式）：核对产出与源的篇幅比例（预算 0.8～1.2 倍）")
    args = ap.parse_args()

    path = Path(args.file)
    if not path.exists():
        print(f"文件不存在：{path}", file=sys.stderr)
        sys.exit(2)

    doc = Doc(path)
    rep = Report(path)
    check_header(doc, rep)
    check_sections(doc, rep)
    check_placeholders(doc, rep)
    check_tables(doc, rep)
    check_hollow_fields(doc, rep)
    check_empty_table_rows(doc, rep)
    check_terms(doc, rep)
    check_rules(doc, rep)
    check_functions(doc, rep)
    check_rule_refs(doc, rep)
    check_vague(doc, rep)
    check_message_text(doc, rep)
    check_slots(doc, rep)
    check_images(doc, rep)
    check_mermaid(doc, rep)
    check_flow_diagram(doc, rep)
    check_data_view(doc, rep)
    check_empty_sections(doc, rep)
    stats = build_stats(doc)

    baseline_info = None
    if args.baseline:
        bpath = Path(args.baseline)
        if not bpath.exists():
            print(f"基线文件不存在：{bpath}", file=sys.stderr)
        else:
            base_chars = len(bpath.read_text(encoding="utf-8", errors="ignore"))
            if base_chars > 0:
                ratio = stats["chars"] / base_chars
                baseline_info = {"name": bpath.name, "chars": base_chars,
                                 "ratio": round(ratio, 2)}
                if ratio > 1.2:
                    rep.add("高", "LEN-01", "篇幅超预算（>1.2 倍源文档）", f"{ratio:.2f}×",
                            f"产出 {stats['chars']} 字 / 源 {base_chars} 字；按压缩顺序减骨架文字："
                            f"删重复说明 → 合并不适用声明 → 聚合同类小节 → 精简新增描述；"
                            f"事实、数值、提示原话不动")
                elif ratio < 0.8:
                    rep.add("中", "LEN-01", "篇幅低于预算（<0.8 倍源文档）", f"{ratio:.2f}×",
                            f"产出 {stats['chars']} 字 / 源 {base_chars} 字；请核对原文信息是否有遗漏")
                else:
                    rep.ok(f"篇幅比例 {ratio:.2f}× 源文档（0.8～1.2 预算内）")

    report_md = render_report(doc, rep, stats, baseline_info)
    if args.output:
        Path(args.output).write_text(report_md, encoding="utf-8")
        print(f"报告已写入：{args.output}")
    else:
        print(report_md)

    if args.json_path:
        payload = {
            "file": str(path),
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "stats": stats,
            "baseline": baseline_info,
            "issues": rep.issues,
            "passes": rep.passes,
            "skips": rep.skips,
        }
        Path(args.json_path).write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                        encoding="utf-8")
        print(f"JSON 已写入：{args.json_path}")


if __name__ == "__main__":
    main()
