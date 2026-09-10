"""Static industrial workbench; all knowledge reads still cross authenticated APIs."""

from importlib.resources import files
import mimetypes
import re

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

KNOWLEDGE_ASSETS = frozenset({
    "browser.mjs", "model.mjs", "browser.css", "graph-view.mjs",
    "sources.mjs", "maintenance.mjs", "maintenance-actions.mjs",
})



def pump_only_page(html: str) -> str:
    """Remove unrelated demo controls before serving the isolated workspace."""
    html = re.sub(r'<option value="(?:canalis-kt|evopact-hvx-up24)">.*?</option>', '', html, flags=re.S)
    html = re.sub(r'<span class="domain-chip">.*?</span>', '', html, flags=re.S)
    html = re.sub(r'<button\s+class="question-example".*?</button\s*>', '', html, flags=re.S)
    html = re.sub(r'<div class="soft-note">.*?</div>',
                  '<div class="soft-note">当前仅使用循环水泵测试包，检索结果以实际上传并发布的资料为准。</div>', html, flags=re.S)
    return html.replace('工业配电与设备运维', '循环水泵知识构建测试').replace('如 BKT-A01', '如 BC-P-101')

def attach_industrial_web(app: FastAPI) -> None:
    root = files("graphrag_prod.playground").joinpath("static", "industrial")
    knowledge_root = files("graphrag_prod.playground").joinpath("static", "knowledge")

    @app.get("/industrial", response_class=HTMLResponse, include_in_schema=False)
    async def industrial_page() -> HTMLResponse:
        html = root.joinpath("index.html").read_text(encoding="utf-8")
        if getattr(app.state, "pump_only", False):
            html = pump_only_page(html)
        return HTMLResponse(html, headers=HEADERS)

    @app.get("/industrial/assets/{asset_path:path}", include_in_schema=False)
    async def industrial_asset(asset_path: str) -> Response:
        parts = asset_path.split("/")
        if getattr(app.state, "pump_only", False) and parts[0] == "demo":
            raise HTTPException(status_code=404, detail="asset not found")
        if (not parts or any(not part or part in {".", ".."} or "\\" in part
                or not all(c.isascii() and (c.isalnum() or c in "._-") for c in part)
                for part in parts)):
            raise HTTPException(status_code=404, detail="asset not found")
        if parts[0] == "knowledge":
            if len(parts) != 2 or parts[1] not in KNOWLEDGE_ASSETS:
                raise HTTPException(status_code=404, detail="asset not found")
            resource = knowledge_root.joinpath(parts[1])
        else:
            resource = root.joinpath(*parts)
        suffix = parts[-1].rsplit(".", 1)[-1]
        if suffix not in {"css", "mjs", "js", "json", "txt"} or not resource.is_file():
            raise HTTPException(status_code=404, detail="asset not found")
        media_type = "text/javascript" if suffix in {"js", "mjs"} else mimetypes.guess_type(parts[-1])[0]
        return Response(resource.read_bytes(), media_type=media_type or "text/plain", headers=HEADERS)
