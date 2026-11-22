# 分账号统计

目标：每个账号在每个通道上用了多少流量，进程重启、机器重启都不丢，
还能按小时/天出曲线。一共三个数据源，采集器（`panel/collect.py`，每
2 分钟一轮）把它们合并成一份快照和一份历史样本。

## 1. HY2：trafficStats API

Hysteria2 自带统计接口（配置里的 `trafficStats`，只绑 localhost，带
secret）：

- `GET /traffic` → `{账号: {tx, rx}}`，自进程启动起的累计值；
- `GET /online` → 当前在线账号。

两个实例（443 直连、2054 住宅链）各有一份，采集器相加。注意 HY2 的
`tx` 是"发给设备"（下行视角与 Xray 的 downlink 对齐），合并前先统一
语义。

## 2. Xray：StatsService + email 约定

Xray 侧开启 `stats` + `policy.levels.0.statsUserUplink/Downlink`，通过
本机 dokodemo-door 入站（127.0.0.1:10085）暴露 StatsService，采集器用
`xray api statsquery` 读取 `user>>>邮箱>>>traffic>>>uplink/downlink`。

**账号身份靠 email 字段**，约定如下：

- TLS 入站（2053/2054）的 client email 直接用账号名（`alice_mac`）；
- Reality 入站（443/8443）每个账号有**两个** client：`mac` 与 `mac-ws`，
  对应两个不同的 UUID。`-ws` 后缀的那份经路由规则走住宅出口，其余直连。
  采集器用一张映射表（`XRAY_EMAIL_MAP`）把 `mac`、`mac-ws` 都归并到
  面板账号 `alice_mac`。

一个入站同时提供直连与住宅两种 Reality，靠的就是这组 email + 路由
规则（`routing.rules` 里按 `user` 列表匹配 `-ws` 身份 → 住宅出站）。

## 3. 持久累计：计数器会清零，总数不能丢

两个数据源的计数器都是"自进程启动起"，重启即归零。采集器对每个来源
单独做增量折算（`accumulate_users`）：

- 保存上次的原始计数 `prev` 与累计值 `acc`；
- 本次 `cur >= prev` 时累加差值；`cur < prev`（发生过重启）时把 `cur`
  整体计入（最多损失一个采集间隔内的增量判断误差）；
- 某个来源本轮取不到数（接口无响应）时**不更新它的基线**，避免恢复后
  把一整段流量误判成增量或丢弃。

HY2 与 Xray 的累计结果最后按账号相加，就是面板"各账号用量"和订阅侧
统计的口径。状态存在 `user_traffic_state.json`，与快照分开存放——面板
的图表逻辑会整表覆写自己的状态文件，混放会互相踩。

## 4. 整机流量：网卡记账

整机口径（面板顶部大数字、订阅 `Subscription-Userinfo` 的 total/used）
来自网卡计数器，与代理层统计是两套账：

- `subscription/traffic-account.py` 每 2 分钟读 `/proc/net/dev`，把增量
  折进 `traffic_state.json` 的 `acc_rx/acc_tx`；用 `boot_id` 判断是否
  重启过，重启后从新计数重新起步，最多丢一个间隔；
- 订阅服务和面板在读数时还会把"上次记账后网卡又走的量"实时补上（同一
  份 `boot_id` + `last_rx/last_tx` 差值逻辑），所以展示值不必等下一轮
  记账。

网卡口径包含转发两端，数值天然大于账号口径之和，属正常现象，面板的
口径说明里写明了这一点。

## 5. 历史样本与曲线

采集器每轮往 `history.sqlite3` 写一行样本：`(ts, 网卡累计 rx/tx,
各账号累计 tx/rx 的 JSON)`，保留 90 天。面板的 `bucketize()` 把累计
序列按相邻差分切成 24 小时（逐小时）/ 7 天 / 30 天的桶，画成曲线；
历史起点之前还有一段"上线初期未记录"的累计，用一个虚拟零基线
（`BASELINE_TS`）按时段均摊进窗口，避免第一天出现一根假尖峰。

## 6. 在线状态与异常事件

- 在线账号：HY2 `/online` 接口（Xray 无等价接口，Reality/VLESS 账号不
  显示在线点，这是已知边界）；
- 异常记录：采集器每轮对比服务状态、证书到期日、自愈日志
  （`fleet-sub-health` 的 journal），生成 down/up/restart/cert/warn
  事件存入同一数据库，面板按时间倒序展示。
