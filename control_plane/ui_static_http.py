from __future__ import annotations

from collections.abc import Callable
import mimetypes
from pathlib import Path
from urllib.parse import unquote


JsonResponse = Callable[..., list[bytes]]
StartResponse = Callable[[str, list[tuple[str, str]]], None]
StatusText = Callable[[int], str]


def serve_ui_route(
    *,
    start_response: StartResponse,
    trace_id: str,
    path: str,
    ui_static_root: Path,
    json_response: JsonResponse,
    http_status_text: StatusText,
) -> list[bytes]:
    index_path = ui_static_root / "index.html"
    if not index_path.is_file():
        return _not_found_response(
            start_response=start_response,
            trace_id=trace_id,
            path=path,
            json_response=json_response,
        )

    if path in {"/", "/ui", "/ui/"}:
        return _ui_file_response(
            start_response=start_response,
            file_path=index_path,
            cache_control="no-store",
            http_status_text=http_status_text,
        )

    if path.startswith("/ui/assets/"):
        relative_asset_path = unquote(path.removeprefix("/ui/"))
        if ".." in Path(relative_asset_path).parts:
            return _not_found_response(
                start_response=start_response,
                trace_id=trace_id,
                path=path,
                json_response=json_response,
            )
        asset_path = (ui_static_root / relative_asset_path).resolve()
        try:
            asset_path.relative_to(ui_static_root.resolve())
        except ValueError:
            return _not_found_response(
                start_response=start_response,
                trace_id=trace_id,
                path=path,
                json_response=json_response,
            )
        if not asset_path.is_file():
            return _not_found_response(
                start_response=start_response,
                trace_id=trace_id,
                path=path,
                json_response=json_response,
            )
        return _ui_file_response(
            start_response=start_response,
            file_path=asset_path,
            cache_control="public, max-age=31536000, immutable",
            http_status_text=http_status_text,
        )

    if path.startswith("/ui/"):
        return _ui_file_response(
            start_response=start_response,
            file_path=index_path,
            cache_control="no-store",
            http_status_text=http_status_text,
        )

    return _not_found_response(
        start_response=start_response,
        trace_id=trace_id,
        path=path,
        json_response=json_response,
    )


def _not_found_response(
    *,
    start_response: StartResponse,
    trace_id: str,
    path: str,
    json_response: JsonResponse,
) -> list[bytes]:
    return json_response(
        start_response=start_response,
        status_code=404,
        payload={
            "status": "rejected",
            "trace_id": trace_id,
            "error": {"code": "not_found", "message": f"No Launchplane route for {path}."},
        },
    )


def _ui_static_response(
    *,
    start_response: StartResponse,
    status_code: int,
    content: bytes,
    content_type: str,
    cache_control: str,
    http_status_text: StatusText,
) -> list[bytes]:
    status_line = f"{status_code} {http_status_text(status_code)}"
    start_response(
        status_line,
        [
            ("Content-Type", content_type),
            ("Content-Length", str(len(content))),
            ("Cache-Control", cache_control),
        ],
    )
    return [content]


def _ui_file_response(
    *,
    start_response: StartResponse,
    file_path: Path,
    cache_control: str,
    http_status_text: StatusText,
) -> list[bytes]:
    content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    return _ui_static_response(
        start_response=start_response,
        status_code=200,
        content=file_path.read_bytes(),
        content_type=content_type,
        cache_control=cache_control,
        http_status_text=http_status_text,
    )
