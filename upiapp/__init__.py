from os import getenv
from pathlib import Path

from authlib.integrations.flask_client import OAuth
from dotenv import load_dotenv
from flask import Flask
from flask_bcrypt import Bcrypt
from flask_login import LoginManager
from flask_mail import Mail
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from upiapp.models import Base, User

_PKG_DIR = Path(__file__).resolve().parent
_ROOT_DIR = _PKG_DIR.parent
load_dotenv(_PKG_DIR / ".env")
load_dotenv(_ROOT_DIR / ".env")

app = Flask(__name__)
app.config["SECRET_KEY"] = getenv("SECRET_KEY")
app.config["MAIL_SERVER"] = "smtp.gmail.com"
app.config["MAIL_PORT"] = 587
app.config["MAIL_USE_TLS"] = True
app.config["MAIL_USERNAME"] = getenv("MAIL_USERNAME", "shubhayon.banik9678@gmail.com")
app.config["MAIL_PASSWORD"] = getenv("MAIL_PASSWORD", "yrjodsbxqqghqrkv")
app.config["MAIL_DEFAULT_SENDER"] = getenv("MAIL_DEFAULT_SENDER", "shubhayon.banik9678@gmail.com")

engine = create_engine(f"sqlite:///{_ROOT_DIR / 'appdata.db'}", echo=False)
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine, expire_on_commit=False)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"
login_manager.login_message_category = "info"

bcrypt = Bcrypt(app)
mail = Mail(app)
oauth = OAuth(app)

oauth.register(
    "inclusiv_client",
    client_id=getenv("CLIENT_ID"),
    client_secret=getenv("CLIENT_SECRET"),
    client_kwargs={"scope": "openid email profile"},
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
)


@login_manager.user_loader
def load_user(user_id):
    with Session() as session:
        user = session.get(User, int(user_id))
        if user is not None:
            session.expunge(user)
        return user


import upiapp.routes  # noqa: E402, F401
