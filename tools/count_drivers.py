from pathlib import Path
import sys
from sqlmodel import Session, select

# ensure project root is importable when running this script from tools/
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import engine
from app.models import Driver

with Session(engine) as s:
    drivers = s.exec(select(Driver)).all()
    print('Drivers in DB:', len(drivers))
