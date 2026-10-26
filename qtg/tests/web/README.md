# 前端测试

三层，全部零额外依赖（Node 22 原生 test runner + 自写 DOM shim）：

1. `utils.test.mjs` — 纯函数契约（XSS 转义、数值格式化、图表路径单调性）
2. `render.test.mjs` — **真实渲染冒烟**：用 `dom-shim.mjs` 提供最小 DOM，
   执行真实前端模块（`app.js` → 路由 → 10 个页面），断言不白屏、关键文本与真实数值渲染出来
3. `node /tmp/import-check.mjs <dir>` — ESM 导入图与导出符号一致性（CI 用 `scripts/check-web.sh`）

## 跑法

```bash
# 1) 纯函数
node --test tests/web/utils.test.mjs

# 2) 渲染冒烟（需要先采集 fixture，见 scripts/capture-fixtures.sh）
QTG_FIXTURES=/tmp/qtg-fixtures node tests/web/render.test.mjs

# 3) 导入图
node scripts/check-imports.mjs qtg/web/assets/js
```

渲染冒烟的价值：在没有浏览器的 CI/服务器上也能证明「页面能渲染」，而不是只做语法检查。
