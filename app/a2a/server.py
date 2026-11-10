"""Minimal Agent2Agent (A2A) protocol server - JSON-RPC 2.0 transport, protocol v0.3.

Implements per agent:
  GET  <base>/.well-known/agent-card.json   agent card (also GET <base>)
  POST <base>                               JSON-RPC: message/send, message/stream,
                                            tasks/get, tasks/cancel
For quick manual testing, POST <base> also accepts a plain {"query": "..."} body and
returns the resulting Task.
"""
from __future__ import annotations

import base64
import json
import logging
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Callable

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.planner.agent import AgentReply

log = logging.getLogger(__name__)

PROTOCOL_VERSION = "0.3.0"

# JSON-RPC / A2A error codes
PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL_ERROR = -32700, -32600, -32601, -32602, -32603
TASK_NOT_FOUND, TASK_NOT_CANCELABLE = -32001, -32002

MAX_TASKS = 1000  # in-memory task history (LRU)


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _id() -> str:
    return str(uuid.uuid4())


def create_a2a_router(base_path: str, agent_card: dict, handler: Callable[[str, dict], AgentReply]) -> APIRouter:
    router = APIRouter()
    tasks: OrderedDict[str, dict] = OrderedDict()

    # ------------------------------------------------------------------ card
    @router.get(base_path + "/.well-known/agent-card.json")
    @router.get(base_path + "/.well-known/agent.json")  # pre-0.3 clients
    @router.get(base_path)
    async def card() -> dict:
        return agent_card

    # ------------------------------------------------------------------ JSON-RPC
    @router.post(base_path)
    async def rpc(request: Request):
        try:
            body = await request.json()
        except Exception:
            return _error_response(None, RpcError(PARSE_ERROR, "Parse error: body is not valid JSON"))

        if isinstance(body, dict) and "jsonrpc" not in body and ("query" in body or "text" in body):
            task = _run(_simple_message(body), handler, tasks)
            return JSONResponse(task)

        req_id = body.get("id") if isinstance(body, dict) else None
        try:
            if not isinstance(body, dict) or body.get("jsonrpc") != "2.0" or not isinstance(body.get("method"), str):
                raise RpcError(INVALID_REQUEST, "Invalid JSON-RPC 2.0 request")
            method, params = body["method"], body.get("params") or {}
            if method == "message/stream":
                message = _validate_message(params)
                return StreamingResponse(_stream(req_id, message, handler, tasks), media_type="text/event-stream")
            if method == "message/send":
                result = _run(_validate_message(params), handler, tasks)
            elif method == "tasks/get":
                result = _get_task(params, tasks)
            elif method == "tasks/cancel":
                task = _get_task(params, tasks)
                raise RpcError(TASK_NOT_CANCELABLE, f"Task {task['id']} is already {task['status']['state']}")
            else:
                raise RpcError(METHOD_NOT_FOUND, f"Method not found: {method}")
            return JSONResponse({"jsonrpc": "2.0", "id": req_id, "result": result})
        except RpcError as err:
            return _error_response(req_id, err)
        except Exception:
            correlation = _id()
            log.exception("A2A internal error correlationId=%s", correlation)
            return _error_response(req_id, RpcError(
                INTERNAL_ERROR, "Data could not be retrieved - please try again or contact IT.",
                {"correlationId": correlation}))

    return router


# ====================================================================== core
def _run(message: dict, handler, tasks: OrderedDict) -> dict:
    text, structured = _extract_input(message)
    task_id = message.get("taskId") or _id()
    context_id = message.get("contextId") or _id()
    try:
        reply = handler(text, structured)
    except ValueError as exc:  # bad skill parameters -> the agent asks for input, not a crash
        return _store(tasks, _task(task_id, context_id, message, "input-required", _agent_message(
            str(exc), task_id, context_id), []))
    agent_msg = _agent_message(reply.text, task_id, context_id)
    return _store(tasks, _task(task_id, context_id, message, "completed", agent_msg, _artifacts(reply)))


async def _stream(req_id, message: dict, handler, tasks: OrderedDict):
    task_id = message.get("taskId") or _id()
    context_id = message.get("contextId") or _id()
    message = {**message, "taskId": task_id, "contextId": context_id}

    def event(result: dict) -> str:
        return f"data: {json.dumps({'jsonrpc': '2.0', 'id': req_id, 'result': result}, ensure_ascii=False)}\n\n"

    yield event(_task(task_id, context_id, message, "submitted", None, []))
    yield event(_status_event(task_id, context_id, "working", None, final=False))
    try:
        task = _run(message, handler, tasks)
    except Exception:
        correlation = _id()
        log.exception("A2A stream error correlationId=%s", correlation)
        msg = _agent_message(f"Data could not be retrieved - please try again or contact IT. (ref {correlation})",
                             task_id, context_id)
        yield event(_status_event(task_id, context_id, "failed", msg, final=True))
        return
    for art in task["artifacts"]:
        yield event({"kind": "artifact-update", "taskId": task_id, "contextId": context_id,
                     "artifact": art, "append": False, "lastChunk": True})
    yield event(_status_event(task_id, context_id, task["status"]["state"], task["status"].get("message"), final=True))


def _validate_message(params: dict) -> dict:
    message = params.get("message") if isinstance(params, dict) else None
    if not isinstance(message, dict) or not isinstance(message.get("parts"), list) or not message["parts"]:
        raise RpcError(INVALID_PARAMS, "params.message with a non-empty 'parts' array is required")
    return message


def _extract_input(message: dict) -> tuple[str, dict]:
    texts, structured = [], {}
    for part in message.get("parts", []):
        kind = part.get("kind") or part.get("type")
        if kind == "text":
            texts.append(part.get("text", ""))
        elif kind == "data" and isinstance(part.get("data"), dict):
            structured.update(part["data"])
    if not texts and not structured:
        raise RpcError(INVALID_PARAMS, "Message must contain a text part or a data part")
    return "\n".join(texts), structured


def _simple_message(body: dict) -> dict:
    parts = [{"kind": "text", "text": str(body.get("query") or body.get("text") or "")}]
    extra = {k: v for k, v in body.items() if k not in {"query", "text"}}
    if extra:
        parts.append({"kind": "data", "data": extra})
    return {"kind": "message", "role": "user", "messageId": _id(), "parts": parts}


def _get_task(params: dict, tasks: OrderedDict) -> dict:
    task_id = params.get("id") if isinstance(params, dict) else None
    if not task_id:
        raise RpcError(INVALID_PARAMS, "params.id is required")
    if task_id not in tasks:
        raise RpcError(TASK_NOT_FOUND, f"Task not found: {task_id}")
    task = tasks[task_id]
    history_len = params.get("historyLength")
    if isinstance(history_len, int) and history_len >= 0:
        task = {**task, "history": task["history"][-history_len:] if history_len else []}
    return task


# ====================================================================== object builders
def _agent_message(text: str, task_id: str, context_id: str) -> dict:
    return {"kind": "message", "role": "agent", "messageId": _id(), "taskId": task_id, "contextId": context_id,
            "parts": [{"kind": "text", "text": text}]}


def _artifacts(reply: AgentReply) -> list[dict]:
    arts = [
        {"artifactId": _id(), "name": "planner-answer", "description": "Narrated answer (markdown)",
         "parts": [{"kind": "text", "text": reply.text}]},
        {"artifactId": _id(), "name": "planner-result", "description": "Structured business result",
         "parts": [{"kind": "data", "data": reply.data}]},
    ]
    for filename, mime, content in reply.files:
        arts.append({"artifactId": _id(), "name": filename,
                     "description": "Excel export" if filename.endswith(".xlsx") else "CSV export",
                     "parts": [{"kind": "file", "file": {
                         "name": filename, "mimeType": mime,
                         "bytes": base64.b64encode(content).decode("ascii")}}]})
    return arts


def _task(task_id, context_id, user_msg, state, agent_msg, artifacts) -> dict:
    user_msg = {**user_msg, "taskId": task_id, "contextId": context_id}
    status = {"state": state, "timestamp": _now()}
    if agent_msg:
        status["message"] = agent_msg
    return {"kind": "task", "id": task_id, "contextId": context_id, "status": status,
            "artifacts": artifacts, "history": [user_msg] + ([agent_msg] if agent_msg else [])}


def _status_event(task_id, context_id, state, message, final: bool) -> dict:
    status = {"state": state, "timestamp": _now()}
    if message:
        status["message"] = message
    return {"kind": "status-update", "taskId": task_id, "contextId": context_id, "status": status, "final": final}


def _store(tasks: OrderedDict, task: dict) -> dict:
    tasks[task["id"]] = task
    tasks.move_to_end(task["id"])
    while len(tasks) > MAX_TASKS:
        tasks.popitem(last=False)
    return task


def _error_response(req_id, err: RpcError) -> JSONResponse:
    error = {"code": err.code, "message": err.message}
    if err.data is not None:
        error["data"] = err.data
    return JSONResponse({"jsonrpc": "2.0", "id": req_id, "error": error})
