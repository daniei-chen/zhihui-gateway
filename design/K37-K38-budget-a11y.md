# K37 / K38 · 首屏预算与移动端可访问性实测（2026-10-25，lab 真实浏览器）

> 环境：`zhihui-local-lab` fixture，真实 Chromium（Playwright），`NEXT_OUTPUT_EXPORT=1`
> 构建产物，冷缓存（新 context）。
> 证据：`evidence/browser/k37k38-*/report.json` + 三档宽度/深浅色截图。
> 判定口径遵循任务卡原文：**不把源文件总量当实际首屏量**，用 CDP
> `Network.loadingFinished.encodedDataLength` 累加真实传输字节。

## 一、K37 首屏预算（真实传输量）

| 指标 | 冷缓存 | 热缓存 |
|---|---|---|
| 总传输 | **2456 KB** | 2454 KB |
| 加载（`load` 事件） | 1030–1428 ms | — |
| 网络静默（含水合） | 3817–4632 ms | — |
| 热缓存节省 | — | **0%** |

按类型（冷）：

| 类型 | KB | 占比 |
|---|---|---|
| JS | **1376** | 56% |
| 字体 woff2 | **680**（12 个文件） | **28%** |
| CSS | 367 | 15% |
| RSC txt | 8 | — |
| 其他 | 32 | — |

### 三个具体问题

**1. 热缓存节省 0% —— 静态资源没有强缓存**

```
warm_total_kb: 2454 / cold_total_kb: 2456 → 节省 0%
```

归档时发现 `release/release-web-v2.sh` 的 `_next/static/*` 是 `immutable`，
但**当前 fixture 的 nginx 对 `/_next/` 没有加 `immutable`**（`location /_next/`
只设了 `root`，缓存头靠 Manager 的中间件；而页面由 nginx 直接返回，不经过
Manager）。生产是否一致需在 `nginx -T` 上确认 —— 这一条与 K33 的
"旧 chunk 保留" 是同一个位置的两个不同症状。

**2. 字体 680 KB / 12 个 woff2 = 28% 首屏**

Next.js 的 `next/font` 会为每种字重+子集生成独立文件。当前 12 个文件说明
加载了多个字重或多个 unicode-range 子集。任务卡点名要看"字体 unicode-range"：
**实际传输量说明子集切分没有起到省钱作用** —— 如果只用到 latin + 少量中文，
就应该只有 2–3 个文件被请求。

**3. 图表 chunk 在首屏就被拉取**

```
chart_chunks_on_first_load: ['9d3e55ffd76089df.css', 'layout-*.js', '*.woff2']
```

`layout-*.js` 在首屏加载，其中包含图表相关依赖。任务卡要求"图表懒加载"——
当前**没有做到**：即使首屏不显示图表，图表代码也跟着 layout 一起进了首屏包。

## 二、K37 结论

| 项 | 结论 |
|---|---|
| 真实首屏传输 | **2456 KB**（非源文件总量） |
| 能否接受 | 需要产品定预算线；作为管理面板 2.4 MB 偏重，主要来自 JS 56% + 字体 28% |
| 热缓存 | **无效**（0% 节省），静态资源缺强缓存头 |
| 字体子集 | 12 个文件 / 680 KB，unicode-range 切分未生效到"按需" |
| 图表懒加载 | **未实现**（chart 相关随 layout 首屏加载） |
| 语言切换控件 | 首页未找到（可能仅在设置页，需产品确认位置） |

## 三、K38 移动端与可访问性

### 三档宽度布局（无横向溢出）

| 宽度 | 横向溢出 | 撑宽元素 | 过小可点目标 |
|---|---|---|---|
| 360 | **无** | — | 0（修复后） |
| 768 | **无** | — | 0 个控件（2 个装饰圆点） |
| 1440 | **无** | — | 0 个控件 |

截图：`evidence/browser/k37k38-*/w360.png` / `w768.png` / `w1440.png`

### 键盘可达性

```
键盘登录：用户名 → Tab → 密码 → Enter  ✅ 成功
Tab 顺序（前 6 个）：
  1. A   "跳转到主要内容"   （skip link，正确置于首位）
  2. BUTTON "知道了"
  3. BUTTON (icon)
  4. A (nav)
  5. A (nav)
  6. A (nav)
焦点环：全部 solid 3px，可见  ✅
```

skip link 在第一位是正确实现 —— 键盘用户可以直接跳过导航。

### 深浅色

| 主题 | body 背景 | body 前景 | `html.dark` |
|---|---|---|---|
| light | `oklch(1 0 0)` | `oklch(0.141 0.005 285.823)` | false |
| dark | `oklch(0.141 0.005 285.823)` | `oklch(0.985 0 0)` | true |

两套完全互为反色，切换真实生效（`themes_differ: true`）。

### 本轮修掉的两个缺陷

**N45a · reduced-motion 形同虚设**

修复前：`prefers-reduced-motion: reduce` 下页面仍有 **67 个 running 动画**。
`globals.css` 里这个媒体查询只覆盖了 toast 的一条进度条动画。

修复：加全局降级规则（时长压到 0.01ms，保留事件触发；`scroll-behavior: auto`）。

**踩到的坑（值得记）**：第一版用裸 `*` 选择器，`next build` 的 cssnano
把整块规则**优化掉了** —— 构建产物里根本不存在。改用 `html *` 前缀后正常。

修复后实测：**67 → 0**。

**N45b · 引导气泡按钮热区 38×20（低于 WCAG 2.2 AA 的 24×24）**

`ManagementBar.tsx` 的两个 tip 按钮是 `h-5`（20px）+ `px-1`。
修复：改为 `min-h-6 min-w-6`（24px 热区），字号不变（仍是 10/11px），
视觉密度不受影响。修复后 360px 宽度下过小目标 **2 → 0**。

### 未覆盖

1. **对话框焦点陷阱/ESC**：本轮没有触发到带 `role=dialog` 的场景
   （`dialog_present_initially: false`）。面板的确认对话框是 Radix Dialog，
   理论上自带焦点陷阱，但**未实测**。要么补一次真实触发，要么标注为未覆盖。
2. **表格读序（`<table>` 语义 / `scope`）**：本轮只看了 Tab 顺序，没验证
   屏幕阅读器的行列播报。
3. **对比度数值**：只确认了深浅色不同，没有算 oklch 到 WCAG 对比度的具体比值。
4. **低配移动端 CPU 节流**：没有用 CDP `Emulation.setCPUThrottlingRate` 跑
   4x/6x 节流的加载曲线。
5. **字体 unicode-range 逐个核对**：只统计了文件数与总字节，没确认每个
   `@font-face` 的 `unicode-range` 是否真的按语言切分。
