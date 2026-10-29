# 从源码开始

智汇的首页、登录页、控制台和 Manager 共用这个仓库。
这里区分**本地开发**与**受控生产发布**，不提供会覆盖现有运行状态的一键安装命令。

[回到产品介绍](../README.md) · [版本与升级](PRODUCT-RELEASE.md) · [审计边界](AUDIT-0.0.2.md)

## 本地开发

建议使用 Linux x86_64、Python 3.12、Node.js 22+。
前端使用 Next.js 16、React 19、Tailwind CSS 4。
后端哈希依赖锁面向上述 Linux 环境，不承诺直接覆盖 Windows / macOS。

```bash
git clone https://github.com/daniei-chen/zhihui-gateway.git
cd zhihui-gateway
```

### 1. 启动最小控制面

```bash
cd manager
python3.12 -m venv venv
venv/bin/pip install --require-hashes -r server/requirements.hashes.txt

WB_DATA_DIR=./data WB_BACKGROUND_JOBS=0 \
venv/bin/python -m uvicorn server.main:app --host 127.0.0.1 --port 7864
```

首次管理员随机密码仅在启动日志输出；确认身份后及时修改，不公开启动日志。
这只启动本地控制面，不会安装三平台引擎，也不会自动获得模型账号或供应商授权。
后台任务在这个开发示例中关闭，生产配置需要单独审查。

### 2. 启动控制台

另开终端，在仓库根目录执行：

```bash
cd src/three-platform/web
npm ci
npm run dev
```

访问 `http://localhost:3000`。开发代理默认连接本机 `127.0.0.1:7864`。
构建与检查：

```bash
npm run typecheck
npm test
npm run build:export
```

静态导出不包含后端。生产反代、认证、引擎与持久化仍需配置，不能将导出目录当作完整网关。

### 3. 预览首页

首页是独立的静态页面，与控制台分开发版。
在仓库根目录执行：

```bash
python3 -m http.server 8080 --bind 127.0.0.1 --directory site/home
```

访问 `http://127.0.0.1:8080`。首页预览使用合成数据生成的控制台截图，不连接生产服务。
此静态服务不提供登录、控制台或 `/favicon/` 的生产路径；相关入口依赖真实部署。
页面、手机入口与交互测试见 [site/README.md](../site/README.md)。

## 生产升级

```text
本仓库的固定提交
  → 构建与依赖锁
  → 隔离测试与输入核验
  → 逐文件摘要、签名和测试回执
  → 可信发布器
  → 运行身份与健康核对
```

- 产品源码与升级来源固定为 `daniei-chen/zhihui-gateway`，不直接运行任意 `main`。
- `tools/repository_source.py` 只取固定提交的源码，不安装、不重启、不覆盖现有目录。
- 首页、登录、图标、控制台与后端有各自的发布边界，不把多个目录称为一次原子发布。
- 网页旧式覆盖安装在受控部署中保持关闭；不要使用 `git pull && restart` 代替发布链。
- 回退代码不回退账本、额度、密钥、设备授权或会话撤销记录。
- `manager/deploy/` 是继承组件参考，不代表这台服务器已安装的生产信任锚。

完整合同见 [PRODUCT-RELEASE.md](PRODUCT-RELEASE.md)。

## 仓库地图

| 目录 | 内容 |
| --- | --- |
| [`site/`](../site/) | 首页、登录页、图标及实际控制台构建的合成预览 |
| [`brand/`](../brand/) | 全站共用的蓝色 Z |
| [`src/three-platform/web/`](../src/three-platform/web/) | 控制台源码、Dock、主题、平台与语言交互 |
| [`manager/server/`](../manager/server/) | FastAPI、权限、路由、协议适配与账本 |
| [`tools/`](../tools/) | 固定源码获取、版本核对及受控工具源码 |
| [`docs/`](./) | 产品合同、审计范围、许可与开发文档 |
| [`design/`](../design/) · [`ops/`](../ops/) | 设计决策、运维配置与历史边界 |

## README 视觉素材

封面、能力模块和光路图是自包含 SVG，没有脚本、外部字体或远程图片依赖。
静态内容始终可见，装饰光点在减少动态效果设置下隐藏。
在仓库根目录维护或核对：

```bash
python3 tools/render_readme_assets.py
python3 tools/render_readme_assets.py --check
python3 tools/check_product.py
python3 -m unittest discover -s tests -p test_readme_assets.py
```

真实界面预览来自本仓库对应源码或控制台构建；其中数字均为合成示例，不是生产状态、性能承诺或供应商可用性证明。
来源、尺寸和摘要见 [`docs/images/readme/manifest.json`](images/readme/manifest.json)。
