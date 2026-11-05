# UI primitives 人工补充审计（只读）

日期：2026-11-05。按主代理分工，排除 frontend-reviewed-files.txt 已 manual-full 的文件后，全文阅读 `src/three-platform/web/components/ui/**` 与 `components/animate-ui/**` 中剩余 **31 个 TSX、5,048 行**；实际清单和 SHA-256 见 `gateway-ui-extra-reviewed.tsv`。另读 web/AGENTS.md；对 globals.css 的 reduced-motion 规则及组件引用做定点检查，未把这些定点检查伪装为额外全文覆盖。

**没有发现当前已复现的 P0/P1；没有源码修改、浏览器运行或新增测试。** 本批是本地 UI 封装与部分项目定制交互，不是完整第三方 Radix、Vaul、Motion、Floating UI 库代码审计，也不是业务账户/钱包/权限 API 审计。

## 已核实的结构

- `alert-dialog`、`drawer`、`popover`、`select`、`switch`、常规 `tooltip` 与 `tabs` 通过第三方 primitives 承担焦点、键盘、portal 与状态管理。本地文本均用 React children；审阅的 31 文件没有 `dangerouslySetInnerHTML`、HTML 字符串拼接或 eval。CountingNumber 使用 `textContent` 更新数字，不是 HTML sink。
- 本地布局对 dialog、select、dock 添加了视口高度/溢出处理，表头默认 scope=col；CopyButton 显式 type=button、aria-label，跨站 dock 链接带 noopener noreferrer。href/src 的业务来源验证不属于这些展示 primitives 自身，不能由此保证任意传入 URL 安全。
- document mousedown、MotionHighlight scroll、Tooltip 全局 keydown/scroll/resize 事件有 removeEventListener；MutationObserver/ResizeObserver 有 disconnect；CountingNumber 的 spring subscription 有 unsubscribe；持续量测 MotionHighlight 的 requestAnimationFrame 有取消。这些是看得见的 cleanup，非全应用无泄漏证明。
- `animate-ui/radix/dialog.tsx` 有受控/非受控 open 处理及将 focus/outside/Escape props 交给 Radix 的路径，不在静态审阅中否定第三方焦点实现。

## 需要后续处理的具体较低级别事项

### 当前有业务调用的组件

1. **AiChatInput 中文输入法 Enter。** `ui/ai-chat-input.tsx` 158 行附近只判断 Enter 与 shift，没有 `nativeEvent.isComposing` 或 composition 状态。输入法确认候选时可能触发 `submit()`，需要浏览器/输入法回归确认并修复；Playground 明确导入了该组件。本次仅静态发现，不声称已经运行复现或达到 P1。
2. **有界但未撤销的定时回调。** AiChatInput 轮播 effect 清 interval，却没有清内部 400 ms timeout；CopyButton 成功后 1500 ms timeout 没有卸载清理。会产生卸载后或切换状态后的旧回调；不是无限定时器泄漏。空 placeholders 数组还会导致取模零，调用方应避免或组件防御。
3. **输入和自绘下拉可访问性。** AiChatInput textarea 没有 label/aria-label（视觉 placeholder 为另一层 span），模型/effort 下拉没有 aria-expanded/aria-controls 与 Escape/方向键逻辑。现有按钮仍可 Tab 聚焦，但不能等同完整 listbox/combobox 语义。
4. **动画 CPU 与 reduced motion 边界。** FloatingDock 每次 mousemove 对分隔线和每个 icon 量测 DOMRect，再通过多个 springs 改宽高；正常导航项数有限，但事件频率/重排成本没有实测。CountingNumber 逐帧 textContent 也是 JS 动画。globals.css 240 行附近确有全局 CSS reduced-motion 规则，但注释也明确它不接管 JS rAF 动画；本批 Motion 组件没有 useReducedMotion/MotionConfig 检查，不能称整个 UI 已满足减少动态效果。应以代表设备的 frame/long-task 数据决定是否合并量测/改 transform。

### 当前未找到业务导入的保留 primitives

对 app、components 的普通静态引用检索没有发现 `animate-ui/icons`、`animate-ui/radix/tabs`、`animate-ui/buttons/input`、`animate-ui/primitives/animate/**`、SlidingNumber、MotionHighlight 在该组之外的导入。以下问题是保留组件的具体代码欠账，不把未可达的库缺陷升级为线上 P1：

- `icons/icon.tsx` getVariants 的 loop 分支直接修改来自共享 animations/staticAnimations 的 nested transition，写入 repeat=Infinity。可污染后来不要求 loop 的实例；onAnimateEnd Promise 回调也没有取消/过期代次检查。
- `motion-highlight.tsx` parent 模式将 activeValue 原样插入 querySelector；包含引号/反斜杠的 value 可使 selector 抛异常，需 CSS.escape 或不拼 selector。Item 参数 disabled 默认 false，因此 `disabled === undefined ? contextDisabled : disabled` 的继承分支默认不会到达。非 asChild 包装还手工调用 element.props.onClick，而 child 的原 onClick 继续保留，可能在冒泡时执行两次；需要组件级测试，不能推断当前重复付费操作。
- 同组件 asChild+children 模式 clone 外层 child 后，内部又放入原 children，可能形成嵌套交互元素。forceUpdateBounds 开启后每帧量测，回调依赖整个 props 对象会频繁重建。当前未证明业务启用这些分支。
- 自定义 `primitives/animate/tooltip.tsx` 只比较不含 children 的 props 来更新内容，单独变化 children 会保留旧文本；provider 卸载时没有 clearTimeout；role=tooltip、aria-describedby、默认触发元素的 tabIndex 均未建立。事件监听器本身有 cleanup。Slot merges 用覆盖而非组合 handlers，需尊重调用方是否取消默认行为；读取 children.type 在 React.isValidElement 之前，误传 null 会抛错。
- `radix/tabs.tsx` 的 ResizeObserver callback 排队 rAF 后未在 disconnect 时取消已排队帧；MutationObserver 观察所有 attributes/subtree，含动画自身 style 变化时可能产生额外扫描。当前外部页面使用另一份 ui/tabs，未发现该文件的实际业务导入。
- CountingNumber/SlidingNumber 的 decimalPlaces 没有本地范围限制；如果调用方传负数/超过 toFixed 上限会抛异常。当前 CountingNumber 调用用固定参数，未发现用户能控制该 prop 的路径。SlidingNumber 每位创建十个 DOM 数字，适合少量仪表值，不适合大表每格无差别开启。

以上后续项保持只读，不在业务冻结后追加低置信重构或扩大测试。已交主代理作为质量边界与后续优先级依据。
