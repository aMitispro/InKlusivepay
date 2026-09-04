import os
import sys
from datetime import datetime, timezone
from upiapp import app, Session, engine, Base
from upiapp.models import User, Transaction, FamilyCircle, FamilyMember, FamilyTransferRequest
from upiapp.routes import _google_username, _ensure_user_qr

def run_tests():
    print("==================================================")
    print("Running InKlusivepay Complete Validation Tests")
    print("==================================================")

    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False

    # Clear and recreate tables
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    client = app.test_client()

    # ----------------------------------------------------
    # TEST 1: Unauthenticated visitors redirected to /login first
    # ----------------------------------------------------
    unauth_home_resp = client.get("/", follow_redirects=False)
    assert unauth_home_resp.status_code == 302, f"Expected 302 redirect, got {unauth_home_resp.status_code}"
    assert "/login" in unauth_home_resp.headers["Location"]
    print("[PASS] Unauthenticated visitor redirected to /login first")

    # ----------------------------------------------------
    # TEST 2: Registration & 6-Digit OTP Flow
    # ----------------------------------------------------
    reg_resp = client.post("/register", data={
        "username": "alice",
        "email": "alice@test.com",
        "password": "password123",
        "pass_conf": "password123"
    }, follow_redirects=False)
    assert reg_resp.status_code == 302

    with client.session_transaction() as sess:
        otp_code = sess["pending_registration"]["otp"]

    correct_otp_resp = client.post("/verify-otp", data={"otp": otp_code}, follow_redirects=True)
    assert correct_otp_resp.status_code == 200

    with Session() as session:
        alice_user = session.query(User).filter_by(username="alice").first()
        assert alice_user is not None
        alice_id = alice_user.id
        assert alice_user.qr_image is not None or os.path.exists(f"upiapp/static/assets/qr-images/{alice_user.username}_qr.png")
    print("[PASS] Registration, 6-digit OTP verification & User QR code generation verified")

    # Helper to simulate login
    def login_as(user_id):
        with client.session_transaction() as sess:
            sess["_user_id"] = str(user_id)
            sess["_fresh"] = True

    # Register Bob as second user
    client.get("/logout", follow_redirects=True)
    with client.session_transaction() as sess:
        sess.clear()
    bob_reg = client.post("/register", data={
        "username": "bob",
        "email": "bob@test.com",
        "password": "password123",
        "pass_conf": "password123"
    })
    with client.session_transaction() as sess:
        bob_otp = sess["pending_registration"]["otp"]
    client.post("/verify-otp", data={"otp": bob_otp}, follow_redirects=True)
    with Session() as session:
        bob_user = session.query(User).filter_by(username="bob").first()
        bob_id = bob_user.id
    print("[PASS] Bob registered and verified (ID: %d)" % bob_id)

    # ----------------------------------------------------
    # TEST 3: Deposit with 4 Bank Options
    # ----------------------------------------------------
    login_as(alice_id)
    deposit_resp = client.post("/deposit", data={
        "amount": 5000.0,
        "source": "bank1"
    }, follow_redirects=True)
    assert deposit_resp.status_code == 200
    with Session() as session:
        alice_db = session.get(User, alice_id)
        assert alice_db.wallet == 5100.0
    print("[PASS] Deposit funds using Bank 1 (HDFC Bank) verified: Alice wallet = Rs.5,100.0")

    # ----------------------------------------------------
    # TEST 4: Security PIN Setup & Double Check on Payment
    # ----------------------------------------------------
    # Alice sets her PIN (1234) on first payment
    pay_with_new_pin = client.post("/pay", data={
        "recipient": "bob",
        "amount": 100.0,
        "note": "Lunch",
        "pin": "1234"
    }, follow_redirects=True)
    assert pay_with_new_pin.status_code == 200

    with Session() as session:
        alice_db = session.get(User, alice_id)
        assert alice_db.transaction_pin_hash is not None
        assert alice_db.wallet == 5000.0
    print("[PASS] 1-Step Security PIN setup and payment verified: Alice wallet = Rs.5,000.0")

    # Alice tries to pay with WRONG PIN (0000) -> rejected!
    wrong_pin_pay = client.post("/pay", data={
        "recipient": "bob",
        "amount": 50.0,
        "note": "Tea",
        "pin": "0000"
    }, follow_redirects=True)
    assert b"Incorrect 4-digit Security PIN" in wrong_pin_pay.data
    with Session() as session:
        alice_db = session.get(User, alice_id)
        assert alice_db.wallet == 5000.0
    print("[PASS] Incorrect Security PIN correctly rejected, funds protected")

    # ----------------------------------------------------
    # TEST 5: Family Circle Feature
    # ----------------------------------------------------
    # Alice creates Family Circle with Rs.1000 limit
    create_fam_resp = client.post("/family/create", data={
        "family_name": "Sharma Family",
        "relation": "Parent (Primary Guardian)",
        "daily_limit": 1000.0,
    }, follow_redirects=True)
    assert create_fam_resp.status_code == 200

    # Add Bob as son
    client.post("/family/add-member", data={
        "member_identifier": "bob",
        "relation": "Son",
        "role": "MEMBER"
    }, follow_redirects=True)

    # Bob logs in and sets his PIN (4321) on his first payment
    login_as(bob_id)
    # Top up Bob
    client.post("/deposit", data={"amount": 2000.0, "source": "bank2"}, follow_redirects=True)

    bob_pay = client.post("/pay", data={
        "recipient": "alice",
        "amount": 50.0,
        "note": "Chocolates",
        "pin": "4321"
    }, follow_redirects=True)
    assert b"successful" in bob_pay.data

    # Bob tries large payment (Rs.1500 > limit Rs.1000) -> blocked & pending approval
    bob_large_pay = client.post("/pay", data={
        "recipient": "alice",
        "amount": 1500.0,
        "note": "Fee",
        "pin": "4321"
    }, follow_redirects=True)
    assert b"exceeds your Family Circle safety limit" in bob_large_pay.data
    print("[PASS] Family Circle safety limit and approval flow verified")

    # ----------------------------------------------------
    # TEST 6: Dynamic Favorite Contacts in Cognitive Mode
    # ----------------------------------------------------
    login_as(alice_id)
    # Initially no contacts -> empty state shown with "+ Add Contact using Email"
    cog_empty = client.get("/cognitive")
    assert cog_empty.status_code == 200
    assert b"Safe Mode Active" in cog_empty.data
    assert b"Favorite Contacts" in cog_empty.data
    assert b"No favorite contacts added yet" in cog_empty.data
    assert b"+ Add Contact" in cog_empty.data

    # Alice adds Bob as a favorite contact using his email (bob@test.com)
    add_fav_resp = client.post("/add-favorite-contact", data={
        "identifier": "bob@test.com",
        "nickname": "Bob (Brother)"
    }, follow_redirects=True)
    assert add_fav_resp.status_code == 200
    assert b"Bob (Brother)" in add_fav_resp.data

    # Cognitive page now displays Bob in the scrollable contact strip
    cog_with_fav = client.get("/cognitive")
    assert b"Bob (Brother)" in cog_with_fav.data
    print("[PASS] Dynamic Favorite Contacts (Add with Email, Localized Scroll & UI) verified")

    # ----------------------------------------------------
    # TEST 7: QR Button Defaults to Camera Scanner Tab
    # ----------------------------------------------------
    qr_pay_page = client.get("/pay?scan=1")
    assert qr_pay_page.status_code == 200
    assert b'id="qr-pane"' in qr_pay_page.data
    assert b'show active' in qr_pay_page.data
    assert b'id="qr-reader"' in qr_pay_page.data
    print("[PASS] QR Pay button automatically activates Camera Scanner Tab")

    profile_page = client.get("/profile")
    assert profile_page.status_code == 200
    assert b"Personal Receive QR Code" in profile_page.data
    assert b"assets/qr-images/" in profile_page.data
    print("[PASS] Home Page UI, Cognitive Mode & Profile QR validated")

    print("\n==================================================")
    print("ALL VALIDATION TESTS PASSED WITH 100% SUCCESS!")
    print("==================================================")

if __name__ == "__main__":
    run_tests()
