from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel, Field, field_validator
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

# 三栏原文：风速、巷温、仪器编号。独立字段、原文存储，旧种子行为 NULL。
TRIPLE_FIELDS = ("wind_speed", "tunnel_temp", "instrument_no")
FIELD_LABELS = {
    "wind_speed": "风速",
    "tunnel_temp": "巷温",
    "instrument_no": "仪器编号",
}


class Base(DeclarativeBase):
    pass


class Reading(Base):
    __tablename__ = "readings"
    id: Mapped[int] = mapped_column(primary_key=True)
    site: Mapped[str] = mapped_column(String(80))
    ch4_pct: Mapped[float] = mapped_column(Float)
    level: Mapped[str] = mapped_column(String(20))
    note: Mapped[str] = mapped_column(String(200))
    # 旧库没有这三列，startup 时自动补上；旧行保持 NULL，仍然照常显示。
    wind_speed: Mapped[str | None] = mapped_column(String(80), nullable=True)
    tunnel_temp: Mapped[str | None] = mapped_column(String(80), nullable=True)
    instrument_no: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class FieldHistory(Base):
    """三栏履历，只追加不改写。上报时每栏写一条原文；改正时再追加一条。"""

    __tablename__ = "field_history"
    id: Mapped[int] = mapped_column(primary_key=True)
    reading_id: Mapped[int] = mapped_column(index=True)
    field_name: Mapped[str] = mapped_column(String(40))
    old_text: Mapped[str | None] = mapped_column(String(80), nullable=True)
    new_text: Mapped[str] = mapped_column(String(80))
    changed_by: Mapped[str] = mapped_column(String(64))
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class FilterScheme(Base):
    """仪器编号片段过滤的命名方案，供下次一键套用。"""

    __tablename__ = "filter_schemes"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    instrument_fragment: Mapped[str] = mapped_column(String(80))
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class LoginIn(BaseModel):
    username: str
    password: str


class ReadingIn(BaseModel):
    site: str = Field(min_length=1, max_length=80)
    ch4_pct: float
    # 三栏强制填写，缺一不可，缺栏直接 422，端点逻辑不会执行（不入库也不推送）。
    wind_speed: str = Field(min_length=1, max_length=80)
    tunnel_temp: str = Field(min_length=1, max_length=80)
    instrument_no: str = Field(min_length=1, max_length=80)

    @field_validator("site", *TRIPLE_FIELDS)
    @classmethod
    def strip_required(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("不能为空")
        return v


class ReadingPatch(BaseModel):
    # 只提交要改正的栏；未提交(None)的栏不动。
    wind_speed: str | None = Field(default=None, max_length=80)
    tunnel_temp: str | None = Field(default=None, max_length=80)
    instrument_no: str | None = Field(default=None, max_length=80)


class SchemeIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    instrument_fragment: str = Field(min_length=1, max_length=80)

    @field_validator("name", "instrument_fragment")
    @classmethod
    def strip_required(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("不能为空")
        return v


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


def reading_dict(r: Reading) -> dict:
    return {
        "id": r.id,
        "site": r.site,
        "ch4_pct": r.ch4_pct,
        "level": r.level,
        "note": r.note,
        "wind_speed": r.wind_speed,
        "tunnel_temp": r.tunnel_temp,
        "instrument_no": r.instrument_no,
        "created_by": r.created_by,
    }


def history_dict(h: FieldHistory) -> dict:
    return {
        "id": h.id,
        "reading_id": h.reading_id,
        "field": h.field_name,
        "label": FIELD_LABELS.get(h.field_name, h.field_name),
        "old": h.old_text,
        "new": h.new_text,
        "changed_by": h.changed_by,
        "changed_at": h.changed_at.isoformat(),
    }


sockets: set[WebSocket] = set()
app = FastAPI(title="矿井瓦斯班测台")


@app.on_event("startup")
def startup():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        # 给旧库补齐三栏（新库 create_all 已含列，跳过）。
        inspector = inspect(engine)
        existing = {c["name"] for c in inspector.get_columns("readings")}
        for name in TRIPLE_FIELDS:
            if name not in existing:
                db.execute(text(f"ALTER TABLE readings ADD COLUMN {name} VARCHAR(80)"))
        db.commit()

        if db.query(Reading).count() == 0:
            now = datetime.now(timezone.utc)
            # 旧种子：没有三栏（NULL），仍照常显示；新上报的行三栏必须齐。
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


@app.get("/api/readings")
def list_readings(instrument: str = "", _user: dict = Depends(current_user)):
    # 服务端按仪器编号片段过滤；旁观账号同样可过滤。
    db = SessionLocal()
    try:
        q = db.query(Reading)
        fragment = instrument.strip()
        if fragment:
            q = q.filter(Reading.instrument_no.ilike(f"%{fragment}%"))
        rows = q.order_by(Reading.id.desc()).all()
        return [reading_dict(r) for r in rows]
    finally:
        db.close()


@app.post("/api/readings", status_code=201)
async def create_reading(body: ReadingIn, user: dict = Depends(require_writer)):
    level, note = classify(body.ch4_pct)
    now = datetime.now(timezone.utc)
    db = SessionLocal()
    try:
        row = Reading(
            site=body.site,
            ch4_pct=body.ch4_pct,
            level=level,
            note=note,
            wind_speed=body.wind_speed,
            tunnel_temp=body.tunnel_temp,
            instrument_no=body.instrument_no,
            created_by=user["username"],
            created_at=now,
        )
        db.add(row)
        db.flush()  # 取 row.id，履历与读数同一事务写入。
        # 每次成功上报，把三栏原文各写一条履历（old 为空，表示首次录入）。
        for name in TRIPLE_FIELDS:
            db.add(
                FieldHistory(
                    reading_id=row.id,
                    field_name=name,
                    old_text=None,
                    new_text=getattr(body, name),
                    changed_by=user["username"],
                    changed_at=now,
                )
            )
        db.commit()
        payload = reading_dict(row)
    finally:
        db.close()
    # 只有提交成功才推送。
    dead = []
    for ws in list(sockets):
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        sockets.discard(ws)
    return payload


@app.get("/api/readings/{reading_id}/history")
def reading_history(reading_id: int, _user: dict = Depends(current_user)):
    # 旁观账号也能看三栏履历。
    db = SessionLocal()
    try:
        if db.get(Reading, reading_id) is None:
            raise HTTPException(status_code=404, detail="记录不存在")
        rows = (
            db.query(FieldHistory)
            .filter(FieldHistory.reading_id == reading_id)
            .order_by(FieldHistory.id.asc())
            .all()
        )
        return [history_dict(h) for h in rows]
    finally:
        db.close()


@app.patch("/api/readings/{reading_id}")
def patch_reading(reading_id: int, body: ReadingPatch, user: dict = Depends(require_writer)):
    db = SessionLocal()
    try:
        row = db.get(Reading, reading_id)
        if row is None:
            raise HTTPException(status_code=404, detail="记录不存在")
        now = datetime.now(timezone.utc)
        changed = []
        for name in TRIPLE_FIELDS:
            value = getattr(body, name)
            if value is None:
                continue  # 这一栏本次没有提交，保持不动。
            value = value.strip()
            if not value:
                raise HTTPException(status_code=400, detail=f"{FIELD_LABELS[name]}不能为空")
            old = getattr(row, name)
            if value == old:
                continue
            # 追加一条新履历；旧履历行原样保留改正前的原文。
            db.add(
                FieldHistory(
                    reading_id=row.id,
                    field_name=name,
                    old_text=old,
                    new_text=value,
                    changed_by=user["username"],
                    changed_at=now,
                )
            )
            setattr(row, name, value)
            changed.append(FIELD_LABELS[name])
        if not changed:
            raise HTTPException(status_code=400, detail="没有改动的栏")
        db.commit()
        return {"ok": True, "changed": changed}
    finally:
        db.close()


@app.get("/api/filter-schemes")
def list_schemes(_user: dict = Depends(current_user)):
    # 旁观账号可以套用已有方案过滤，只是不能新建。
    db = SessionLocal()
    try:
        rows = db.query(FilterScheme).order_by(FilterScheme.name.asc()).all()
        return [
            {
                "id": s.id,
                "name": s.name,
                "instrument_fragment": s.instrument_fragment,
                "created_by": s.created_by,
            }
            for s in rows
        ]
    finally:
        db.close()


@app.post("/api/filter-schemes", status_code=201)
def create_scheme(body: SchemeIn, user: dict = Depends(require_writer)):
    # 仅检查员能新建命名过滤方案。
    db = SessionLocal()
    try:
        if db.query(FilterScheme).filter(FilterScheme.name == body.name).first():
            raise HTTPException(status_code=400, detail="方案名已存在")
        row = FilterScheme(
            name=body.name,
            instrument_fragment=body.instrument_fragment,
            created_by=user["username"],
            created_at=datetime.now(timezone.utc),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return {"id": row.id, "name": row.name, "instrument_fragment": row.instrument_fragment}
    finally:
        db.close()


@app.delete("/api/filter-schemes/{scheme_id}", status_code=204)
def delete_scheme(scheme_id: int, _user: dict = Depends(require_writer)):
    db = SessionLocal()
    try:
        row = db.get(FilterScheme, scheme_id)
        if row is None:
            raise HTTPException(status_code=404, detail="方案不存在")
        db.delete(row)
        db.commit()
    finally:
        db.close()


@app.websocket("/ws/alerts")
async def alerts(ws: WebSocket):
    await ws.accept()
    sockets.add(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        sockets.discard(ws)
