# WorkBuddy Manager 容器镜像（管理端 + 反代网关）
#
# 设计取舍说明（值得先读，因为它解释了为什么容器版有些功能是"降级"的）：
#
# 1. **本镜像不含上游 workbuddy2api**。上游是独立的 Go 服务、自带
#    docker-compose.yml 与 auths/data 卷，硬塞进来会让两边的升级互相牵制。
#    推荐做法：两边分别用 compose 起，管理端通过 WB2API_BASE 连接上游。
#
# 2. **容器内不执行自更新或宿主 Docker 运维**。更新与重建由宿主机的受控流程
#    完成，容器只运行已审计的代码与依赖；这样更新权限与业务请求路径保持隔离。
#
# 3. **生产运行时默认不携带 docker.sock、Git、SSH 或 Docker CLI**。
#    这些工具会扩大镜像攻击面，且 docker.sock 等价于把宿主机控制权交给容器。
#    需要上游重载、日志或更新时，应在宿主机执行受控运维命令；面板会明确提示
#    该能力在最小权限容器中不可用。
#
# 4. **数据与凭据全部走卷**，不烘进镜像：data/（数据库、日志、更新状态）、
#    以及上游的 auths/ 与 config.json。
#
# 5. **前端由前端构建阶段自己搞定**（见下，修 issue #38）。
#    为什么需要它：`web/out` 是构建产物、被 .gitignore 排除，所以
#    `git clone && docker build .` 拿到的工作区里**没有**它 —— 原先直接
#    `COPY web/out` 会失败在：
#
#      ERROR: failed to build: ... "/web/out": not found
#
#    （issue #38 报的就是这个。发布包用户不受影响，因为包里带着构建好的
#    web/out。）
#
#    现在：有现成产物就直接用（发布包 / CI），没有就在容器内构建
#    （git clone）—— 两种部署都一条命令跑通，用户不必先装 Node。

# ── 前端构建阶段 ─────────────────────────────────────────
#
# 这一步的存在是为了修 issue #38：`git pull && docker build .` 会失败在
# `COPY web/out: not found` —— 因为 web/out 是构建产物、被 .gitignore 排除，
# clone 出来的工作区里没有它。
#
# 但两种部署形态下 web/ 目录**都存在**，只是内容不同：
#   · git clone：有源码，没有 out/
#   · 发布包 / CI：有 out/（已构建），没有源码
#
# 所以这里 `COPY web/ /src/` 两种情况都不会失败，再由下一步在**构建期**
# 判断走哪条路 —— 避免了「条件 COPY」这个 Docker 不支持的写法。
#
#   web/out 已存在 → 直接用它（发布包与 CI 走这条，秒过）
#   否则           → 容器内 npm ci + next build（git clone 走这条，
#                    用户不必先自行构建前端）
FROM node:20-slim AS web-builder

WORKDIR /src

# 可选：npm 依赖镜像。与 Debian/PyPI 同一个成因——中国大陆访问官方 registry
# 实测缓慢且会断。留空 = 官方源（失败时自动降级到 npmmirror）。
#   docker compose build --build-arg NPM_REGISTRY=https://registry.npmmirror.com
ARG NPM_REGISTRY=""
# 子路径部署前缀（例如 /workbuddy-manager）。留空 = 部署在根路径。
# 构建期写进 NEXT_PUBLIC_BASE_PATH，Next 会把它内联进前端产物：
# 所有资源引用与 axios 请求都会带上该前缀。
ARG BASE_PATH=""
ENV NEXT_PUBLIC_BASE_PATH=$BASE_PATH

COPY web/ /src/

RUN set -eu; \
    if [ -f /src/out/index.html ]; then \
        echo "前端：使用已有的 web/out，跳过构建"; \
        cp -r /src/out /dist; \
    else \
        echo "前端：工作区没有构建产物，在容器内构建（git clone 形态）"; \
        mkdir -p /build && cp -r /src/. /build/; \
        cd /build && rm -rf out .next; \
        npm_ci() { npm ci --no-audit --no-fund ${1:+--registry="$1"}; }; \
        if [ -n "${NPM_REGISTRY}" ]; then \
            echo "npm: 使用指定源 ${NPM_REGISTRY}"; \
            npm_ci "${NPM_REGISTRY}"; \
        elif npm_ci ""; then \
            echo "npm: 依赖安装成功（官方源）"; \
        else \
            echo "npm: 官方源失败，改用 registry.npmmirror.com 重试…" >&2; \
            rm -rf node_modules; \
            npm_ci https://registry.npmmirror.com; \
            echo "npm: 改用镜像后安装成功（如需固定，构建时传 NPM_REGISTRY）"; \
        fi; \
        NEXT_OUTPUT_EXPORT=1 npx --no-install next build; \
        test -f out/index.html || { echo "前端构建失败：缺少 out/index.html" >&2; exit 1; }; \
        cp -r out /dist; \
    fi; \
    test -f /dist/index.html

# ── 主镜像 ────
FROM python:3.12-slim

# 环境变量：Python 不要写 pyc（容器是一次性的，写了也没用）、日志不缓冲
#
# PYTHONUSERBASE 指到 /app/.local：容器里以 uid 10001 运行，而依赖是 root 装的、
# site-packages 归 root，**更新时装新依赖只能走用户站点**。钉死这个路径有两个理由：
# ① 不依赖 Docker 把 HOME 设对；② /app 已经 chown 给 10001（见下方 useradd 那步），
# 目录必须**先存在**——CPython 的 site.py 只在用户站点目录存在时才把它加进 sys.path。
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    WB_RUN_MODE=docker \
    WB_MANAGER_HOST=0.0.0.0 \
    WB_MANAGER_PORT=7864 \
    WB_INSTALL_DIR=/app \
    WB_DATA_DIR=/app/data \
    WB_STATIC_DIR=/app/web/out \
    WB_AUTH_DIR=/opt/workbuddy2api/auths \
    WB_UPSTREAM_CONFIG=/opt/workbuddy2api/config.json \
    WB2API_BASE=http://127.0.0.1:7863 \
    WB_BASE_PATH="" \
    PYTHONUSERBASE=/app/.local

WORKDIR /app

# 先装依赖（利用层缓存：代码改动不必重装依赖）
#
# 可选：PyPI 镜像。官方 PyPI 走国际 CDN，
# 中国大陆访问实测会出现 `SSL: UNEXPECTED_EOF_WHILE_READING`，重试几次才通，
# 赶上抖动就直接构建失败。留空 = 官方源（失败时自动降级到清华镜像）。
#
#   docker compose build --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PIP_INDEX_URL=""
COPY server/requirements.txt server/requirements.lock.txt /app/server/
RUN set -eu; \
    pip_install() { \
        pip install --no-cache-dir --retries 5 --timeout 60 \
            --index-url "$1" -r /app/server/requirements.txt; \
    }; \
    if [ -n "${PIP_INDEX_URL}" ]; then \
        echo "pip: 使用指定镜像源 ${PIP_INDEX_URL}"; \
        pip_install "${PIP_INDEX_URL}"; \
    elif pip_install https://pypi.org/simple; then \
        echo "pip: 依赖安装成功（官方源）"; \
    else \
        echo "pip: 官方源失败，改用清华镜像重试…" >&2; \
        pip_install https://pypi.tuna.tsinghua.edu.cn/simple; \
        echo "pip: 改用镜像后安装成功（如需固定，构建时传 PIP_INDEX_URL 或写入 compose build.args）"; \
    fi

# 再拷代码与已构建的前端
COPY server /app/server
COPY --from=web-builder /dist /app/web/out
COPY deploy /app/deploy
COPY CHANGELOG.md README.md /app/

# 非 root 运行。目录归属交给 app 用户，使容器内更新能写回代码目录。
# 注意与上游 auths/data 卷的 uid 对齐：上游容器以 uid 10001 运行，
# 这里用同一 uid 可避免跨容器写同一个卷时的权限问题。
#
# /app/.local 是 PYTHONUSERBASE 的落点（见上面的 ENV）：容器内更新装新依赖时
# 只能写用户站点，且该目录必须**先存在**，否则重启后的新进程不在 sys.path 上找它。
RUN useradd -u 10001 -m -s /bin/bash app \
    && mkdir -p /app/data /app/.local /opt/workbuddy2api \
    && chown -R 10001:10001 /app /opt/workbuddy2api

USER app

EXPOSE 7864

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:7864/api/healthz', timeout=5).read()"]

CMD ["python", "-m", "uvicorn", "server.main:app", "--host", "0.0.0.0", "--port", "7864"]

