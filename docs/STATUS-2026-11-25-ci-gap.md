# 2026-11-25 CI 覆盖缺口确认（第 21 轮）

## 发现

`web/tests/` 下有 12 个 `.test.cjs`（控制台状态机、i18n、静态导出等），
**在 GitHub CI 上从未运行过**。

证据：

```
.github/workflows/quality.yml:45   - run: node --test lib/*.test.mjs
web/package.json scripts.test       node --test lib/*.test.mjs tests/*.test.cjs
```

CI 的 frontend job 只跑 29 个 `.mjs`，漏掉 12 个 `.cjs`。
而本地门禁 `tools/ci_regression.py` 用的是 `npm test`（完整），
所以**本地全绿、CI 只覆盖一半**。

这种差异最危险的地方在于：它不会报错，只会静默地少测一部分。
「本地过了 CI 挂」尚能发现，「CI 漏测」则长期不会被发现。

## 尝试修复，被权限挡住

改动本身只有一行（第 45 行补上 `tests/*.test.cjs`，与 `package.json` 一致），
且第 42 行已有 `npm ci`，不需要其他前置。

推送结果：

```
! [remote rejected] main -> main
  (refusing to allow an OAuth App to create or update workflow
   `.github/workflows/quality.yml` without `workflow` scope)
```

当前 `gh` 认证的 token 没有 `workflow` scope，**无法推送 `.github/workflows/` 下的任何改动**。

## 已回滚，避免阻塞后续推送

这个 commit 如果留在历史里，**之后每一次 push 都会被同样拒绝**。
所以立即 revert 并推送（`a62cc7e`），`quality.yml:45` 已恢复原样。

## 需要的外部条件

二选一：

1. 给 `gh` 的 token 加上 `workflow` scope（`gh auth refresh -s workflow`）
2. 由有权限的人手动改这一行

改法：

```yaml
# .github/workflows/quality.yml:45
- run: node --test lib/*.test.mjs tests/*.test.cjs
```

## 本地门禁不受影响

`tools/ci_regression.py` 已经完整跑 `npm test`，本轮实测 **245 例通过**。
所以这道缺口只影响 GitHub CI 的可见性，不影响本地发版判定。