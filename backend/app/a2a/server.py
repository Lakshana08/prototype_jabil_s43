"""Minimal Agent2Agent (A2A) protocol server - JSON-RPC 2.0 binding, protocol v1.0.

Per agent:
  GET  <base>/.well-known/agent-card.json   agent card (also GET <base>)
  POST <base>                               JSON-RPC methods:
                                              SendMessage            (0.3 name: message/send)
                                              SendStreamingMessage   (0.3 name: message/stream)
                                              GetTask                (0.3 name: tasks/get)
                                              CancelTask             (0.3 name: tasks/cancel)
For quick manual testing (Postman), POST <base> also accepts a plain {"query": "..."} body.

Response of SendMessage (same layout as the other Smart Material agents):
  {"jsonrpc": "2.0", "id": ..., "result": {"task": {
      "id", "contextId",
      "status": {"state": "TASK_STATE_COMPLETED",
                 "message": {"messageId", "contextId", "taskId", "role": "ROLE_AGENT",
                             "parts": [{"text": "<answer>"}, {"data": {<tool_results, prompts, trace ...>}}]}},
      "artifacts": [...]   only when an Excel / CSV file is attached
  }}}
Requests may use 1.0 parts ({"text": ...}) or 0.3 parts ({"kind": "text", "text": ...}).
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import uuid
from collections import OrderedDict
from typing import Any, Callable

from fastapi import APIRouter, Request
from fastapi.responses import Response, StreamingResponse

from app.planner.agent import AgentReply

log = logging.getLogger(__name__)

PROTOCOL_VERSION = "1.0"

# Task states (A2A 1.0)
SUBMITTED, WORKING, COMPLETED = "TASK_STATE_SUBMITTED", "TASK_STATE_WORKING", "TASK_STATE_COMPLETED"
FAILED, INPUT_REQUIRED = "TASK_STATE_FAILED", "TASK_STATE_INPUT_REQUIRED"

# JSON-RPC / A2A error codes
PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL_ERROR = -32700, -32600, -32601, -32602, -32603
TASK_NOT_FOUND, TASK_NOT_CANCELABLE = -32001, -32002

# 1.0 method names, plus the 0.3 names still sent by older clients.
METHODS = {
    "SendMessage": "send", "message/send": "send",
    "SendStreamingMessage": "stream", "message/stream": "stream",
    "GetTask": "get", "tasks/get": "get",
    "CancelTask": "cancel", "tasks/cancel": "cancel",
}

MAX_TASKS = 1000  # in-memory task history (LRU)
ERROR_TEXT = "Data could not be retrieved - please try again or contact IT."


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


def _id() -> str:
    return str(uuid.uuid4())


def _json(payload: dict) -> Response:
    # default=str: dates / decimals in result rows never break the response
    return Response(json.dumps(payload, ensure_ascii=False, default=str), media_type="application/json")


def _rpc_result(req_id, result: dict) -> Response:
    return _json({"jsonrpc": "2.0", "id": req_id, "result": result})


def _rpc_error(req_id, err: RpcError) -> Response:
    error = {"code": err.code, "message": err.message}
    if err.data is not None:
        error["data"] = err.data
    return _json({"jsonrpc": "2.0", "id": req_id, "error": error})


# ====================================================================== router
def create_a2a_router(base_path: str, agent_card: dict, handler: Callable[..., AgentReply]) -> APIRouter:
    router = APIRouter()
    tasks: OrderedDict[str, dict] = OrderedDict()

    # ------------------------------------------------------------------ agent card
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
            return _rpc_error(None, RpcError(PARSE_ERROR, "Parse error: body is not valid JSON"))

        # Postman shortcut: {"query": "..."} (extra keys become the data part, e.g. "skill")
        if isinstance(body, dict) and "jsonrpc" not in body and ("query" in body or "text" in body):
            task = await asyncio.to_thread(_run, _simple_message(body), handler, tasks)
            return _rpc_result(body.get("id"), {"task": task})

        req_id = body.get("id") if isinstance(body, dict) else None
        try:
            if not isinstance(body, dict) or body.get("jsonrpc") != "2.0" or not isinstance(body.get("method"), str):
                raise RpcError(INVALID_REQUEST, "Invalid JSON-RPC 2.0 request")
            action = METHODS.get(body["method"])
            params = body.get("params") or {}

            if action == "send":
                task = await asyncio.to_thread(_run, _validate_message(params), handler, tasks)
                return _rpc_result(req_id, {"task": task})
            if action == "stream":
                events = _stream(req_id, _validate_message(params), handler, tasks)
                return StreamingResponse(events, media_type="text/event-stream")
            if action == "get":
                return _rpc_result(req_id, _get_task(params, tasks))
            if action == "cancel":
                task = _get_task(params, tasks)
                raise RpcError(TASK_NOT_CANCELABLE, f"Task {task['id']} is already {task['status']['state']}")
            raise RpcError(METHOD_NOT_FOUND, f"Method not found: {body['method']}")
        except RpcError as err:
            return _rpc_error(req_id, err)
        except Exception:
            correlation = _id()
            log.exception("A2A internal error correlationId=%s", correlation)
            return _rpc_error(req_id, RpcError(INTERNAL_ERROR, ERROR_TEXT, {"correlationId": correlation}))

    return router


# ====================================================================== running a message
def _run(message: dict, handler, tasks: OrderedDict) -> dict:
    """Ask the agent and return the finished Task."""
    text, structured = _extract_input(message)
    task_id = message.get("taskId") or _id()
    context_id = message.get("contextId") or _id()
    try:
        reply = handler(text, structured, context_id)
    except ValueError as exc:  # bad skill parameters -> the agent asks for input, not a crash
        msg = _agent_message(task_id, context_id, [{"text": str(exc)}])
        return _store(tasks, _task(task_id, context_id, INPUT_REQUIRED, msg))
    msg = _agent_message(task_id, context_id, [{"text": reply.text}, {"data": reply.data}])
    return _store(tasks, _task(task_id, context_id, COMPLETED, msg, _file_artifacts(reply)))


async def _stream(req_id, message: dict, handler, tasks: OrderedDict):
    """Server-sent events: task submitted -> working -> (file artifacts) -> final status with the answer."""
    task_id = message.get("taskId") or _id()
    context_id = message.get("contextId") or _id()
    message = {**message, "taskId": task_id, "contextId": context_id}

    def event(result: dict) -> str:
        return f"data: {json.dumps({'jsonrpc': '2.0', 'id': req_id, 'result': result}, ensure_ascii=False, default=str)}\n\n"

    def status_update(status: dict) -> str:
        return event({"statusUpdate": {"taskId": task_id, "contextId": context_id, "status": status}})

    yield event({"task": _task(task_id, context_id, SUBMITTED)})
    yield status_update({"state": WORKING})
    try:
        task = await asyncio.to_thread(_run, message, handler, tasks)
    except Exception:
        correlation = _id()
        log.exception("A2A stream error correlationId=%s", correlation)
        msg = _agent_message(task_id, context_id, [{"text": f"{ERROR_TEXT} (ref {correlation})"}])
        yield status_update({"state": FAILED, "message": msg})
        return
    for artifact in task.get("artifacts", []):
        yield event({"artifactUpdate": {"taskId": task_id, "contextId": context_id,
                                        "artifact": artifact, "lastChunk": True}})
    yield status_update(task["status"])


# ====================================================================== request parsing
def _validate_message(params: dict) -> dict:
    message = params.get("message") if isinstance(params, dict) else None
    if not isinstance(message, dict) or not isinstance(message.get("parts"), list) or not message["parts"]:
        raise RpcError(INVALID_PARAMS, "params.message with a non-empty 'parts' array is required")
    return message


def _extract_input(message: dict) -> tuple[str, dict]:
    """Question text and structured parameters. Accepts 1.0 parts ({"text"}) and 0.3 parts ({"kind": "text"})."""
    texts, structured = [], {}
    for part in message.get("parts", []):
        if isinstance(part.get("text"), str):
            texts.append(part["text"])
        elif isinstance(part.get("data"), dict):
            structured.update(part["data"])
    if not texts and not structured:
        raise RpcError(INVALID_PARAMS, "Message must contain a text part or a data part")
    return "\n".join(texts), structured


def _simple_message(body: dict) -> dict:
    parts = [{"text": str(body.get("query") or body.get("text") or "")}]
    extra = {k: v for k, v in body.items() if k not in {"query", "text", "id"}}
    if extra:
        parts.append({"data": extra})
    return {"messageId": _id(), "role": "ROLE_USER", "parts": parts}


def _get_task(params: dict, tasks: OrderedDict) -> dict:
    task_id = params.get("id") if isinstance(params, dict) else None
    if not task_id:
        raise RpcError(INVALID_PARAMS, "params.id is required")
    if task_id not in tasks:
        raise RpcError(TASK_NOT_FOUND, f"Task not found: {task_id}")
    return tasks[task_id]


# ====================================================================== response objects
def _agent_message(task_id: str, context_id: str, parts: list[dict]) -> dict:
    return {"messageId": _id(), "contextId": context_id, "taskId": task_id, "role": "ROLE_AGENT", "parts": parts}


def _task(task_id: str, context_id: str, state: str, message: dict | None = None,
          artifacts: list[dict] | None = None) -> dict:
    status = {"state": state}
    if message:
        status["message"] = message
    task = {"id": task_id, "contextId": context_id, "status": status}
    if artifacts:
        task["artifacts"] = artifacts
    return task


def _file_artifacts(reply: AgentReply) -> list[dict]:
    """Excel / CSV exports as A2A 1.0 file parts (base64 in ``raw``)."""
    return [{"artifactId": _id(), "name": filename,
             "description": "Excel export" if filename.endswith(".xlsx") else "CSV export",
             "parts": [{"raw": base64.b64encode(content).decode("ascii"),
                        "filename": filename, "mediaType": mime}]}
            for filename, mime, content in reply.files]


def _store(tasks: OrderedDict, task: dict) -> dict:
    tasks[task["id"]] = task
    tasks.move_to_end(task["id"])
    while len(tasks) > MAX_TASKS:
        tasks.popitem(last=False)
    return task
