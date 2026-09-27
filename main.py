from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
import asyncio
import base64
import json
import time
import uuid

from memory import cria_tabela, listar_memorias, processar_turno

app = FastAPI(title="Nora IA - WebSocket Relay")

# Tabela de memórias locais do relay (SQLite clássico)
cria_tabela()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================================
# CONEXÕES
# ============================================================

pc_connection: WebSocket | None = None
celular_connection: WebSocket | None = None
lock = asyncio.Lock()

# Requisições HTTP aguardando resposta do PC (proxy → WebSocket)
pending_futures: dict[str, asyncio.Future] = {}

# Última transcrição do usuário (usada junto da resposta para extrair memórias)
ultima_transcricao: str = ""

def _resolver_mensagem_do_pc(texto: str):
    """Se o PC respondeu uma requisição HTTP (http_*_response), resolve a future."""
    try:
        dados = json.loads(texto)
    except Exception:
        return
    rid = dados.get("id")
    if rid and rid in pending_futures and not pending_futures[rid].done():
        pending_futures[rid].set_result(dados)


def _capturar_turno(texto: str):
    """Observa transcrição/resposta entre PC e celular e extrai memórias importantes."""
    global ultima_transcricao
    try:
        dados = json.loads(texto)
    except Exception:
        return
    tipo = dados.get("tipo") or dados.get("type")
    if tipo == "transcricao" and dados.get("texto"):
        ultima_transcricao = dados["texto"]
    elif tipo == "resposta" and dados.get("texto"):
        resposta = dados["texto"]
        usuario = ultima_transcricao
        ultima_transcricao = ""
        asyncio.create_task(
            asyncio.to_thread(processar_turno, usuario, resposta)
        )

def _falhar_pendentes():
    for future in list(pending_futures.values()):
        if not future.done():
            future.set_exception(HTTPException(status_code=503, detail="PC da Nora desconectado."))
    pending_futures.clear()

async def _enviar_controle(ws: WebSocket, tipo: str, payload: dict) -> str:
    """Envia um JSON de controle para o PC e retorna o id da requisição."""
    rid = uuid.uuid4().hex
    if ws is None:
        raise HTTPException(status_code=503, detail="PC da Nora não está conectado no momento.")
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    pending_futures[rid] = future
    await ws.send_text(json.dumps({"tipo": tipo, "id": rid, **payload}))
    return rid

async def _aguardar_resposta(rid: str, timeout: float = 60.0) -> dict:
    try:
        return await asyncio.wait_for(pending_futures[rid], timeout=timeout)
    except asyncio.TimeoutError:
        pending_futures.pop(rid, None)
        raise HTTPException(status_code=504, detail="O PC da Nora não respondeu a tempo.")

# ============================================================
# STATUS
# ============================================================

@app.get("/")
async def root():
    return {
        "status": "online",
        "service": "Nora IA WebSocket Relay",
        "version": "2.0.0"
    }

@app.get("/status")
async def status():
    return {
        "pc_conectado": pc_connection is not None,
        "celular_conectado": celular_connection is not None,
        "timestamp": time.time()
    }

@app.get("/memorias")
async def memorias(limite: int = 20, importancia_min: int | None = None):
    """Lista as memórias importantes salvas pelo relay (SQLite local)."""
    return {"memorias": listar_memorias(limite=limite, importancia_min=importancia_min)}

# ============================================================
# PROXY HTTP → PC (usado pelo front-end web / desktop)
# O front-end só envia áudio/texto; todo o processamento
# (STT, chat, TTS) acontece no PC e volta por aqui.
# ============================================================

async def _stt_proxy(file: UploadFile):
    if pc_connection is None:
        raise HTTPException(status_code=503, detail="PC da Nora não está conectado no momento.")
    audio = await file.read()
    rid = await _enviar_controle(pc_connection, "http_stt", {})
    try:
        await pc_connection.send_bytes(audio)
    except Exception as e:
        pending_futures.pop(rid, None)
        raise HTTPException(status_code=502, detail=f"Falha ao enviar áudio ao PC: {e}")
    resposta = await _aguardar_resposta(rid)
    return {
        "texto": resposta.get("texto", ""),
        "duracao_segundos": resposta.get("duracao_segundos", 0.0) or 0.0,
        "idioma": resposta.get("idioma", "pt"),
    }

@app.post("/transcrever/texto")
async def transcrever_texto(file: UploadFile = File(...)):
    """Transcrição de áudio (somente texto, sem resposta oral)."""
    return await _stt_proxy(file)

@app.post("/transcrever")
async def transcrever_audio(file: UploadFile = File(...)):
    """Transcrição de áudio de fallback (compatibilidade)."""
    return await _stt_proxy(file)

@app.post("/api/chat")
async def api_chat(req: Request):
    """Chat por texto com o cérebro da Nora (tools reais)."""
    corpo = await req.json()
    texto = (corpo.get("texto") or "").strip()
    if not texto:
        raise HTTPException(status_code=400, detail="texto vazio.")
    rid = await _enviar_controle(pc_connection, "http_chat", {"texto": texto, "contexto": corpo.get("contexto") or ""})
    resposta = await _aguardar_resposta(rid)
    return {"ok": True, "resposta": resposta.get("resposta", "")}

@app.post("/api/avatar/falar")
async def api_avatar_falar(req: Request):
    """Sintetiza a fala no PC e devolve o áudio base64 para o front web."""
    if pc_connection is None:
        raise HTTPException(status_code=503, detail="PC da Nora não está conectado no momento.")
    corpo = await req.json()
    texto = (corpo.get("texto") or "").strip()
    if not texto:
        return {"ok": False, "audio_b64": None, "erro": "texto vazio"}
    rid = await _enviar_controle(pc_connection, "http_tts", {"texto": texto, "voz": corpo.get("voz") or ""})
    resposta = await _aguardar_resposta(rid)
    if resposta.get("erro"):
        return {"ok": False, "audio_b64": None, "erro": resposta.get("erro")}
    return {
        "ok": True,
        "audio_b64": resposta.get("audio_b64"),
        "formato": resposta.get("formato", "wav"),
        "voz": resposta.get("voz", ""),
    }

@app.post("/api/avatar/interromper")
async def api_avatar_interromper(req: Request):
    """Interrompe a síntese de voz em andamento no PC."""
    if pc_connection is not None:
        rid = await _enviar_controle(pc_connection, "http_interrupt", {})
        try:
            await _aguardar_resposta(rid, timeout=5.0)
        except HTTPException:
            pass  # interrupção é best-effort
    return {"ok": True}

@app.get("/musicas/{music_id}/file")
async def proxy_musica_file(music_id: str):
    """Proxy do arquivo MP3 da biblioteca do PC via WebSocket (entrega por API).

    O front-end web usa o mesmo padrão do app mobile: baixa/streama a faixa
    por `GET /musicas/{music_id}/file`. Aqui o arquivo é buscado no PC
    conectado e devolvido como `audio/mpeg`.
    """
    if pc_connection is None:
        raise HTTPException(status_code=503, detail="PC da Nora não está conectado no momento.")
    rid = await _enviar_controle(pc_connection, "http_musica_file", {"music_id": music_id})
    resposta = await _aguardar_resposta(rid, timeout=120.0)
    if not resposta.get("ok"):
        raise HTTPException(status_code=404, detail=resposta.get("erro") or "Música não encontrada no PC.")
    audio_b64 = resposta.get("audio_b64") or ""
    if not audio_b64:
        raise HTTPException(status_code=500, detail="Áudio vazio recebido do PC.")
    dados = base64.b64decode(audio_b64)
    filename = resposta.get("filename") or "musica.mp3"
    return Response(
        content=dados,
        media_type="audio/mpeg",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )

# ============================================================
# WEBSOCKET DO PC
# ============================================================

@app.websocket("/ws/pc")
async def websocket_pc(websocket: WebSocket):
    global pc_connection

    await websocket.accept()
    async with lock:
        pc_connection = websocket

    print("[PC] Conectado ao Relay")

    # Notificar PC
    await websocket.send_text(
        json.dumps({
            "tipo": "status",
            "mensagem": "PC conectado ao servidor",
            "pc_conectado": True
        })
    )

    # Avisar celular se estiver conectado
    if celular_connection:
        try:
            await celular_connection.send_text(
                json.dumps({
                    "tipo": "status",
                    "mensagem": "PC da Nora online",
                    "pc_conectado": True
                })
            )
        except Exception:
            pass

    try:
        while True:
            mensagem = await websocket.receive()

            # Texto / JSON do PC -> Celular
            if mensagem.get("text") is not None:
                texto = mensagem["text"]
                # Serve respostas de requisições HTTP (proxy)
                _resolver_mensagem_do_pc(texto)
                # Captura o turno e salva memórias importantes (SQLite local)
                _capturar_turno(texto)
                # Respostas das bridges HTTP (http_*_response) são do app web
                # (caíram no PC via pending_futures) e não devem ir ao celular.
                eh_resposta_bridge = False
                try:
                    dados_pc = json.loads(texto)
                    eh_resposta_bridge = (dados_pc.get("tipo") or dados_pc.get("type") or "").startswith("http_")
                except Exception:
                    pass
                if celular_connection and not eh_resposta_bridge:
                    try:
                        await celular_connection.send_text(texto)
                    except Exception as e:
                        print(f"[Relay] Erro ao repassar texto para celular: {e}")

            # Áudio (bytes) do PC -> Celular
            elif mensagem.get("bytes") is not None:
                audio = mensagem["bytes"]
                if celular_connection:
                    try:
                        await celular_connection.send_bytes(audio)
                        print(f"[Relay] {len(audio)} bytes de áudio repassados para celular")
                    except Exception as e:
                        print(f"[Relay] Erro ao repassar áudio para celular: {e}")

    except WebSocketDisconnect:
        print("[PC] Desconectado")
    finally:
        async with lock:
            if pc_connection == websocket:
                pc_connection = None
        _falhar_pendentes()
        if celular_connection:
            try:
                await celular_connection.send_text(
                    json.dumps({
                        "tipo": "status",
                        "mensagem": "PC da Nora desconectado",
                        "pc_conectado": False
                    })
                )
            except Exception:
                pass


# ============================================================
# WEBSOCKET DO CELULAR
# ============================================================

@app.websocket("/ws/celular")
async def websocket_celular(websocket: WebSocket):
    global celular_connection

    await websocket.accept()
    async with lock:
        celular_connection = websocket

    print("[CELULAR] Conectado ao Relay")

    # Notificar celular sobre status do PC
    await websocket.send_text(
        json.dumps({
            "tipo": "status",
            "mensagem": "Celular conectado ao relay",
            "pc_conectado": pc_connection is not None
        })
    )

    try:
        while True:
            mensagem = await websocket.receive()

            # Texto / JSON do Celular -> PC
            if mensagem.get("text") is not None:
                texto = mensagem["text"]
                if pc_connection:
                    try:
                        await pc_connection.send_text(texto)
                    except Exception as e:
                        print(f"[Relay] Erro ao repassar texto para PC: {e}")
                else:
                    await websocket.send_text(
                        json.dumps({
                            "tipo": "erro",
                            "mensagem": "PC da Nora não está conectado no momento.",
                            "pc_conectado": False
                        })
                    )

            # Áudio (bytes) do Celular -> PC
            elif mensagem.get("bytes") is not None:
                audio = mensagem["bytes"]
                if pc_connection:
                    try:
                        await pc_connection.send_bytes(audio)
                        print(f"[Relay] {len(audio)} bytes de áudio repassados para PC")
                    except Exception as e:
                        print(f"[Relay] Erro ao repassar áudio para PC: {e}")
                else:
                    await websocket.send_text(
                        json.dumps({
                            "tipo": "erro",
                            "mensagem": "Não é possível processar voz: PC da Nora desconectado.",
                            "pc_conectado": False
                        })
                    )

    except WebSocketDisconnect:
        print("[CELULAR] Desconectado")
    finally:
        async with lock:
            if celular_connection == websocket:
                celular_connection = None