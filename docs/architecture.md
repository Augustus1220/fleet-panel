# 架构总览

这套系统在一台小 VPS（1 核 1G 起）上同时做四件事：多协议接入、多账号隔离、
分账号流量统计、按账号下发订阅，并带一个只读状态面板。核心设计思想是
**门脸与身份分离**：

- **入口（门脸）**只看线路质量、带宽和流量额度，用机房 IP 即可；
- **出口身份**由一条链式住宅代理承担，需要"像住户一样上网"的流量走住宅
  出口，其余流量从机房直接出。

## 通道矩阵

| 通道 | 协议/端口 | 出口 | 统计来源 |
| --- | --- | --- | --- |
| HY2 | Hysteria2 / UDP 443（+20000-30000 跳跃） | 机房直连 | HY2 trafficStats |
| 住宅 HY2 | Hysteria2 / UDP 2054 | 住宅代理 | HY2 trafficStats（第二实例） |
| VLESS | VLESS+TLS+Vision / TCP 2053 | 机房直连 | Xray StatsService |
| 住宅 VLESS | VLESS+TLS+Vision / TCP 2054 | 住宅代理 | Xray StatsService |
| Reality | VLESS+Reality+Vision / TCP 443 | 机房直连 | Xray StatsService |
| 住宅 Reality | 同一入站，`-ws` 身份 | 住宅代理 | Xray StatsService |
| Reality 备选 | 同上 / TCP 8443（不同 dest） | 同上 | Xray StatsService |

细节见 [channels.md](channels.md)。

## 组件

```
客户端 (Clash Verge / Shadowrocket / sing-box)
   │  订阅拉取 (HTTPS 2097, 每账号一条 token 路由)
   ▼
订阅服务 subscription/server.py ── payload.json（启动时读入，改后须重启）
   │
节点服务
   ├─ Hysteria2 ×2（443 直连 / 2054 住宅链）── trafficStats API (localhost)
   └─ Xray（2053 / 2054 / 443 / 8443）────── StatsService (localhost:10085)
   │
记账
   ├─ traffic-account.py（每 2 分钟）: 网卡计数 → traffic_state.json
   └─ panel/collect.py（每 2 分钟）: 汇总各源 → status.json + history.sqlite3
   │
面板 panel/panel.py（HTTPS 2096, 密码登录, 只读）
自愈 fleet-sub-healthcheck（每 2 分钟探测订阅服务，连续失败自动重启）
```

## 目录布局（单机）

| 路径 | 内容 |
| --- | --- |
| `/opt/fleet-hy2/`, `/opt/fleet-hy2-webshare/` | HY2 两实例的配置与证书 |
| `/usr/local/etc/xray/config.json` | Xray 配置 |
| `/usr/local/lib/fleet-subscriptions/` | 订阅服务与记账脚本 |
| `/var/lib/fleet-subscriptions/` | payload.json、userinfo.json、traffic_state.json |
| `/etc/fleet-subscriptions/` | 订阅服务用的证书副本 |
| `/usr/local/lib/fleet-panel/` | 面板与采集器 |
| `/var/lib/fleet-panel/` | status.json、history.sqlite3、links.json、会话 |
| `/etc/fleet-panel/` | 面板 config.json、password.json、住宅代理 API key |

## 设计取舍

- **统计不依赖任何单个进程的内存计数**：HY2/Xray 的计数器都随进程重启
  清零，采集器用"持久累计 + 重置感知"把它们折成不会丢的总数（见
  [accounting.md](accounting.md)）。
- **订阅内容静态化**：订阅文件在 payload 里以 base64 存放，服务只做解码
  下发，没有模板引擎、没有数据库，改规则就是改一段文本再重启服务。
- **面板严格只读**：面板进程以低权限用户运行，除了触发一次新采集外不写
  任何业务状态；所有"写"都发生在采集器和记账脚本里。
- **住宅链只走 TCP**：住宅代理是 HTTP 代理，承载不了 UDP。UDP 流量在住宅
  通道上会被拒绝而不是漏走机房 IP——失败即失败，不串身份。
