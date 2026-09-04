import os
import random
import re
from datetime import datetime, timezone

import qrcode
from flask import current_app, flash, jsonify, redirect, render_template, request, session as flask_session, url_for
from flask_login import current_user, login_required, login_user, logout_user
from sqlalchemy import desc, or_
from sqlalchemy.exc import IntegrityError

from upiapp import Session, app, bcrypt, oauth
from upiapp.email_utils import send_otp_email
from upiapp.forms import DepositForm, LoginForm, PaymentForm, RegistrationForm, VerifyOtpForm
from upiapp.models import (
    FamilyCircle,
    FamilyMember,
    FamilyTransferRequest,
    FavoriteContact,
    Transaction,
    User,
)


def _google_username(session, userinfo: dict, email: str) -> str:
    given_name = userinfo.get("given_name")
    if not given_name and userinfo.get("name"):
        given_name = userinfo["name"].strip().split()[0]
    if not given_name:
        given_name = email.split("@")[0]

    base = re.sub(r"[^a-z0-9_]", "", (given_name or "").lower())[:15] or "user"
    username = base
    suffix = 1
    while session.query(User).filter_by(username=username).first():
        suffix += 1
        username = f"{base[:12]}{suffix}"
    return username


def _clean_recipient_identifier(raw: str) -> str:
    cleaned = (raw or "").strip()
    if cleaned.endswith("@inklusive"):
        return cleaned[:-10]
    return cleaned


def _generate_family_code(session) -> str:
    while True:
        code = f"FAM{random.randint(100, 999)}"
        if not session.query(FamilyCircle).filter_by(invite_code=code).first():
            return code


def _ensure_user_qr(session, user: User) -> str:
    folder = os.path.join(app.root_path, "static", "assets", "qr-images")
    os.makedirs(folder, exist_ok=True)
    filename = f"{user.username}_qr.png"
    filepath = os.path.join(folder, filename)

    if not os.path.exists(filepath) or not user.qr_image:
        upi_uri = f"upi://pay?pa={user.username}@inklusive&pn={user.username}&cu=INR"
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_H,
            box_size=10,
            border=2,
        )
        qr.add_data(upi_uri)
        qr.make(fit=True)
        img = qr.make_image(fill_color="#0f2b48", back_color="#ffffff")
        img.save(filepath)

        user.qr_image = filename
        session.commit()

    return filename


@app.route("/")
def home():
    if not current_user.is_authenticated:
        return redirect(url_for("login"))

    balance = None
    recent_transactions = []
    family_status = None

    with Session() as session:
        user = session.get(User, current_user.id)
        if user:
            balance = user.wallet
            _ensure_user_qr(session, user)

        # Fetch top 10 most recent transactions for Home Page (Image 2)
        txns = (
            session.query(Transaction)
            .filter(or_(Transaction.sender_id == current_user.id, Transaction.receiver_id == current_user.id))
            .order_by(desc(Transaction.created_on))
            .limit(10)
            .all()
        )

        for t in txns:
            if t.sender_id is None:
                direction = "DEPOSIT"
                counterparty = "Self (Wallet Top-up)"
            elif t.sender_id == current_user.id:
                direction = "SENT"
                counterparty = t.receiver.username if t.receiver else "User"
            else:
                direction = "RECEIVED"
                counterparty = t.sender.username if t.sender else "User"

            recent_transactions.append({
                "id": t.id,
                "amount": t.amount,
                "direction": direction,
                "counterparty": counterparty,
                "note": t.note,
                "status": t.status,
                "transaction_type": t.transaction_type,
                "created_on": t.created_on,
            })

        # Check Family Circle status
        membership = session.query(FamilyMember).filter_by(user_id=current_user.id).first()
        if membership and membership.family:
            family_status = {
                "is_active": membership.family.is_active,
                "name": membership.family.name,
                "daily_limit": membership.family.daily_limit,
            }

    return render_template(
        "home.html",
        title="Home",
        balance=balance,
        recent_transactions=recent_transactions,
        family_status=family_status,
    )



@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("home"))

    form = RegistrationForm()
    if form.validate_on_submit():
        email = form.email.data.strip().lower()
        username = form.username.data.strip()
        hashed_password = bcrypt.generate_password_hash(form.password.data).decode("utf-8")

        otp = f"{random.randint(100000, 999999):06d}"
        flask_session["pending_registration"] = {
            "username": username,
            "email": email,
            "password_hash": hashed_password,
            "otp": otp,
            "otp_created_at": datetime.now(timezone.utc).timestamp(),
        }

        try:
            send_otp_email(email, otp)
            flash(f"A 6-digit verification code was sent to {email}. Enter it below to complete registration.", "info")
        except Exception:
            current_app.logger.exception("Failed to send OTP email")
            flash("Could not send email directly (network SMTP timeout), but code was generated in logs.", "warning")

        return redirect(url_for("verify_otp"))

    return render_template("register.html", title="Register", form=form)


@app.route("/verify-otp", methods=["GET", "POST"])
def verify_otp():
    if current_user.is_authenticated:
        return redirect(url_for("home"))

    pending = flask_session.get("pending_registration")
    if not pending:
        flash("No registration in progress. Please sign up first.", "warning")
        return redirect(url_for("register"))

    form = VerifyOtpForm()
    if form.validate_on_submit():
        submitted_otp = form.otp.data.strip()
        otp_created = pending.get("otp_created_at", 0)

        # 10 minute expiration (600 seconds)
        if datetime.now(timezone.utc).timestamp() - otp_created > 600:
            flash("Verification code has expired. Please sign up again.", "danger")
            flask_session.pop("pending_registration", None)
            return redirect(url_for("register"))

        if submitted_otp != pending.get("otp"):
            flash("Incorrect verification code. Please check your email and try again.", "danger")
            return render_template("verify_otp.html", title="Verify Email", form=form, email=pending.get("email"))

        # Valid OTP: create user in DB & generate QR code
        with Session() as session:
            new_user = User(
                username=pending["username"],
                email=pending["email"],
                password_hash=pending["password_hash"],
                wallet=100.0,
            )
            session.add(new_user)
            try:
                session.commit()
                _ensure_user_qr(session, new_user)
            except IntegrityError:
                session.rollback()
                flash("An account with this username or email already exists.", "danger")
                flask_session.pop("pending_registration", None)
                return redirect(url_for("register"))

            session.expunge(new_user)

        flask_session.pop("pending_registration", None)
        login_user(new_user)
        flash("Email verified successfully! Welcome to InKlusivepay.", "success")
        return redirect(url_for("home"))

    return render_template("verify_otp.html", title="Verify Email", form=form, email=pending.get("email"))


@app.route("/resend-otp")
def resend_otp():
    if current_user.is_authenticated:
        return redirect(url_for("home"))

    pending = flask_session.get("pending_registration")
    if not pending:
        flash("No registration in progress. Please sign up first.", "warning")
        return redirect(url_for("register"))

    new_otp = f"{random.randint(100000, 999999):06d}"
    pending["otp"] = new_otp
    pending["otp_created_at"] = datetime.now(timezone.utc).timestamp()
    flask_session["pending_registration"] = pending

    try:
        send_otp_email(pending["email"], new_otp)
        flash("A fresh 6-digit verification code has been sent to your email.", "info")
    except Exception:
        current_app.logger.exception("Failed to resend OTP email")
        flash("Could not resend email directly, but a fresh code was generated.", "warning")

    return redirect(url_for("verify_otp"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("home"))

    form = LoginForm()
    if form.validate_on_submit():
        email = form.email.data.strip().lower()
        password = form.password.data

        with Session() as session:
            user = session.query(User).filter_by(email=email).first()

            if user is None:
                flash("Login failed. Check your email and password.", "danger")
                return render_template("login.html", title="Login", form=form)

            if user.password_hash is None:
                flash("This account was created with Google. Please click 'Continue with Google'.", "warning")
                return render_template("login.html", title="Login", form=form)

            if not bcrypt.check_password_hash(user.password_hash, password):
                flash("Login failed. Check your email and password.", "danger")
                return render_template("login.html", title="Login", form=form)

            session.expunge(user)

        login_user(user, remember=form.remember.data)
        next_page = request.args.get("next")
        flash("Welcome back!", "success")
        return redirect(next_page) if next_page else redirect(url_for("home"))

    return render_template("login.html", title="Login", form=form)


@app.route("/login/google")
def google_login():
    if current_user.is_authenticated:
        return redirect(url_for("home"))
    redirect_uri = url_for("google_callback", _external=True)
    return oauth.inclusiv_client.authorize_redirect(redirect_uri)


@app.route("/login/google/callback")
def google_callback():
    if current_user.is_authenticated:
        return redirect(url_for("home"))

    try:
        token = oauth.inclusiv_client.authorize_access_token()
    except Exception:
        current_app.logger.exception("Google OAuth token exchange failed")
        flash("Google login failed. Check authorized redirect URI in Google Cloud Console.", "danger")
        return redirect(url_for("login"))

    userinfo = token.get("userinfo") or {}
    email = (userinfo.get("email") or "").strip().lower()
    if not email:
        flash("Google did not return an email address. Allow email access and try again.", "danger")
        return redirect(url_for("login"))

    if userinfo.get("email_verified") is False:
        flash("Your Google email is not verified.", "danger")
        return redirect(url_for("login"))

    with Session() as session:
        user = session.query(User).filter_by(email=email).first()
        if user is None:
            user = User(
                username=_google_username(session, userinfo, email),
                email=email,
                password_hash=None,
                wallet=100.0,
            )
            session.add(user)
            try:
                session.commit()
                _ensure_user_qr(session, user)
            except IntegrityError:
                session.rollback()
                flash("Could not create account from Google. Try again.", "danger")
                return redirect(url_for("login"))
        session.expunge(user)

    login_user(user)
    flash("Logged in with Google.", "success")
    return redirect(url_for("home"))


@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("You have been logged out.", "info")
    return redirect(url_for("login"))


@app.route("/set-pin", methods=["POST"])
@login_required
def set_transaction_pin():
    pin = (request.form.get("pin") or "").strip()
    if len(pin) != 4 or not pin.isdigit():
        flash("Transaction PIN must be exactly 4 digits.", "danger")
        return redirect(request.referrer or url_for("profile"))

    with Session() as session:
        user = session.get(User, current_user.id)
        if user:
            user.transaction_pin_hash = bcrypt.generate_password_hash(pin).decode("utf-8")
            session.commit()
            flash("4-Digit Security PIN set successfully!", "success")

    return redirect(request.referrer or url_for("profile"))


@app.route("/pay", methods=["GET", "POST"])
@login_required
def pay():
    form = PaymentForm()
    if request.method == "GET":
        to_param = request.args.get("to", "").strip()
        amount_param = request.args.get("amount", "").strip()
        if to_param:
            form.recipient.data = to_param
        if amount_param:
            try:
                form.amount.data = float(amount_param)
            except ValueError:
                pass

    with Session() as session:
        user_db = session.get(User, current_user.id)
        current_balance = user_db.wallet if user_db else 0.0
        has_pin = bool(user_db.transaction_pin_hash) if user_db else False

    if form.validate_on_submit():
        target_str = form.recipient.data.strip()
        clean_target = _clean_recipient_identifier(target_str)
        amount = round(float(form.amount.data), 2)
        note = (form.note.data or "").strip() or None
        submitted_pin = (request.form.get("pin") or "").strip()

        if amount <= 0:
            flash("Payment amount must be greater than zero.", "danger")
            return render_template("pay.html", title="Send Money", form=form, current_balance=current_balance, has_pin=has_pin)

        with Session() as session:
            sender = session.get(User, current_user.id)
            if sender is None:
                flash("User session error. Please log in again.", "danger")
                return redirect(url_for("login"))

            # ----------------------------------------------------
            # TRANSACTION PIN VERIFICATION (DOUBLE CHECK)
            # ----------------------------------------------------
            if sender.transaction_pin_hash is None:
                # 1-Step PIN setup on first payment
                if len(submitted_pin) == 4 and submitted_pin.isdigit():
                    sender.transaction_pin_hash = bcrypt.generate_password_hash(submitted_pin).decode("utf-8")
                    session.commit()
                else:
                    flash("Please enter a valid 4-digit Security PIN to authorize this transfer.", "danger")
                    return render_template("pay.html", title="Send Money", form=form, current_balance=current_balance, has_pin=False)
            else:
                # Check existing PIN
                if not submitted_pin or not bcrypt.check_password_hash(sender.transaction_pin_hash, submitted_pin):
                    flash("Incorrect 4-digit Security PIN. Transfer rejected.", "danger")
                    return render_template("pay.html", title="Send Money", form=form, current_balance=current_balance, has_pin=True)

            receiver = session.query(User).filter(
                or_(
                    User.username == clean_target,
                    User.email == clean_target.lower(),
                )
            ).first()

            if receiver is None:
                flash(f"Recipient '{target_str}' not found. Please check username or email.", "danger")
                return render_template("pay.html", title="Send Money", form=form, current_balance=current_balance, has_pin=has_pin)

            if receiver.id == sender.id:
                flash("You cannot send money to yourself.", "warning")
                return render_template("pay.html", title="Send Money", form=form, current_balance=current_balance, has_pin=has_pin)

            if sender.wallet < amount:
                flash(f"Insufficient funds! Your current wallet balance is ₹{sender.wallet:,.2f}.", "danger")
                return render_template("pay.html", title="Send Money", form=form, current_balance=current_balance, has_pin=has_pin)

            # ----------------------------------------------------
            # FAMILY CIRCLE SAFETY THRESHOLD CHECK
            # ----------------------------------------------------
            membership = session.query(FamilyMember).filter_by(user_id=sender.id).first()
            if membership and membership.family and membership.family.is_active:
                family = membership.family
                if membership.role == "MEMBER" and family.admin_id != sender.id:
                    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
                    sent_today = (
                        session.query(Transaction)
                        .filter(
                            Transaction.sender_id == sender.id,
                            Transaction.status == "SUCCESS",
                            Transaction.created_on >= today_start,
                        )
                        .all()
                    )
                    spent_today = sum(t.amount for t in sent_today)

                    if (spent_today + amount) > family.daily_limit or amount > family.daily_limit:
                        transfer_req = FamilyTransferRequest(
                            family_id=family.id,
                            requester_id=sender.id,
                            recipient=receiver.username,
                            amount=amount,
                            note=note,
                            status="PENDING",
                        )
                        session.add(transfer_req)
                        session.commit()

                        flash(
                            f"Transfer of ₹{amount:,.2f} exceeds your Family Circle safety limit (₹{family.daily_limit:,.0f}). "
                            f"An approval request has been sent to your primary guardian ({family.admin.username}).",
                            "warning",
                        )
                        return redirect(url_for("family_circle"))

            # Execute instant transfer
            sender.wallet = round(sender.wallet - amount, 2)
            receiver.wallet = round(receiver.wallet + amount, 2)

            txn = Transaction(
                amount=amount,
                sender_id=sender.id,
                receiver_id=receiver.id,
                note=note,
                status="SUCCESS",
                transaction_type="TRANSFER",
            )
            session.add(txn)
            try:
                session.commit()
                flash(f"Payment of ₹{amount:,.2f} to {receiver.username} was successful!", "success")
                return redirect(url_for("transactions"))
            except Exception:
                session.rollback()
                current_app.logger.exception("Payment transaction failed")
                flash("Transaction failed due to a server error. Please try again.", "danger")

    return render_template("pay.html", title="Send Money", form=form, current_balance=current_balance, has_pin=has_pin)


@app.route("/deposit", methods=["GET", "POST"])
@app.route("/wallet/deposit", methods=["GET", "POST"])
@login_required
def deposit():
    form = DepositForm()
    with Session() as session:
        user_db = session.get(User, current_user.id)
        current_balance = user_db.wallet if user_db else 0.0

    if form.validate_on_submit():
        amount = round(float(form.amount.data), 2)
        source_bank = request.form.get("source", "bank1")
        bank_names = {
            "bank1": "HDFC Bank (Bank 1)",
            "bank2": "State Bank of India (Bank 2)",
            "bank3": "ICICI Bank (Bank 3)",
            "bank4": "Axis Bank (Bank 4)",
        }
        chosen_bank = bank_names.get(source_bank, "Bank 1")

        if amount <= 0:
            flash("Deposit amount must be greater than zero.", "danger")
            return render_template("deposit.html", title="Add Money", form=form, current_balance=current_balance)

        with Session() as session:
            user = session.get(User, current_user.id)
            if user is None:
                flash("User session error. Please log in again.", "danger")
                return redirect(url_for("login"))

            user.wallet = round(user.wallet + amount, 2)
            txn = Transaction(
                amount=amount,
                sender_id=None,
                receiver_id=user.id,
                note=f"Top-up via {chosen_bank}",
                status="SUCCESS",
                transaction_type="DEPOSIT",
            )
            session.add(txn)
            try:
                session.commit()
                flash(f"₹{amount:,.2f} added to your wallet successfully via {chosen_bank}!", "success")
                return redirect(url_for("balance"))
            except Exception:
                session.rollback()
                current_app.logger.exception("Deposit transaction failed")
                flash("Deposit failed. Please try again.", "danger")

    return render_template("deposit.html", title="Add Money", form=form, current_balance=current_balance)


@app.route("/balance")
@login_required
def balance():
    with Session() as session:
        user = session.get(User, current_user.id)
        current_balance = user.wallet if user else 0.0

        sent_txns = session.query(Transaction).filter_by(sender_id=current_user.id, status="SUCCESS").all()
        received_txns = (
            session.query(Transaction)
            .filter(
                Transaction.receiver_id == current_user.id,
                Transaction.sender_id.is_not(None),
                Transaction.status == "SUCCESS",
            )
            .all()
        )
        deposit_txns = (
            session.query(Transaction)
            .filter(
                Transaction.receiver_id == current_user.id,
                Transaction.sender_id.is_(None),
                Transaction.status == "SUCCESS",
            )
            .all()
        )

        total_sent = sum(t.amount for t in sent_txns)
        total_received = sum(t.amount for t in received_txns)
        total_deposited = sum(t.amount for t in deposit_txns)

        recent_txns = (
            session.query(Transaction)
            .filter(or_(Transaction.sender_id == current_user.id, Transaction.receiver_id == current_user.id))
            .order_by(desc(Transaction.created_on))
            .limit(5)
            .all()
        )

        formatted_txns = []
        for t in recent_txns:
            if t.sender_id is None:
                direction = "DEPOSIT"
                counterparty = "Self (Wallet Top-up)"
            elif t.sender_id == current_user.id:
                direction = "SENT"
                counterparty = t.receiver.username if t.receiver else "User"
            else:
                direction = "RECEIVED"
                counterparty = t.sender.username if t.sender else "User"

            formatted_txns.append({
                "id": t.id,
                "amount": t.amount,
                "direction": direction,
                "counterparty": counterparty,
                "note": t.note,
                "created_on": t.created_on,
                "status": t.status,
            })

    return render_template(
        "balance.html",
        title="My Wallet & Balance",
        balance=current_balance,
        total_sent=total_sent,
        total_received=total_received,
        total_deposited=total_deposited,
        recent_transactions=formatted_txns,
    )


@app.route("/transactions")
@login_required
def transactions():
    page = request.args.get("page", 1, type=int)
    per_page = 10

    with Session() as session:
        user = session.get(User, current_user.id)
        current_balance = user.wallet if user else 0.0

        query = (
            session.query(Transaction)
            .filter(or_(Transaction.sender_id == current_user.id, Transaction.receiver_id == current_user.id))
            .order_by(desc(Transaction.created_on))
        )

        total_count = query.count()
        total_pages = max(1, (total_count + per_page - 1) // per_page)
        if page < 1:
            page = 1
        elif page > total_pages and total_count > 0:
            page = total_pages

        offset = (page - 1) * per_page
        all_txns = query.offset(offset).limit(per_page).all()

        tx_list = []
        for t in all_txns:
            if t.sender_id is None:
                direction = "DEPOSIT"
                counterparty = "Self (Wallet Top-up)"
            elif t.sender_id == current_user.id:
                direction = "SENT"
                counterparty = t.receiver.username if t.receiver else "User"
            else:
                direction = "RECEIVED"
                counterparty = t.sender.username if t.sender else "User"

            tx_list.append({
                "id": t.id,
                "amount": t.amount,
                "direction": direction,
                "counterparty": counterparty,
                "note": t.note or "",
                "status": t.status,
                "transaction_type": t.transaction_type,
                "created_on": t.created_on,
            })

        all_user_txns = session.query(Transaction).filter(
            or_(Transaction.sender_id == current_user.id, Transaction.receiver_id == current_user.id),
            Transaction.status == "SUCCESS",
        ).all()
        total_sent = sum(t.amount for t in all_user_txns if t.sender_id == current_user.id)
        total_received = sum(t.amount for t in all_user_txns if t.receiver_id == current_user.id and t.sender_id is not None)

    return render_template(
        "transactions.html",
        title="Transaction History",
        transactions=tx_list,
        page=page,
        total_pages=total_pages,
        total_count=total_count,
        has_prev=(page > 1),
        has_next=(page < total_pages),
        current_balance=current_balance,
        total_sent=total_sent,
        total_received=total_received,
    )


# ----------------------------------------------------
# FAMILY CIRCLE ROUTES
# ----------------------------------------------------

@app.route("/family")
@login_required
def family_circle():
    with Session() as session:
        membership = session.query(FamilyMember).filter_by(user_id=current_user.id).first()

        if not membership or not membership.family:
            return render_template("family_intro.html", title="Set up your Family Circle")

        family = membership.family
        members = session.query(FamilyMember).filter_by(family_id=family.id).all()
        is_admin = (family.admin_id == current_user.id or membership.role in ["ADMIN", "PRIMARY_GUARDIAN"])

        if is_admin:
            pending_requests = (
                session.query(FamilyTransferRequest)
                .filter_by(family_id=family.id, status="PENDING")
                .order_by(desc(FamilyTransferRequest.created_at))
                .all()
            )
        else:
            pending_requests = (
                session.query(FamilyTransferRequest)
                .filter_by(family_id=family.id, requester_id=current_user.id, status="PENDING")
                .order_by(desc(FamilyTransferRequest.created_at))
                .all()
            )

        return render_template(
            "family_dashboard.html",
            title=f"{family.name} - Family Circle",
            family=family,
            members=members,
            is_admin=is_admin,
            pending_requests=pending_requests,
        )


@app.route("/family/create", methods=["POST"])
@login_required
def create_family():
    family_name = (request.form.get("family_name") or f"{current_user.username}'s Family").strip()
    relation = request.form.get("relation", "Parent (Primary Guardian)").strip()
    try:
        daily_limit = float(request.form.get("daily_limit", 5000.0))
    except ValueError:
        daily_limit = 5000.0

    with Session() as session:
        existing = session.query(FamilyMember).filter_by(user_id=current_user.id).first()
        if existing:
            flash("You are already part of a Family Circle.", "warning")
            return redirect(url_for("family_circle"))

        invite_code = _generate_family_code(session)
        family = FamilyCircle(
            name=family_name,
            admin_id=current_user.id,
            daily_limit=daily_limit,
            is_active=True,
            invite_code=invite_code,
        )
        session.add(family)
        session.flush()

        member = FamilyMember(
            family_id=family.id,
            user_id=current_user.id,
            role="ADMIN",
            relation=relation,
        )
        session.add(member)
        session.commit()

        flash(f"Family Circle '{family_name}' created! Share your invite code ({invite_code}) with family.", "success")
        return redirect(url_for("family_circle"))


@app.route("/family/join", methods=["POST"])
@login_required
def join_family():
    identifier = (request.form.get("invite_identifier") or "").strip()
    relation = (request.form.get("relation") or "Family Member").strip()

    if not identifier:
        flash("Please provide a valid Family Invite Code or Admin Username.", "danger")
        return redirect(url_for("family_circle"))

    with Session() as session:
        existing = session.query(FamilyMember).filter_by(user_id=current_user.id).first()
        if existing:
            flash("You are already part of a Family Circle.", "warning")
            return redirect(url_for("family_circle"))

        family = session.query(FamilyCircle).filter(
            or_(
                FamilyCircle.invite_code == identifier.upper(),
                FamilyCircle.invite_code == identifier,
            )
        ).first()

        if not family:
            admin_user = session.query(User).filter(
                or_(User.username == identifier, User.email == identifier.lower())
            ).first()
            if admin_user:
                family = session.query(FamilyCircle).filter_by(admin_id=admin_user.id).first()

        if not family:
            flash(f"No Family Circle found matching '{identifier}'.", "danger")
            return redirect(url_for("family_circle"))

        member = FamilyMember(
            family_id=family.id,
            user_id=current_user.id,
            role="MEMBER",
            relation=relation,
        )
        session.add(member)
        session.commit()

        flash(f"You have joined {family.name}!", "success")
        return redirect(url_for("family_circle"))


@app.route("/family/add-member", methods=["POST"])
@login_required
def add_family_member():
    identifier = (request.form.get("member_identifier") or "").strip()
    relation = (request.form.get("relation") or "Family Member").strip()
    role = (request.form.get("role") or "MEMBER").strip()

    with Session() as session:
        family = session.query(FamilyCircle).filter_by(admin_id=current_user.id).first()
        if not family:
            flash("Only the Family Circle admin can add members.", "danger")
            return redirect(url_for("family_circle"))

        target_user = session.query(User).filter(
            or_(User.username == identifier, User.email == identifier.lower())
        ).first()

        if not target_user:
            flash(f"User '{identifier}' not found.", "danger")
            return redirect(url_for("family_circle"))

        existing = session.query(FamilyMember).filter_by(user_id=target_user.id).first()
        if existing:
            flash(f"{target_user.username} is already in a Family Circle.", "warning")
            return redirect(url_for("family_circle"))

        new_member = FamilyMember(
            family_id=family.id,
            user_id=target_user.id,
            role=role,
            relation=relation,
        )
        session.add(new_member)
        session.commit()

        flash(f"{target_user.username} has been added to {family.name} as {relation}!", "success")
        return redirect(url_for("family_circle"))


@app.route("/family/set-limit", methods=["POST"])
@login_required
def set_family_limit():
    try:
        new_limit = float(request.form.get("daily_limit", 5000.0))
    except ValueError:
        new_limit = 5000.0

    with Session() as session:
        family = session.query(FamilyCircle).filter_by(admin_id=current_user.id).first()
        if not family:
            flash("Only the family admin can edit the safety limit.", "danger")
            return redirect(url_for("family_circle"))

        family.daily_limit = round(new_limit, 2)
        session.commit()

        flash(f"Transfer safety limit updated to ₹{new_limit:,.0f}!", "success")
        return redirect(url_for("family_circle"))


@app.route("/family/toggle-status", methods=["POST"])
@login_required
def toggle_family_status():
    with Session() as session:
        family = session.query(FamilyCircle).filter_by(admin_id=current_user.id).first()
        if not family:
            flash("Only the family admin can pause or resume protection.", "danger")
            return redirect(url_for("family_circle"))

        family.is_active = not family.is_active
        session.commit()

        status_text = "resumed" if family.is_active else "paused"
        flash(f"Family Circle protection has been {status_text}.", "info")
        return redirect(url_for("family_circle"))


@app.route("/family/remove-member/<int:member_id>", methods=["POST"])
@login_required
def remove_family_member(member_id):
    with Session() as session:
        family = session.query(FamilyCircle).filter_by(admin_id=current_user.id).first()
        if not family:
            flash("Unauthorized action.", "danger")
            return redirect(url_for("family_circle"))

        member = session.get(FamilyMember, member_id)
        if member and member.family_id == family.id:
            username = member.user.username if member.user else "Member"
            session.delete(member)
            session.commit()
            flash(f"{username} was removed from the Family Circle.", "info")

    return redirect(url_for("family_circle"))


@app.route("/family/approve/<int:request_id>", methods=["POST"])
@login_required
def approve_transfer(request_id):
    with Session() as session:
        req = session.get(FamilyTransferRequest, request_id)
        if not req or req.status != "PENDING":
            flash("Transfer request not found or already processed.", "danger")
            return redirect(url_for("family_circle"))

        family = req.family
        if family.admin_id != current_user.id:
            guard_member = session.query(FamilyMember).filter_by(family_id=family.id, user_id=current_user.id).first()
            if not guard_member or guard_member.role not in ["ADMIN", "PRIMARY_GUARDIAN", "BACKUP_GUARDIAN"]:
                flash("Only a guardian can approve transfers.", "danger")
                return redirect(url_for("family_circle"))

        requester = session.get(User, req.requester_id)
        receiver = session.query(User).filter_by(username=req.recipient).first()

        if not requester or not receiver:
            flash("Sender or recipient user account not found.", "danger")
            return redirect(url_for("family_circle"))

        if requester.wallet < req.amount:
            flash(f"{requester.username} has insufficient funds (₹{requester.wallet:,.2f}) for this transfer.", "danger")
            return redirect(url_for("family_circle"))

        requester.wallet = round(requester.wallet - req.amount, 2)
        receiver.wallet = round(receiver.wallet + req.amount, 2)
        req.status = "APPROVED"

        txn = Transaction(
            amount=req.amount,
            sender_id=requester.id,
            receiver_id=receiver.id,
            note=f"[Guardian Approved] {req.note or ''}".strip(),
            status="SUCCESS",
            transaction_type="TRANSFER",
        )
        session.add(txn)
        session.commit()

        flash(f"Approved! ₹{req.amount:,.2f} transferred from {requester.username} to {receiver.username}.", "success")
        return redirect(url_for("family_circle"))


@app.route("/family/reject/<int:request_id>", methods=["POST"])
@login_required
def reject_transfer(request_id):
    with Session() as session:
        req = session.get(FamilyTransferRequest, request_id)
        if not req or req.status != "PENDING":
            flash("Transfer request not found.", "danger")
            return redirect(url_for("family_circle"))

        family = req.family
        if family.admin_id != current_user.id:
            guard_member = session.query(FamilyMember).filter_by(family_id=family.id, user_id=current_user.id).first()
            if not guard_member or guard_member.role not in ["ADMIN", "PRIMARY_GUARDIAN", "BACKUP_GUARDIAN"]:
                flash("Only a guardian can reject transfers.", "danger")
                return redirect(url_for("family_circle"))

        req.status = "REJECTED"
        session.commit()

        flash(f"Transfer request of ₹{req.amount:,.2f} by {req.requester.username} has been rejected.", "info")
        return redirect(url_for("family_circle"))


@app.route("/profile")
@login_required
def profile():
    with Session() as session:
        user = session.get(User, current_user.id)
        balance = user.wallet if user else 0.0
        created = user.created if user else current_user.created
        qr_image = _ensure_user_qr(session, user) if user else None
        has_pin = bool(user.transaction_pin_hash) if user else False

    upi_id = f"{current_user.username}@inklusive"
    upi_uri = f"upi://pay?pa={upi_id}&pn={current_user.username}&cu=INR"
    payment_url = url_for("pay", to=current_user.username, _external=True)

    return render_template(
        "profile.html",
        title="My Profile",
        upi_id=upi_id,
        upi_uri=upi_uri,
        payment_url=payment_url,
        balance=balance,
        created=created,
        qr_image=qr_image,
        has_pin=has_pin,
    )


@app.route("/api/check-user/<identifier>")
@login_required
def check_user(identifier):
    clean_target = _clean_recipient_identifier(identifier.strip())
    with Session() as session:
        user = session.query(User).filter(
            or_(
                User.username == clean_target,
                User.email == clean_target.lower(),
            )
        ).first()

        if user:
            is_self = user.id == current_user.id
            return jsonify({
                "exists": True,
                "username": user.username,
                "is_self": is_self,
                "message": "You cannot send money to yourself." if is_self else f"Sending to {user.username}",
            })
        return jsonify({"exists": False, "message": "No account found matching this username or email."})


@app.route("/cognitive")
@login_required
def cognitive_mode():
    with Session() as session:
        user = session.get(User, current_user.id)
        balance = user.wallet if user else 0.0
        fav_contacts = (
            session.query(FavoriteContact)
            .filter_by(user_id=current_user.id)
            .order_by(FavoriteContact.id.desc())
            .all()
        )
        contacts_list = []
        for fc in fav_contacts:
            target_user = session.get(User, fc.contact_user_id)
            if target_user:
                contacts_list.append({
                    "id": fc.id,
                    "contact_id": target_user.id,
                    "username": target_user.username,
                    "email": target_user.email,
                    "nickname": fc.nickname if fc.nickname else target_user.username.title(),
                    "profile_image": target_user.profile_image or "alex_avatar.png",
                })

        membership = session.query(FamilyMember).filter_by(user_id=current_user.id).first()
        is_family_active = bool(membership and membership.family and membership.family.is_active)

    return render_template(
        "cognitive.html",
        title="Cognitive Mode",
        balance=balance,
        favorite_contacts=contacts_list,
        is_family_active=is_family_active,
    )


@app.route("/add-favorite-contact", methods=["POST"])
@login_required
def add_favorite_contact():
    identifier = (request.form.get("identifier") or "").strip()
    nickname = (request.form.get("nickname") or "").strip()

    if not identifier:
        flash("Please provide the email or username of the user.", "warning")
        return redirect(url_for("cognitive_mode"))

    clean_target = _clean_recipient_identifier(identifier)
    with Session() as session:
        target_user = session.query(User).filter(
            or_(
                User.username == clean_target,
                User.email == clean_target.lower(),
            )
        ).first()

        if not target_user:
            flash(f"No registered user found with email or username '{identifier}'.", "danger")
            return redirect(url_for("cognitive_mode"))

        if target_user.id == current_user.id:
            flash("You cannot add yourself as a favorite contact.", "warning")
            return redirect(url_for("cognitive_mode"))

        existing = session.query(FavoriteContact).filter_by(
            user_id=current_user.id,
            contact_user_id=target_user.id,
        ).first()

        if existing:
            flash(f"{target_user.username} is already in your favorite contacts.", "info")
            return redirect(url_for("cognitive_mode"))

        new_fav = FavoriteContact(
            user_id=current_user.id,
            contact_user_id=target_user.id,
            nickname=nickname if nickname else target_user.username.title(),
        )
        session.add(new_fav)
        session.commit()
        disp_name = f"{target_user.username} ({nickname})" if nickname else target_user.username
        flash(f"Added {disp_name} to your favorite contacts!", "success")

    return redirect(url_for("cognitive_mode"))


@app.route("/remove-favorite-contact/<int:contact_id>", methods=["POST"])
@login_required
def remove_favorite_contact(contact_id):
    with Session() as session:
        fav = session.get(FavoriteContact, contact_id)
        if fav and fav.user_id == current_user.id:
            session.delete(fav)
            session.commit()
            flash("Contact removed from favorites.", "info")
    return redirect(url_for("cognitive_mode"))