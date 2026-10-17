from html import escape
from typing import Tuple


def build_password_reset_email(
    app_name: str, user_name: str, otp: str, expires_in_minutes: int
) -> Tuple[str, str, str]:
    subject = f"{app_name} password reset code"
    text_body = (
        f"Hello {user_name},\n\n"
        f"Your password reset code is {otp}.\n"
        f"This code is valid for {expires_in_minutes} minutes and can be used only once.\n\n"
        "If you did not request a password reset, you can safely ignore this email.\n\n"
        f"{app_name}"
    )
    html_body = f"""<!DOCTYPE html>
<html>
<body style="margin:0;padding:24px;background:#f4f5f7;font-family:Arial,Helvetica,sans-serif;color:#1f2937;">
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0">
    <tr>
      <td align="center">
        <table role="presentation" width="480" cellspacing="0" cellpadding="0" style="background:#ffffff;border-radius:8px;padding:32px;">
          <tr>
            <td>
              <h2 style="margin:0 0 16px 0;font-size:20px;">Password reset</h2>
              <p style="margin:0 0 16px 0;font-size:15px;">Hello {escape(user_name)},</p>
              <p style="margin:0 0 16px 0;font-size:15px;">Use the code below to reset your password.</p>
              <p style="margin:0 0 16px 0;font-size:32px;font-weight:bold;letter-spacing:8px;">{escape(otp)}</p>
              <p style="margin:0 0 16px 0;font-size:14px;color:#4b5563;">This code is valid for {expires_in_minutes} minutes and can be used only once.</p>
              <p style="margin:0;font-size:14px;color:#4b5563;">If you did not request a password reset, you can safely ignore this email.</p>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""
    return subject, text_body, html_body
