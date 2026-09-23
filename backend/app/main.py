from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings
from sqlalchemy import DateTime, Float, String, create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from app.rules import classify


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg2://app:app@localhost:54391/methane"
    jwt_secret: str = "mine-methane-dev-secret"


settings = Settings()
pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")
security = HTTPBearer(auto_error=False)
USERS = {
    "gasman": {"role": "writer", "password_hash": pwd.hash("gas123456")},
    "viewer": {"role": "reader", "password_hash": pwd.hash("view123456")},
}

engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine)


class Base(DeclarativeBase):
    pass


# 三栏在数据库中各自独立字段；旧种子行没有这三栏，允许为空，列表照常显示
class Reading(Base):
    __tablename__ = "readings"
    id: Mapped[int] = mapped_column(primary_key=True)
    site: Mapped[str] = mapped_column(String(80))
    ch4_pct: Mapped[float] = mapped_column(Float)
    level: Mapped[str] = mapped_column(String(20))
    note: Mapped[str] = mapped_column(String(200))
    wind_speed: Mapped[str | None] = mapped_column(String(40), nullable=True)
    roadway_temp: Mapped[str | None] = mapped_column(String(40), nullable=True)
    instrument_no: Mapped[str | None] = mapped_column(String(60), nullable=True)
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


# 追加式三栏履历：每行是一次成功上报或一次改正时的原文快照，永不修改
class ReadingHistory(Base):
    __tablename__ = "reading_histories"
    id: Mapped[int] = mapped_column(primary_key=True)
    reading_id: Mapped[int] = mapped_column(index=True)
    wind_speed: Mapped[str] = mapped_column(String(40))
    roadway_temp: Mapped[str] = mapped_column(String(40))
    instrument_no: Mapped[str] = mapped_column(String(60))
    action: Mapped[str] = mapped_column(String(20))
    changed_by: Mapped[str] = mapped_column(String(64))
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


# 命名过滤方案：保存仪器编号片段，下次一键套用（仅检查员可新建）
class FilterScheme(Base):
    __tablename__ = "filter_schemes"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(60), unique=True)
    instrument: Mapped[str] = mapped_column(String(60))
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class LoginIn(BaseModel):
    username: str
    password: str


class ReadingIn(BaseModel):
    site: str = Field(min_length=1, max_length=80)
    ch4_pct: float
    # 三栏原文按字符串收，空串等同缺栏
    wind_speed: str | None = None
    roadway_temp: str | None = None
    instrument_no: str | None = None


class ReadingPatchIn(BaseModel):
    wind_speed: str | None = None
    roadway_temp: str | None = None
    instrument_no: str | None = None


class FilterSchemeIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    instrument: str = Field(max_length=60)


def current_user(credentials: HTTPAuthorizationCredentials | None = Depends(security)) -> dict:
    if credentials is None:
        raise HTTPException(status_code=401, detail="未登录")
    try:
        payload = jwt.decode(credentials.credentials, settings.jwt_secret, algorithms=["HS256"])
    except JWTError as exc:
        raise HTTPException(status_code=401, detail="无效令牌") from exc
    username = payload.get("sub")
    if username not in USERS:
        raise HTTPException(status_code=401, detail="无效令牌")
    return {"username": username, "role": payload.get("role")}


def require_writer(user: dict = Depends(current_user)) -> dict:
    if user["role"] != "writer":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="仅瓦斯检查员可操作")
    return user


# 取三栏原文：去掉首尾空白后缺一即拒，既不入库也不推送
def require_triplet(body: ReadingIn | ReadingPatchIn) -> dict[str, str]:
    values = {}
    missing = []
    for key, label in (
        ("wind_speed", "风速"),
        ("roadway_temp", "巷温"),
        ("instrument_no", "仪器编号"),
    ):
        raw = getattr(body, key)
        text_value = (raw or "").strip()
        if not text_value:
            missing.append(label)
        values[key] = text_value
    if missing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"必填栏缺失：{'、'.join(missing)}",
        )
    return values


sockets: set[WebSocket] = set()
app = FastAPI(title="矿井瓦斯班测台")


@app.on_event("startup")
def startup():
    Base.metadata.create_all(bind=engine)
    # 旧库没有三栏列，幂等补列；旧种子行该三列为 NULL，仍照常显示
    columns = {c["name"] for c in inspect(engine).get_columns("readings")}
    with engine.begin() as conn:
        for column, ddl in (
            ("wind_speed", "VARCHAR(40)"),
            ("roadway_temp", "VARCHAR(40)"),
            ("instrument_no", "VARCHAR(60)"),
        ):
            if column not in columns:
                conn.execute(text(f"ALTER TABLE readings ADD COLUMN {column} {ddl}"))
        # 旧数据若以显式 id 导入，序列可能落后，补齐到 max(id)，避免新行主键冲突
        conn.execute(
            text(
                "SELECT setval(pg_get_serial_sequence('readings', 'id'), "
                "(SELECT COALESCE(MAX(id), 1) FROM readings), (SELECT COUNT(*) > 0 FROM readings))"
            )
        )
    db = SessionLocal()
    try:
        if db.query(Reading).count() == 0:
            now = datetime.now(timezone.utc)
            for site, ch4 in (("东翼-12", 0.35), ("回风巷", 1.4)):
                level, note = classify(ch4)
                db.add(
                    Reading(
                        site=site,
                        ch4_pct=ch4,
                        level=level,
                        note=note,
                        created_by="gasman",
                        created_at=now,
                    )
                )
            db.commit()
    finally:
        db.close()


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "mine-methane-shift"}


@app.post("/api/auth/login")
def login(body: LoginIn):
    user = USERS.get(body.username.strip())
    if not user or not pwd.verify(body.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    exp = datetime.now(timezone.utc) + timedelta(hours=8)
    token = jwt.encode(
        {"sub": body.username.strip(), "role": user["role"], "exp": exp},
        settings.jwt_secret,
        algorithm="HS256",
    )
    return {"access_token": token, "username": body.username.strip(), "role": user["role"]}


def reading_payload(r: Reading) -> dict:
    return {
        "id": r.id,
        "site": r.site,
        "ch4_pct": r.ch4_pct,
        "level": r.level,
        "note": r.note,
        "wind_speed": r.wind_speed,
        "roadway_temp": r.roadway_temp,
        "instrument_no": r.instrument_no,
        "created_by": r.created_by,
    }


@app.get("/api/readings")
def list_readings(
    instrument: str | None = None,
    _user: dict = Depends(current_user),
):
    db = SessionLocal()
    try:
        q = db.query(Reading)
        # 服务端按仪器编号片段过滤
        if instrument and instrument.strip():
            q = q.filter(Reading.instrument_no.ilike(f"%{instrument.strip()}%"))
        rows = q.order_by(Reading.id.desc()).all()
        return [reading_payload(r) for r in rows]
    finally:
        db.close()


@app.post("/api/readings", status_code=201)
async def create_reading(body: ReadingIn, user: dict = Depends(require_writer)):
    # 缺栏在这里抛 400：下面的入库与推送都不会发生
    triplet = require_triplet(body)
    level, note = classify(body.ch4_pct)
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        row = Reading(
            site=body.site.strip(),
            ch4_pct=body.ch4_pct,
            level=level,
            note=note,
            wind_speed=triplet["wind_speed"],
            roadway_temp=triplet["roadway_temp"],
            instrument_no=triplet["instrument_no"],
            created_by=user["username"],
            created_at=now,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        # 每次成功上报，把三栏原文写入三栏履历
        db.add(
            ReadingHistory(
                reading_id=row.id,
                wind_speed=row.wind_speed,
                roadway_temp=row.roadway_temp,
                instrument_no=row.instrument_no,
                action="上报",
                changed_by=user["username"],
                changed_at=now,
            )
        )
        db.commit()
        payload = reading_payload(row)
    finally:
        db.close()
    await broadcast(payload)
    return payload


@app.patch("/api/readings/{reading_id}")
def patch_reading(reading_id: int, body: ReadingPatchIn, user: dict = Depends(require_writer)):
    # 改正时提交的三栏也必须齐全；缺栏不改不写履历
    triplet = require_triplet(body)
    db = SessionLocal()
    try:
        row = db.get(Reading, reading_id)
        if row is None:
            raise HTTPException(status_code=404, detail="记录不存在")
        # 旧行（原本没有三栏）不允许改正：没有可保留的上报原文履历
        if row.wind_speed is None:
            raise HTTPException(status_code=400, detail="旧记录缺少三栏，不能改正")
        row.wind_speed = triplet["wind_speed"]
        row.roadway_temp = triplet["roadway_temp"]
        row.instrument_no = triplet["instrument_no"]
        # 追加一条新快照，旧履历行保持改正前原文不动
        db.add(
            ReadingHistory(
                reading_id=row.id,
                wind_speed=row.wind_speed,
                roadway_temp=row.roadway_temp,
                instrument_no=row.instrument_no,
                action="改正",
                changed_by=user["username"],
                changed_at=datetime.now(timezone.utc),
            )
        )
        db.commit()
        db.refresh(row)
        return reading_payload(row)
    finally:
        db.close()


@app.get("/api/readings/{reading_id}/histories")
def list_histories(reading_id: int, _user: dict = Depends(current_user)):
    db = SessionLocal()
    try:
        rows = (
            db.query(ReadingHistory)
            .filter(ReadingHistory.reading_id == reading_id)
            .order_by(ReadingHistory.id.asc())
            .all()
        )
        return [
            {
                "id": h.id,
                "wind_speed": h.wind_speed,
                "roadway_temp": h.roadway_temp,
                "instrument_no": h.instrument_no,
                "action": h.action,
                "changed_by": h.changed_by,
                "changed_at": h.changed_at.isoformat(),
            }
            for h in rows
        ]
    finally:
        db.close()


@app.get("/api/filter-schemes")
def list_schemes(_user: dict = Depends(current_user)):
    db = SessionLocal()
    try:
        rows = db.query(FilterScheme).order_by(FilterScheme.id.asc()).all()
        return [{"id": s.id, "name": s.name, "instrument": s.instrument} for s in rows]
    finally:
        db.close()


@app.post("/api/filter-schemes", status_code=201)
def create_scheme(body: FilterSchemeIn, user: dict = Depends(require_writer)):
    instrument = body.instrument.strip()
    if not instrument:
        raise HTTPException(status_code=400, detail="过滤片段不能为空")
    db = SessionLocal()
    try:
        scheme = FilterScheme(
            name=body.name.strip(),
            instrument=instrument,
            created_by=user["username"],
            created_at=datetime.now(timezone.utc),
        )
        db.add(scheme)
        try:
            db.commit()
        except Exception:
            db.rollback()
            raise HTTPException(status_code=400, detail="方案名已存在")
        db.refresh(scheme)
        return {"id": scheme.id, "name": scheme.name, "instrument": scheme.instrument}
    finally:
        db.close()


async def broadcast(payload: dict):
    dead = []
    for ws in list(sockets):
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        sockets.discard(ws)


@app.websocket("/ws/alerts")
async def alerts(ws: WebSocket):
    await ws.accept()
    sockets.add(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        sockets.discard(ws)
