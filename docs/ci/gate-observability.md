# 门禁的观测面：从 artifact 里读出「跑了多少、skip 了哪些」

这份文档回答一个问题：**我怎么知道上一次跑的门禁到底跑了什么？**
`tools/ci_regression.py` 的判据只在红的时候喊；绿的时候它必须能自证清白，
否则「绿」和「没跑」在日志里长得一模一样（见 `docs/CI-GATE-CONTRACT.md` 第一节）。

## 去哪里看

| job | artifact 名 | 文件 |
|---|---|---|
| `manager` | `backend-validation-manager-<run_id>-<run_attempt>` | `ci-validation-summary.json` |
| `gateway` | `backend-validation-gateway-<run_id>-<run_attempt>` | `ci-validation-summary.json` |

两个 job 的 upload 步骤都带 `if: always()`（`quality.yml:50`、`:152`），**红了也有 artifact**——
这正是失败诊断要用的东西。保留 14 天，不要为了省空间改短。

页面路径：Actions → 选那次 run → 选 job → 页面底部 Artifacts。

## artifact 里有什么

`ci-validation-summary.json` 的关键字段：

| 字段 | 含义 |
|---|---|
| `tests` | 真正执行过的 unittest 条数（`startTest` 累加，不是「加载出多少」） |
| `failures` / `errors` | 失败与错误条数 |
| `skips` | 被 skip 的条数 |
| `skipped_tests` | **逐条** skip 的 `test` id 与 `reason`——「哪里在悄悄不跑」看这里 |
| `empty_modules` | 一条用例都没加载出来的模块（import 失败、清单名字写错） |
| `fully_skipped_modules` | 整模块全部 skip、且不在白名单里的模块（有内容即红） |
| `historical_pytest` | pytest 赛道的 counts、`skipped_tests`、`empty_modules`、`fully_skipped_modules` |
| `modules` | 这次实际参与的 `MODULES` 清单 |
| `frontend` / `static` | 前端与静态检查的门禁结果（`required` 表示该 job 是否要求它跑） |

改了测试、加了 skip、动了白名单之后，**先读这个文件、再读日志**。

## 一份真实样本（`zhihui-gateway` run `46130893007`，gateway job）

```
tests            = 6969
failures         = 1        # 受保护哈希漂移，见下
skips            = 11
empty_modules    = []
fully_skipped_modules = []
skipped_tests    = 11 条，全部在 server.tests.test_k36_security_headers
                   例：test_qtg_csp | QTG 源文件不在服务器视图内
                       test_edge_uses_report_only_so_app_csp_is_authoritative | ops/nginx 不在服务器视图内
```

两条读法：

1. **`skips=11` 且 `fully_skipped_modules=[]`** —— 没有模块靠整块 skip 蒙过关，这 11 条是
   用例自己声明的（CSP 断言需要在含 `ops/nginx` 的部署树上跑）。
2. **`failures=1` 同时 `tests=6969`** —— 那次的失败是
   `test_reviewed_patch_files_match_current_source`：分支基线旧、`main` 改过
   `tools/ci_regression.py`，受保护哈希对不上。**看到这一条先怀疑基线旧，再怀疑改坏了**，
   处置是 `git merge origin/main` + `python tools/bump_version.py`。

## 别把它当成的两件事

- **不是「绿了就等于有覆盖」**：`tests` 只说明跑过多少条，不说明断言够不够。
- **不是「skip 数越少越好」**：一条写着 `reason` 的平台 skip 比一条假装在 Linux 上也能跑的
  用例更诚实。要降的是**没有原因**的 skip，不是有原因的。

## 什么时候这个文件要更新

- `tools/ci_regression.py` 改了 summary 的字段（加字段、改名、删字段）；
- `quality.yml` 改了 artifact 名或保留期；
- 出现了新的「看起来绿、其实是没跑」的形态。
