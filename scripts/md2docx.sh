#!/bin/bash
# 将标准需求文档 .md 转为 .docx（供 Word 流程使用）。
# 用法: md2docx.sh <文件.md> [--ref <参考Word模板.docx>]
# 说明:
#   - 传入 --ref 时套用参考文档的样式（字体、标题样式）；不传则用 pandoc 默认样式。
#   - 文档内的 ![]() 图片引用按 md 文件所在目录解析并嵌入 Word；【材料名称】类占位会保留为普通文本，产出前建议替换为正式图片或删除。
set -euo pipefail

md="$1"; shift || true
ref=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --ref) ref="${2:-}"; shift 2 ;;
    *) shift ;;
  esac
done

if [[ ! -f "$md" ]]; then
  echo "文件不存在：$md" >&2
  exit 2
fi
if ! command -v pandoc >/dev/null 2>&1; then
  echo "未找到 pandoc，请先安装 pandoc（macOS: brew install pandoc；Debian/Ubuntu: apt install pandoc；或见 https://pandoc.org/installing.html）" >&2
  exit 2
fi

respath="$(cd "$(dirname "$md")" && pwd)"
out="${md%.md}.docx"
if [[ -n "$ref" && -f "$ref" ]]; then
  pandoc "$md" -f gfm -t docx --resource-path="$respath" --reference-doc="$ref" -o "$out"
  echo "已生成：${out}（套用模板：${ref}）"
else
  pandoc "$md" -f gfm -t docx --resource-path="$respath" -o "$out"
  echo "已生成：${out}（默认样式）"
fi
