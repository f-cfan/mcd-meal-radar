"""SMTP 推送层。

渠道说明（合规）：本项目使用**标准 SMTP**，收件邮箱由用户自行配置。
示例配置为 QQ 邮箱，但代码不绑定任何邮箱服务商。

密钥来源：优先读环境变量，其次读本地 .env（已被 .gitignore 排除）。
绝不在代码或日志中出现真实授权码。
"""

from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage
from email.utils import formataddr, formatdate


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


class MailConfig:
    """SMTP 配置。全部来自环境变量，代码内无任何默认密钥。"""

    def __init__(self, env: dict[str, str] | None = None) -> None:
        e = env or load_env()
        get = lambda k: os.environ.get(k) or e.get(k, "")
        self.host: str = get("SMTP_HOST") or "smtp.qq.com"
        self.port: int = int(get("SMTP_PORT") or 465)
        self.user: str = get("SMTP_USER")
        self.password: str = get("SMTP_PASSWORD")   # 授权码，非登录密码
        self.sender: str = get("SMTP_SENDER") or self.user
        self.sender_name: str = get("SMTP_SENDER_NAME") or "麦麦饭点雷达"
        self.to: str = get("RADAR_TO") or self.user
        self.use_ssl: bool = str(get("SMTP_USE_SSL") or "1") not in ("0", "false", "False")

    @property
    def configured(self) -> bool:
        return bool(self.user and self.password and self.to)

    def describe(self) -> str:
        """脱敏描述，用于日志。绝不打印授权码。"""
        return (f"SMTP {self.host}:{self.port} | 发件 {self.sender} "
                f"| 收件 {self.to} | 授权码 {'已配置' if self.password else '未配置'}")


def send_html(
    subject: str,
    html_body: str,
    config: MailConfig,
    text_body: str = "",
) -> tuple[bool, str]:
    """发送 HTML 邮件。返回 (是否成功, 说明)。

    失败不抛异常 —— 定时任务里推送失败不该中断主流程，
    调用方记录日志后继续。
    """
    if not config.configured:
        return False, "SMTP 未配置：请设置 SMTP_USER / SMTP_PASSWORD / RADAR_TO 环境变量"

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((str(Header_Charset(config.sender_name)),
                              config.sender))
    msg["To"] = config.to
    msg["Date"] = formatdate(localtime=True)
    msg.set_content(text_body or "本邮件含 HTML 内容，请使用支持 HTML 的客户端查看。")
    msg.add_alternative(html_body, subtype="html")

    try:
        if config.use_ssl:
            server = smtplib.SMTP_SSL(config.host, config.port, timeout=30)
        else:
            server = smtplib.SMTP(config.host, config.port, timeout=30)
            server.starttls()
        with server:
            server.login(config.user, config.password)
            server.send_message(msg)
        return True, f"已发送至 {config.to}"
    except smtplib.SMTPAuthenticationError:
        return False, "SMTP 鉴权失败：授权码无效或未开启 SMTP 服务"
    except smtplib.SMTPRecipientsRefused:
        return False, f"收件人被拒绝：{config.to}"
    except (smtplib.SMTPException, OSError) as exc:
        return False, f"SMTP 发送失败：{type(exc).__name__}"


def Header_Charset(name: str) -> str:
    """邮件头编码：中文发件人名需 RFC 2047 编码，否则部分客户端乱码。"""
    try:
        name.encode("ascii")
        return name
    except UnicodeEncodeError:
        from email.header import Header
        return str(Header(name, "utf-8"))
