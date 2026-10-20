import fcntl
import hmac
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.collector import Worker
from app.links import parse_share
from app.storage import Store

STATIC = Path(__file__).parent / "static"


class Submission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=8000)


class Login(BaseModel):
    token: str = Field(min_length=1, max_length=500)


def create_app(data_dir=None, token=None, start_worker=True):
    directory = Path(data_dir or os.getenv("PDD_DATA_DIR", ".local/pdd-mvp")).resolve()
    store = Store(directory)
    token = token or os.getenv("PDD_LOCAL_TOKEN")
    if not token:
        token_file = directory / "access-token"
        if not token_file.exists():
            fd = os.open(token_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w") as file:
                file.write(secrets.token_urlsafe(32))
        token = token_file.read_text().strip()
    if len(token) < 24:
        raise ValueError("PDD_LOCAL_TOKEN 至少需要 24 个字符")

    @asynccontextmanager
    async def lifespan(app):
        if not start_worker:
            yield
            return
        with (directory / "worker.lock").open("w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("已有一个采集服务在使用此数据目录；请只运行一个 worker") from exc
            worker = Worker(store)
            worker.start()
            try:
                yield
            finally:
                worker.close()

    app = FastAPI(title="拼多多商品采集 MVP", version="0.1.0", lifespan=lifespan)
    app.state.store = store
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]", "testserver"]
    )
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.middleware("http")
    async def protect(request, call_next):
        if request.method in ("POST", "PUT"):
            chunks, size = [], 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > 32000:
                    return Response("请求过大", status_code=413)
                chunks.append(chunk)
            request._body = b"".join(chunks)
            origin = request.headers.get("origin")
            if origin and origin != str(request.base_url).rstrip("/"):
                return Response("不允许跨站请求", status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        if not request.url.path.startswith("/docs"):
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; img-src 'self'; style-src 'self'; script-src 'self'; frame-ancestors 'none'; base-uri 'none'"
            )
        return response

    def authenticated(request: Request):
        supplied = request.headers.get("authorization", "").removeprefix(
            "Bearer "
        ) or request.cookies.get("pdd_session", "")
        if not hmac.compare_digest(supplied.encode(), token.encode()):
            raise HTTPException(401, "请先输入本机访问口令")

    def job_or_404(job_id):
        job = store.get(job_id)
        if not job:
            raise HTTPException(404, "任务不存在")
        return job

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/healthz")
    def health():
        return {"status": "ok", "mode": "pdd-collector"}

    @app.post("/api/session")
    def login(body: Login, response: Response):
        if not hmac.compare_digest(body.token.encode(), token.encode()):
            raise HTTPException(401, "口令不正确")
        response.set_cookie(
            "pdd_session", token, httponly=True, samesite="strict", max_age=8 * 3600
        )
        return {"ok": True}

    @app.delete("/api/session")
    def logout(response: Response):
        response.delete_cookie("pdd_session")
        return {"ok": True}

    @app.get("/api/jobs", dependencies=[Depends(authenticated)])
    def list_jobs():
        return [
            {k: v for k, v in job.items() if k != "product"}
            | {"title": (job["product"] or {}).get("title")}
            for job in store.list()
        ]

    @app.post("/api/jobs", status_code=202, dependencies=[Depends(authenticated)])
    def submit(body: Submission, request: Request):
        key = request.headers.get("idempotency-key", "")
        if not 1 <= len(key) <= 200:
            raise HTTPException(422, "需要 Idempotency-Key（1–200 字符）")
        try:
            source = parse_share(body.text)
            return store.create(key, source["url"])
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/jobs/{job_id}", dependencies=[Depends(authenticated)])
    def get_job(job_id: str):
        return job_or_404(job_id)

    @app.post("/api/jobs/{job_id}/resume", dependencies=[Depends(authenticated)])
    def resume(job_id: str):
        job_or_404(job_id)
        try:
            return store.resume(job_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/jobs/{job_id}/download", dependencies=[Depends(authenticated)])
    def download(job_id: str):
        job = job_or_404(job_id)
        path = directory / "products" / job["id"] / "product.zip"
        if not job["download_ready"] or not path.is_file():
            raise HTTPException(409, "资料包尚未生成")
        return FileResponse(
            path, media_type="application/zip", filename=f"pdd-{job['product']['goods_id']}.zip"
        )

    @app.get("/api/jobs/{job_id}/images/{index}", dependencies=[Depends(authenticated)])
    def image(job_id: str, index: int):
        job = job_or_404(job_id)
        images = (job["product"] or {}).get("images", [])
        if not 0 <= index < len(images) or images[index]["status"] != "downloaded":
            raise HTTPException(404, "图片未下载")
        path = directory / "products" / job["id"] / images[index]["file"]
        if not path.is_file():
            raise HTTPException(404, "图片文件缺失，请重新采集")
        return FileResponse(path)

    return app
