# 产品检查 workflow 模板

`product-ci.yml.example` 是可审查的 GitHub Actions 模板，**目前未启用**。本地已执行版本/来源检查、凭据扫描、前端类型检查、单元测试和构建。

当前 GitHub OAuth 连接没有 `workflow` scope，GitHub 拒绝创建 `.github/workflows/product-ci.yml`。本次不扩大授权、不换端点绕过限制；保留模板供仓库所有者之后在具有相应权限时启用。

即使启用，此任务也不构成生产发布批准，不包含真实供应商调用，不运行历史 real-nginx 夹具，不替代后端受信完整门禁与签名回执。
