# 部署步骤

面向一台全新的 Ubuntu 24.04 VPS（1 核 1G 起）。所有 `<占位符>` 在部署
时替换为真实值；真实值只落在服务器上，不要回写到任何仓库。

## 0. 准备

- 一个域名，DNS 可控（证书走 DNS-01 签发，不依赖入站 80/443 是否可达）；
- 一个住宅 HTTP 代理（地址/端口/账号/密码），可跳过住宅链部分；
- 防火墙（云安全组 + 本机 ufw）入站清单：
  TCP 443、UDP 443、UDP 20000-30000、TCP 2053、TCP 2054、UDP 2054、
  TCP 8443、TCP 2096（面板）、TCP 2097（订阅）、SSH 端口。

## 1. 基础

1. 系统更新，开启 BBR（`fq` + `bbr` 持久化）；
2. 放入 `templates/sysctl/99-proxy-tuning.conf` 并 `sysctl --system`
   （UDP 缓冲 16MiB 是 HY2 的关键项）；小内存机型可加 zram + 1G swapfile；
3. SSH 改为密钥登录，新建低权限用户 `fleet-panel`、`fleet-sub`。

## 2. 证书

用 acme.sh 以 DNS-01 为节点域名签证书，装到 `/opt/fleet-hy2/`，并配置
续期后自动分发：HY2 两实例、Xray（每周定时重启吃新证）、订阅服务
（unit 的 ExecStartPre 每次启动时拷贝）、面板各一份。

## 3. Hysteria2

1. 安装官方二进制到 `/usr/local/bin/hysteria`；
2. 按 `templates/hysteria/` 写两份配置（账号密码用强随机值生成，记入
   服务器本地的凭据清单文件，权限 600）；
3. 装 `fleet-hy2.service`、`fleet-hy2-webshare.service`、
   `fleet-porthop.service` 并 enable。

## 4. Xray

1. 安装官方 Xray；
2. `xray x25519` 生成 Reality 密钥对、`xray uuid` 为每个账号生成
   **两个** UUID（直连与 `-ws`），shortId 用 8 字节随机 hex；
3. 按 `templates/xray/config.json` 写配置（公钥只出现在客户端订阅里，
   私钥只在本文件）；
4. `xray run -test` 校验后启动。

## 5. 订阅服务

1. 生成各账号的订阅文件（Clash YAML 可从 `templates/clash/clash.yaml`
   改），按 [subscriptions.md](subscriptions.md) 的约定组装
   `payload.json`（token 用 `openssl rand -hex 32`）；
2. 写 `userinfo.json`（总额度与到期时间）；
3. 装 `subscription/server.py` 与 `fleet-subscription.service`，
   `traffic-account.py` 与其 timer 一并装上；
4. 装自愈脚本与 timer。

## 6. 面板

1. 装 `panel/panel.py`、`panel/collect.py` 到 `/usr/local/lib/fleet-panel/`；
2. `/etc/fleet-panel/config.json`：`{"cert": ..., "key": ..., "port": 2096}`；
3. `python3 panel/setpw.py` 设置面板密码（从标准输入读，不进 shell 历史）；
4. 住宅代理的 API key（如需面板展示用量）写入
   `/etc/fleet-panel/webshare.key`（权限 600）；
5. 写 `/var/lib/fleet-panel/links.json`（订阅链接展示清单）；
6. 装 `fleet-panel.service` 与 collect timer，首跑一次采集器。

## 7. 验收清单（重要）

不要只在本机测——本机回环测不出公网路径与伪装问题。从**另一台机器**
逐项验：

- [ ] 每个账号 × 每个通道各拉一次真实流量，出口 IP 与预期一致
      （直连通道 = 本机 IP，住宅通道 = 住宅 IP）；
- [ ] 订阅链接从公网可拉取，节点数、规则数正确，userinfo 头存在；
- [ ] 面板各模块有数：分账号用量在涨、整机流量与网卡量级对得上、
      在线状态、证书到期日正确；
- [ ] 住宅链下用 UDP 应用测试：应失败（被拒绝），且**不能**漏出机房 IP；
- [ ] 重启 HY2 与 Xray 各一次，确认分账号累计不回退、不暴涨；
- [ ] 客户端实测：mihomo 打开订阅、Reality 节点能通（ML-KEM 开关与
      chrome 指纹），手机端导入 sub.txt 能通。
