from flask import Flask
from sqlalchemy import create_engine
from upiapp.models import Base

engine = create_engine(url="sqlite:///appdata.db",echo=True)
Base.metadata.create_all(engine)

app = Flask(__name__)

import upiapp.routes