# 分订阅下发

每个账号一条独立链接，链接即钥匙。订阅服务（`subscription/server.py`）
是一个单文件 Python HTTPS 服务：启动时一次性读入 `payload.json` 建好
路由表，之后只做"按路径查表 → 解码 → 下发"，没有数据库、没有模板
引擎。

## payload 模型

```json
{
  "alice_mac":   {"token": "<64位随机hex>", "filename": "clash.yaml", "content": "<base64>"},
  "alice_iphone":{"token": "<64位随机hex>", "filename": "sub.txt",    "content": "<base64>"},
  "shadowrocket-conf": {"token": "<64位随机hex>", "filename": "shadowrocket.conf", "content": "<base64>"}
}
```

- 路由 = `/<token>/<filename>`，token 猜不到即安全边界，所以 token 只
  出现在 payload 和下发链接里，**永远不要提交进任何仓库**；
- `content` 统一以 base64 存储，服务下发前解码一层；
- 服务启动时读入，**改完 payload 必须重启服务**才生效。

## 三种下发形态

| 形态 | 文件 | 给谁 | 说明 |
| --- | --- | --- | --- |
| Clash 配置 | `clash.yaml` | 桌面/安卓（mihomo 内核） | 完整 YAML：节点、分组、规则、DNS |
| URI 列表 | `sub.txt` | iPhone 小火箭等 | 一行一条分享链接，整体再做一层 base64 |
| 规则文件 | `shadowrocket.conf` | 小火箭（全队共享一条） | **只含分流规则、不含节点**，节点链接另行下发 |

`sub.txt` 有个容易踩的约定：客户端期望收到的正文本身就是 base64（标
准订阅格式），而服务对所有路由统一"解码一层再下发"，所以 payload 里
存的必须是 **base64(base64(链接列表))**。生成时多套一层即可，验证时
抓下发正文解一层应得到链接明文。

## Subscription-Userinfo

`clash.yaml` 与 `sub.txt` 的响应带标准头：

```
Subscription-Userinfo: upload=<tx>; download=<rx>; total=<额度>; expire=<到期unix时间>
```

数值来自网卡记账（见 [accounting.md](accounting.md) 第 4 节），额度与
到期写在同目录的 `userinfo.json` 里——**服务器续费/换套餐后记得改
expire**，客户端显示的到期完全以这里为准。

## 改订阅的标准流程

1. 备份当前 `payload.json`；
2. 把目标路由的 `content` base64 解码成明文；
3. **行级编辑**明文（不要用 YAML 库整表重排，会打乱格式与注释）；
4. 重新 base64 编码写回，重启订阅服务；
5. 用真实链接抓一次下发内容验证：状态 200、节点数/规则数与预期一致、
   关键字段无误；
6. 有第二台机器时，从另一台机器跨机验证一次（本机回环测不全）。

分流规则只在一处维护：如果手机端用的是规则订阅（shadowrocket-conf），
改规则只改服务器 payload，客户端刷新订阅即可，不要再到处发配置文件。

## 链接管理

- 每个账号的链接在面板"订阅链接"页里集中展示（`links.json`，只存展
  示用的标签与完整 URL，页面默认打码，点按钮才复制）；
- 换服务器 IP 时链接不用变：订阅里节点写域名或可替换的 IP 字段，后台
  改 payload 的 server 地址、全员刷新订阅即可切换；
- 怀疑链接泄漏时，只需给对应路由换一个新 token，旧链接立即失效。
