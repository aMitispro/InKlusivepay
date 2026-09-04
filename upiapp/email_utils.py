from flask import current_app, url_for
from flask_mail import Message
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from upiapp import mail


def _serializer():
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt="email-confirm")


def generate_confirmation_token(email: str) -> str:
    return _serializer().dumps(email)


def confirm_token(token: str, max_age: int = 3600) -> str | None:
    try:
        return _serializer().loads(token, max_age=max_age)
    except (SignatureExpired, BadSignature):
        return None


def send_confirmation_email(email: str) -> str:
    token = generate_confirmation_token(email)
    confirm_url = url_for("confirm_email", token=token, _external=True)
    sender = current_app.config.get("MAIL_DEFAULT_SENDER") or current_app.config.get("MAIL_USERNAME")
    msg = Message(
        subject="Confirm your InKlusivepay account",
        recipients=[email],
        sender=sender,
        body=(
            "Welcome to InKlusivepay.\n\n"
            "Confirm your email by opening this link (valid for 1 hour):\n"
            f"{confirm_url}\n\n"
            "If you did not create an account, ignore this message."
        ),
    )
    mail.send(msg)
    return confirm_url


def send_otp_email(email: str, otp: str) -> bool:
    sender = current_app.config.get("MAIL_DEFAULT_SENDER") or current_app.config.get("MAIL_USERNAME")
    msg = Message(
        subject="Your InKlusivepay Verification Code",
        recipients=[email],
        sender=sender,
        body=(
            "Welcome to InKlusivepay.\n\n"
            f"Your 6-digit email verification code is:\n\n"
            f"    {otp}\n\n"
            "This code will expire in 10 minutes.\n"
            "Enter this code on the verification screen to complete your registration.\n\n"
            "If you did not request this code, you can safely ignore this email."
        ),
    )
    current_app.logger.info("Verification code for %s: %s", email, otp)
    import socket
    old_timeout = socket.getdefaulttimeout()
    try:
        socket.setdefaulttimeout(2.0)
        mail.send(msg)
        return True
    except Exception as exc:
        current_app.logger.warning("SMTP server connect failed (%s). Generated OTP is %s", exc, otp)
        return False
    finally:
        socket.setdefaulttimeout(old_timeout)

