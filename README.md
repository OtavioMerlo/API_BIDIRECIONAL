# Nora IA — WebSocket Relay

Backend responsável por fazer a comunicação entre o **aplicativo mobile da Nora IA** e o **PC local que executa o processamento da inteligência artificial**.

O servidor hospedado no Render funciona apenas como uma **ponte WebSocket**, encaminhando áudio e mensagens entre o celular e o computador.

## Arquitetura

```text
                ☁️ RENDER
          Nora IA WebSocket Relay
                  │
          ┌───────┴───────┐
          │               │
          ▼               ▼
       📱 CELULAR       💻 PC
          │               │
          │               ├── Whisper
          │               ├── EngineIa
          │               └── TTS
          │
          │
          └────── WebSocket ──────┘
```

### Fluxo de áudio

```text
📱 Celular
    │
    │ áudio em bytes
    ▼
☁️ Render
    │
    │ encaminha
    ▼
💻 PC
    │
    ├── Transcrição
    ├── Inteligência Artificial
    └── Geração de voz
    │
    │ audio.wav
    ▼
☁️ Render
    │
    │ encaminha
    ▼
📱 Celular
```

O Render **não processa o áudio**.

O processamento acontece no PC local.

---

# Estrutura

```text
render/
│
├── main.py
├── requirements.txt
├── Dockerfile
└── .gitignore
```

## Endpoints

### HTTP

```text
GET /
```

Retorna informações básicas do servidor.

Exemplo:

```json
{
  "status": "online",
  "service": "Nora IA WebSocket Relay",
  "pc_conectado": true,
  "celular_conectado": true
}
```

### Health Check

```text
GET /health
```

Resposta:

```json
{
  "status": "healthy"
}
```

### Memórias (extração automática)

O relay observa os turnos (transcrição + resposta da Indi) que passam entre o
PC e o celular e, em segundo plano, pergunta ao Groq quais fatos são duráveis
e importantes. Os fatos com `importancia >= MEMORIA_IMPORTANCIA_MIN` são
salvos em um **SQLite local** (`memorias.db`), com dedupe por conteúdo.

```text
GET /memorias?limite=20&importancia_min=3
```

Resposta:

```json
{
  "memorias": [
    {
      "id": 1,
      "conteudo": "O usuário adora rock clássico",
      "categoria": "PREFERENCIA",
      "tags": "musica, rock",
      "importancia": 5,
      "contexto": "conversa",
      "criada_em": "2026-09-17 00:57:23",
      "atualizada_em": "2026-09-17 00:57:23"
    }
  ]
}
```

**Variáveis de ambiente do relay:**

| Variável | Padrão | Descrição |
|---|---|---|
| `GROQ_API_KEY` | — | Chave do Groq usada para extrair os fatos (obrigatória p/ extração). |
| `MEMORIA_EXTRACT` | `1` | `1` liga a extração automática; `0` desliga. |
| `MEMORIA_IMPORTANCIA_MIN` | `3` | Só salva fatos com importância ≥ esse valor (1–5). |
| `MEMORIA_MODEL` | `openai/gpt-oss-120b` | Modelo do Groq usado na extração. |

> O disco do Render é efêmero: `memorias.db` pode ser apagado em redeploys
> do plano gratuito. Se precisar de persistência real, use um banco externo.

---

# WebSocket

## Celular

Endpoint:

```text
wss://SEU-APP.onrender.com/ws/celular
```

O aplicativo mobile deve utilizar esse endpoint.

O celular pode enviar:

* áudio;
* mensagens de texto.

Quando o celular enviar áudio, o Render encaminhará os bytes diretamente para o PC.

---

## PC

Endpoint:

```text
wss://SEU-APP.onrender.com/ws/pc
```

O PC mantém essa conexão aberta aguardando requisições.

Quando receber áudio:

```text
WebSocket
    ↓
audio bytes
    ↓
transcribe()
    ↓
EngineIa()
    ↓
speaktext()
    ↓
audio.wav
```

Depois o PC envia o `audio.wav` de volta pelo WebSocket.

O Render encaminha automaticamente o áudio para o celular.

---

# Comunicação

## Celular → Render → PC

O celular envia o áudio como **binário**:

```text
audio bytes
```

Não é necessário enviar:

```json
{
  "audio": "base64..."
}
```

O áudio deve ser enviado diretamente como bytes pelo WebSocket.

---

## PC → Render → Celular

O PC gera:

```text
audio.wav
```

Depois lê o arquivo:

```python
with open("audio.wav", "rb") as arquivo:
    audio = arquivo.read()
```

E envia:

```python
await websocket.send(audio)
```

O Render recebe os bytes e encaminha para o celular.

---

# Instalação local

Entre no diretório:

```bash
cd render
```

Crie o ambiente virtual:

```bash
python3 -m venv venv
```

Ative:

```bash
source venv/bin/activate
```

Instale as dependências:

```bash
pip install -r requirements.txt
```

Execute:

```bash
uvicorn main:app --host 0.0.0.0 --port 10000 --ws-max-size 67108864
```

O servidor estará disponível em:

```text
http://localhost:10000
```

WebSocket do PC:

```text
ws://localhost:10000/ws/pc
```

WebSocket do celular:

```text
ws://localhost:10000/ws/celular
```

---

# Deploy no Render

O projeto utiliza Docker.

O `Dockerfile` executa:

```dockerfile
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

EXPOSE 10000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "10000", "--ws-max-size", "67108864"]
```

Depois do deploy, o Render fornecerá um endereço semelhante a:

```text
https://nora-ia-relay.onrender.com
```

O WebSocket utiliza:

```text
wss://nora-ia-relay.onrender.com/ws/celular
```

e:

```text
wss://nora-ia-relay.onrender.com/ws/pc
```

---

# Testando

## 1. Inicie o Render

Verifique:

```text
https://SEU-APP.onrender.com/health
```

Deve retornar:

```json
{
  "status": "healthy"
}
```

## 2. Inicie o PC

O PC deve conectar:

```text
wss://SEU-APP.onrender.com/ws/pc
```

No terminal:

```text
PC conectado ao Render
```

## 3. Abra o aplicativo

O celular deve conectar:

```text
wss://SEU-APP.onrender.com/ws/celular
```

O servidor deverá indicar:

```text
PC conectado
Celular conectado
```

## 4. Envie um áudio

O fluxo será:

```text
📱 Enviando áudio
        ↓
☁️ Render recebeu
        ↓
💻 PC recebeu
        ↓
🎤 Whisper
        ↓
🧠 EngineIa
        ↓
🔊 TTS
        ↓
📄 audio.wav
        ↓
💻 PC enviou
        ↓
☁️ Render recebeu
        ↓
📱 Celular recebeu
        ↓
🔊 Reprodução
```

---

# Estados

O servidor trabalha com duas conexões principais:

```text
PC
├── conectado
└── desconectado

CELULAR
├── conectado
└── desconectado
```

O celular também recebe informações sobre o estado do PC.

Exemplo:

```json
{
  "tipo": "pc_status",
  "status": "online"
}
```

Quando o PC sair:

```json
{
  "tipo": "pc_status",
  "status": "offline"
}
```

---

# Segurança

Não coloque chaves de API no Render ou no aplicativo mobile.

O Render deve funcionar somente como relay.

As credenciais da IA devem permanecer no ambiente responsável pelo processamento.

Nunca coloque uma chave diretamente no código:

```python
api="gsk_..."
```

Prefira variáveis de ambiente:

```text
GROQ_API_KEY
```

---

# Limitação atual

A implementação atual foi projetada para:

```text
1 celular
     ↕
1 PC
```

As conexões são mantidas em memória.

Isso significa que, se houver múltiplos usuários no futuro, será necessário implementar um sistema de:

* autenticação;
* sessões;
* identificação de dispositivos;
* salas;
* roteamento de mensagens;
* múltiplas conexões simultâneas.

Exemplo futuro:

```text
                 RENDER
                   │
       ┌───────────┼───────────┐
       │           │           │
    Sessão 1    Sessão 2    Sessão 3
       │           │           │
      📱💻         📱💻         📱💻
```

---

# Tecnologias

* Python
* FastAPI
* WebSocket
* Uvicorn
* Docker
* Render
* React Native / Expo
* Faster-Whisper
* Nora IA

---

# Objetivo

O objetivo deste serviço é permitir que a Nora IA tenha uma arquitetura híbrida:

```text
📱 Mobile
    ↓
☁️ Cloud Relay
    ↓
💻 PC Local
    ↓
🧠 IA
    ↓
🔊 Voz
    ↓
☁️ Cloud Relay
    ↓
📱 Mobile
```

Dessa forma, o aplicativo mobile pode ser utilizado de qualquer lugar enquanto o processamento pesado da Nora permanece no PC local.
