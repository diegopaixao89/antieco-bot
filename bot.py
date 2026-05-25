import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from telegram import Update, MessageEntity
from telegram.ext import ApplicationBuilder, Application, MessageHandler, CommandHandler, filters, ContextTypes

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TOKEN       = os.environ["TELEGRAM_BOT_TOKEN"]
DATABASE_URL    = os.environ.get("DATABASE_URL")
RENDER_URL      = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
PORT            = int(os.environ.get("PORT", 8080))
WINDOW_DAYS     = int(os.environ.get("DUPLICATE_WINDOW_DAYS", "30"))
TEXT_MIN_LEN    = int(os.environ.get("TEXT_MIN_LENGTH", "20"))
TEXT_MAX_REPEATS = int(os.environ.get("TEXT_MAX_REPEATS", "3"))

# ---------------------------------------------------------------------------
# Banco de dados — SQLite (local/Render) ou PostgreSQL (DATABASE_URL)
# ---------------------------------------------------------------------------

PH = "%s" if DATABASE_URL else "?"

if DATABASE_URL:
    import psycopg2
    import psycopg2.pool

    _pg_pool = psycopg2.pool.ThreadedConnectionPool(1, 5, DATABASE_URL)

    def _query(sql: str, params: tuple = ()) -> list:
        conn = _pg_pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchall()
        finally:
            _pg_pool.putconn(conn)

    def _run(sql: str, params: tuple = ()):
        conn = _pg_pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            _pg_pool.putconn(conn)

    def _init_db():
        _run("""
            CREATE TABLE IF NOT EXISTS registros (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                conteudo TEXT NOT NULL,
                tipo TEXT NOT NULL DEFAULT 'texto',
                criado_em TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            )
        """)
        _run("""
            CREATE INDEX IF NOT EXISTS idx_registros_lookup
                ON registros (chat_id, conteudo, criado_em)
        """)

    logger.info("Banco: PostgreSQL")

else:
    _DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "mensagens.db"))
    sqlite3.register_adapter(datetime, lambda d: d.isoformat())
    _sqlite = sqlite3.connect(_DB_PATH, check_same_thread=False)

    def _query(sql: str, params: tuple = ()) -> list:
        norm = tuple(p.isoformat() if isinstance(p, datetime) else p for p in params)
        return _sqlite.execute(sql, norm).fetchall()

    def _run(sql: str, params: tuple = ()):
        norm = tuple(p.isoformat() if isinstance(p, datetime) else p for p in params)
        _sqlite.execute(sql, norm)
        _sqlite.commit()

    def _init_db():
        _sqlite.executescript("""
            CREATE TABLE IF NOT EXISTS registros (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                conteudo TEXT NOT NULL,
                tipo TEXT NOT NULL DEFAULT 'texto',
                criado_em DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_registros_lookup
                ON registros (chat_id, conteudo, criado_em);
        """)
        _sqlite.commit()

    logger.info("Banco: SQLite — %s", _DB_PATH)

_init_db()

# ---------------------------------------------------------------------------
# Operações de banco
# ---------------------------------------------------------------------------

def _cutoff() -> datetime | None:
    if WINDOW_DAYS <= 0:
        return None
    return datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)


def registrar(chat_id: int, conteudo: str, tipo: str):
    _run(
        f"INSERT INTO registros (chat_id, conteudo, tipo) VALUES ({PH}, {PH}, {PH})",
        (chat_id, conteudo, tipo),
    )


def ja_foi_postado(chat_id: int, conteudo: str) -> bool:
    cutoff = _cutoff()
    if cutoff:
        rows = _query(
            f"SELECT 1 FROM registros WHERE chat_id={PH} AND conteudo={PH} AND criado_em>{PH} LIMIT 1",
            (chat_id, conteudo, cutoff),
        )
    else:
        rows = _query(
            f"SELECT 1 FROM registros WHERE chat_id={PH} AND conteudo={PH} LIMIT 1",
            (chat_id, conteudo),
        )
    return len(rows) > 0


def contar_ocorrencias(chat_id: int, conteudo: str) -> int:
    cutoff = _cutoff()
    if cutoff:
        rows = _query(
            f"SELECT COUNT(*) FROM registros WHERE chat_id={PH} AND conteudo={PH} AND criado_em>{PH}",
            (chat_id, conteudo, cutoff),
        )
    else:
        rows = _query(
            f"SELECT COUNT(*) FROM registros WHERE chat_id={PH} AND conteudo={PH}",
            (chat_id, conteudo),
        )
    return rows[0][0]


def limpar_antigos():
    cutoff = _cutoff()
    if not cutoff:
        return
    _run(f"DELETE FROM registros WHERE criado_em < {PH}", (cutoff,))
    logger.info("Limpeza: registros anteriores a %s removidos", cutoff.date())

# ---------------------------------------------------------------------------
# Lógica do bot
# ---------------------------------------------------------------------------

def normalizar(texto: str) -> str:
    return texto.lower().strip()


def normalizar_url(url: str) -> str:
    url = url.lower().strip()
    for prefix in ("https://", "http://"):
        if url.startswith(prefix):
            url = url[len(prefix):]
            break
    if url.startswith("www."):
        url = url[4:]
    return url.rstrip("/")


def extrair_links(message) -> list[str]:
    if not message.entities:
        return []
    links = []
    for entity in message.entities:
        if entity.type == MessageEntity.TEXT_LINK:
            links.append(normalizar_url(entity.url))
        elif entity.type == MessageEntity.URL:
            links.append(normalizar_url(message.text[entity.offset: entity.offset + entity.length]))
    return list(set(links))


async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = _query("SELECT COUNT(*) FROM registros")
    total = rows[0][0] if rows else "?"
    modo = "PostgreSQL" if DATABASE_URL else "SQLite"
    await update.message.reply_text(
        f"pong — banco: {modo} | registros: {total}"
    )


async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Erro no handler: %s", context.error, exc_info=context.error)


async def processar_mensagem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message:
        return
    logger.info("MSG recebida: chat=%s tipo=%s texto=%s",
                message.chat_id,
                message.chat.type,
                bool(message.text))

    chat_id = message.chat_id
    nome = (message.from_user.first_name if message.from_user else None) or "Alguem"
    RODAPE = "Use a pesquisa do grupo para encontrar conteudo ja compartilhado."

    async def bloquear(aviso: str):
        try:
            await message.delete()
        except Exception as e:
            logger.warning("Nao foi possivel deletar msg %s: %s", message.message_id, e)
        try:
            await context.bot.send_message(
                chat_id,
                f"{aviso}\n\n_{RODAPE}_",
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.warning("Nao foi possivel enviar aviso no chat %s: %s", chat_id, e)

    # Mensagem encaminhada
    if message.forward_from or message.forward_from_chat or message.forward_date:
        conteudo = normalizar(message.text or message.caption or "encaminhada")
        duplicado = ja_foi_postado(chat_id, conteudo)
        registrar(chat_id, conteudo, "encaminhada")
        if duplicado:
            await bloquear(f"*{nome}*, esse conteudo encaminhado ja foi postado aqui.")
        return

    # Mensagem com link
    if message.text:
        links = extrair_links(message)
        if links:
            for link in links:
                duplicado = ja_foi_postado(chat_id, link)
                registrar(chat_id, link, "link")
                if duplicado:
                    await bloquear(f"*{nome}*, esse link ja foi postado aqui.")
                    return
            return

    # Texto simples (ignora textos muito curtos)
    if message.text:
        texto = normalizar(message.text)
        if len(texto) < TEXT_MIN_LEN:
            return
        registrar(chat_id, texto, "texto")
        count = contar_ocorrencias(chat_id, texto)
        if count > TEXT_MAX_REPEATS:
            await bloquear(
                f"*{nome}*, essa mensagem ja foi enviada {count - 1}x nos ultimos {WINDOW_DAYS} dias."
            )


async def _on_startup(app: Application):
    limpar_antigos()
    modo = "webhook" if RENDER_URL else "polling"
    logger.info(
        "Bot pronto | modo=%s | janela=%dd | min_len=%d | max_repeats=%d",
        modo, WINDOW_DAYS, TEXT_MIN_LEN, TEXT_MAX_REPEATS,
    )


def _build_ptb() -> Application:
    ptb = (
        ApplicationBuilder()
        .token(TOKEN)
        .post_init(_on_startup)
        .build()
    )
    ptb.add_handler(CommandHandler("ping", cmd_ping))
    ptb.add_handler(
        MessageHandler(
            (filters.TEXT | filters.FORWARDED) & ~filters.COMMAND,
            processar_mensagem,
        )
    )
    ptb.add_error_handler(_error_handler)
    return ptb

# ---------------------------------------------------------------------------
# Modo webhook (Render) — FastAPI + uvicorn
# ---------------------------------------------------------------------------

if RENDER_URL:
    from contextlib import asynccontextmanager
    from fastapi import FastAPI, Request, Response

    _ptb_instance: Application | None = None

    @asynccontextmanager
    async def _lifespan(app):
        global _ptb_instance
        _ptb_instance = _build_ptb()
        await _ptb_instance.initialize()
        await _ptb_instance.bot.set_webhook(f"{RENDER_URL}/webhook")
        await _ptb_instance.start()
        yield
        await _ptb_instance.stop()
        await _ptb_instance.shutdown()

    fastapi_app = FastAPI(lifespan=_lifespan)

    @fastapi_app.api_route("/", methods=["GET", "HEAD"])
    async def health():
        return {"status": "ok", "mode": "webhook"}

    @fastapi_app.post("/webhook")
    async def telegram_webhook(request: Request):
        body = await request.json()
        await _ptb_instance.process_update(
            Update.de_json(body, _ptb_instance.bot)
        )
        return Response(status_code=200)

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    if RENDER_URL:
        import uvicorn
        logger.info("Iniciando em modo webhook na porta %d", PORT)
        uvicorn.run(fastapi_app, host="0.0.0.0", port=PORT)
    else:
        logger.info("Iniciando em modo polling")
        _build_ptb().run_polling()


if __name__ == "__main__":
    main()
