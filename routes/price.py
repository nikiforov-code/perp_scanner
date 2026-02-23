from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()


@router.get("/price", response_class=HTMLResponse)
def price_page() -> str:
    return "Price view (stub)"
