# 发版脚本的两处误判（第 69 轮，v1.5.92 / v1.5.93）

## 1. 本轮改了什么

两件事，分两版发：

**v1.5.92** —— 上游超时的类型不再丢失（`server/deadline.py`）。
**v1.5.93** —— 发布脚本的两处误判（`deploy/rollout-rolling.sh`）。

## 2. 上游超时的类型为什么值得记

`deadline.py` 里有一张表：

```python
_BUDGET_STAGE = {
    'connect':      ('transport_error', 'stream_read'),
    'send_request': ('transport_error', 'stream_read'),
    'first_byte':   ('transport_error', 'stream_read'),
    'stream_idle':  ('transport_error', 'stream_read'),
    'total':        ('transport_error', 'stream_read'),
}
```

五类完全不同的事情压成同一个标签。注释说「具体是哪一类预算，由 `kind` 与
message 表达」—— 但那两样都没落地：6 个调用方把 `error_stage` 落库成
`stream_read`、终态归成 `gateway.transport.error` 之后就结束了，
**没有一处写日志或写库**。

### 证据

```
request_outcomes: 253 条 gateway.transport.error
                  error_stage 全部是 stream_read
                  平均 31.7 秒，最长 173 秒
journalctl:       grep '上游响应超出时间预算：' → 零命中
```

### 后果：归因错了三成

21 天的统计（641 条 5xx）：

```
gateway   330  51.5%   ← 我们的责任
upstream  310  48.4%
client      1   0.2%
```

**我们被算得比上游还多。** 而其中 218 条是 `gateway.transport.error` ——
平均等 31 秒、最长等 173 秒，那是**在等上游**，不是我们算错。

按天看更清楚：这 218 条里 193 条集中在 11-11 一天（那天 5xx 率 8.10%），
11-22 之后就没了。

### 改动

只在 `classify_budget_exceeded` 里补一行 warning，把 `kind` 写进日志。
**不动 `error_stage` 和 `error_code` 的取值** —— 那两样有既有统计和测试依赖，
改了会让历史对比断裂。先让信息可见，再谈归因。

## 3. 发布脚本的两处误判

这个问题是**发 v1.5.92 时自己撞出来的**。脚本报：

```
!! 重启后 60 秒仍未返回 vv1.5.92，请检查 workbuddy-web
```

**双 v。** 而服务其实已经正常返回 `{"version":"1.5.92",...}`，五个页面全 200。

### 其一：版本号前缀没归一化

`/api/release` 里两个字段的写法不一样：`version` 是 `1.5.92`，
`release_id` 是 `v1.5.92`。脚本比对的是 `version`，却把调用方传进来的
`v1.5.92` 直接拼进去：

```bash
grep -q "\"version\":\"$version\""     # 变成 "version":"v1.5.92"，永不命中
```

手工验证：

```
with v    : NO MATCH   ← 脚本实际做的
without v : MATCH
```

### 其二：5xx 统计没限定推理流量

```bash
ACCESS_LOG=/var/log/nginx/access.log
awk -v s="$before" 'NR>s && $9 ~ /^5/' "$ACCESS_LOG"
```

`/var/log/nginx/access.log` 是**整台机器的共享日志**，同一个文件里还有
控制台页面、`/_next` 静态资源、`/dsh-login`（属于另一个站点）。
实测这份日志的 5xx：

```
非 /v1/ 的 5xx：4 个   ← 会被算进「本次发版丢了几个请求」
    /v1/ 的 5xx：8 个   ← 才是真正该统计的
```

### 为什么这两条都值得修

后果**不是**「发布失败」，而是更麻烦的东西：**发布成功了却看起来像失败**。
脚本 `die` 之后连 5xx 统计都没跑，退出码也没给 —— 而那个统计正是
「本次发版有没有丢请求」的唯一证据。

**一个会误报的判据比没有判据更糟**，因为下次真出问题时它不再被相信。

## 4. 这个脚本此前零测试覆盖

`server/tests/test_v12152_rollout_script.py` 测的是 `deploy/rollout.sh`
（prepare/run/cleanup 三段式），**两个脚本名字很像、职责完全不同**。
而 `rollout-rolling.sh` 是「发版不打断在途调用」的全部实现，从没被检查过。

补了 18 条契约测试。上面两个 bug 就是补测试时暴露的。

## 5. 发布结果（v1.5.93，用修好的脚本发布的）

```
== 重启窗口内的 5xx（只算推理流量）
   0 条 —— 零中断

== 页面与就绪探针
   /            200
   /portal/     200
   /keys/       200
   /dashboard/  200
   /members/    200
   readyz       200
```

`/api/release` → `v1.5.93` / `source_commit ac7ed1e`
巡检 → `ok 26 项，警告 3 项，问题 0 项`

## 6. 三个我自己犯的错，都记下来

### 错一：写了个 `refresh_reviewed_hashes.py`，是重复实现

`tools/bump_version.py` 的 `sync_provenance()` 早就在重算全部 145 个哈希了
（第 100-110 行）。我没查就重写了一遍，等于把同一套逻辑维护两份。
发现后改成一个**只读**的 `check_reviewed_hashes.py` —— 它填的是
`bump_version.py` 覆盖不到的场景（只改文档、不升版本时的确认），
写回的职责仍归 `sync_provenance`。

**这是「改成新东西之前先 grep 有没有现成的」这条老教训的重演。**

### 错二：先把 CHANGELOG 写完才升版本

`bump_version.py` 会重算哈希，但那时 CHANGELOG 还没改，所以记下的是旧哈希。
第二个提交当场在 CI 上炸：

```
AssertionError: '58cbcff8...' not found in {'be3d7376...'} : CHANGELOG.md
```

**这正是 `docs/OPS-PITFALLS.md` 第十九条描述的场景，而我是那一条的作者。**
正确顺序：先提交代码 → 先写 CHANGELOG → 再升版本 → 最后提交版本面。

### 错三：猜 `curl` 下载没坏

用 `curl -sfL --max-time 9` 拉 494 KB 的 CHANGELOG，9 秒不够，
`curl` 写了 239 KB 就返回了。`-f` 只在 **HTTP 错误**时失败，**超时不算**。
于是 stage 时报：

```
UnicodeDecodeError: 'utf-8' codec can't decode byte 0xe4 in position 239746
```

一开始我还误判成「`--retry 2` 导致内容重复」（那次 494841 其实是对的），
把正确的结果当成了错误。**「下载成功」必须校验，不能假设。**

## 7. 一句话

**判据的错误比业务的错误更危险** —— 业务错了有人说，判据错了没人怀疑，
直到它把真的问题也一起掩盖掉。
