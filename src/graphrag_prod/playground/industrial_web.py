"""Static industrial workbench; all knowledge reads still cross authenticated APIs."""

from importlib.resources import files
import mimetypes

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, Response


HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; connect-src 'self'; object-src 'none'; "
        "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
    ),
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), geolocation=(), microphone=()",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


def attach_industrial_web(app: FastAPI) -> None:
    root = files("graphrag_prod.playground").joinpath("static", "industrial")

    @app.get("/industrial", response_class=HTMLResponse, include_in_schema=False)
    async def industrial_page() -> HTMLResponse:
        return HTMLResponse(root.joinpath("index.html").read_text(encoding="utf-8"), headers=HEADERS)

    @app.get("/industrial/assets/{asset_path:path}", include_in_schema=False)
    async def industrial_asset(asset_path: str) -> Response:
        parts = asset_path.split("/")
        if (not parts or any(not part or part in {".", ".."} or "\\" in part
                or not all(c.isascii() and (c.isalnum() or c in "._-") for c in part)
                for part in parts)):
            raise HTTPException(status_code=404, detail="asset not found")
        resource = root.joinpath(*parts)
        suffix = parts[-1].rsplit(".", 1)[-1]
        if suffix not in {"css", "mjs", "js", "json", "txt"} or not resource.is_file():
            raise HTTPException(status_code=404, detail="asset not found")
        media_type = "text/javascript" if suffix in {"js", "mjs"} else mimetypes.guess_type(parts[-1])[0]
        return Response(resource.read_bytes(), media_type=media_type or "text/plain", headers=HEADERS)
