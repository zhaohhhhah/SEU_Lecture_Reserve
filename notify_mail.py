#!/usr/bin/env python3
"""独立邮件小工具——给 seu-reserve.sh 的看门狗在“放弃重启”时发通知用。

不 import main.py（那会把浏览器拉起来、还会跑主流程），而是按
环境变量 QQ_EMAIL_ADDRESS / QQ_EMAIL_AUTH_CODE → 同目录 config.yaml 的
qq_email / qq_auth_code → main.py 里的字面量（旧版兼容）顺序取凭据。
凭据留空则静默跳过。

用法：python3 notify_mail.py "主题" "正文"
"""
import os
import re
import sys
import ssl
import smtplib
from email.mime.text import MIMEText


def _scrape(src, env_key, config_key):
    m = re.search(env_key + r'",\s*"([^"]+)"', src) or re.search(r'"' + config_key + r'":\s*"([^"]+)"', src)
    return m.group(1) if m else ""


def _from_yaml():
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")
    try:
        import yaml
        with open(p, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        return str(cfg.get("qq_email") or ""), str(cfg.get("qq_auth_code") or "")
    except Exception:
        return "", ""


def _creds():
    addr = os.environ.get("QQ_EMAIL_ADDRESS", "")
    code = os.environ.get("QQ_EMAIL_AUTH_CODE", "")
    if addr and code:
        return addr, code
    y_addr, y_code = _from_yaml()
    addr, code = addr or y_addr, code or y_code
    if addr and code:
        return addr, code
    try:  # 旧版兼容：凭据写死在 main.py 字面量里
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py")
        src = open(p, encoding="utf-8").read()
    except Exception:
        src = ""
    addr = addr or _scrape(src, "QQ_EMAIL_ADDRESS", "qq_email")
    code = code or _scrape(src, "QQ_EMAIL_AUTH_CODE", "qq_auth_code")
    return addr, code


def main():
    if len(sys.argv) < 3:
        print("usage: notify_mail.py <subject> <body>")
        return 2
    addr, code = _creds()
    if not addr or not code:
        print("⚠ 未找到 QQ 邮箱凭据，跳过发信")
        return 0
    msg = MIMEText(sys.argv[2], "plain", "utf-8")
    msg["Subject"] = sys.argv[1]
    msg["From"] = addr
    msg["To"] = addr
    for i in (1, 2, 3):
        try:
            with smtplib.SMTP_SSL("smtp.qq.com", 465,
                                  context=ssl.create_default_context(), timeout=15) as s:
                s.login(addr, code)
                s.sendmail(addr, [addr], msg.as_string())
            print(f"📧 已发信给 {addr}")
            return 0
        except Exception as e:
            print(f"📧 发送失败({i}/3): {e}")
            if i < 3:
                import time
                time.sleep(2 * i)
    return 1


if __name__ == "__main__":
    sys.exit(main())
