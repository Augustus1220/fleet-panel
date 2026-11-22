# 基础版安装器（install.sh）

一条命令在一台干净的 Ubuntu 24.04 VPS 上装好整套节点。面向已经会买
VPS、会配 DNS 的人；购买 VPS、域名注册、云厂商安全组不在安装器范围
内（安全组无法从 VPS 内部配置，安装器只做检测和清单提示）。

## 用法

先把一个子域名的 A 记录指向 VPS 的公网 IP（Cloudflare 请用 DNS only），
然后在仓库根目录：

```bash
sudo ./install.sh --domain node.example.com
```

常用选项：

| 选项 | 说明 |
| --- | --- |
| `--accounts a,b,c,d` | 1–4 个账号槽位名（默认沿用模板名 alice_mac 等）；凭据全部随机生成 |
| `--cert http` | 默认。HTTP-01 签发，要求入站 TCP 80 可达；签发后可关闭 80 |
| `--cert dns-cf` | DNS-01（环境变量 `CF_Token`），80/443 被商家过滤时用 |
| `--panel loopback` | 默认。面板只绑 127.0.0.1，用 SSH 隧道访问（见结尾输出） |
| `--panel public` | 面板绑公网 2096（自行承担暴露面；会话规则见 security-review） |
| `--residential HOST:PORT:USER:PASS` | 高级项：同时装住宅出口链；不给则不装 |
| `--quota-gb N` | 订阅里展示的流量额度（默认 1000） |
| `--dry-run` | 只打印动作；渲染结果落在 /tmp/fleet-dryrun 供检查 |

## 安装器做什么

1. **预检**：系统版本、DNS 解析与本机公网 IP 一致、所需端口空闲；
   打印云安全组应放行的入站清单（TCP 443、UDP 443、UDP 20000-30000、
   TCP 2053、TCP 2097、SSH；面板 loopback 时不需要 2096）。
2. 下载固定版本的 Hysteria（v2.14.0）与 Xray（v26.11.9）官方二进制。
3. 渲染全部配置：账号密码、双 UUID、Reality 密钥、统计 secret、订阅
   token 均为随机生成，只落在服务器 `/etc/fleet-panel/credentials.json`
   （600），不打印、不回写仓库。
4. acme.sh 签发证书并装到 `/opt/fleet-hy2/`，续期由 acme.sh 自带 cron
   完成，续期后自动重启相关服务。
5. 建低权限用户、装 systemd 单元（含 xray.service）、sysctl 调优与
   BBR、本机 ufw 若已启用则同步放行。
6. 启动后自检：各服务 active、Xray 配置自检、面板回环可达且未绑公网
   （loopback 模式）、订阅链接可拉取。

## 幂等

再次运行安装器时，若 `/etc/fleet-panel/credentials.json` 已存在，只
打印节点状态与订阅链接并以 0 退出，不改任何凭据与配置。

## 已知边界（v1）

- 未在全新 VPS 上做过完整彩排前，不要把它当成生产唯一路径；
  `docs/deployment.md` 的手工流程仍是基准。
- 未装住宅链时，面板服务列表中 `fleet-hy2-webshare` 显示未运行，属
  预期；采集器对缺失的住宅配置只跳过不报错的部分以实测为准。
- 订阅的 shadowrocket 规则文件（shadowrocket-conf 路由）不在基础版
  生成范围内，需要时按 docs/subscriptions.md 手工补。
- userinfo 的到期时间默认装机后 30 天，续费/延期后改
  `/var/lib/fleet-subscriptions/userinfo.json` 并重启订阅服务。
- 面板经 SSH 隧道访问：`ssh -L 2096:127.0.0.1:2096 root@<域名>`，
  然后打开 https://127.0.0.1:2096 （证书对应节点域名，浏览器会提示
  名称不匹配，属预期；介意可改用 --panel public 或自备 hosts 解析）。
