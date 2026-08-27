from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

import asyncio
import json


app = FastAPI(title="Nora IA - WebSocket Relay")


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================
# CONEXÕES
# ============================================================

pc_connection: WebSocket | None = None
celular_connection: WebSocket | None = None

lock = asyncio.Lock()


# ============================================================
# STATUS
# ============================================================

@app.get("/")
async def root():
    return {
        "status": "online",
        "service": "Nora IA WebSocket Relay"
    }


@app.get("/status")
async def status():

    return {
        "pc_conectado": pc_connection is not None,
        "celular_conectado": celular_connection is not None
    }


# ============================================================
# WEBSOCKET DO PC
# ============================================================

@app.websocket("/ws/pc")
async def websocket_pc(websocket: WebSocket):

    global pc_connection

    await websocket.accept()

    async with lock:
        pc_connection = websocket

    print("[PC] Conectado")

    # Avisar ao PC que está conectado
    await websocket.send_text(
        json.dumps({
            "tipo": "status",
            "mensagem": "PC conectado ao servidor"
        })
    )

    try:

        while True:

            mensagem = await websocket.receive()

            # ------------------------------------------------
            # PC ENVIOU TEXTO
            # ------------------------------------------------

            if mensagem.get("text") is not None:

                texto = mensagem["text"]

                print("[PC] Texto:", texto)

                # Encaminhar para o celular
                if celular_connection:

                    await celular_connection.send_text(texto)

            # ------------------------------------------------
            # PC ENVIOU ÁUDIO
            # ------------------------------------------------

            elif mensagem.get("bytes") is not None:

                audio = mensagem["bytes"]

                print(
                    f"[PC] Recebido áudio de resposta: "
                    f"{len(audio)} bytes"
                )

                # Encaminhar áudio para celular
                if celular_connection:

                    await celular_connection.send_bytes(audio)

                    print("[RENDER] Áudio enviado para celular")

                else:

                    print(
                        "[RENDER] Celular não está conectado"
                    )

    except WebSocketDisconnect:

        print("[PC] Desconectado")

    finally:

        async with lock:

            if pc_connection == websocket:
                pc_connection = None


# ============================================================
# WEBSOCKET DO CELULAR
# ============================================================

@app.websocket("/ws/celular")
async def websocket_celular(websocket: WebSocket):

    global celular_connection

    await websocket.accept()

    async with lock:
        celular_connection = websocket

    print("[CELULAR] Conectado")

    # Avisar ao celular
    await websocket.send_text(
        json.dumps({
            "tipo": "status",
            "mensagem": "Celular conectado ao servidor"
        })
    )

    try:

        while True:

            mensagem = await websocket.receive()

            # ------------------------------------------------
            # CELULAR ENVIOU TEXTO
            # ------------------------------------------------

            if mensagem.get("text") is not None:

                texto = mensagem["text"]

                print("[CELULAR] Texto:", texto)

                # Encaminhar para PC
                if pc_connection:

                    await pc_connection.send_text(texto)

                else:

                    await websocket.send_text(
                        json.dumps({
                            "tipo": "erro",
                            "mensagem": "PC não conectado"
                        })
                    )

            # ------------------------------------------------
            # CELULAR ENVIOU ÁUDIO
            # ------------------------------------------------

            elif mensagem.get("bytes") is not None:

                audio = mensagem["bytes"]

                print(
                    f"[CELULAR] Áudio recebido: "
                    f"{len(audio)} bytes"
                )

                # Verificar PC
                if pc_connection:

                    # Enviar áudio diretamente para PC
                    await pc_connection.send_bytes(audio)

                    print(
                        "[RENDER] Áudio encaminhado para PC"
                    )

                else:

                    print(
                        "[RENDER] PC não está conectado"
                    )

                    await websocket.send_text(
                        json.dumps({
                            "tipo": "erro",
                            "mensagem": "PC não conectado"
                        })
                    )

    except WebSocketDisconnect:

        print("[CELULAR] Desconectado")

    finally:

        async with lock:

            if celular_connection == websocket:
                celular_connection = None