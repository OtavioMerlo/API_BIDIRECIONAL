"""Memórias importantes do relay — SQLite local + extração via Groq.

O relay observa os turnos (transcrição + resposta) que passam entre o PC e o
celular e, em background, pergunta ao Groq quais fatos são duráveis e
importantes. Os fatos com importância >= MEMORIA_IMPORTANCIA_MIN são salvos
em um SQLite local (técnica clássica), com dedupe por conteúdo.
"""

import json
import os
import re
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

import httpx

DB_PATH = Path(__file__).resolve().parent / "memorias.db"

CATEGORIAS_VALIDAS = {
    "INBOX", "PESSOA", "PROJETO", "PREFERENCIA", "CONVERSA", "CONHECIMENTO",
}
MODELO = os.environ.get("MEMORIA_MODEL", "openai/gpt-oss-120b")
MODELOS_FALLBACK = [
    "openai/gpt-oss-20b",
    "qwen/qwen3.6-27b",
    "groq/compound-mini",
]
_GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
_PLOCK = threading.Lock()


def _importancia_min() -> int:
    try:
        return max(1, min(5, int(os.environ.get("MEMORIA_IMPORTANCIA_MIN", "3"))))
    except ValueError:
        return 3


def _ativa() -> bool:
    return os.environ.get("MEMORIA_EXTRACT", "1").strip() == "1"


# ─── Banco (SQLite clássico) ────────────────────────────────────────────────

def _conexao() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def cria_tabela():
    with _PLOCK:
        conn = _conexao()
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS memorias (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conteudo TEXT NOT NULL,
                categoria TEXT DEFAULT 'INBOX',
                tags TEXT,
                importancia INTEGER DEFAULT 3,
                contexto TEXT,
                links TEXT,
                criada_em TEXT NOT NULL,
                atualizada_em TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_memorias_importancia
                ON memorias (importancia DESC);
            """
        )
        conn.commit()
        conn.close()


def salvar_memoria(
    conteudo: str,
    categoria: str = "INBOX",
    tags: str = "",
    importancia: int = 3,
    contexto: str = "conversa",
    links: str = "",
) -> bool:
    """Insere uma memória evitando duplicatas de conteúdo já salvo."""
    conteudo = conteudo.strip()
    if not conteudo:
        return False
    agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _PLOCK:
        conn = _conexao()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, importancia FROM memorias WHERE conteudo = ? ORDER BY criada_em DESC LIMIT 1",
            (conteudo,),
        )
        existente = cursor.fetchone()
        if existente:
            if importancia > int(existente["importancia"]):
                cursor.execute(
                    "UPDATE memorias SET importancia = ?, atualizada_em = ? WHERE id = ?",
                    (importancia, agora, existente["id"]),
                )
                conn.commit()
            conn.close()
            return True
        cursor.execute(
            """
            INSERT INTO memorias
                (conteudo, categoria, tags, importancia, contexto, links, criada_em, atualizada_em)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                conteudo,
                categoria if categoria in CATEGORIAS_VALIDAS else "INBOX",
                tags,
                max(1, min(5, int(importancia))),
                contexto,
                links,
                agora,
                agora,
            ),
        )
        conn.commit()
        conn.close()
    return True


def listar_memorias(limite: int = 20, importancia_min: int | None = None) -> list[dict]:
    with _PLOCK:
        conn = _conexao()
        sql = "SELECT * FROM memorias"
        params: tuple = ()
        if importancia_min is not None:
            sql += " WHERE importancia >= ?"
            params = (importancia_min,)
        sql += " ORDER BY importancia DESC, criada_em DESC LIMIT ?"
        cursor = conn.execute(sql, params + (int(limite),))
        linhas = [dict(linha) for linha in cursor.fetchall()]
        conn.close()
    return linhas


# ─── Extração via Groq ──────────────────────────────────────────────────────

_PROMPT = (
    "Você é o extrator de memórias de um assistente pessoal. Analise a conversa abaixo "
    "(fala do usuário + resposta do assistente) e extraia apenas os fatos relevantes e "
    "duráveis: preferências, projetos, pessoas, decisões, gostos, rotinas, informações pessoais. "
    "Ignore saudações, perguntas triviais e conversa sem valor de memória.\n"
    "Responda SOMENTE com JSON: "
    '{"fatos":[{"conteudo":"...","categoria":"PESSOA|PROJETO|PREFERENCIA|CONHECIMENTO|TAREFA|INBOX",'
    '"tags":["..."],"importancia":1-5,"contexto":"..."}]}. '
    "Não invente dados ausentes. Sem fatos importantes, retorne {\"fatos\":[]}."
)


def _parse_fatos(texto: str) -> list[dict]:
    bruto = texto.strip()
    fences = re.findall(r"```(?:json)?\s*(.*?)```", bruto, flags=re.DOTALL)
    if fences:
        bruto = fences[-1]
    inicio, fim = bruto.find("{"), bruto.rfind("}")
    if inicio == -1 or fim == -1:
        return []
    try:
        dados = json.loads(bruto[inicio : fim + 1])
    except json.JSONDecodeError:
        return []

    fatos = []
    for f in dados.get("fatos", []):
        if not isinstance(f, dict):
            continue
        conteudo = (f.get("conteudo") or f.get("memoria") or f.get("texto") or "").strip()
        if not conteudo:
            continue
        try:
            importancia = int(f.get("importancia", 3))
        except (TypeError, ValueError):
            importancia = 3
        tags = f.get("tags") or []
        if not isinstance(tags, list):
            tags = []
        fatos.append(
            {
                "conteudo": conteudo,
                "categoria": str(f.get("categoria", "INBOX")).upper(),
                "tags": ", ".join(str(t) for t in tags),
                "importancia": max(1, min(5, importancia)),
                "contexto": str(f.get("contexto") or "conversa"),
            }
        )
    return fatos


def _gerar_resposta(usuario: str, resposta: str) -> str:
    chave = os.environ.get("GROQ_API_KEY", "")
    headers = {"Authorization": f"Bearer {chave}", "Content-Type": "application/json"}
    mensagem = f"Usuário: {usuario or '(áudio sem transcrição)'}\nAssistente: {resposta or '(sem resposta)'}"
    payload = {
        "model": MODELO,
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": _PROMPT},
            {"role": "user", "content": mensagem},
        ],
    }
    modelos = [MODELO] + [m for m in MODELOS_FALLBACK if m != MODELO]
    with httpx.Client(timeout=30.0) as client:
        for modelo in modelos:
            payload["model"] = modelo
            try:
                resp = client.post(_GROQ_URL, headers=headers, json=payload)
                if resp.status_code != 200:
                    continue
                return resp.json()["choices"][0]["message"]["content"]
            except Exception:
                continue
    return ""


def processar_turno(usuario: str, resposta: str) -> int:
    """Extrai fatos do turno e salva os importantes. Retorna quantos salvou."""
    if not _ativa():
        return 0
    try:
        bruto = _gerar_resposta(usuario, resposta)
        if not bruto:
            return 0
        fatos = _parse_fatos(bruto)
    except Exception as e:
        print(f"[Memórias] Erro na extração: {e}")
        return 0

    salvos = 0
    minimo = _importancia_min()
    for f in fatos:
        if f["importancia"] < minimo:
            continue
        if salvar_memoria(
            f["conteudo"],
            categoria=f["categoria"],
            tags=f["tags"],
            importancia=f["importancia"],
            contexto=f["contexto"],
        ):
            salvos += 1
    if salvos:
        print(f"[Memórias] {salvos} memória(s) importante(s) salva(s) no SQLite.")
    return salvos