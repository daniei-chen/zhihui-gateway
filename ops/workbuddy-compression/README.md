# WorkBuddy / 先衡量，再压缩

2026-11-01 已上线。历史候选 `manifest.json` 原样保留；当前运行身份见
[production-verification.json](production-verification.json)和
[稳定性发布记录](../../docs/RELEASE-20261101-STABILITY.md)。

- 小于 256 KiB 原样发送。
- 大请求使用 `gzip.BestSpeed`；压后至少节省 10% 才带 `Content-Encoding: gzip`。
- 不修改身份、渠道校验、帐号轮换或推理重试语义。
- 单请求 128 MiB、原始 body 总预算 256 MiB、32 租约从正式配置核验。
- 客户端上传压缩和引擎到上游压缩是两段独立链路，不能相互替代。

## 准确重建源码

补丁只改运行实现 `internal/upstream/client.go` 的压缩阈值/级别/收益判断，
另调整旧测试的输入长度并添加边界测试。基线必须是前一轮已核验的 266 文件 ingress
候选，不接受同名上游的可变 HEAD。准备器拒绝额外文件、链接、大小写冲突和源树漂移；
保留失败目录，不覆盖既有目录，不读取配置或账号。

```sh
python tools/prepare_workbuddy_compression.py \
  --source /isolated/verified-previous-ingress-source \
  --destination /isolated/workbuddy-compression-source

cd /isolated/workbuddy-compression-source
GOPROXY=off GOSUMDB=off GOTOOLCHAIN=local go test -count=1 ./...
CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -buildvcs=false -o wb2api ./cmd/server
```

工具链为 Go 1.27.0，依赖按 `go.sum` 预先校验。本仓库附补丁、有限准备器和逐文件清单，
不附生产账号、凭据、数据库或完整私有运行目录。准备和测试成功仍不等于可以覆盖线上镜像。
267 文件源码树使用 `sha256(json.dumps(files, sort_keys=True).encode())`，与历史候选一致；
重建清单另附紧凑 JSON 摘要，两种序列化摘要不能直接混比。

源组件版权为 MIT / Copyright (c) 2026 Sliverkiss，许可证保留于
`../workbuddy-ingress/LICENSE`。没有用历史工具链逐字节复现原基线 ELF，不能称为完整
可复现供应链。部署控制器继续要求签名、实际运行 ELF/镜像、单写者和兼容底线。
