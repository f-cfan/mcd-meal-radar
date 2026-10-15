# 定时部署指南

四档任务：09:00 领券 · 11:00 午餐 · 16:30 晚餐 · 20:00 过期提醒。

本脚本不自带常驻进程。以下三种方式任选其一。

---

## 方式一：系统 cron（Linux / macOS）

先确认 Python 环境可用：

```bash
cd /path/to/mcd-meal-radar
python demo.py          # 先跑通再配 cron
```

生成配置：

```bash
python scheduler.py --install-cron
```

写入 crontab：

```bash
crontab -e
```

粘贴以下内容（把路径换成实际路径）：

```cron
# 麦麦饭点雷达 · 北京时间
0  9  * * *  cd /path/to/mcd-meal-radar && python scheduler.py --live --now morning >> logs/cron.log 2>&1
0  11 * * *  cd /path/to/mcd-meal-radar && python scheduler.py --live --now lunch   >> logs/cron.log 2>&1
30 16  * * *  cd /path/to/mcd-meal-radar && python scheduler.py --live --now dinner  >> logs/cron.log 2>&1
0  20 * * *  cd /path/to/mcd-meal-radar && python scheduler.py --live --now expiry  >> logs/cron.log 2>&1
```

⚠️ **时区**：crontab 用系统时区。若服务器不在 UTC+8，请自行换算：

```bash
date                          # 确认系统时区
sudo timedatectl               # Linux 查看
```

macOS 上 crontab 若在休眠期间错过执行点，可改用 `launchd`（见方式三）。

---

## 方式二：WorkBuddy 自动化任务（推荐）

本项目的开发环境即 WorkBuddy，可直接用其自动化体系调度。

### 推荐做法：用 Agent Mail 发送（零配置）

**推送通道用 Agent Mail**（见 README 双通道说明）—— 在 WorkBuddy 里无需 SMTP 授权码。
把下面提示词里的「推送」一步改为：

> 生成 out/lunch.html 后，用 Agent Mail 把它发到我的邮箱，标题「麦麦饭点雷达 · 午间建议」。

WorkBuddy 会自动用内置的 Agent Mail 能力完成发送。

### 提示词示例（若走 scheduler.py 脚本）

**任务 1 · 早间领券（09:00）**

```
执行 mcd-meal-radar 的早间领券任务。

1. cd 到项目目录
2. 运行 python scheduler.py --live --now morning --send
3. 读取 out/morning.txt 汇报本次领券结果
```

**任务 2 · 午餐建议（11:00）**

```
执行 mcd-meal-radar 的午餐建议任务。

1. 运行 python scheduler.py --live --now lunch --send
2. 汇报推荐餐品、价格、营养数据与用券情况
3. 若输出中出现"需要重新授权"，明确告知用户需手动更新 Token
```

**任务 3 · 晚餐建议（16:30）** —— 同上，把 `lunch` 换成 `dinner`

**任务 4 · 过期提醒（20:00）**

```
执行 mcd-meal-radar 的过期提醒任务。

1. 运行 python scheduler.py --live --now expiry --send
2. 汇报即将过期的券与积分
```

优势：失败可重跑、有执行记录、不受本机休眠影响。

---

## 方式三：macOS launchd（抗休眠，推荐给 Mac 用户）

`~/Library/LaunchAgents/com.mealradar.morning.plist`：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.mealradar.morning</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>/path/to/mcd-meal-radar/scheduler.py</string>
    <string>--live</string><string>--now</string><string>morning</string>
    <string>--send</string>
  </array>
  <key>WorkingDirectory</key><string>/path/to/mcd-meal-radar</string>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>9</integer><key>Minute</key><integer>0</integer></dict>
  <key>StandardOutPath</key><string>/path/to/mcd-meal-radar/logs/morning.log</string>
  <key>StandardErrorPath</key><string>/path/to/mcd-meal-radar/logs/morning.err</string>
</dict>
</plist>
```

加载：

```bash
launchctl load ~/Library/LaunchAgents/com.mealradar.morning.plist
```

优势：系统休眠结束后会补跑错过的任务，Mac 上比 cron 可靠。

---

## Token 失效怎么办

这是定时任务必然遇到的情况 —— 官方未公布 MCP Token 有效期天数，实测无 Token 时端点会拒绝请求。

本项目已实现降级：检测到 401 会发一封「需要重新授权」邮件，并附缓存快照，**不会静默失败**。

你需要做的：

```
1. 手机号登录 https://open.mcd.cn/mcp
2. 控制台 → 激活 → 复制新 Token
3. 更新 .env 中的 MCD_MCP_TOKEN
4. 重新触发一次任务（launchd 下 unload/load，或手动跑一次）
```

第 4 步的原因是部分进程会缓存环境变量。若使用 WorkBuddy 自动化，任务本身是新进程，通常无需额外处理。

---

## 日志与排错

```bash
# 手动跑一次指定档位（不发邮件）
python scheduler.py --live --now lunch

# 看 SMTP 配置是否被识别（不打印授权码明文）
python -c "import sys; sys.path.insert(0,'src'); from mail import MailConfig; print(MailConfig().describe())"

# 验证 MCP Token 是否有效
python -c "
import sys; sys.path.insert(0,'src')
from mcd_adapter import McdMcpClient, MCPError
c = McdMcpClient()
try:
    print('Token 有效，账户积分：', c.call_tool('query-my-account', {}).get('data', {}).get('available'))
except MCPError as e:
    print('Token 不可用：', e, '(code=%s)' % e.code)
"
```

---

## 自定义档位时间

档位定义在 `scheduler.py` 的 `SLOTS` 常量：

```python
SLOTS = [
    Slot("morning", "早间总览", 9, 0, auto_claim=True),
    Slot("lunch",   "午间",     11, 0),
    Slot("dinner",  "晚间",     16, 30),
    Slot("expiry",  "过期提醒", 20, 0),
]
```

改时间直接改这里；增删档位同理（`--now` 的可选值会自动跟随）。
