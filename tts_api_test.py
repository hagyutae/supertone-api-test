import os, struct, uuid
import requests, sounddevice as sd
from dotenv import load_dotenv

load_dotenv()

API_URL = "https://supertoneapi.com/v1/text-to-speech/{voice_id}/stream"
API_KEY = (os.getenv("SUPERTONE_API_KEY") or "").strip()
VOICE_ID = (os.getenv("SUPERTONE_VOICE_ID") or "").strip()

DEFAULT_LANGUAGE = os.getenv("SUPERTONE_LANGUAGE", "ko")
DEFAULT_STYLE = os.getenv("SUPERTONE_STYLE", "happy")
DEFAULT_MODEL = os.getenv("SUPERTONE_MODEL", "sona_speech_1")
DEFAULT_PITCH_VARIANCE = float(os.getenv("SUPERTONE_PITCH_VARIANCE", "1"))
DEFAULT_SPEED = float(os.getenv("SUPERTONE_SPEED", "1"))

TEXT_TO_SPEACH = "안녕? 만나서 반가워"

PAYLOAD = {
    "text": TEXT_TO_SPEACH,
    "language": DEFAULT_LANGUAGE,
    "style": DEFAULT_STYLE,
    "model": DEFAULT_MODEL,
    "voice_settings": {
        "pitch_variance": DEFAULT_PITCH_VARIANCE,
        "speed": DEFAULT_SPEED
    },
}

OUTPUT_DEVICE = os.getenv("OUTPUT_DEVICE", None)  # "7" 또는 장치명
MAX_HEADER_BYTES = 1024 * 1024  # 헤더 최대 1MB까지 모아봄
HEADER_DUMP = "./wav_header_dump.bin"

KSDATAFORMAT_SUBTYPE_PCM         = uuid.UUID('{00000001-0000-0010-8000-00aa00389b71}')
KSDATAFORMAT_SUBTYPE_IEEE_FLOAT  = uuid.UUID('{00000003-0000-0010-8000-00aa00389b71}')

def parse_guid_le(b16):
    d1, d2, d3 = struct.unpack('<IHH', b16[:8])
    d4 = b16[8:]
    return uuid.UUID(fields=(d1, d2, d3, d4[0], d4[1], d4[2:]))

def scan_chunks_streaming(buf: bytearray):
    """
    스트리밍 WAV 대응: 'data' 청크는 사이즈가 버퍼를 넘어도
    헤더(8바이트)만 보이면 '데이터 시작'으로 인정한다.
    """
    found = []
    if len(buf) < 12 or buf[:4] != b'RIFF' or buf[8:12] != b'WAVE':
        return found, None, None
    i = 12
    fmt = None
    data_off = None

    while i + 8 <= len(buf):
        cid = buf[i:i+4]
        # 사이즈 필드는 읽을 수 있어야 함(8바이트는 있어야 함)
        if i + 8 > len(buf):
            break
        size = struct.unpack('<I', buf[i+4:i+8])[0]

        # 일반 WAV: end = i+8+size
        end = i + 8 + size

        if cid == b'fmt ':
            # fmt는 완전히 들어와야 파싱 가능
            if end > len(buf):
                break
            if size >= 16:
                (audio_format, channels, rate, byte_rate, block_align, bits) = struct.unpack(
                    '<HHIIHH', buf[i+8:i+8+16]
                )
                fmt = {
                    "audio_format": audio_format,
                    "channels": channels,
                    "rate": rate,
                    "bits": bits,
                    "block_align": block_align,
                    "byte_rate": byte_rate,
                }
            found.append((cid, i, size))
            i = end
            continue

        if cid == b'data':
            # ⭐ 스트리밍 대응: 사이즈가 커서 end>len(buf) 여도 '데이터 시작'으로 인정
            if fmt is None:
                # fmt가 먼저 와야 포맷을 알 수 있음 → 더 받자
                break
            data_off = i + 8  # 여기부터가 PCM 스트림
            found.append((cid, i, size))
            # data 이후는 전부 오디오 데이터로 취급하므로 루프 종료
            return found, fmt, data_off

        # 그 외 LIST/fact/JUNK 등: 완전히 들어온 것만 확정
        if end > len(buf):
            break
        found.append((cid, i, size))
        i = end

    return found, fmt, data_off

def dtype_for(code, bits):
    if code == 1:  # PCM
        if bits == 8:  return "int8"
        if bits == 16: return "int16"
        if bits == 24: return "int24"  # 일부 드라이버 비호환 시 24→32 변환 필요
        if bits == 32: return "int32"
    if code == 3 and bits == 32:
        return "float32"
    raise ValueError(f"지원하지 않는 포맷 code={code}, bits={bits}")

def device_value(s):
    if not s: return None
    try: return int(s)
    except ValueError: return s

def play_stream():
    if not API_KEY:
        raise RuntimeError("환경변수 SUPERTONE_API_KEY가 비어 있음 (SUPERTONE_API_KEY)")

    url = API_URL.format(voice_id=VOICE_ID)
    headers = {"Content-Type": "application/json", "x-sup-api-key": API_KEY}

    with requests.post(url, headers=headers, json=PAYLOAD, stream=True) as r:
        print(f"[HTTP] status={r.status_code}")
        for k, v in r.headers.items():
            print(f"[HTTP] {k}: {v}")
        r.raise_for_status()

        buf = bytearray()
        data_started = False
        stream = None
        dev = device_value(OUTPUT_DEVICE)

        try:
            for chunk in r.iter_content(chunk_size=4096):
                if not chunk:
                    continue
                if not data_started:
                    buf.extend(chunk)
                    # 디버그: 청크 위치 스캔
                    found, fmt, data_off = scan_chunks_streaming(buf)

                    # 최초 1회 혹은 새 청크 발견 시 로그
                    if found:
                        # 마지막 청크 정보만 출력(시끄러움 방지)
                        cid, start, size = found[-1]
                        print(f"[HDR] seen chunk id={cid.decode('ascii','ignore')} start={start} size={size} (buf={len(buf)})")

                    if fmt and data_off is not None:
                        # 최종 포맷 결정
                        code = fmt["audio_format"]
                        if code == 0xFFFE:
                            code = fmt.get("resolved_format") or 1
                        bits = fmt["bits"]
                        ch   = fmt["channels"]
                        rate = fmt["rate"]
                        print(f"[WAV] format_code={code} bits={bits} channels={ch} rate={rate} (header_bytes={len(buf)})")

                        dtype = dtype_for(code, bits)

                        # 스트림 오픈
                        stream = sd.RawOutputStream(
                            samplerate=rate,
                            channels=ch,
                            dtype=dtype,
                            device=dev,     # None이면 기본 출력
                            blocksize=0,
                        )
                        stream.start()

                        # 헤더 뒤에 붙어온 PCM 먼저 흘리기
                        initial_pcm = buf[data_off:]
                        if initial_pcm:
                            stream.write(initial_pcm)
                        data_started = True

                    elif len(buf) > MAX_HEADER_BYTES:
                        with open(HEADER_DUMP, "wb") as f:
                            f.write(buf)
                        raise RuntimeError(f"WAV 헤더를 {MAX_HEADER_BYTES}바이트 안에 못 찾음. 덤프 저장: {HEADER_DUMP}")

                else:
                    # 재생 중: 들어오는 청크 즉시 재생
                    stream.write(chunk)

        finally:
            if stream:
                stream.stop()

if __name__ == "__main__":
    play_stream()
