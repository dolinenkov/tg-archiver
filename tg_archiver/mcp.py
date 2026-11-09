"""A small read-only MCP Streamable HTTP interface to the local archive."""
import asyncio
import json
from pathlib import Path
import uuid
import zipfile
from urllib.parse import urlsplit

from aiohttp import web
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from .config import CATEGORIES


def schema(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


PAGE = {"type": "integer", "minimum": 0}
TOOLS = [
    {"name": "find_chats", "description": "Find archived chats by title. Paginate with offset.",
     "inputSchema": schema({"query": {"type": "string"}, "offset": PAGE})},
    {"name": "search_messages", "description": "Search all captured message revisions and transcripts using SQLite FTS5 syntax; quote a phrase for an exact phrase. Paginate with offset.",
     "inputSchema": schema({"query": {"type": "string"}, "chat_id": {"type": "integer"}, "offset": PAGE}, ["query"])},
    {"name": "list_attachments", "description": "List archived media in a chat, including downloadable local paths and availability. Paginate with offset. Categories include voice, videos and video_notes.",
     "inputSchema": schema({"chat_id": {"type": "integer"}, "categories": {"type": "array", "items": {"type": "string", "enum": sorted(CATEGORIES)}}, "offset": PAGE}, ["chat_id"])},
    {"name": "export_attachments", "description": "Save all available attachments in a chat, optionally filtered by category, to a ZIP in the server's exports directory. Returns the server-local path and a manifest of missing files.",
     "inputSchema": schema({"chat_id": {"type": "integer"}, "categories": {"type": "array", "items": {"type": "string", "enum": sorted(CATEGORIES)}}}, ["chat_id"])},
]


def validate(name, args):
    if not isinstance(args, dict):
        raise ValueError("Arguments must be an object")
    spec = next((tool for tool in TOOLS if tool["name"] == name), None)
    if spec is None:
        raise ValueError("Unknown tool")
    props = spec["inputSchema"]["properties"]
    if set(args) - set(props) or any(key not in args for key in spec["inputSchema"]["required"]):
        raise ValueError("Invalid arguments")
    for key, value in args.items():
        typ = props[key]["type"]
        if (typ == "integer" and type(value) is not int or typ == "string" and not isinstance(value, str)
                or typ == "array" and (not isinstance(value, list) or any(not isinstance(v, str) or v not in CATEGORIES for v in value))):
            raise ValueError(f"Invalid {key}")
    if args.get("offset", 0) < 0 or len(args.get("query", "")) > 500:
        raise ValueError("Invalid offset or query")
    if name == "search_messages" and not args["query"].strip():
        raise ValueError("Query must not be empty")
    return args


async def call_tool(db, config, name, arguments):
    args = validate(name, arguments)
    if name == "export_attachments":
        items = []
        offset = 0
        while True:
            page = await call_tool(db, config, "list_attachments", dict(args, offset=offset))
            items.extend(page["items"])
            if page["next_offset"] is None:
                break
            offset = page["next_offset"]
        directory = config.data_dir / "exports"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        target = directory / ("attachments-" + uuid.uuid4().hex + ".zip")

        def write_export():
            try:
                with zipfile.ZipFile(target, "x", compression=zipfile.ZIP_STORED) as archive:
                    for item in items:
                        if item["local_path"]:
                            filename = Path(item["name"].replace("\\", "/")).name or "attachment"
                            archive.write(item["local_path"], f"media/{item['id']}-{item['message_id']}-{filename}")
                    archive.writestr("manifest.json", json.dumps(items, ensure_ascii=False, indent=2))
            except BaseException:
                target.unlink(missing_ok=True)
                raise

        await asyncio.to_thread(write_export)
        return {"path": str(target.resolve()), "attachments": len(items),
                "missing": sum(item["local_path"] is None for item in items)}
    params = {"offset": args.get("offset", 0), "limit": 101}
    if name == "find_chats":
        params["query"] = "%" + args.get("query", "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        sql = "SELECT id, title, kind FROM chats WHERE title LIKE :query ESCAPE '\\' ORDER BY title, id LIMIT :limit OFFSET :offset"
    elif name == "search_messages":
        params["query"] = args["query"]
        sql = """SELECT v.id AS version_id, v.body, m.chat_id, c.title AS chat_title,
                 m.telegram_id AS message_id, m.sent_at, m.deleted_at,
                 (SELECT group_concat(t.body, char(10)) FROM transcriptions t JOIN version_attachments va
                  ON va.attachment_id=t.attachment_id WHERE va.version_id=v.id AND t.body!='') AS transcript
                 FROM search JOIN versions v ON v.id=search.rowid JOIN messages m ON m.id=v.message_id
                 JOIN chats c ON c.id=m.chat_id WHERE search MATCH :query"""
        if "chat_id" in args:
            sql += " AND m.chat_id=:chat_id"
            params["chat_id"] = args["chat_id"]
        sql += " ORDER BY m.sent_at DESC, v.id DESC LIMIT :limit OFFSET :offset"
    else:
        params["chat_id"] = args["chat_id"]
        sql = """SELECT DISTINCT a.id, a.category, a.name, a.mime, a.size, a.state, a.path,
                 m.telegram_id AS message_id, m.sent_at FROM attachments a
                 JOIN version_attachments va ON va.attachment_id=a.id JOIN versions v ON v.id=va.version_id
                 JOIN messages m ON m.id=v.message_id WHERE m.chat_id=:chat_id"""
        if args.get("categories"):
            keys = []
            for index, category in enumerate(args["categories"]):
                key = f"category{index}"
                keys.append(":" + key)
                params[key] = category
            sql += " AND a.category IN (" + ",".join(keys) + ")"
        sql += " ORDER BY m.sent_at DESC, a.id DESC LIMIT :limit OFFSET :offset"
    try:
        async with db.session() as session:
            rows = [dict(row) for row in (await session.execute(text(sql), params)).mappings().all()]
    except OperationalError as error:
        if name == "search_messages" and any(word in str(error).lower() for word in ("fts5", "syntax", "unterminated", "no such column")):
            raise ValueError("Invalid FTS5 search expression") from None
        raise
    more = len(rows) > 100
    rows = rows[:100]
    if name == "list_attachments":
        root = (config.data_dir / "media").resolve()
        for row in rows:
            path = row.pop("path")
            target = (root / path).resolve() if path else None
            row["local_path"] = str(target) if row["state"] == "complete" and target and target.is_relative_to(root) and target.is_file() else None
    return {"items": rows, "next_offset": args.get("offset", 0) + 100 if more else None}


def make_mcp_app(db, config):
    app = web.Application(client_max_size=1024 * 1024)

    async def handler(request):
        # Browser origins are rejected: MCP clients do not require browser access.
        if request.headers.get("Origin"):
            raise web.HTTPForbidden(text="Browser origins are not allowed")
        host = urlsplit("//" + request.host).hostname
        allowed = {"localhost", "127.0.0.1", "::1", config.mcp_host.lower()}
        if not host or host.lower() not in allowed:
            raise web.HTTPForbidden(text="Invalid Host")
        if request.method != "POST":
            raise web.HTTPMethodNotAllowed(request.method, ["POST"])
        try:
            payload = await request.json()
        except (ValueError, UnicodeDecodeError):
            return web.json_response({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}, status=400)
        if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0" or not isinstance(payload.get("method"), str):
            return web.json_response({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}, status=400)
        method, request_id = payload["method"], payload.get("id")
        if "id" not in payload:
            return web.Response(status=202)
        error = None
        params = payload.get("params", {})
        if not isinstance(params, dict):
            error = {"code": -32602, "message": "Invalid params"}
            result = None
        elif method == "initialize":
            version = params.get("protocolVersion")
            result = {"protocolVersion": version if version in {"2025-03-26", "2025-06-18", "2025-11-25"} else "2025-11-25", "capabilities": {"tools": {}}, "serverInfo": {"name": "tg-archiver", "version": "0.1.0"}}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            try:
                data = await call_tool(db, config, params.get("name"), params.get("arguments", {}))
                result = {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}]}
            except ValueError as exc:
                result = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        else:
            result = None
            error = {"code": -32601, "message": "Method not found"}
        response = {"jsonrpc": "2.0", "id": request_id}
        response["error" if error else "result"] = error or result
        return web.json_response(response)

    app.router.add_route("*", "/mcp", handler)
    return app
