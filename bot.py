import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from telegram import Update, MessageEntity
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes, Application

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
DATABASE_URL = os.environ.get("DATABASE_URL")
WINDOW_DAYS = int(os.environ.get("DUPLICATE_WINDOW_DAYS", "30"))
TEXT_MIN_LEN = int(os.environ.get("TEXT_MIN_LENGTH", "20"))
TEXT_MAX_REPEATS = int(os.environ.get("TEXT_MAX_REPEATS", "3"))

# ---------------------------------------------------------------------------
# Camada de banco de dados — SQLite (local) ou PostgreSQL (nuvem)
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
    _DB_PATH = os.path.join(os.path.dirname(__file__), "mensagens.db")
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
            f"SELECT 1 FROM registros WHERE chat_id = {PH} AND conteudo = {PH} AND criado_em > {PH} LIMIT 1",
            (chat_id, conteudo, cutoff),
        )
    else:
        rows = _query(
            f"SELECT 1 FROM registros WHERE chat_id = {PH} AND conteudo = {PH} LIMIT 1",
            (chat_id, conteudo),
        )
    return len(rows) > 0


def contar_ocorrencias(chat_id: int, conteudo: str) -> int:
    cutoff = _cutoff()
    if cutoff:
        rows = _query(
            f"SELECT COUNT(*) FROM registros WHERE chat_id = {PH} AND conteudo = {PH} AND criado_em > {PH}",
            (chat_id, conteudo, cutoff),
        )
    else:
        rows = _query(
            f"SELECT COUNT(*) FROM registros WHERE chat_id = {PH} AND conteudo = {PH}",
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


def extrair_links(message) -> list[str]:
    if not message.entities:
        return []
    links = []
    for entity in message.entities:
        if entity.type == MessageEntity.TEXT_LINK:
            links.append(normalizar(entity.url))
        elif entity.type == MessageEntity.URL:
            links.append(normalizar(message.text[entity.offset: entity.offset + entity.length]))
    return list(set(links))


async def processar_mensagem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message:
        return

    chat_id = message.chat_id
    nome = (message.from_user.first_name if message.from_user else None) or "Alguém"
    RODAPE = "Use a pesquisa do grupo para encontrar conteúdo já compartilhado."

    async def bloquear(aviso: str):
        try:
            await message.delete()
        except Exception as e:
            logger.warning("Nao foi possivel deletar mensagem %s: %s", message.message_id, e)
        try:
            await context.bot.send_message(
                chat_id,
                f"{aviso}\n\n_{RODAPE}_",
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.warning("Nao foi possivel enviar aviso no chat %s: %s", chat_id, e)

    # --- Mensagem encaminhada ---
    if message.forward_from or message.forward_from_chat or message.forward_date:
        conteudo = normalizar(message.text or message.caption or "encaminhada")
        duplicado = ja_foi_postado(chat_id, conteudo)
        registrar(chat_id, conteudo, "encaminhada")
        if duplicado:
            await bloquear(f"*{nome}*, esse conteudo encaminhado ja foi postado aqui.")
        return

    # --- Mensagem com link ---
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

    # --- Texto simples (ignora mensagens muito curtas) ---
    if message.text:
        texto = normalizar(message.text)
        if len(texto) < TEXT_MIN_LEN:
            return
        registrar(chat_id, texto, "texto")
        count = contar_ocorrencias(chat_id, texto)
        if count > TEXT_MAX_REPEATS:
            await bloquear(
                f"*{nome}*, essa mensagem ja foi enviada {count - 1}x no grupo nos ultimos {WINDOW_DAYS} dias."
            )


async def _on_startup(app: Application):
    limpar_antigos()
    logger.info(
        "Bot pronto | janela=%dd | min_len=%d | max_repeats=%d",
        WINDOW_DAYS, TEXT_MIN_LEN, TEXT_MAX_REPEATS,
    )


def main():
    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .post_init(_on_startup)
        .build()
    )
    app.add_handler(
        MessageHandler(
            (filters.TEXT | filters.FORWARDED) & ~filters.COMMAND,
            processar_mensagem,
        )
    )
    app.run_polling()


if __name__ == "__main__":
    main()
