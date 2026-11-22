# 提示词：部署一台新节点

把下面整段发给你的 AI agent（替换所有 `[方括号]` 内容）。模板假设
agent 能通过 SSH 操作目标服务器、能读写本仓库的模板文件。

---

你是一名运维 agent。请在下面的 VPS 上从零部署一套多账号代理节点，
架构与配置模板见本仓库（先读 `docs/architecture.md`、
`docs/deployment.md`、`docs/accounting.md` 再动手）。

**目标环境**

- 服务器：`[IP]`，SSH 端口 `[端口]`，登录用户 `[用户]`（密钥登录）
- 系统：Ubuntu 24.04，`[核数]` 核 `[内存]`，月流量额度 `[额度]`
- 节点域名：`[node.example.com]`（DNS 已可控，证书走 DNS-01）
- 账号清单：`[alice_mac / alice_desktop / alice_iphone / bob]`
- 住宅代理：`[地址:端口 用户名 密码]`（没有则跳过所有住宅链步骤）

**要完成的事**

1. 基础：系统更新、BBR、放入 `templates/sysctl/99-proxy-tuning.conf`、
   SSH 密钥登录、低权限用户 `fleet-panel` / `fleet-sub`；
2. 证书：acme.sh 以 DNS-01 签发并配置续期后自动分发；
3. Hysteria2 两实例（443 直连 + 2054 住宅链）与端口跳跃，按
   `templates/hysteria/` 与 `templates/systemd/`；
4. Xray 按 `templates/xray/config.json`：2053/2054 TLS 入站、443
   Reality 入站（每账号两个 UUID：直连与 `-ws` 住宅身份）、8443 备选
   dest 入站、StatsService 开启；
5. 订阅服务：为每个账号生成订阅（Clash YAML 从
   `templates/clash/clash.yaml` 改；iPhone 账号另给 sub.txt），组装
   `payload.json`，token 用 `openssl rand -hex 32` 生成；装自愈脚本；
6. 面板：装 `panel/` 三件套并设密码，写 `links.json`，采集器首跑；
7. 防火墙：云安全组与 ufw 按 `docs/deployment.md` 的清单放行。

**纪律（必须遵守）**

- 所有密码/UUID/Reality 密钥/token 在服务器上用强随机生成，只写入服
  务器本地凭据文件（权限 600）；**不要在对话里明文回显任何凭据**，
  汇报时只说"已生成并存于 <路径>"；
- 每完成一步先验证再继续：服务 `systemctl is-active`、配置先过
  `xray run -test` / YAML 语法检查；
- 验收必须从**另一台机器**跨机执行 `docs/deployment.md` 第 7 节的清
  单，本机回环测试不算数；
- 住宅链最后验 UDP：应被拒绝且不得漏出机房 IP；
- 遇到与模板不一致的现场情况先停下来报告，不要自行改架构。

**汇报格式**

完成后按"服务状态 / 各通道出口实测 / 订阅链接（只给账号名与文件名，
不给完整 token）/ 面板地址 / 遗留问题"五段汇报。
