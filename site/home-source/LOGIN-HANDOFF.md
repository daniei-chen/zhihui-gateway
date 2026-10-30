# 登录页重构交接

日期：2026-10-30。用户选定的“白瓷智汇之门＋右侧登录表单”本地实现，未部署。

## 查看与运行

运行 `node server.mjs`，访问 `http://127.0.0.1:4173/login` 或 `/login/`。静态文件位于 `dist/login/index.html`；其他静态服务器应将 `/login` 映射至该文件，根目录仍为 `dist/`。

## 当前实现

- 桌面左侧淡蓝展示、右侧白色表单；手机模型缩为顶部品牌展示。
- 实时 Three.js 白瓷门框、蓝色玻璃内衬、悬浮 Z、四路光线，查看器约 570 KiB，无外部模型或 CDN；本地静态海报约 32 KiB。
- 表单先就绪，再空闲加载装饰渲染，失败保留海报。输入时减弱运动；模型支持暂停、减少动态效果、后台与离屏停止。
- 用户名/密码、自动填充、显示密码、大小写提示、空值聚焦、防重复提交、错误恢复、会话过期、返回首页。页面后台或离开时隐藏密码。
- 模型提交/成功状态可联动，但认证成功立即跳转，不依赖模型加载或强制播放过渡。

## 接口与预览保护

HTML 根元素为 `data-auth-mode="preview"`，本地不会探测会话、提交密码、模拟登录成功或保存凭据。

2026-10-30 通过只读 GET 核对了公开线上登录页源码，其接口约定为：

1. `GET /api/me`，同源凭据、禁止缓存；成功则跳转 `/dashboard`。
2. `POST /api/login`，JSON `{username,password}`，同源凭据、禁止缓存。
3. HTTP 成功且 JSON `ok === true` 才跳转 `/dashboard`。
4. 分别处理 401、429、400、5xx 与网络异常。读取 DOM 实际值兼容自动填充；用户名去首尾空格，密码不修剪。

后续只有在用户明确授权部署、与网关负责人核对接口后，才将根元素改为 `data-auth-mode="same-origin"`。还需 HTTPS 与非本地来源；即便误设该值也不会从 localhost 跨域向生产提交凭据。不要改为生产域名的绝对 API 地址。

本次没有调用真实登录接口，没有使用凭据，也没有更改线上服务。模拟响应测试不能证明线上端到端认证已验收。

## 修改入口

| 文件 | 用途 |
| --- | --- |
| `public/login/index.html` | 结构、文案、预览/接口开关；不受首页 prepare 覆盖。 |
| `public/styles/login.css` | 独立浅色布局、响应式、焦点/错误/加载状态。 |
| `public/scripts/login.js` | 表单与同源认证约定、本地预览保护。 |
| `public/scripts/gateway.js` | 空闲加载、动效状态、失败海报。 |
| `src/gateway-model.js` | 门框、内衬、Z、展台、四路光线几何。 |
| `src/gateway-viewer.js` | 光照、环境、镜头与约 30fps 动效。 |
| `build-viewer.mjs` | 同时打包雕塑与登录两个查看器。 |
| `export-gateway.mjs`、`render_gateway.py` | 导出实际几何、独立 OpenGL 渲染海报；不是网页截图。 |
| `tests/login.test.mjs` | 认证边界、输入、状态、模型与动效检查。 |
| `reference-login/` | 公开登录页只读源码与来源哈希、模型中间数据。 |

修改模型后运行 `npm run build:gateway`（需 Python numpy、Pillow、moderngl），再运行 `npm run build:viewer`。修改表单和样式无需重建模型。检查/打包：

```powershell
npm ci
npm run check
npm run build
```

## 验收状态

当前 20 组检查通过，登录新增 6 组。测试使用 jsdom 和模拟响应，没有发送真实凭据。独立模型渲染已查看，白框、内衬与 Z 可见；与浏览器物理光照不同，不能替代网页验收。

浏览器运行时仍返回 `Local browser use is disabled in Work settings`。桌面/手机排版、实际 WebGL 材质、真实键盘/触摸/密码管理器尚未截图验收。接手后先检查 1440×900、1024×768、390×844：模型不遮标题、表单可见、无横向溢出、失败回退和减少动效正常；授权部署后再验证真实认证。
