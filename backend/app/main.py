"""FastAPI app: upload a protocol, stream graph execution over SSE, approve the HITL gate."""

import json
from collections.abc import AsyncIterator
from uuid import uuid4

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from langchain_core.runnables import RunnableConfig

from app.config import get_settings
from app.graph.builder import graph
from app.services.pdf import extract_eligibility_text

# Resolve settings at import time so a misconfigured deployment fails at
# startup (e.g. LLM_PROVIDER=anthropic without ANTHROPIC_API_KEY), not
# mid-screening.
settings = get_settings()

app = FastAPI(title="Clinical Trial Protocol Screener")
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_methods=["*"],
    allow_headers=["*"],
)

# thread_id -> initial state; the graph checkpointer holds execution state
THREADS: dict[str, dict] = {}


@app.post("/api/screenings")
async def create_screening(file: UploadFile) -> dict:
    raw = await file.read()
    if file.filename and file.filename.lower().endswith(".pdf"):
        text = extract_eligibility_text(raw)
    else:
        text = raw.decode("utf-8", errors="replace")

    thread_id = str(uuid4())
    THREADS[thread_id] = {
        "raw_protocol_text": text,
        "source_filename": file.filename or "upload",
        "parsed_criteria": None,
        "compliance_passed": False,
        "critic_feedback": None,
        "parse_attempts": 0,
        "compliance_findings": [],
        "matched_patients": [],
        "events": [],
        "current_step": "routing",
    }
    return {"thread_id": thread_id}


@app.get("/api/screenings/{thread_id}/stream")
async def stream_screening(thread_id: str) -> StreamingResponse:
    if thread_id not in THREADS:
        raise HTTPException(404, "unknown thread_id")
    config: RunnableConfig = {"configurable": {"thread_id": thread_id}}

    async def generate() -> AsyncIterator[str]:
        async for chunk in graph.astream(THREADS[thread_id], config, stream_mode="updates"):
            for node, update in chunk.items():
                payload = {"node": node, "update": jsonable_encoder(update)}
                yield f"data: {json.dumps(payload)}\n\n"
        # Interrupted before matcher (awaiting approval) vs. fully finished
        pending = graph.get_state(config).next
        terminal = "__interrupt__" if pending else "__end__"
        yield f"data: {json.dumps({'node': terminal})}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.post("/api/screenings/{thread_id}/approve")
async def approve_screening(thread_id: str) -> dict:
    if thread_id not in THREADS:
        raise HTTPException(404, "unknown thread_id")
    config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
    if not graph.get_state(config).next:
        raise HTTPException(409, "screening is not awaiting approval")
    # Resume past the interrupt_before=["matcher"] gate
    result = graph.invoke(None, config)
    return {
        "matched_patients": result["matched_patients"],
        "events": jsonable_encoder(result["events"]),
    }


@app.get("/api/screenings/{thread_id}/state")
async def get_state(thread_id: str) -> dict:
    if thread_id not in THREADS:
        raise HTTPException(404, "unknown thread_id")
    config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
    snapshot = graph.get_state(config)
    return {"values": jsonable_encoder(snapshot.values), "pending": list(snapshot.next)}
