"""
SMTP email for group invitations and monthly report delivery.

If SMTP_HOST is not set in .env, invites are saved as pending in the database
but no email is sent — use the invite link instead.
"""
import logging
import os
import smtplib
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)


def is_email_configured() -> bool:
    return bool(os.environ.get("SMTP_HOST", "").strip())


def _smtp_settings() -> tuple[str, int, str, str, str, bool]:
    host = os.environ.get("SMTP_HOST", "").strip()
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "")
    from_addr = os.environ.get("SMTP_FROM", user or "noreply@billbuddy.local")
    use_tls = os.environ.get("SMTP_TLS", "true").lower() in ("1", "true", "yes")
    return host, port, user, password, from_addr, use_tls


def _send_message(msg: MIMEMultipart, to_email: str) -> None:
    host, port, user, password, from_addr, use_tls = _smtp_settings()
    if not host:
        raise RuntimeError("SMTP is not configured")

    msg["From"] = from_addr
    msg["To"] = to_email

    with smtplib.SMTP(host, port, timeout=30) as server:
        if use_tls:
            server.starttls()
        if user and password:
            server.login(user, password)
        server.sendmail(from_addr, [to_email], msg.as_string())


def _period_label(period_key: str) -> str:
    year, month = period_key.split("-")
    from datetime import datetime
    return datetime(int(year), int(month), 1).strftime("%B %Y")


def send_password_reset_email(to_email: str, reset_url: str) -> bool:
    if not is_email_configured():
        return False

    subject = "Reset your BillBuddy password"
    text_body = f"""Hi,

We received a request to reset your BillBuddy password.

Click here to choose a new password:
{reset_url}

This link expires in 1 hour. If you didn't request this, you can ignore this email.

— BillBuddy
"""

    html_body = f"""<html><body style="font-family:sans-serif;color:#222;max-width:560px;">
<p>Hi,</p>
<p>We received a request to reset your BillBuddy password.</p>
<p><a href="{reset_url}" style="display:inline-block;padding:10px 18px;background:#2F5233;color:#fff;text-decoration:none;border-radius:6px;">Reset password</a></p>
<p>Or copy this link: <a href="{reset_url}">{reset_url}</a></p>
<p style="color:#666;font-size:13px;">This link expires in 1 hour. If you didn't request this, you can ignore this email.</p>
<p style="color:#666;font-size:13px;">— BillBuddy</p>
</body></html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        _send_message(msg, to_email)
        logger.info("Password reset email sent to %s", to_email)
        return True
    except Exception:
        logger.exception("Failed to send password reset email to %s", to_email)
        raise


def send_invite_email(
    to_email: str,
    group_name: str,
    invite_url: str,
    inviter_name: str,
) -> bool:
    if not is_email_configured():
        return False

    subject = f'You\'re invited to join "{group_name}" on BillBuddy'
    text_body = f"""Hi,

{inviter_name} invited you to join the expense group "{group_name}" on BillBuddy.

Click here to join:
{invite_url}

If you don't have an account yet, sign up with this email address ({to_email}) and you'll be added to the group automatically.

— BillBuddy
"""

    html_body = f"""<html><body style="font-family:sans-serif;color:#222;max-width:560px;">
<p>Hi,</p>
<p><strong>{inviter_name}</strong> invited you to join the expense group <strong>{group_name}</strong> on BillBuddy.</p>
<p><a href="{invite_url}" style="display:inline-block;padding:10px 18px;background:#2F5233;color:#fff;text-decoration:none;border-radius:6px;">Join group</a></p>
<p>Or copy this link: <a href="{invite_url}">{invite_url}</a></p>
<p>If you don't have an account yet, sign up with <strong>{to_email}</strong> and you'll be added automatically.</p>
</body></html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        _send_message(msg, to_email)
        logger.info("Invite email sent to %s for group %s", to_email, group_name)
        return True
    except Exception:
        logger.exception("Failed to send invite email to %s", to_email)
        raise


def send_monthly_report_email(
    to_email: str,
    member_name: str,
    group_name: str,
    period_key: str,
    pdf_path: str,
    pdf_filename: str,
) -> bool:
    if not is_email_configured():
        return False

    period_label = _period_label(period_key)
    subject = f'{group_name} — {period_label} expense report'

    text_body = f"""Hi {member_name},

Your monthly expense report for "{group_name}" ({period_label}) is attached.

This PDF includes the full expense list, balance summary, settlement plan, and receipt gallery for the month.

— BillBuddy
"""

    html_body = f"""<html><body style="font-family:sans-serif;color:#222;max-width:560px;">
<p>Hi <strong>{member_name}</strong>,</p>
<p>Your monthly expense report for <strong>{group_name}</strong> (<strong>{period_label}</strong>) is attached.</p>
<p>This PDF includes the full expense list, balance summary, settlement plan, and receipt gallery for the month.</p>
<p style="color:#666;font-size:13px;">— BillBuddy</p>
</body></html>"""

    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject

    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(text_body, "plain"))
    alt.attach(MIMEText(html_body, "html"))
    msg.attach(alt)

    with open(pdf_path, "rb") as pdf_file:
        attachment = MIMEApplication(pdf_file.read(), _subtype="pdf")
    attachment.add_header("Content-Disposition", "attachment", filename=pdf_filename)
    msg.attach(attachment)

    try:
        _send_message(msg, to_email)
        logger.info("Monthly report emailed to %s for group %s period %s", to_email, group_name, period_key)
        return True
    except Exception:
        logger.exception("Failed to email monthly report to %s", to_email)
        raise
