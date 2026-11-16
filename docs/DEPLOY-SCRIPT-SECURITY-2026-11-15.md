# deploy 脚本安全审计 — 2026-11-15

这 8 个脚本里有几个**以 root 运行**（`install.sh`、`prepare-managed-stage.sh`、
`mig.sh` 的调用方）。它们是整条部署链上权限最高的一环。

**结论：没有发现危险模式。** 三个看起来「缺选项」的地方都查清了，都是有意的。

---

## 一、清单与结论

| 脚本 | 行数 | set 选项 | 结论 |
| --- | --- | --- | --- |
| `install.sh` | 398 | `-euo` | 安全 |
| `prune-releases.sh` | 215 | `-uo` | **缺 `-e` 是有意的**（见第三节） |
| `prepare-retry.sh` | 172 | `-euo` | 安全 |
| `verify-staged-files.sh` | 99 | `-uo` | **缺 `-e` 不影响**（见第二节） |
| `verify-release.sh` | 57 | `-euo` | 安全 |
| `prepare-managed-stage.sh` | 56 | `-eu` | 缺 `pipefail`，无危险管道 |
| `relay-restart-safe.sh` | 56 | `-euo` | 安全 |
| `check-upstream.sh` | 19 | `-euo` | 安全 |

## 二、`verify-staged-files.sh` 缺 `-e` —— 不影响

它是**部署前的最后一道门**：逐一核对暂存文件的哈希。缺 `-e` 会让人担心
「校验出错但继续跑、最后误报 OK」。

**查过了，不会。** 它每个出口都是显式的：

| 行 | 出口 |
| --- | --- |
| 33 | `exit 2`（参数错误） |
| 40 | `exit 2`（目标目录不存在） |
| 44 | `exit 2`（清单不可读） |
| 83 | `exit 2`（清单里没有可校验条目） |
| 89 | `exit 0`（`missing == 0 && differ == 0`） |
| 99 | `exit 1`（其余） |

**「清单里没有可校验条目」单独挡了一道** —— 这一条尤其重要：
空清单会让「全部通过」变成真话里的假话，所以那里显式判了 `checked -eq 0` 就退出。

## 三、`prune-releases.sh` 缺 `-e` —— 这是**正确**写法

它删目录，缺 `-e` 看起来最危险。实际相反：

```bash
if rm -rf -- "$d" 2>/dev/null; then
    echo "  removed $name"
    DONE=$((DONE + 1))
else
    echo "  失败：$name" >&2
    FAILED=$((FAILED + 1))
fi
```

**如果加了 `-e`，第一次删除失败就会直接退出，后面的账号不会被处理、
失败计数也不会打印。** 缺 `-e` 才能把整轮跑完并汇总。

另外三处是刻意的：

- **`rm -rf -- "$d"` 里的 `--`**：防止路径以 `-` 开头时被当成选项
- **默认 dry-run**，`--apply` 才真的删
- **自检**：`current`、正在被进程占用的目录进了删除清单就中止

这个脚本我写过一版**有两个真 bug**（漏排序、版本按字符串比），
所以它的 dry-run 输出是被逐行核对过的。**默认 dry-run 不是形式，是它的救命机制。**

## 四、危险模式：全部为零

对 8 个脚本统一扫的：

| 模式 | 命中 |
| --- | --- |
| `rm -rf $VAR`（变量未加引号） | **0** |
| `rm -rf` 后面跟裸路径 | **0** |
| `curl ... \| bash` / `wget ... \| sh` | **0** |
| `chmod 777` | **0** |
| `eval` | **0** |

**没有外部下载**。唯一出现的 `curl` 全是 `http://127.0.0.1:...` 的健康检查。

## 五、`install.sh` 里唯一一处写系统目录

```
"${APP_DIR}/deploy/workbuddy-web.service" > /etc/systemd/system/workbuddy-web.service
```

**内容来自仓库内的模板文件，变量是安装时算出来的应用目录。**
不是远程下载的、不是拼接来的。写 `/etc/systemd/system/` 正是安装脚本的职责。

`rm -rf "$tmp"` 带引号，是 `trap` 里的临时目录清理。

## 六、这份审计**没有**覆盖的

- **`mig.sh` 本身** —— 它在 `/tmp/`，不在 `deploy/` 里，且由我每次临时写
- **`writer_inventory.py` / `migration.py`** 的内部逻辑（那是 Python 侧，不是 shell）
- **部署链之外的 root 任务**（备份、清理、看门狗）
- **脚本读取的配置文件本身是否可被非特权用户篡改** —— 这一项单独查过：
  密钥文件都是 `0600`，目录 `0700`，属主 `10001`
