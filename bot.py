import logging
import os
import sqlite3

from telegram import Update, MessageEntity
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

DB_PATH = os.path.join(os.path.dirname(__file__), "mensagens.db")

conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cursor = conn.cursor()

cursor.executescript("""
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
conn.commit()


def normalizar(texto: str) -> str:
    return texto.lower().strip()


def extrair_links(message) -> list[str]:
    links = []
    if not message.entities:
        return links
    for entity in message.entities:
        if entity.type in (MessageEntity.URL, MessageEntity.TEXT_LINK):
            if entity.type == MessageEntity.TEXT_LINK:
                url = entity.url
            else:
                url = message.text[entity.offset: entity.offset + entity.length]
            links.append(normalizar(url))
    return list(set(links))


def registrar(chat_id: int, conteudo: str, tipo: str):
    cursor.execute(
        "INSERT INTO registros (chat_id, conteudo, tipo) VALUES (?, ?, ?)",
        (chat_id, conteudo, tipo),
    )
    conn.commit()


def ja_foi_postado(chat_id: int, conteudo: str) -> bool:
    cursor.execute(
        "SELECT 1 FROM registros WHERE chat_id = ? AND conteudo = ? LIMIT 1",
        (chat_id, conteudo),
    )
    return cursor.fetchone() is not None


def contar_total(chat_id: int, conteudo: str) -> int:
    cursor.execute(
        "SELECT COUNT(*) FROM registros WHERE chat_id = ? AND conteudo = ?",
        (chat_id, conteudo),
    )
    return cursor.fetchone()[0]


async def processar_mensagem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message:
        return

    chat_id = message.chat_id
    user = message.from_user
    nome = user.first_name if user else "Alguém"

    AVISO_FIXO = "Essa mensagem já foi postada anteriormente. Por favor, utilize a ferramenta de pesquisa do grupo."

    async def bloquear(aviso: str):
        try:
            await message.delete()
        except Exception as e:
            logger.warning(f"Não foi possível deletar a mensagem: {e}")
        await context.bot.send_message(
            chat_id,
            f"{aviso}\n\n{AVISO_FIXO}",
            parse_mode="Markdown",
        )

    # --- Mensagem encaminhada ---
    if message.forward_from or message.forward_from_chat or message.forward_date:
        conteudo = normalizar(message.text or message.caption or "encaminhada")
        duplicado = ja_foi_postado(chat_id, conteudo)
        registrar(chat_id, conteudo, "encaminhada")

        if duplicado:
            await bloquear(f"📨 *{nome}*, esse conteúdo encaminhado já foi postado aqui.")
        return

    # --- Links externos ---
    if message.text:
        links = extrair_links(message)
        if links:
            for link in links:
                duplicado = ja_foi_postado(chat_id, link)
                registrar(chat_id, link, "link")

                if duplicado:
                    await bloquear(f"🔗 *{nome}*, esse link já foi postado aqui.")
                    return
            return  # mensagem com link já foi tratada, não checar texto

    # --- Texto duplicado (apenas mensagens sem links) ---
    if message.text:
        texto = normalizar(message.text)
        registrar(chat_id, texto, "texto")
        count = contar_total(chat_id, texto)

        if count > 3:
            await bloquear(f"⚠️ *{nome}*, essa mensagem já foi enviada mais de 3 vezes no grupo.")
            return


def main():
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(
        MessageHandler(
            (filters.TEXT | filters.FORWARDED) & (~filters.COMMAND),
            processar_mensagem,
        )
    )
    logger.info("Bot iniciado. Aguardando mensagens...")
    app.run_polling()


if __name__ == "__main__":
    main()
