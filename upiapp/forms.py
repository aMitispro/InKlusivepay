from flask_wtf import FlaskForm
from wtforms import BooleanField, FloatField, PasswordField, StringField, SubmitField
from wtforms.validators import DataRequired, Email, EqualTo, Length, NumberRange, ValidationError

from upiapp import Session
from upiapp.models import User


class RegistrationForm(FlaskForm):
    username = StringField("Username", validators=[DataRequired(), Length(min=2, max=20)])
    email = StringField("Email", validators=[DataRequired(), Email()])
    password = PasswordField("Password", validators=[DataRequired(), Length(min=6, max=128)])
    pass_conf = PasswordField(
        "Confirm Password",
        validators=[DataRequired(), EqualTo("password", message="Passwords must match.")],
    )
    submit = SubmitField("Sign Up")

    def validate_username(self, username):
        with Session() as session:
            user = session.query(User).filter_by(username=username.data).first()
        if user:
            raise ValidationError("This username is already taken. Try another username.")

    def validate_email(self, email):
        with Session() as session:
            user = session.query(User).filter_by(email=email.data.lower()).first()
        if user:
            raise ValidationError("This email already exists.")


class LoginForm(FlaskForm):
    email = StringField("Email", validators=[DataRequired(), Email()])
    password = PasswordField("Password", validators=[DataRequired()])
    remember = BooleanField("Remember me")
    submit = SubmitField("Log In")


class PaymentForm(FlaskForm):
    recipient = StringField(
        "Recipient (Username, Email, or UPI ID)",
        validators=[DataRequired(message="Please enter recipient's username, email, or UPI ID.")],
    )
    amount = FloatField(
        "Amount (₹)",
        validators=[
            DataRequired(message="Please enter an amount."),
            NumberRange(min=1.0, message="Amount must be at least ₹1.00."),
        ],
    )
    note = StringField(
        "Note (Optional)",
        validators=[Length(max=100, message="Note must be 100 characters or fewer.")],
    )
    submit = SubmitField("Send Money")


class DepositForm(FlaskForm):
    amount = FloatField(
        "Amount (₹)",
        validators=[
            DataRequired(message="Please enter amount to add."),
            NumberRange(min=1.0, max=100000.0, message="Amount must be between ₹1 and ₹1,00,000."),
        ],
    )
    submit = SubmitField("Add Money to Wallet")


class VerifyOtpForm(FlaskForm):
    otp = StringField(
        "6-Digit Verification Code",
        validators=[
            DataRequired(message="Please enter the 6-digit code."),
            Length(min=6, max=6, message="The code must be exactly 6 digits."),
        ],
    )
    submit = SubmitField("Verify & Create Account")

