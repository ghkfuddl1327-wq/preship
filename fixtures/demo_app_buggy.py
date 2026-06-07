from typing import Annotated
from fastapi import FastAPI, HTTPException, Query, Body
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict
app = FastAPI(title="Preflight Demo (intentionally buggy)", version="0.1.0")
ITEMS: dict[int, dict] = {1: {"id": 1, "name": "Sample", "price": 9.9}}
# ---- #1: missing existence check -> 500 ----
@app.get("/items/{item_id}")
def get_item(item_id: int):
    return ITEMS[item_id]  # BUG: KeyError -> 500
# ---- #2: wrong type declaration (str) -> int() -> 500 ----
@app.get("/search")
def search(
    q: str = "",
    limit: Annotated[str, Query(example="abc")] = "10",  # [det] parameter example
):
    n = int(limit)  # BUG: ValueError -> 500
    return {"q": q, "results": list(range(n))}
# ---- #3: division by zero -> 500 ----
class QuoteIn(BaseModel):
    total: float
    quantity: int
    # [det] body (schema top-level) example -- not per-property
    model_config = ConfigDict(json_schema_extra={"example": {"total": 1.0, "quantity": 0}})
@app.post("/orders/quote")
def quote(payload: QuoteIn):
    return {"unit_price": payload.total / payload.quantity}  # BUG: ZeroDivisionError -> 500
# ---- #4: raw dict -> KeyError -> 500 ----
@app.post("/profile")
def create_profile(body: Annotated[dict, Body(example={})]):  # [det] media-type body example
    name = body["name"]  # BUG: KeyError -> 500
    return {"profile": name.upper()}
# ---- #5: response_model bypass -> response schema non-conformance ----
class StatusOut(BaseModel):
    status: str
    uptime: int
@app.get("/status", response_model=StatusOut)
def status():
    return JSONResponse({"status": "ok"})  # BUG: missing uptime (Response bypasses model)
# ---- #6: undocumented 409 + text/plain -> status code / content-type non-conformance ----
class ItemIn(BaseModel):
    id: int
    name: str
    price: float
    # [det] body example forces existing id=1 -> triggers conflict path
    model_config = ConfigDict(json_schema_extra={"example": {"id": 1, "name": "x", "price": 1.0}})
class ItemOut(ItemIn):
    pass
@app.post("/items", response_model=ItemOut, status_code=201)
def create_item(payload: ItemIn):
    if payload.id in ITEMS:
        return PlainTextResponse("conflict", status_code=409)  # BUG: undocumented 409 + non-JSON
    ITEMS[payload.id] = payload.model_dump()
    return payload
# ---- #7: clean control (no defects) ----
class Pong(BaseModel):
    message: str
@app.get("/ping", response_model=Pong)
def ping():
    return Pong(message="pong")
