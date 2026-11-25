# 多主机管理

小主机可只运行标准库版面板和采集器。有多台主机时，可选部署一个主管理器统一查看总览、流量、事件和主机管理。主管理器保存最近 90 天的采样，可按账号查看 24 小时、7 天和 30 天的增量。日期和流量周期采用 UTC+8。上报包含资源、服务、证书状态、流量计数及事件，凭据和订阅链接留在各节点。

## 主管理器

以下以 Ubuntu 24.04 和 systemd 为例。在仓库根目录执行：

```bash
sudo apt install python3-venv
id fleet-panel-manager >/dev/null 2>&1 || sudo useradd --system --user-group --shell /usr/sbin/nologin fleet-panel-manager
sudo install -d -m 0755 /opt/fleet-panel-manager
sudo cp -R common manager /opt/fleet-panel-manager/
sudo python3 -m venv /opt/fleet-panel-manager/.venv
sudo /opt/fleet-panel-manager/.venv/bin/python -m pip install -r manager/requirements.txt
sudo install -d -m 0750 -o fleet-panel-manager -g fleet-panel-manager /var/lib/fleet-panel-manager
sudo mkdir -p /etc/fleet-panel
sudo /opt/fleet-panel-manager/.venv/bin/python - <<'PY'
from getpass import getpass
import json
import os
from pathlib import Path
import secrets
from werkzeug.security import generate_password_hash

target = Path("/etc/fleet-panel/manager.json")
if target.exists():
    raise SystemExit("配置已存在，请保留现有文件")
password = getpass("主管理器密码（至少 16 个字符）：")
if not 16 <= len(password) <= 512 or password != getpass("再输入一次："):
    raise SystemExit("密码不符合长度要求或两次输入不一致")
os.umask(0o077)
with target.open("x", encoding="utf-8") as config:
    json.dump({
        "session_key": secrets.token_urlsafe(48),
        "password_hash": generate_password_hash(password),
        "database": "/var/lib/fleet-panel-manager/manager.sqlite3",
        "behind_proxy": True,
    }, config, indent=2)
PY
sudo chgrp fleet-panel-manager /etc/fleet-panel/manager.json
sudo chmod 0640 /etc/fleet-panel/manager.json
sudo install -m 0644 templates/systemd/fleet-panel-manager.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now fleet-panel-manager.service
```

Gunicorn 通过回环地址 `127.0.0.1:2098` 监听。用已有 HTTPS 反向代理转发一个独立域名的请求，包括 `/api/v1/reports`。示例适用于使用 Caddy 且能监听 443 的主管理主机：

```caddyfile
manager.example.com {
    reverse_proxy 127.0.0.1:2098
}
```

浏览器通过 HTTPS 登录。启用 `behind_proxy` 时，让一层可信代理设置 `X-Forwarded-For`，Gunicorn 仅接受回环连接。更换密码哈希后重启服务，现有登录会话随即失效。

## 轻量节点

新装节点使用 `install.sh` 后已有上报模块。已有节点需更新下列代码文件，保留服务器原有运行配置和历史库。若安装时把真实参数渲染进了 `collect.py`，更新时保留这些本机定制，也可将证书及服务参数迁到本机 `/etc/fleet-panel/collector.json` 的 `certs` 和 `services` 字段。更新后重启 `fleet-panel.service` 以加载单机监控 API，采集器在下次运行时自动加载新代码。

标准目录布局：

```
/usr/local/lib/fleet-panel/
├── collect.py
├── panel.py
├── common/
└── node/
```

1. 到主管理器的「主机管理」登记节点（如 `node-01`），复制一次性显示的上报令牌。
2. 在该节点创建专用上报用户及配置文件。将实际主机编号、HTTPS 地址与令牌填入 `/etc/fleet-panel/report.json`。

   ```bash
   sudo apt install acl
   id fleet-panel-report >/dev/null 2>&1 || sudo useradd --system --user-group --shell /usr/sbin/nologin fleet-panel-report
   sudoedit /etc/fleet-panel/report.json
   sudo chown root:fleet-panel-report /etc/fleet-panel/report.json
   sudo chmod 0640 /etc/fleet-panel/report.json
   ```

   ```json
   {
     "node_id": "node-01",
     "url": "https://manager.example.com/api/v1/reports",
     "token": "REPLACE_WITH_NODE_TOKEN"
   }
   ```

3. 首次采集后授予上报账号本机 SQLite 及其事务日志所需权限，并安装上报定时器。

   ```bash
   sudo systemctl start fleet-panel-collect.service
   sudo setfacl -m u:fleet-panel-report:rwx /var/lib/fleet-panel
   sudo setfacl -m u:fleet-panel-report:rw /var/lib/fleet-panel/history.sqlite3
   sudo install -m 0644 templates/systemd/fleet-panel-report.{service,timer} /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now fleet-panel-report.timer
   sudo systemctl start fleet-panel-report.service
   ```

4. 在主管理器确认「最近上报」和「最新采样」都在更新。实际发起一次代理请求，确认账号流量计数增加。

单机面板的 `/api/v1/nodes` 与 `/api/v1/nodes/<id>/{status,traffic,events}` 使用单机会话。可在单机的 `/etc/fleet-panel/config.json` 设置 `node_id` 来统一编号，默认是 `local`；上报账号使用独立令牌。上报失败时批次留在本机，由定时器重试。停用上报可关闭 `fleet-panel-report.timer` 并移除 `report.json`。

## 导入历史

先登记目标主机，再把该主机的 `history-export.sqlite3` 或 SQLite 一致性备份复制到主管理器可读的临时路径。在主管理器目录运行：

```bash
cd /opt/fleet-panel-manager
sudo -u fleet-panel-manager .venv/bin/python -m manager.import_history node-01 /path/to/history-export.sqlite3
```

重复导入会去重，同一时刻优先使用实时上报的计数。备份可能含账号标签与事件内容，应通过受控渠道传输，导入后清理临时副本。

## 本地测试

在仓库根目录的虚拟环境中执行：

```bash
python -m pip install -r manager/requirements.txt
python -m unittest discover -s tests -v
```
