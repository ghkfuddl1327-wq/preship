from typing import Annotated
from fastapi import FastAPI, HTTPException, Query, Body
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict

app = FastAPI(title="Preflight Demo (intentionally buggy)", version="0.1.0")
ITEMS: dict[int, dict] = {1: {"id": 1, "name": "Sample", "price": 9.9}}

# ---- #1: 존재 확인 누락 → 500 ----
@app.get("/items/{item_id}")
def get_item(item_id: int):
    return ITEMS[item_id]  # BUG: KeyError → 500

# ---- #2: 타입 오선언(str) → int() → 500 ----
@app.get("/search")
def search(
    q: str = "",
    limit: Annotated[str, Query(example="abc")] = "10",  # [det] 파라미터 example
):
    n = int(limit)  # BUG: ValueError → 500
    return {"q": q, "results": list(range(n))}

# ---- #3: 0으로 나눗셈 → 500 ----
class QuoteIn(BaseModel):
    total: float
    quantity: int
    # [det] 바디(스키마 최상위) example — 속성 단위 아님
    model_config = ConfigDict(json_schema_extra={"example": {"total": 1.0, "quantity": 0}})

@app.post("/orders/quote")
def quote(payload: QuoteIn):
    return {"unit_price": payload.total / payload.quantity}  # BUG: ZeroDivisionError → 500

# ---- #4: 생짜 dict → KeyError → 500 ----
@app.post("/profile")
def create_profile(body: Annotated[dict, Body(example={})]):  # [det] 미디어타입 바디 example
    name = body["name"]  # BUG: KeyError → 500
    return {"profile": name.upper()}

# ---- #5: response_model 우회 → 응답 스키마 미준수 ----
class StatusOut(BaseModel):
    status: str
    uptime: int

@app.get("/status", response_model=StatusOut)
def status():
    return JSONResponse({"status": "ok"})  # BUG: uptime 누락(Response라 model 미적용)

# ---- #6: 미문서 409 + text/plain → 상태코드/콘텐츠타입 미준수 ----
class ItemIn(BaseModel):
    id: int
    name: str
    price: float
    # [det] 바디 example로 기존 id=1 강제 → 충돌 경로 발사
    model_config = ConfigDict(json_schema_extra={"example": {"id": 1, "name": "x", "price": 1.0}})

class ItemOut(ItemIn):
    pass

@app.post("/items", response_model=ItemOut, status_code=201)
def create_item(payload: ItemIn):
    if payload.id in ITEMS:
        return PlainTextResponse("conflict", status_code=409)  # BUG: 미문서 409 + 비JSON
    ITEMS[payload.id] = payload.model_dump()
    return payload

# ---- #7: 클린 대조군 (결함 없음) ----
class Pong(BaseModel):
    message: str

@app.get("/ping", response_model=Pong)
def ping():
    return Pong(message="pong")
