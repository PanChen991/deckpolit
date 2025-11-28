import os
import re
import json
import hashlib
import requests
import logging
import sys
from typing import Optional, Dict, Any, Literal
from urllib.parse import urljoin, urlparse, parse_qs

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

# ... (前面的 Config, App, Models 部分保持不变) ...

# =========================
# Config & App
# =========================
APP_NAME = "DeckPilot Backend (stream-only)"
SKY_ID  = os.getenv("SKYWORK_SECRET_ID", "")
SKY_KEY = os.getenv("SKYWORK_SECRET_KEY", "")
MCP_SSE = os.getenv("SKYWORK_MCP_SSE_URL", "https://api.skywork.ai/open/sse")

app = FastAPI(title=APP_NAME)

# CORS（按需放宽）
allowed_origins = [
    "http://localhost:3001", "http://127.0.0.1:3001",
    "http://localhost:8080", "http://127.0.0.1:8080"
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
    expose_headers=["Content-Type"],
)

@app.options("/{path:path}")
def options_handler():
    return {}

# =========================
# Models
# =========================
class MakeDeckReq(BaseModel):
    topic: str
    template_hint: Optional[str] = None
    use_network: bool = True
    mode: Literal["ppt", "ppt-fast", "doc", "excel"] = "ppt-fast"
    outline_md: Optional[str] = None

class ToolsCallReq(BaseModel):
    endpoint: str = Field(..., description="Skywork /open/message 的完整 URL（来自 endpoint 事件）")
    name: str = Field(..., description="工具名，例如 gen_ppt_fast")
    arguments: Dict[str, Any] = Field(default_factory=dict)
    context: Dict[str, Any] = Field(default_factory=dict)

# =========================
# Logging Config (全量日志记录)
# =========================
# 同时输出到 控制台 和 server.log 文件
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler("server.log", encoding='utf-8'), # 记录到文件
        logging.StreamHandler(sys.stdout)                    # 输出到黑窗口
    ]
)
logger = logging.getLogger("DeckPilot")
# =========================
# Utils (优化版)
# =========================
def sky_sign(secret_id: str, secret_key: str) -> str:
    return hashlib.md5(f"{secret_id}:{secret_key}".encode("utf-8")).hexdigest()

def build_query(req: MakeDeckReq) -> str:
    # ... (保持不变) ...
    if req.outline_md:
        prefix = f"请基于以下大纲生成：\n{req.outline_md}\n"
    else:
        mode_desc = {
            "ppt": "{topic}",
            "ppt-fast": "{topic}",
            "doc": "{topic}",
            "excel": "{topic}"
        }
        prefix = mode_desc.get(req.mode, mode_desc["ppt-fast"]).format(topic=req.topic)
    hint = f" 模板/品牌要求：{req.template_hint}。" if req.template_hint else ""
    return (prefix + hint).strip()

# —— 优化正则，增加容错 ——
URL_RE = re.compile(r'https?://[^\s"\'<>]+', re.IGNORECASE)
PREFERRED_EXTS = tuple(os.getenv("DECKPILOT_PREFERRED_EXTS", "pptx,docx,xlsx,pdf,zip").lower().split(","))

def _safe_decode(b_line: bytes) -> str:
    """强制使用 UTF-8 解码，忽略错误，防止流中断"""
    try:
        return b_line.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return b_line.decode("utf-8", errors="ignore")
        except Exception:
            return b_line.decode("latin1", errors="ignore")

def _collect_urls_from_text(text: str) -> list[str]:
    """简单粗暴地从文本中提取所有 URL"""
    return list(set(URL_RE.findall(text)))

def _score_url(u: str) -> int:
    # ... (保持不变) ...
    s = u.lower()
    score = 10
    for i, ext in enumerate(PREFERRED_EXTS, start=1):
        if s.endswith("." + ext):
            score += 100 - i
    if "download" in s or "export" in s or "file" in s:
        score += 15
    if "signature=" in s or "token=" in s:
        score += 10
    return score

def _pick_best_url(urls: list[str]) -> Optional[str]:
    if not urls:
        return None
    return sorted(urls, key=_score_url, reverse=True)[0]

def _endpoint_and_context_from_data(data: str):
    # ... (保持不变) ...
    endpoint = None
    ctx = {}
    if data.startswith("{"):
        try:
            meta = json.loads(data)
            endpoint = meta.get("endpoint") or meta.get("url")
            if isinstance(meta.get("context"), dict):
                ctx.update(meta["context"])
            for k in ("session_id","sessionId","conversation_id","conversationId","channel","room"):
                if k in meta:
                    ctx[k] = meta[k]
        except Exception:
            endpoint = data
    else:
        endpoint = data
    if not endpoint:
        return None, ctx
    if endpoint.startswith("/"):
        endpoint = urljoin("https://api.skywork.ai", endpoint)
    # 尝试从 URL 参数里捞 session
    try:
        parsed = urlparse(endpoint)
        q = parse_qs(parsed.query)
        for k in ("sessionId","session_id","conversationId","conversation_id"):
            if k in q and q[k]:
                ctx[k] = q[k][0]
    except Exception:
        pass
    return endpoint, ctx

# ... (Health, Mock Chat, Tools Proxy 保持不变) ...
# =========================
# Health & Mock Chat (给 WebUI 连接探活)
# =========================
@app.get("/health")
def health():
    return {"status": "ok", "service": APP_NAME}

@app.post("/v1/chat/completions")
async def chat_completions(_: Request):
    return {
        "id": "deckpilot-test",
        "object": "chat.completion",
        "created": 1234567890,
        "model": "deckpilot-mock",
        "choices": [{
            "index": 0,
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": "DeckPilot 后端在线。使用 /make-deck-stream 触发 Skywork 生成；如需手动调 tools/call 用 /tools/call-proxy。"}
        }]
    }

# =========================
# 手动 tools/call 代理（可选保留）
# =========================
@app.post("/tools/call-proxy")
def tools_call_proxy(req: ToolsCallReq):
    if not SKY_ID or not SKY_KEY:
        raise HTTPException(500, "Server is not configured with SKYWORK_SECRET_ID/KEY")
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "secret_id": SKY_ID,
        "sign": sky_sign(SKY_ID, SKY_KEY),
        "method": "tools/call",
        "params": {
            "name": req.name,
            "arguments": req.arguments
        }
    }
    if req.context:
        payload["params"]["context"] = req.context
    try:
        r = requests.post(req.endpoint, json=payload, headers={"Accept": "application/json"}, timeout=60)
        return {"status_code": r.status_code, "headers": dict(r.headers), "text": r.text}
    except requests.RequestException as e:
        raise HTTPException(502, f"POST failed: {e}")

# =========================
# 核心修复部分
# =========================
@app.post("/make-deck-stream")
def make_deck_stream(req: MakeDeckReq):
    # [LOG] 记录请求的完整参数，确认 OpenWebUI 是否传入了页数限制
    logger.info(f">>> [收到请求] Topic: {req.topic} | UseNetwork: {req.use_network} | Mode: {req.mode}")

    if not SKY_ID or not SKY_KEY:
        raise HTTPException(500, "Server is not configured with SKYWORK_SECRET_ID/KEY")

    mode_to_tool = {"ppt":"gen_ppt", "ppt-fast":"gen_ppt_fast", "doc":"gen_doc", "excel":"gen_excel"}
    tool_name = mode_to_tool.get(req.mode, "gen_ppt_fast")
    export_ext = {"gen_ppt":"pptx", "gen_ppt_fast":"pptx", "gen_doc":"docx", "gen_excel":"xlsx"}[tool_name]

    query_text = build_query(req)
    sse_params = {
        "secret_id": SKY_ID,
        "sign": sky_sign(SKY_ID, SKY_KEY),
        "query": query_text,
        "use_network": str(req.use_network).lower(),
        "status_updates": "true",
        "debug": "true",
    }

    def event_stream():
        headers = {"Accept": "text/event-stream", "Connection": "keep-alive"}
        sent_done = False

        logger.info(f"--- 开始连接 Skywork SSE: {MCP_SSE} ---")

        try:
            # 使用 decode_unicode=False 获取原始字节，防止中文乱码中断连接
            with requests.get(MCP_SSE, params=sse_params, headers=headers, stream=True, timeout=1200) as r:
                if r.status_code != 200:
                    err_msg = f"SSE connect failed: {r.text}"
                    logger.error(f"!!! [Skywork Error] {err_msg}")
                    yield f"event: error\ndata: {err_msg}\n\n"
                    yield "event: done\ndata: [DONE]\n\n"
                    return

                endpoint_url = None
                called = False
                context = {}
                buffer = []

                def flush_and_emit(buf):
                    nonlocal endpoint_url, called, context, sent_done
                    if not buf or sent_done:
                        return

                    # 1. 这里是关键！必须先定义 data_str，才能在下面打日志
                    name, data_str = "message", ""
                    for ln in buf:
                        if ln.startswith("event:"):
                            name = ln.split(":", 1)[1].strip()
                        elif ln.startswith("data:"):
                            data_str += ln.split(":", 1)[1].lstrip() + "\n"
                    
                    data_str = data_str.strip()
                    if not data_str:
                        return

                    # 2. 现在可以安全地打日志了
                    # log_snippet = data_str[:100] + "..." if len(data_str) > 100 else data_str
                    # logger.info(f"<-- [Skywork Raw] Event: {name} | Data len: {len(data_str)}")

                    # 3. 优先搜索 URL (抢跑逻辑)
                    raw_urls = _collect_urls_from_text(data_str)
                    best_link = _pick_best_url(raw_urls)
                    
                    if best_link:
                        best_link = best_link.replace(r"\u0026", "&")
                        logger.info(f">>> [任务完成] 捕获到下载链接: {best_link}")
                        yield "event: done\ndata: " + json.dumps(
                            {"download_url": best_link}, ensure_ascii=False
                        ) + "\n\n"
                        yield ": EOF\n\n"
                        sent_done = True
                        return

                    # 4. JSON 解析与逻辑处理
                    json_obj = None
                    try:
                        json_obj = json.loads(data_str)
                    except Exception:
                        pass

                    if isinstance(json_obj, dict) and json_obj.get("method") == "ping":
                        # 发送一个看不见的字符或者点，保持连接活跃
                        yield "event: log\ndata: ...\n\n"
                        return

                    # Endpoint 处理
                    if name == "endpoint":
                        endpoint_url, ctx = _endpoint_and_context_from_data(data_str)
                        if ctx:
                            context.update(ctx)

                        if not called and endpoint_url:
                            logger.info(f"--> [Trigger] 正在向 {endpoint_url} 发起 tools/call")
                            arguments = {
                                "query": sse_params.get("query", ""),
                                "use_network": sse_params.get("use_network", "false"),
                                "export": export_ext,
                                "status_updates": True,
                                "debug": True,
                            }
                            payload = {
                                "jsonrpc": "2.0",
                                "id": 1,
                                "secret_id": SKY_ID,
                                "sign": sky_sign(SKY_ID, SKY_KEY),
                                "method": "tools/call",
                                "params": {"name": tool_name, "arguments": arguments},
                            }
                            if context:
                                payload["params"]["context"] = context
                            
                            try:
                                _ = requests.post(
                                    endpoint_url,
                                    json=payload,
                                    headers={"Accept": "application/json"},
                                    timeout=60,
                                )
                            except Exception as e:
                                logger.error(f"!!! [Tools Call Failed] {str(e)}")
                                yield f"event: error\ndata: tools/call failed: {str(e)}\n\n"
                                if not sent_done:
                                    yield "event: done\ndata: [DONE]\n\n"
                                    sent_done = True
                                return
                            
                            called = True
                            # 明确告诉前端正在生成
                            yield "event: log\ndata: 已连接引擎，正在生成大纲与内容...\n\n"
                        return

                    if "token exhausted" in data_str.lower():
                        logger.warning("!!! Token Exhausted")
                        yield "event: error\ndata: Token exhausted\n\n"
                        if not sent_done:
                            yield "event: done\ndata: [DONE]\n\n"
                            sent_done = True
                        return

                    # 5. 日志推送给前端 (只推有意义的文本)
                    log_msg = None
                    if isinstance(json_obj, dict):
                        if "message" in json_obj:
                            log_msg = json_obj["message"]
                        elif "content" in json_obj:
                             c = json_obj["content"]
                             if isinstance(c, list) and len(c) > 0 and "text" in c[0]:
                                 t = c[0]["text"]
                                 if not t.strip().startswith("{"):
                                     log_msg = t
                    
                    if not log_msg and not data_str.startswith("{") and len(data_str) < 200:
                        log_msg = data_str

                    if log_msg:
                         # logger.info(f">>> [推送前端] {log_msg}")
                         yield f"event: log\ndata: {log_msg}\n\n"

                # —— 读取循环 —— 
                for raw_line in r.iter_lines(decode_unicode=False):
                    if sent_done:
                        break
                    
                    if raw_line:
                        # 必须使用 _safe_decode，在 app.py 其他地方定义的那个函数
                        line = _safe_decode(raw_line).strip()
                        buffer.append(line)
                    else:
                        for out in (flush_and_emit(buffer) or []):
                            yield out
                        buffer = []

                if not sent_done:
                    logger.info("--- Stream ended without explicit DONE, sending DONE manually ---")
                    yield "event: done\ndata: [DONE]\n\n"
                    sent_done = True

        except Exception as e:
            logger.error(f"!!! [Stream Exception] {str(e)}")
            yield f"event: error\ndata: {str(e)}\n\n"
            if not sent_done:
                yield "event: done\ndata: [DONE]\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")