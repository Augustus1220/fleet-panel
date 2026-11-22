# 通道设计：HY2 与 Reality 的分工与统计

同一台机器上并行跑 Hysteria2（UDP）与 Xray（TCP）两套协议栈，不是冗
余堆料，而是两种传输特性互补：

- **HY2（QUIC/UDP）**：弱网和拥塞下吞吐强，带宽利用激进；弱点是 UDP
  在某些网络环境下会被限速或拦截，且客户端生态（端口跳跃等特性）支
  持不齐。
- **Reality（TCP）**：借真实大站的 TLS 握手做伪装，抗识别能力最强，
  在线路不拥塞时速度与 HY2 接近；是 UDP 被针对时的天然回退。

生产经验：线路换成优质回程后，两者速度差距消失，日常主力可以放
Reality、HY2 留作备用与住宅链的快速通道。两套都留着，切换只在客户端
选节点，不动服务器。

## 各通道的统计口径

| 通道 | 计数点 | 备注 |
| --- | --- | --- |
| HY2 443 | HY2 实例 1 的 trafficStats（29003） | 含在线状态 |
| 住宅 HY2 2054 | HY2 实例 2 的 trafficStats（29004） | 与实例 1 相加 |
| VLESS 2053 / 住宅 2054 | Xray StatsService（email=账号名） | |
| Reality 443 / 8443 | Xray StatsService（email=设备名 / 设备名-ws） | 映射表归并到账号 |

合并逻辑与防丢设计见 [accounting.md](accounting.md)。

## Reality 的双身份

Reality 入站里每个账号注册两个 client：

- `mac`（UUID 甲）→ 直连出站；
- `mac-ws`（UUID 乙）→ 路由规则命中住宅出站。

客户端侧表现为两个节点（"Reality"与"住宅 Reality"），共用同一入站、
同一 Reality 密钥与 shortId。住宅身份的区分完全在服务器端完成，客户
端不需要知道住宅代理的存在。备选入站（8443）克隆同一套 client，只换
dest/SNI，用于 A/B 不同的伪装目标。

伪装目标（dest）的选择原则：与服务器 IP 同 ASN 或知名大站、自有证书
链完整、用 `xray tls ping` 实测过握手质量；被官方列入不推荐名单的目
标要及时换。客户端 mihomo 侧需注意：Reality 密钥带 ML-KEM 时，
reality-opts 必须加 `support-x25519mlkem768: true` 并使用 chrome 指
纹，否则握手失败且报错不直观。

## HY2 的两个细节

- **端口跳跃**：iptables 把 UDP 20000-30000 REDIRECT 到 443，mihomo
  订阅里给 HY2 节点加 `ports` 与 `hop-interval: 30`。Shadowrocket 不
  支持跳跃，手机端直连 443。
- **客户端密码格式**：Clash 里 HY2 的 password 必须是 `用户名:密码`
  组合串，与服务器 userpass 对应。

## 住宅链的边界

住宅出站是一个 HTTP 代理，只能承载 TCP：

- 住宅 HY2 入站收到的 UDP 流量无法经 HTTP 出站转发，会被拒绝——**失
  败即失败，不会改走机房 IP**，身份不会串；
- 客户端应用一般自动回落 TCP（视频、流媒体无感）；游戏、语音通话的
  UDP 不应走住宅链，应在分流规则里直连或走直连通道；
- 住宅 VLESS/Reality 同理（TCP 入站天然无此问题）。
