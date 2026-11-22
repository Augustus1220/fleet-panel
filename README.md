# Fleet Panel — 多账号代理节点工具包

在一台小 VPS 上运营一套多账号代理节点的完整方案：Hysteria2 + Xray
双协议栈、直连/住宅双出口、**分账号流量统计**、**按账号下发订阅**，
以及一个只读的深色状态面板。附带可直接交给 AI agent 执行的部署与
维护提示词。

> 本仓库是**模板版**：所有域名、IP、账号、密钥均为占位符（示例 IP
> 使用文档保留段 203.0.113.0/24）。真实部署参数只应存在于服务器本地，
> 审查方法见 [docs/security-review.md](docs/security-review.md)。

## 功能一览

- **双栈多通道**：HY2（UDP 443 + 端口跳跃）、HY2 住宅链、VLESS+TLS、
  VLESS 住宅链、Reality（双 dest A/B），共 7 类通道；
- **住宅出口**：经一条 HTTP 住宅代理链式出站，靠 Xray email 后缀
  （`-ws`）在同一 Reality 入站内区分直连/住宅身份；
- **分账号统计**：HY2 trafficStats + Xray StatsService 双源合并，持
  久累计、进程重启不丢；整机流量另有网卡记账，与订阅额度对齐；
- **分订阅下发**：每账号一条 token 链接（Clash YAML / URI 列表 /
  Shadowrocket 规则三种形态），带 Subscription-Userinfo 流量头；
- **状态面板**：单文件、零依赖（Python 标准库），密码登录；流量环形
  表、当前网速、各账号迷你曲线、性能仪表、异常记录、订阅链接集中
  展示；页面自动刷新；
- **自愈与记账**：订阅服务探针自愈、证书续期监控、异常事件自动记录。

## 仓库布局

```
panel/         面板与采集器（panel.py / collect.py / setpw.py）
subscription/  订阅服务（server.py）、网卡记账（traffic-account.py）、payload 示例
templates/     Xray / Hysteria2 / systemd / sysctl / Clash 配置模板
docs/          架构、统计口径、订阅下发、通道设计、部署步骤、安全审查
prompts/       给 AI agent 的部署 / 加账号 / 换出口 / 健康检查提示词
```

## 快速开始

1. 读 [docs/architecture.md](docs/architecture.md) 了解整体设计；
2. 一键安装：把子域名 A 记录指向一台干净的 Ubuntu 24.04 VPS，然后
   `sudo ./install.sh --domain node.example.com`（见
   [docs/installer.md](docs/installer.md)）；或按
   [docs/deployment.md](docs/deployment.md) 手工部署，或把
   [prompts/deploy-new-node.md](prompts/deploy-new-node.md) 填好
   交给你的 AI agent 执行；
3. 部署后用 [prompts/health-check.md](prompts/health-check.md) 做例
   行检查。

## 文档

- [架构总览](docs/architecture.md)
- [分账号统计](docs/accounting.md)
- [分订阅下发](docs/subscriptions.md)
- [通道设计：HY2 与 Reality](docs/channels.md)
- [部署步骤](docs/deployment.md)
- [安全审查](docs/security-review.md)

## License

MIT
