#!/usr/bin/env bash
# 前端静态检查 + 单元测试 + 渲染冒烟（无浏览器环境可用）
set -euo pipefail
cd "$(dirname "$0")/.."
# 自适应源码布局（qtg/web）与部署布局（web）
WEB_DIR="${QTG_WEB_DIR:-}"
if [ -z "$WEB_DIR" ]; then
  if [ -d qtg/web/assets/js ]; then WEB_DIR="$(cd qtg/web/assets/js && pwd)"; WEB_REL=qtg/web/assets/js
  elif [ -d web/assets/js ]; then WEB_DIR="$(cd web/assets/js && pwd)"; WEB_REL=web/assets/js
  else echo "找不到前端目录（试 QTG_WEB_DIR=...）" >&2; exit 1; fi
fi
export QTG_APP_DIR="$WEB_DIR"
echo "== 1/3 ESM 导入图（$WEB_DIR） =="
node scripts/check-imports.mjs "$WEB_REL"
echo "== 2/3 纯函数单测 =="
node --test tests/web/utils.test.mjs
echo "== 3/3 渲染冒烟 =="
if [ -d "${QTG_FIXTURES:-/tmp/qtg-fixtures}" ]; then
  QTG_FIXTURES="${QTG_FIXTURES:-/tmp/qtg-fixtures}" node tests/web/render.test.mjs
else
  echo "跳过（缺少 fixture，先跑 scripts/capture-fixtures.sh）"
fi
