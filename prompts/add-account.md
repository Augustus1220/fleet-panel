# 提示词：新增一个账号

把下面整段发给你的 AI agent（替换 `[方括号]` 内容）。

---

你是一名运维 agent。给现有节点新增一个账号 `[carol]`（设备后缀按需，
如 `carol_iphone`），架构见本仓库 `docs/`。涉及四处，必须全部完成
并逐项验证：

1. **HY2 两实例**（`/opt/fleet-hy2/server.yaml`、
   `/opt/fleet-hy2-webshare/server.yaml`）的 `auth.userpass` 各加一条，
   密码强随机生成；重启两实例并确认 active；
2. **Xray**：2053/2054 两入站各加一个 client（email=新账号名）；
   Reality 入站（443 与 8443）各加**两个** client：`[设备名]` 与
   `[设备名]-ws`，使用两个新 UUID；`xray run -test` 通过后重启；
   同步更新采集器的 `XRAY_EMAIL_MAP`（新 email → 新账号名）；
3. **订阅**：生成该账号的订阅文件并加入 `payload.json`（新 token），
   按 `docs/subscriptions.md` 的流程重启订阅服务并抓取验证；
4. **面板**：`links.json` 增加该账号链接；等一轮采集后确认"各账号用
   量"里出现新账号。

纪律：新凭据只写入服务器本地凭据文件（600），对话里不回显明文；每步
改配置前先备份原文件；最后从另一台机器跨机验证新账号每个通道的出口
IP（直连=机房 IP，住宅=住宅 IP）。汇报只列完成项与验证结果。
