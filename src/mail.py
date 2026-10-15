"""推送层 —— 双通道设计（Agent Mail 优先，SMTP 备选）。

为什么优先用 Agent Mail
------------------------
Agent Mail 是 QQ 邮箱团队为 Agent 提供的专属邮箱服务（`agent.qq.com`）：

  ✅ 免 SMTP 授权码 —— 不需要 16 位短信验证，扫码即用
  ✅ 与个人邮箱隔离 —— 不用暴露私人邮箱密码或授权码
  ✅ 天然适配 Agent —— 官方一等公民，WorkBuddy 内置支持
  ✅ 额度充足 —— 每地址 1GB、每天 50 封，够每天 4 封推送

而自建 SMTP 需要：开启 IMAP/SMTP 服务 → 短信验证 → 拿 16 位授权码 → 填进配置。
**这个流程必须用户本人操作，第三方无法代替**（账号安全设计）。

因此本项目默认走 Agent Mail，SMTP 作为备选保留：

  MAIL_CHANNEL=agent  → 交给 Agent Mail（推荐，见 README「双通道推送」）
  MAIL_CHANNEL=smtp   → 自建 SMTP（适合无 Agent 的独立部署环境）
  MAIL_CHANNEL=auto   → 先 Agent，失败自动回落 SMTP（默认）

为什么仍保留 SMTP
------------------
1. 本项目要能在**没有 Agent 的环境**里独立跑起来（别人 clone 下来直接 cron）
2. 应急通道：Agent Mail 不可用时至少有兜底
3. 两者的能力差异本身就是一个可讲的技术点

合规说明：标准 SMTP，收件邮箱由用户自行配置；代码不绑定任何邮箱服务商。
密钥来源：优先环境变量，其次本地 .env（已被 .gitignore 排除）；日志中不出现明文。
"""

from __future__ import annotations

import os
import smtplib
import subprocess
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, formatdate
from shutil import which


def load_env(path: str = ".env") -> dict[str, str]:
    """读取本地 .env。不覆盖已存在的环境变量。"""
    env: dict[str, str] = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def _get(key: str, default: str = "") -> str:
    return os.environ.get(key) or load_env().get(key, default) or default


@dataclass
class PushResult:
    """推送结果。channel 说明实际走了哪条通道。"""

    ok: bool
    note: str
    channel: str = "none"
    address: str = ""


# --------------------------------------------------------------------------
# 通道一：Agent Mail（推荐）
# --------------------------------------------------------------------------

def agent_mail_available() -> bool:
    """检测 Agent Mail CLI 是否可用。

    注意：WorkBuddy **内置**的 Agent Mail 能力不一定暴露为 CLI。
    这里只检测 CLI；内置通道由 Agent 上层直接调用（见 README 双通道说明）。
    """
    return which("agently-cli") is not None


def push_via_agent(to: str, subject: str, html_body: str,
                   text_body: str = "") -> PushResult:
    """通过 Agent Mail CLI 发送。

    仅在 CLI 已安装时可用。WorkBuddy 内置通道不由本函数处理 ——
    那是 Agent 上层的职责，不在离线脚本的能力范围内。
    """
    cli = which("agently-cli")
    if not cli:
        return PushResult(
            ok=False,
            note="Agent Mail CLI 未安装。可在 WorkBuddy 中用「更多-我的邮箱」的内置"
                 "能力发送，或改用 MAIL_CHANNEL=smtp",
            channel="agent-mail",
        )

    cmd = [cli, "message", "send", "--to", to, "--subject", subject]
    try:
        proc = subprocess.run(
            cmd, input=html_body, text=True,
            capture_output=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        return PushResult(False, "Agent Mail 发送超时", "agent-mail", to)
    except Exception as exc:
        return PushResult(False, f"Agent Mail 调用失败：{type(exc).__name__}",
                          "agent-mail", to)

    if proc.returncode == 0:
        return PushResult(True, f"已通过 Agent Mail 发送至 {to}", "agent-mail", to)
    detail = (proc.stderr or proc.stdout or "").strip()[:160]
    return PushResult(False, f"Agent Mail 返回错误：{detail}", "agent-mail", to)


# --------------------------------------------------------------------------
# 通道二：自建 SMTP（备选）
# --------------------------------------------------------------------------

class SmtpConfig:
    """SMTP 配置。全部来自环境变量，代码内无任何默认密钥。"""

    def __init__(self) -> None:
        self.host: str = _get("SMTP_HOST", "smtp.qq.com")
        self.port: int = int(_get("SMTP_PORT", "465"))
        self.user: str = _get("SMTP_USER")
        self.password: str = _get("SMTP_PASSWORD")   # 授权码，非登录密码
        self.sender: str = _get("SMTP_SENDER") or self.user
        self.sender_name: str = _get("SMTP_SENDER_NAME", "麦麦饭点雷达")
        self.to: str = _get("RADAR_TO") or self.user
        self.use_ssl: bool = _get("SMTP_USE_SSL", "1") not in ("0", "false", "False")

    @property
    def configured(self) -> bool:
        return bool(self.user and self.password and self.to)

    def describe(self) -> str:
        """脱敏描述，用于日志。绝不打印授权码。"""
        return (f"SMTP {self.host}:{self.port} | 发件 {self.sender or '未配置'} "
                f"| 授权码 {'已配置' if self.password else '未配置'}")


def send_via_smtp(subject: str, html_body: str,
                  text_body: str = "") -> PushResult:
    """通过自建 SMTP 发送。失败不抛异常 —— 推送失败不该中断主流程。"""
    cfg = SmtpConfig()
    if not cfg.configured:
        return PushResult(
            False,
            "SMTP 未配置：需设置 SMTP_USER / SMTP_PASSWORD / RADAR_TO",
            "smtp")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((_header_charset(cfg.sender_name), cfg.sender))
    msg["To"] = cfg.to
    msg["Date"] = formatdate(localtime=True)
    msg.set_content(text_body or "本邮件含 HTML 内容，请使用支持 HTML 的客户端查看。")
    msg.add_alternative(html_body, subtype="html")

    try:
        if cfg.use_ssl:
            server = smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=30)
        else:
            server = smtplib.SMTP(cfg.host, cfg.port, timeout=30)
            server.starttls()
        with server:
            server.login(cfg.user, cfg.password)
            server.send_message(msg)
        return PushResult(True, f"已通过 SMTP 发送至 {cfg.to}", "smtp", cfg.to)
    except smtplib.SMTPAuthenticationError:
        return PushResult(
            False, "SMTP 鉴权失败：授权码无效或未开启 SMTP 服务", "smtp")
    except smtplib.SMTPRecipientsRefused:
        return PushResult(False, f"收件人被拒绝：{cfg.to}", "smtp")
    except (smtplib.SMTPException, OSError) as exc:
        return PushResult(False, f"SMTP 发送失败：{type(exc).__name__}", "smtp")


def _header_charset(name: str) -> str:
    """邮件头编码：中文发件人名需 RFC 2047 编码，否则部分客户端乱码。"""
    try:
        name.encode("ascii")
        return name
    except UnicodeEncodeError:
        from email.header import Header
        return str(Header(name, "utf-8"))


# --------------------------------------------------------------------------
# 统一入口
# --------------------------------------------------------------------------

def push(to: str, subject: str, html_body: str,
         text_body: str = "", channel: str = "") -> PushResult:
    """统一推送入口。**自动降级**：Agent Mail 失败时回落到 SMTP。

    降级是有意设计 —— 推送通道可能因额度、网络、授权过期等原因失败，
    但用户不该因此收不到提醒。
    """
    channel = channel or _get("MAIL_CHANNEL", "auto")
    to = to or _get("RADAR_TO") or _get("SMTP_USER")

    # 先校验收件地址：没有地址就不必跑通道，避免无谓的连接尝试
    if not to:
        return PushResult(False, "未配置收件地址：请设置 RADAR_TO 或 SMTP_USER")

    order = {
        "agent": ["agent"],
        "smtp": ["smtp"],
        "auto": ["agent", "smtp"],
    }.get(channel, ["agent", "smtp"])

    notes: list[str] = []
    for name in order:
        if name == "agent":
            res = push_via_agent(to, subject, html_body, text_body)
        else:
            res = send_via_smtp(subject, html_body, text_body)
        if res.ok:
            if notes:
                res.note = res.note + "；回落原因：" + "；".join(notes)
            return res
        notes.append(f"[{name}] {res.note}")

    return PushResult(False, "；".join(notes), "none", to)


def describe_channels() -> str:
    """通道状态摘要，用于日志与自检。绝不打印授权码。"""
    return (
        f"MAIL_CHANNEL={_get('MAIL_CHANNEL', 'auto')} | "
        f"Agent Mail CLI {'可用' if agent_mail_available() else '未安装（WorkBuddy 内置可用）'} | "
        f"{SmtpConfig().describe()}"
    )
