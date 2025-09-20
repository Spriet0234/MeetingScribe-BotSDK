from fastapi import APIRouter

router = APIRouter()

@router.get("/healthz", include_in_schema=False)
def healthz():
    return {"ok": True}
