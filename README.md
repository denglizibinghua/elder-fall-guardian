# elder-fall-guardian

Raspberry Pi + mmWave radar elder fall monitor (edge): LD2450 + MPU6050 fusion, offline-first, local dashboard.

> 状态：L1 闭环 + 判据校准完成（2026.10.14），transition 判据已上线。
> 一句话：LD2450 看人在哪，按「活动 → 速度骤停 → 持续静止」的边缘特征判摔；DHT11/MPU6050/MAX30102 采环境体征，大按键一键求助；判定与落盘全部在设备端，断网报警能力零损失，局域网移动看板。

## 1. 系统判据（核心）

判决式：**末次运动帧之后 3 秒内出现静止沿，静止再持续满 3 秒 → FALL**。

判据由板载数据驱动，完成了四轮校准（详见 `docs/误报率记录表.md` 与 `fall_fusion.py` 内嵌注释）：

1. **径向速度盲区**：实测 90s，人体移动期间 LD2450 径向速度读数恒 0~8 cm/s（侧向走动径向分量 ≈ 0）→ 运动判据迁移至**两帧坐标位移速度**；
2. **身份门控**：多反射体槽位瞬移（1m 跳变伪装成幻影运动帧）→ **最近邻关联 + 单帧 600mm 位移门**，超限按身份丢失处理，永不瞬移；
3. **噪声免疫**：EMA 慢攻击（0.15 / 0.85）使孤立噪声帧够不到阈值，真实步行 0.3s 内爬升；带符号径向速度移出判定管道（仅展示）；
4. **阈值校准**：运动/静止阈值按实测走速标定（详见第 2 节）。

误报治理实测（板载 `alarms.jsonl` 真值）：改造前 8 分钟 14 条误报 → 治理后日常使用接近于零。

## 2. 当前参数（rules.json）

```json
{"alarm_mode":"transition","active_speed_threshold":50.0,"alarm_cooldown":30.0,"fusion_window":10.0,
 "spike_threshold":25000,"still_speed_threshold":50.0,"stillness_duration":3.0,"transition_window":3.0}
```

⚠️ **改 rules.json 后必须重启主程序才生效**：`load_rules()` 只在 `main.py` 启动时调用一次，不是热重载。

```bash
sudo systemctl restart food_project.service
```

### 阈值调校记录（2026-10-20）

阈值由 100 cm/s 下调至 50 cm/s。实测说明：人在雷达前正常活动时，EMA 平滑后的位移速度峰值约 38.7 cm/s。
若发现跌倒判定不触发，可继续下调 `active_speed_threshold`；若误报增多则上调。

保留 **fusion 模式**（MPU 冲击尖峰 + 雷达静止 融合）为可一键切回的备选判据（修改 `alarm_mode`）；50Hz 板载实测证明该安装位下人体冲击到板仅 ±0.09g、与日常振动不可分，故暂以 transition 为主判据（诚实边界，见 `docs/架构说明.md`）。

## 3. 系统链路

```
LD2450(UART2, 坐标/径向速度) + LD2410(UART4, 存在) + MPU6050(I2C, 计步)
  → fall_fusion.FallDetector(位移判据 + 身份门控, rules.json 可调)
  → FALL: alarms.jsonl 落盘 + 摄像头抓拍 + 看板横幅（"已知晓"闩锁）
DHT11(GPIO5 温湿度) + MAX30102(软I2C bus4 心率血氧) + 大按键(GPIO17 立即报警)
  → main.py 每 0.5s 原子写 status.json → webapp/app.py(:8080) 浏览器 0.6s 轮询
```

- 主程序 `main.py`（板端权威版本）：4 个传感器线程 + 主循环 + 判据挂接 + 状态快照
- 判据模块 `fall_fusion.py`：纯逻辑、零硬件依赖、可离线自测（`python fall_fusion.py`，9/9 用例）
- 传感器驱动 `max30102.py` / `hrcalc.py`：MAX30102 I2C 驱动与 PPG 心率血氧算法
- 看板 `webapp/app.py`：纯标准库 HTTP + 内嵌移动端页面 + CSV 报警导出
- 工具 `tools/`：判据校准的实测脚本（50Hz MPU 冲击谱、90s 位移速度谱等）+ 硬件自检脚本

## 4. Web 演示（四步）

浏览器开 `http://<树莓派IP>:8080`（同局域网任意设备）：

1. **存在**：走过 → 坐标动，挥手 0.4m 跟手
2. **跌倒模拟**：快走两步 → 骤停躺平 → 横幅报警 + 落盘 + 自动抓拍
3. **按键求助**：按按钮 → 立即报警落盘
4. **断网加分动作**：关热点重演 → 本地照样判定落盘 → 重开后报警记录完整可查

## 5. 网络与部署

- 双服务自启：`food_project.service`（主程序）+ `webapp.service`（看板）
- 代码风格纯标准库（除 `requirements.txt` 所列外无第三方依赖）
- MQTT：主程序内置报警/状态主题发布，接入 Broker 即启用（预留对外集成，默认本机 broker 无订阅者）

### 树莓派 IP 会漂移

WiFi 重连后 DHCP 可能换 IP。按 MAC `2c-cf-67-5f-76-e9` 在局域网里定位即可。

## 6. 踩坑记录（血泪浓缩）

- 双服务打架 → 停用冗余后台，CPU 从 2×100% 降回单进程
- SSH Permission denied ≠ 密码错，是 CPU 满载限流，晾 60s+ 再试
- 三脚有源按键 VCC 必须接 3V3（禁 5V），逻辑反转已在代码适配
- 面包板同排 5 孔内部直通，GPIO 与 GND 别落同排
- 派端脚本禁止 BOM（首行必死）
- LD2450 槽位会被反射体瞬移骗（多目标换位 = 幻影运动），必须有身份门控
- 板载 MPU 看不见人体（±0.09g@50Hz 实测）——安装位决定传感器能力，量力选判据
- **MAX30102 中断脚忙等烧满一个核**：`while GPIO.input(x)==1: pass` 会让 CPU 100%，必须加 `sleep` 轮询 + 超时
- **`PAGE` 是非 raw 三引号**：app.py 内联 JS 里写 `\"` 会被 Python 塌缩成 `"`，导致整个 `<script>` 静默失效（页面正常渲染但所有数值恒为 `—`）。JS 一律用单引号，改完必须对解析后的 PAGE 抽 script 跑 `node --check`
- **看板 innerHTML 每轮重建会杀掉图片加载**：`renderPhotos` 每 600ms 重建 20 张图的 DOM，图片来不及下载完就被销毁。用签名（文件名拼接）去重，只在列表变化时才重建

## 7. 已知边界

- 单平面雷达无高度通道：**"坐下"与"跌倒"同构**，统一为骤停事件判定（诚实标注）
- 桌载 MPU 与人体物理解耦：冲击证据不可用，计步功能保留但不参与判定
- 单目标追踪架构（单老人监护定位）：双目标在场时仅追踪已锁定目标
- LD2410 存在检测与 LD2450 目标数可能不一致（存在雷达灵敏度/安装位差异）

## 8. License

MIT
