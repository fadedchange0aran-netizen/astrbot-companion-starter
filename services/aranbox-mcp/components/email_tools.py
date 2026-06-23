# 文件名: components/email_tools.py (中央档案馆终极版)
import os
import smtplib
import imaplib
import email
from email import encoders
from email.header import decode_header
from email.mime.base import MIMEBase
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# 发信功能保持不变
def send_email_zoho(recipient: str, subject: str, body: str) -> str:
    # ... 此处代码与之前完全相同，无需改动 ...
    sender_email = os.getenv("ZOHO_EMAIL")
    app_password = os.getenv("ZOHO_APP_PASSWORD")
    if not sender_email or not app_password: return "❌ 邮件服务尚未配置 ZOHO_EMAIL / ZOHO_APP_PASSWORD。"
    message = MIMEMultipart()
    message["From"] = f"Companion <{sender_email}>"
    message["To"] = recipient
    message["Subject"] = subject
    message.attach(MIMEText(body, "plain", "utf-8"))
    try:
        with smtplib.SMTP_SSL("smtp.zoho.com", 465) as server:
            server.login(sender_email, app_password)
            server.sendmail(sender_email, recipient, message.as_string())
        return "✅ 邮件已成功寄出。"
    except Exception as e:
        return f"❌ 邮件发送失败: {e}"


def send_email_with_attachment_zoho(
    recipient: str,
    subject: str,
    body: str,
    attachment_path: str,
    attachment_name: str = "",
) -> str:
    sender_email = os.getenv("ZOHO_EMAIL")
    app_password = os.getenv("ZOHO_APP_PASSWORD")
    if not sender_email or not app_password:
        return "❌ 邮件服务尚未配置 ZOHO_EMAIL / ZOHO_APP_PASSWORD。"

    resolved_path = os.path.abspath(str(attachment_path or "").strip())
    if not resolved_path or not os.path.isfile(resolved_path):
        return f"❌ 附件不存在：{attachment_path}"

    message = MIMEMultipart()
    message["From"] = f"Companion <{sender_email}>"
    message["To"] = recipient
    message["Subject"] = subject
    message.attach(MIMEText(body, "plain", "utf-8"))

    try:
        with open(resolved_path, "rb") as handle:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(handle.read())
        encoders.encode_base64(part)
        part.add_header(
            "Content-Disposition",
            f'attachment; filename="{attachment_name or os.path.basename(resolved_path)}"',
        )
        message.attach(part)

        with smtplib.SMTP_SSL("smtp.zoho.com", 465) as server:
            server.login(sender_email, app_password)
            server.sendmail(sender_email, recipient, message.as_string())
        return "✅ 附件邮件已成功寄出。"
    except Exception as e:
        return f"❌ 附件邮件发送失败: {e}"

# 改造“收信管家”，让他学会编目
def read_emails_zoho(folder: str = "INBOX", count: int = 5) -> str:
    user_email = os.getenv("ZOHO_EMAIL")
    app_password = os.getenv("ZOHO_APP_PASSWORD")
    if not user_email or not app_password: return "❌ 邮箱读取服务尚未配置 ZOHO_EMAIL / ZOHO_APP_PASSWORD。"

    try:
        mail = imaplib.IMAP4_SSL("imap.zoho.com")
        mail.login(user_email, app_password)
        mail.select(f'"{folder}"') # 使用引号以支持带空格的文件夹名
        status, data = mail.search(None, "ALL")
        mail_ids = data[0].split()
        if not mail_ids: return f"📬 邮箱文件夹 [{folder}] 里当前没有邮件。"
        
        results = []
        # 从最新的邮件开始读取
        for i in reversed(mail_ids[-count:]):
            status, msg_data = mail.fetch(i, "(RFC822)")
            for response_part in msg_data:
                if isinstance(response_part, tuple):
                    msg = email.message_from_bytes(response_part[1])
                    subject = decode_header(msg["Subject"])[0][0]
                    if isinstance(subject, bytes): subject = subject.decode()
                    
                    # 🏛️【核心升级】：为每封邮件提取唯一的 Message-ID
                    message_id = msg.get("Message-ID")
                    
                    results.append(f"【标题】: {subject}\n【邮件ID】: {message_id}\n---")
        
        mail.logout()
        return f"📬 在[{folder}]里找到最近的 {len(results)} 封邮件档案:\n\n" + "\n".join(results)
    except Exception as e:
        return f"❌ 读取邮件列表失败: {e}"

# 新增“档案管理员”，让他学会按ID取件
def read_specific_email_zoho(message_id: str) -> str:
    user_email = os.getenv("ZOHO_EMAIL")
    app_password = os.getenv("ZOHO_APP_PASSWORD")
    if not user_email or not app_password: return "❌ 邮箱读取服务尚未配置 ZOHO_EMAIL / ZOHO_APP_PASSWORD。"
    if not message_id: return "❌ 请提供邮件ID！"
    
    try:
        mail = imaplib.IMAP4_SSL("imap.zoho.com")
        mail.login(user_email, app_password)
        mail.select("INBOX")
        
        # 🏛️【核心魔法】：通过 Message-ID 搜索邮件
        # 注意：移除 Message-ID 两边的尖括号
        clean_message_id = message_id.strip().lstrip('<').rstrip('>')
        status, data = mail.search(None, 'HEADER', 'Message-ID', f'"{clean_message_id}"')
        
        if not data[0]:
            return f"❌ 档案馆里没有找到ID为 {message_id} 的邮件。"
        
        latest_id = data[0].split()[-1]
        status, msg_data = mail.fetch(latest_id, "(RFC822)")
        msg = email.message_from_bytes(msg_data[0][1])
        
        subject = decode_header(msg["Subject"])[0][0]
        if isinstance(subject, bytes): subject = subject.decode()

        # 读取完整正文
        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    body = part.get_payload(decode=True).decode()
                    break
        else:
            body = msg.get_payload(decode=True).decode()
            
        mail.logout()
        return f"【标题】: {subject}\n\n【完整内容】:\n\n{body}"
    except Exception as e:
        return f"❌ 档案管理员在查找时摔倒了: {e}"
