"""Backend vòng quay may mắn – FastAPI + Supabase (Postgres).

Bảng public.players(name, phone) có ràng buộc UNIQUE trên phone nên không thể trùng số.
Backend gọi Supabase qua REST (PostgREST) bằng khóa bí mật, chạy dạng serverless trên Vercel (file này: api/index.py).
"""
import csv
import io
import os
import random
import re
import time
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

# ---------- Cấu hình (đặt trong Settings > Variables and secrets của Space) ----------
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")  # https://xxxx.supabase.co
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")  # service_role (hoặc secret key) – TUYỆT ĐỐI không để lộ ra frontend
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")  # trống = tắt các API xem danh sách
ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",") if o.strip()]

VN_TZ = timezone(timedelta(hours=7))

# Tỉ lệ trúng từng giải (trọng số, không cần cộng đủ 100). Máy chủ là nơi quyết định giải.
# Khóa phải TRÙNG với giá trị trong PRIZES của file LuckyWheel.jsx: số 50 -> "50", chữ "Loa" -> "Loa".
# Muốn một giải không bao giờ trúng thì đặt 0.
PRIZE_RATES = {
    "50": 35,
    "100": 35,
    "150": 25,
    "200": 7,
    "Chuột KD": 40,
    "300": 0.00000001,
    "Loa": 30,
}

# Khóa kiểu cũ là JWT (bắt đầu bằng "eyJ") cần gửi thêm Authorization; khóa kiểu mới (sb_secret_...) thì không
_headers = {"apikey": SUPABASE_KEY, "Content-Type": "application/json"}
if SUPABASE_KEY.startswith("eyJ"):
    _headers["Authorization"] = f"Bearer {SUPABASE_KEY}"

http = httpx.Client(base_url=f"{SUPABASE_URL}/rest/v1", headers=_headers, timeout=10)


# ---------- Tiện ích ----------
def normalize_phone(raw) -> str:
    """'0912 345 678', '+84912345678', '84912345678' -> '0912345678'"""
    p = re.sub(r"[\s.\-()]", "", str(raw or ""))
    if p.startswith("+84"):
        p = "0" + p[3:]
    elif p.startswith("84") and len(p) == 11:
        p = "0" + p[2:]
    return p


def is_valid_phone(p: str) -> bool:
    return re.fullmatch(r"0[35789]\d{8}", p) is not None


def pick_prize() -> str:
    keys = [k for k, w in PRIZE_RATES.items() if w > 0]
    return random.choices(keys, weights=[PRIZE_RATES[k] for k in keys], k=1)[0]


def prize_value(key: str):
    """'150' -> 150 (số), 'Loa' -> 'Loa' (chữ) – giống kiểu dữ liệu trong PRIZES ở frontend"""
    return int(key) if key.isdigit() else key


def prize_label(key: str) -> str:
    return f"{key}K" if key.isdigit() else key


def mask_phone(p: str) -> str:
    return f"{p[:4]}***{p[-3:]}" if len(p) >= 8 else "***"  # 0912***678


def mask_name(n: str) -> str:
    """'Nguyễn Văn An' -> 'Nguyễn A***' (họ đầy đủ + chữ cái đầu của tên)"""
    parts = n.split()
    if not parts:
        return "***"
    if len(parts) == 1:
        return parts[0][0] + "***"
    return f"{parts[0]} {parts[-1][0]}***"


def clean_name(raw) -> str:
    return re.sub(r"\s+", " ", str(raw or "")).strip()


def err(status: int, code: str, message: str = "") -> JSONResponse:
    return JSONResponse({"ok": False, "error": code, "message": message}, status_code=status)


def server_error(exc: Exception | None = None) -> JSONResponse:
    if exc:
        print("Supabase error:", repr(exc))
    return err(500, "SERVER_ERROR", "Có lỗi xảy ra, vui lòng thử lại.")


def configured() -> bool:
    return bool(SUPABASE_URL and SUPABASE_KEY)


def vn_time(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(VN_TZ).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return iso


# Giới hạn tốc độ nhẹ theo IP: tối đa `limit` request / 60 giây
_hits: dict[str, tuple[int, float]] = {}


def too_many(request: Request, limit: int) -> bool:
    fwd = request.headers.get("x-forwarded-for", "")
    ip = fwd.split(",")[0].strip() or (request.client.host if request.client else "?")
    key = f"{ip}:{request.url.path}"
    now = time.time()
    count, start = _hits.get(key, (0, now))
    if now - start > 60:
        count, start = 0, now
    count += 1
    _hits[key] = (count, start)
    if len(_hits) > 5000:
        _hits.clear()
    return count > limit


def is_admin(token: str | None) -> bool:
    return bool(ADMIN_TOKEN) and token == ADMIN_TOKEN


# ---------- API ----------
app = FastAPI(title="Lucky Wheel API", docs_url="/docs", redoc_url=None)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


@app.exception_handler(RequestValidationError)
async def bad_body(_req, _exc):
    return err(400, "INVALID_BODY", "Dữ liệu gửi lên không hợp lệ.")


class PlayerIn(BaseModel):
    name: str | None = None
    phone: str | None = None


@app.get("/")
@app.get("/api/health")
def health():
    return {"ok": True, "service": "lucky-wheel-api", "supabase_configured": configured()}


@app.get("/api/check")
def check(request: Request, phone: str = Query("")):
    """Kiểm tra số đã tham gia chưa (gọi trước khi cho quay)."""
    if too_many(request, 60):
        return err(429, "TOO_MANY_REQUESTS", "Thao tác quá nhanh, vui lòng thử lại sau.")
    p = normalize_phone(phone)
    if not is_valid_phone(p):
        return err(400, "INVALID_PHONE", "Số điện thoại không hợp lệ.")
    if not configured():
        return server_error()
    try:
        r = http.get("/players", params={"select": "id", "phone": f"eq.{p}", "limit": 1})
        r.raise_for_status()
        return {"ok": True, "exists": len(r.json()) > 0}
    except Exception as e:
        return server_error(e)


@app.post("/api/players", status_code=201)
def register(body: PlayerIn, request: Request):
    """Đăng ký người chơi: {"name": "Nguyễn Văn A", "phone": "0912345678"}"""
    if too_many(request, 20):
        return err(429, "TOO_MANY_REQUESTS", "Thao tác quá nhanh, vui lòng thử lại sau.")

    name = clean_name(body.name)
    phone = normalize_phone(body.phone)
    if not 2 <= len(name) <= 60:
        return err(400, "INVALID_NAME", "Vui lòng nhập họ tên (2–60 ký tự).")
    if not is_valid_phone(phone):
        return err(400, "INVALID_PHONE", "Số điện thoại không hợp lệ.")
    if not configured():
        return server_error()

    try:
        r = http.post("/players", json={"name": name, "phone": phone}, headers={"Prefer": "return=representation"})
    except Exception as e:
        return server_error(e)

    if r.status_code == 201:
        row = r.json()[0]
        return JSONResponse({"ok": True, "id": row["id"], "name": row["name"], "phone": row["phone"]}, status_code=201)

    # Postgres báo vi phạm UNIQUE bằng mã 23505 (kể cả khi 2 người gửi cùng lúc)
    code = ""
    try:
        code = r.json().get("code", "")
    except Exception:
        pass
    if r.status_code == 409 or code == "23505":
        return err(409, "PHONE_EXISTS", "Số điện thoại này đã tham gia quay thưởng.")
    return server_error(Exception(f"{r.status_code} {r.text[:200]}"))


@app.post("/api/spin")
def spin(body: PlayerIn, request: Request):
    """Quay thưởng: máy chủ chọn giải, lưu cùng tên + số điện thoại (số trùng -> 409 và không được quay)."""
    if too_many(request, 10):
        return err(429, "TOO_MANY_REQUESTS", "Thao tác quá nhanh, vui lòng thử lại sau.")

    name = clean_name(body.name)
    phone = normalize_phone(body.phone)
    if not 2 <= len(name) <= 60:
        return err(400, "INVALID_NAME", "Vui lòng nhập họ tên (2–60 ký tự).")
    if not is_valid_phone(phone):
        return err(400, "INVALID_PHONE", "Số điện thoại không hợp lệ.")
    if not configured():
        return server_error()

    key = pick_prize()
    row = {"name": name, "phone": phone, "prize": key, "won_at": datetime.now(timezone.utc).isoformat()}
    try:
        r = http.post("/players", json=row, headers={"Prefer": "return=representation"})
    except Exception as e:
        return server_error(e)

    if r.status_code == 201:
        _winners_cache.clear()
        return JSONResponse(
            {"ok": True, "prize": prize_value(key), "label": prize_label(key)}, status_code=201
        )

    code = ""
    try:
        code = r.json().get("code", "")
    except Exception:
        pass
    if r.status_code == 409 or code == "23505":
        return err(409, "PHONE_EXISTS", "Số điện thoại này đã tham gia quay thưởng.")
    return server_error(Exception(f"{r.status_code} {r.text[:200]}"))


# Danh sách trúng thưởng công khai (đã che tên + số điện thoại). Lưu tạm 5 giây để đỡ gọi Supabase.
_winners_cache: dict[int, tuple[float, list]] = {}


@app.get("/api/winners")
def winners(request: Request, limit: int = Query(30, ge=1, le=100)):
    if too_many(request, 60):
        return err(429, "TOO_MANY_REQUESTS", "Thao tác quá nhanh, vui lòng thử lại sau.")
    if not configured():
        return server_error()
    hit = _winners_cache.get(limit)
    if hit and time.time() - hit[0] < 5:
        return {"ok": True, "winners": hit[1]}
    try:
        r = http.get(
            "/players",
            params={
                "select": "id,name,phone,prize,won_at",
                "prize": "not.is.null",
                "order": "won_at.desc",
                "limit": limit,
            },
        )
        r.raise_for_status()
        data = [
            {
                "id": p["id"],
                "name": mask_name(p["name"]),
                "phone": mask_phone(p["phone"]),
                "prize": prize_value(p["prize"]),
                "label": prize_label(p["prize"]),
                "at": p["won_at"],
            }
            for p in r.json()
        ]
    except Exception as e:
        return server_error(e)
    _winners_cache[limit] = (time.time(), data)
    return {"ok": True, "winners": data}


# ----- Quản trị (cần header x-admin-token) -----
@app.get("/api/players")
def list_players(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    x_admin_token: str | None = Header(None),
):
    if not is_admin(x_admin_token):
        return err(401, "UNAUTHORIZED")
    if not configured():
        return server_error()
    try:
        r = http.get(
            "/players",
            params={"select": "id,name,phone,prize,won_at,created_at", "order": "id.desc", "limit": limit, "offset": offset},
            headers={"Prefer": "count=exact"},
        )
        r.raise_for_status()
        total = int(r.headers.get("content-range", "*/0").split("/")[-1] or 0)
        players = [
            {**p, "created_at": vn_time(p["created_at"]), "won_at": vn_time(p["won_at"]) if p.get("won_at") else None}
            for p in r.json()
        ]
        return {"ok": True, "total": total, "players": players}
    except Exception as e:
        return server_error(e)


@app.get("/api/players.csv")
def export_csv(x_admin_token: str | None = Header(None)):
    """Tải CSV: mở bằng Excel vẫn đúng tiếng Việt và giữ số 0 đầu."""
    if not is_admin(x_admin_token):
        return err(401, "UNAUTHORIZED")
    if not configured():
        return server_error()
    try:
        rows, offset, page = [], 0, 1000
        while True:  # Supabase trả tối đa 1000 dòng mỗi lần nên lấy theo từng trang
            r = http.get(
                "/players",
                params={"select": "id,name,phone,prize,won_at,created_at", "order": "id.asc", "limit": page, "offset": offset},
            )
            r.raise_for_status()
            batch = r.json()
            rows += batch
            if len(batch) < page:
                break
            offset += page
    except Exception as e:
        return server_error(e)

    buf = io.StringIO()
    w = csv.writer(buf, quoting=csv.QUOTE_NONNUMERIC)
    w.writerow(["id", "name", "phone", "prize", "won_at"])
    for p in rows:
        w.writerow([
            p["id"], p["name"], f'="{p["phone"]}"',
            prize_label(p["prize"]) if p.get("prize") else "",
            vn_time(p["won_at"] or p["created_at"]),
        ])
    return Response(
        "\ufeff" + buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="players.csv"'},
    )

