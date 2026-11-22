# panel/ — 只读状态面板

单文件 Python（仅标准库）面板 + 采集器。

## 文件

| 文件 | 作用 |
| --- | --- |
| `panel.py` | HTTPS 面板服务：密码登录、片段化刷新、流量曲线 |
| `collect.py` | 采集器：每 2 分钟汇总 HY2/Xray 统计、服务与证书状态、住宅代理用量，写快照与历史样本 |
| `setpw.py` | 设置面板密码（从 stdin 读，写 PBKDF2 记录到 `/etc/fleet-panel/password.json`） |

## 运行时文件（不入库）

- `/etc/fleet-panel/config.json`：`{"cert": "/opt/fleet-panel/public-tls.crt", "key": "/opt/fleet-panel/public-tls.key", "port": 2096}`
- `/etc/fleet-panel/password.json`：由 `setpw.py` 生成
- `/etc/fleet-panel/webshare.key`：住宅代理的 API key（可选，用于面板
  展示用量；权限 600）
- `/var/lib/fleet-panel/status.json`：采集器快照（面板的数据源）
- `/var/lib/fleet-panel/history.sqlite3`：90 天历史样本与事件
- `/var/lib/fleet-panel/links.json`：订阅链接展示清单，
  格式 `{"items": [{"label": "Alice · Mac", "url": "https://..."}]}`

## 部署

见仓库 `docs/deployment.md` 第 6 节与 `templates/systemd/fleet-panel*`。
面板进程以低权限用户运行，除"触发一次新采集"外不写任何业务数据。
