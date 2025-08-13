import asyncio
import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator
from typing import Optional, Dict, Any

import anyio
import httpx
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi import Request, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

load_dotenv()

SUPERTONE_API_KEY = (os.getenv("SUPERTONE_API_KEY") or "").strip()
SUPERTONE_VOICE_ID = (os.getenv("SUPERTONE_VOICE_ID") or "").strip()
if not SUPERTONE_API_KEY or not SUPERTONE_VOICE_ID:
    raise RuntimeError("SUPERTONE_API_KEY / SUPERTONE_VOICE_ID 환경변수를 설정하세요.")

SUPERTONE_URL = f"https://supertoneapi.com/v1/text-to-speech/{SUPERTONE_VOICE_ID}/stream"

@asynccontextmanager
async def lifespan(app: FastAPI):
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10.0, read=None, write=10.0, pool=None),
        headers={"x-sup-api-key": SUPERTONE_API_KEY, "Content-Type": "application/json"},
        http2=False,
    )
    app.state.client = client
    try:
        yield
    finally:
        await client.aclose()

app = FastAPI(lifespan=lifespan, title="Supertone TTS Stream Proxy")

class TTSRequest(BaseModel):
    text: str = Field(..., description="TTS 대상 텍스트 (<=300자)")
    language: Optional[str] = None
    style: Optional[str] = None
    model: Optional[str] = None
    pitch_variance: Optional[float] = None
    speed: Optional[float] = None

    @field_validator("text")
    def _len_guard(cls, v: str):
        s = v.strip()
        if not s:
            raise ValueError("text is required")
        if len(s) > 300:
            raise ValueError("text must be <= 300 characters")
        return s

DEFAULTS = {
    "language": os.getenv("SUPERTONE_LANGUAGE", "ko"),
    "style": os.getenv("SUPERTONE_STYLE", "happy"),
    "model": os.getenv("SUPERTONE_MODEL", "sona_speech_1"),
    "pitch_variance": float(os.getenv("SUPERTONE_PITCH_VARIANCE", "1")),
    "speed": float(os.getenv("SUPERTONE_SPEED", "1")),
}

def build_payload(body: TTSRequest) -> Dict[str, Any]:
    return {
        "text": body.text,
        "language": body.language or DEFAULTS["language"],
        **({"style": body.style or DEFAULTS["style"]} if (body.style or DEFAULTS["style"]) else {}),
        "model": body.model or DEFAULTS["model"],
        "voice_settings": {
            "pitch_variance": DEFAULTS["pitch_variance"] if body.pitch_variance is None else body.pitch_variance
            if "pitch_VARIANCE" in DEFAULTS else DEFAULTS["pitch_variance"] if body.pitch_variance is None else body.pitch_variance,
            "speed": DEFAULTS["speed"] if body.speed is None else body.speed,
        },
    }

@app.post("/tts/stream")
async def tts_stream(request: Request, body: TTSRequest):
    client: httpx.AsyncClient = request.app.state.client
    payload = build_payload(body)

    async def gen() -> AsyncGenerator[bytes, None]:
        sent = 0
        # 제너레이터 안에서 컨텍스트 오픈 → 응답 소비가 끝날 때까지 유지
        async with client.stream("POST", SUPERTONE_URL, json=payload) as upstream:
            # 헤더/상태 확인은 여기서 처리 (비200은 즉시 반환)
            if upstream.status_code != 200:
                data = await upstream.aread()
                ctype = upstream.headers.get("content-type", "")
                if "application/json" in ctype:
                    # 200이 아니므로 제너레이터 대신 즉시 예외로 끊고 핸들러 바깥에서 캐치
                    raise HTTPException(status_code=upstream.status_code,
                                        detail=httpx.Response(200, content=data).json())
                raise HTTPException(status_code=upstream.status_code,
                                    detail=(data.decode("utf-8", "ignore") or "Upstream error"))

            # 정상 스트리밍 전송
            try:
                async for chunk in upstream.aiter_bytes():
                    if chunk:
                        sent += len(chunk)
                        yield chunk
            except (httpx.ReadError, httpx.StreamClosed,
                    anyio.EndOfStream, anyio.ClosedResourceError,
                    anyio.BrokenResourceError, asyncio.CancelledError) as e:
                # 바디를 한 바이트도 못 보낸 경우엔 502로 명확히 실패
                if sent == 0:
                    # 제너레이터에서 예외를 다시 던지면 외부 StreamingResponse가 502로 마감되게 핸들링 필요
                    raise HTTPException(status_code=502, detail=f"Upstream stream error: {e.__class__.__name__}")
                # 일부라도 보냈으면 연결을 조용히 끊음(로그는 남기되 응답을 '성공'으로 위장하지 않음)
                return

    # StreamingResponse는 바깥에서 생성(미디어타입은 오디오로 고정하거나, 필요 시 사전 프리스텝으로 HEAD 호출)
    # 업스트림 header를 먼저 얻어야 한다면, gen() 구조를 살짝 바꿔 첫 yield 전에 헤더만 받아오는 프롤로그를 추가
    return StreamingResponse(
        gen(),
        media_type="audio/wav",
        headers={
            "Cache-Control": "no-cache",
            "X-Content-Type-Options": "nosniff",
        },
        status_code=200,
    )

@app.get("/healthz")
async def health():
    return {"ok": True}
