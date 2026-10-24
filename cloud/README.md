# cloud/ — 云端部署资产（脱敏样例）

「老人跌倒监护」项目云端部分的可复现素材：树莓派边缘端通过 MQTT(TLS) 上报到云端，
云端做**数据留档 + 外网查看看板**；云端不参与实时判定（断网时边缘端本地照常判定与落盘）。

> 本目录为**脱敏样例**：不含任何凭据、公网 IP 或真实域名（示例域名 `monitor.example.com` 请自行替换）。
> 实际部署的环境细节见私有记录。

## 文件

- `elder-bridge.py` — 云端桥接器：订阅 `elder/v1/#` → MySQL 留档（`event_id` 幂等去重）+ 生成板端同格式文件（`status.json` / `alarms.jsonl`）供看板读取；任何消息刷新在线计时；断线自动重连。
- `nginx/elder-guardian-8080.conf` — 备用入口（:8080，反代云端 UI，WebSocket 直通）。
- `nginx/elder-guardian-monitor.conf` — 主入口（HTTPS + Let's Encrypt 证书 + 反代 + WS；含 80 跳转与 ACME 验证路径样例）。

## 部署要点（摘要）

1. **MQTT broker（mosquitto）**：TLS :8883，独立账号 + ACL + 禁匿名。板端**零代码改动**，仅通过 systemd 环境变量切换：
   `MQTT_HOST / MQTT_PORT / MQTT_TLS / MQTT_USER / MQTT_PASSWORD`（自签 CA 装入板子系统信任库即可通过校验）。
2. **桥接器**：`/opt/elder-bridge/`（venv：paho-mqtt + PyMySQL）+ systemd `elder-bridge.service`（`bridge.env` 提供连接参数，勿入库）。
3. **云端 UI**：板端 `app.py` 原样部署（仅改监听端口），nginx 反代 + WebSocket 直通。
   ⚠️ `app.py` 必须与 `ws.py` 同目录（缺 `ws.py` 会走降级路径导致 WebSocket 一连就断——画面表现为数据停留在初始快照，酷似"数据陈旧"）。
4. **HTTPS**：certbot webroot 模式签发 Let's Encrypt 证书，自动续期（`--deploy-hook "systemctl reload nginx"`）。
5. 板端完整代码见仓库根目录与 `board-snapshot-*` 分支（本目录不含板端代码）。

## 验证要点

- **WS 帧级测试**（原始 socket 读帧，不只测握手）：确认实时推送真帧到达，握手 101 ≠ 通道可用。
- 断 broker 45s → 恢复后约 19s 自动续传（板端 outbox 兜底，零人工）。
- 事件幂等：同 `event_id` 发布 2 次 → 数据库 1 行。
