#!/usr/bin/env bash
# fork-upgrade.sh —— WorkBuddy 网关升级编排（保守模式）
#
# 设计原则：每个阶段独立、可中断、失败即停；未知情况一律不自动决策。
#
# 用法：
#   fork-upgrade.sh status
#   fork-upgrade.sh preflight <version>     # 备份 + 拉取上游源码/发布包到 /tmp
#   fork-upgrade.sh merge     <version>     # git 试合并 + 解已知冲突（未知则停）
#   fork-upgrade.sh go        <version>     # 同步 9 个 Go 文件 + 编译
#   fork-upgrade.sh test                    # Python 全量 + Go 全量
#   fork-upgrade.sh build     <version>     # 前端 + build_release + verify
#   fork-upgrade.sh deploy                  # 切软链 + 重启 + 健康检查
#   fork-upgrade.sh rollback                # 回滚到 releases/ 里上一个版本
#
# 已知冲突解法（v2.4.0→v2.8.3 实测，来自 fork-manifest.json）：
#   1. server/main.py            import 两边都留
#   2. server/routers/gateway.py import 两边都留
#   3. server/routers/system.py  删 set_upstream_ref 路由
#   4. server/services/updater.py 删 UPSTREAM_API_REPO
#   5. server/upstreamschema.py  语义断层：加 top_level 支持（非文本冲突）
set -uo pipefail

REPO="${REPO:-/home/agentuser/wbfork/repo}"
MANAGER="/opt/workbuddy-manager"
WORK="${WORK:-/tmp/wb-upgrade}"
TOOLS="$MANAGER/fork-tools"
GH_MIRROR="${GH_MIRROR:-https://gh-proxy.com/}"

die() { echo "!! $*" >&2; exit 1; }
say() { echo "==> $*"; }
ok()  { echo "  [OK] $*"; }
wrn() { echo "  [WARN] $*"; }

need_root() { [ "$(id -u)" -eq 0 ] || die "本阶段需要 root（sudo $0 $*）"; }

cmd=${1:-status}; shift || true

case "$cmd" in
status)
  echo "仓库: $REPO"
  echo "当前发布: $(cat "$MANAGER/.version" 2>/dev/null || echo '?')"
  echo "current -> $(readlink -f "$MANAGER/releases/current" 2>/dev/null || echo '?')"
  echo "上游容器: $(docker inspect workbuddy2api --format '{{.Config.Image}} {{.State.Health.Status}}' 2>/dev/null || echo '?')"
  echo
  echo "可用发布树:"
  ls -1 "$MANAGER/releases/" | grep -v '^current$' | tail -6 | sed 's/^/  /'
  echo
  echo "git 分支: $(git -C "$REPO" branch --show-current)"
  git -C "$REPO" log --oneline -3 | sed 's/^/  /'
  ;;

preflight)
  ver=${1:?用法: preflight <version>  例如 v2.8.4}
  need_root "$cmd" "$ver"
  mkdir -p "$WORK"
  say "1/4 备份"
  ts=$(date +%Y%m%d-%H%M%S); echo "$ts" > "$WORK/ts"
  cp -a "$MANAGER" "$MANAGER.bak-$ts" || die "管理端备份失败"
  cp -a /opt/workbuddy2api /opt/workbuddy2api.bak-$ts || die "上游备份失败"
  cp -a "$MANAGER/data/manager.db" "$MANAGER/data/manager.db.bak-$ts" || die "数据库备份失败"
  ok "备份完成（时间戳 $ts）"
  say "2/4 拉取上游源码归档"
  src="$WORK/src-$ver.tar.gz"
  curl -fsSL --max-time 300 "${GH_MIRROR}https://github.com/ithtelab/workbuddy-manager/archive/refs/tags/$ver.tar.gz" -o "$src" \
    || die "拉取失败：$ver（检查版本号或镜像）"
  ok "源码 $(du -h "$src" | cut -f1)"
  say "3/4 拉取发布包（含 upstream/ Go 源码）"
  rel="$WORK/rel-$ver.tar.gz"
  curl -fsSL --max-time 300 "${GH_MIRROR}https://github.com/ithtelab/workbuddy-manager/releases/download/$ver/workbuddy-manager-$ver.tar.gz" -o "$rel" \
    || die "拉取发布包失败"
  ok "发布包 $(du -h "$rel" | cut -f1)"
  say "4/4 解包"
  rm -rf "$WORK/src-$ver" "$WORK/rel-$ver"
  mkdir -p "$WORK/src-$ver" "$WORK/rel-$ver"
  tar xzf "$src" -C "$WORK/src-$ver" --strip-components=1 || die "源码解包失败"
  tar xzf "$rel" -C "$WORK/rel-$ver" --strip-components=1 || die "发布包解包失败"
  ok "源码: $WORK/src-$ver"
  ok "发布包: $WORK/rel-$ver（upstream/ 在 $WORK/rel-$ver/upstream）"
  echo; echo "下一步: $0 merge $ver"
  ;;

merge)
  ver=${1:?用法: merge <version>}
  src="$WORK/src-$ver"; [ -d "$src" ] || die "缺少 $src，先跑 preflight"
  cd "$REPO" || die "仓库不存在"
  [ -z "$(git status --porcelain)" ] || die "工作区不干净，先提交或 stash"
  say "1/3 构造上游分支"
  br="upstream-${ver}"
  if git rev-parse --verify "refs/heads/$br" >/dev/null 2>&1; then
    wrn "分支 $br 已存在，复用"
  else
    wt="$WORK/wt-$ver"
    git worktree add -q "$wt" -b "$br" HEAD || die "worktree 创建失败"
    find "$wt" -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf {} + 2>/dev/null
    cp -a "$src/." "$wt/" 2>/dev/null
    ( cd "$wt" && git add -A && git -c user.name=upstream -c user.email=up@local commit -q -m "upstream $ver" ) || die "提交失败"
    ok "分支 $br 就绪"
  fi
  say "2/3 试合并到临时分支"
  git checkout -q "$br"
  git checkout -q -B "upgrade/$ver" "$(git rev-parse HEAD)" 2>/dev/null
  # 从本地分支合并
  base=$(git branch --contains "$(git log --format=%H -1 master)" 2>/dev/null | head -1)
  git checkout -q master
  git worktree add -q "$WORK/wt-merge-$ver" -B "upgrade/$ver" master 2>/dev/null || \
    git worktree add -q "$WORK/wt-merge-$ver" "upgrade/$ver" 2>/dev/null
  mw="$WORK/wt-merge-$ver"
  [ -d "$mw" ] || die "合并 worktree 创建失败"
  ( cd "$mw" && git -c user.name=merge -c user.email=m@local merge --no-commit --no-ff "$br" ) 2>&1 | tail -20
  cd "$mw" || die "进入合并目录失败"
  say "3/3 冲突检查"
  conflicts=$(git diff --name-only --diff-filter=U 2>/dev/null)
  if [ -z "$conflicts" ]; then
    ok "无文本冲突"; echo; echo "下一步: $0 go $ver"; exit 0
  fi
  echo "冲突文件:"; echo "$conflicts" | sed 's/^/  /'
  # 只自动处理已知的 3 处 import 型冲突；其余停下
  python3 "$TOOLS/fork-resolve-conflicts.py" "$mw" <<<"$conflicts" && {
    echo; echo "已知冲突已自动解决，请人工 review："; echo "  cd $mw && git diff"
    echo "确认后: $0 go $ver"
  } || {
    echo; echo "!! 存在未知冲突，已停止（保守模式）。"
    echo "   手工解决后提交，再跑: $0 go $ver"
    exit 1
  }
  ;;

go)
  ver=${1:?用法: go <version>}
  rel="$WORK/rel-$ver/upstream"; [ -d "$rel" ] || die "缺少 $rel/upstream"
  say "1/3 复制上游 Go 源码到工作树"
  dst="$WORK/go-$ver"; rm -rf "$dst"; cp -a "$rel" "$dst" || die "复制失败"
  say "2/3 同步 9 个本地 Go 文件"
  "$TOOLS/fork-sync-go.sh" import "$dst" --apply || die "同步失败"
  say "3/3 编译"
  ( cd "$dst" && CGO_ENABLED=0 go build ./... ) && ok "go build 通过" || die "go build 失败，检查接口变更"
  echo; echo "下一步: $0 test"
  ;;

test)
  say "Python 全量测试"
  ( cd "$WORK/wt-merge-$(cat "$WORK/ts" 2>/dev/null || echo x)" 2>/dev/null && \
    /opt/workbuddy-manager/venv/bin/python -m unittest discover -s server/tests -t . 2>&1 | tail -6 ) || \
    wrn "合并工作树不在，跳过 Python 测试（需先 merge）"
  say "Go 全量测试"
  for d in "$WORK"/go-*/; do
    [ -d "$d" ] || continue
    ( cd "$d" && CGO_ENABLED=0 go test ./... 2>&1 | grep -vE '^ok' | head -10 ) && ok "$(basename "$d") 通过" || wrn "$(basename "$d") 有失败"
  done
  ;;

build)
  ver=${1:?用法: build <version>}
  need_root "$cmd" "$ver"
  mw=$(ls -d "$WORK"/wt-merge-* 2>/dev/null | head -1); [ -d "$mw" ] || die "缺合并工作树"
  say "1/3 前端构建"
  ( cd "$mw/web" && [ -d node_modules ] || ln -sfn "$REPO/web/node_modules" node_modules; \
    npm run build:export 2>&1 | tail -3 ) || die "前端构建失败"
  say "2/3 把合并结果同步进主仓库（fast-forward 检查）"
  git -C "$mw" status --porcelain | head -3
  wrn "本脚本不自动合并进 master——请人工确认后执行："
  echo "  cd $REPO && git merge $(basename "$mw" | sed 's/wt-merge-//')"
  say "3/3 构建+签名发布树"
  VERSION="$ver+local.$(date +%Y%m%d).1" "$TOOLS/build_release.sh" 2>&1 | tail -6
  echo; echo "下一步: 人工 review → $0 deploy"
  ;;

deploy)
  need_root "$cmd"
  latest=$(ls -1dt "$MANAGER"/releases/*/ 2>/dev/null | grep -v current | head -1)
  [ -n "$latest" ] || die "没有可用发布树"
  latest=${latest%/}
  say "切换到 $(basename "$latest")"
  ln -sfn "$latest" "$MANAGER/releases/current" || die "软链失败"
  cp -f "$latest/.version" "$MANAGER/.version" 2>/dev/null || true
  systemctl restart workbuddy-web || die "重启失败"
  for i in $(seq 1 30); do
    sleep 2
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 http://127.0.0.1:7864/ 2>/dev/null)
    [ -n "$code" ] && [ "$code" != "000" ] && { ok "健康检查 HTTP $code（${i}次）"; break; }
    [ "$i" -eq 30 ] && die "健康检查超时，请执行 $0 rollback"
  done
  say "完整性自检"
  "$TOOLS/fork-verify.sh" | tail -5
  ;;

rollback)
  need_root "$cmd"
  cur=$(readlink -f "$MANAGER/releases/current")
  prev=$(ls -1dt "$MANAGER"/releases/*/ 2>/dev/null | grep -v current | grep -v "$(basename "$cur")" | head -1)
  [ -n "$prev" ] || die "找不到上一个发布树"
  prev=${prev%/}
  say "回滚 current: $(basename "$cur") -> $(basename "$prev")"
  ln -sfn "$prev" "$MANAGER/releases/current" || die "软链失败"
  cp -f "$prev/.version" "$MANAGER/.version" 2>/dev/null || true
  systemctl restart workbuddy-web
  sleep 12
  curl -s -o /dev/null -w "  健康检查 HTTP %{http_code}\n" --max-time 5 http://127.0.0.1:7864/
  echo "已回滚。数据库如需回滚，手动恢复 $MANAGER/data/manager.db.bak-* 并删除 -wal/-shm"
  ;;

*)
  sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
  exit 2
  ;;
esac
