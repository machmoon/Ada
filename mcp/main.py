import os
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import JSONResponse
import mcp.llm_processor as llm_processor
import asyncio
import json

app = FastAPI(title="MCP Datasheet -> SKiDL (LLM)")

@app.get("/")
def root():
    return {"message": "MCP LLM server running"}

@app.post("/parse")
async def parse_endpoint(pdfUrl: str, part_name: str):
    # 2) Ask LLM to parse the datasheet into structured component list
    try:
        structured = llm_processor.parse_datasheet_with_llm(pdfUrl, part_name)
        print(structured)
    except Exception as e:
        print(e.__str__())
    return JSONResponse({"structured": structured})
