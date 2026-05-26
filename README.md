# AntiEco Bot

Bot Telegram que detecta e remove conteudo duplicado em grupos — links repetidos, mensagens encaminhadas e textos iguais sao automaticamente apagados antes de criar "ecos" na conversa.

Deployado no Render com PostgreSQL Neon, mantido vivo 24/7 via GitHub Actions.

---

## O que faz

| Tipo de conteudo | Comportamento |
|---|---|
| Link repetido | Apaga a mensagem e avisa o usuario |
| Mensagem encaminhada duplicada | Apaga e avisa |
| Texto identico (> 20 chars) | Apaga apos 3 repeticoes |

Cada grupo tem seu historico isolado. A janela padrao e de 30 dias — mensagens mais antigas que isso sao ignoradas e removidas do banco automaticamente.

---

## Stack

- **python-telegram-bot 21.9** — SDK oficial
- **FastAPI + uvicorn** — webhook (modo producao)
- **PostgreSQL Neon** — banco em producao (Render)
- **SQLite** — banco local para dev/teste
- **Render** — hosting free tier
- **GitHub Actions** — keepalive (ping a cada 5 min para evitar sleep do Render)

---

## Deploy rapido (Render)

1. Fork este repo
2. Crie um bot no [@BotFather](https://t.me/BotFather) e guarde o token
3. Crie um banco PostgreSQL no [Neon](https://neon.tech) (plano free)
4. No Render, crie um **Web Service** apontando para este repo:
   - **Build command:** `pip install -r requirements.txt`
   - **Start command:** `python bot.py`
5. Configure as variaveis de ambiente:

```
TELEGRAM_BOT_TOKEN=seu_token_aqui
DATABASE_URL=postgresql://user:pass@host/dbname
DUPLICATE_WINDOW_DAYS=30
TEXT_MIN_LENGTH=20
TEXT_MAX_REPEATS=3
```

6. Adicione o bot como **administrador** no grupo (precisa de permissao para apagar mensagens)

---

## Rodar localmente

```bash
git clone https://github.com/diegopaixao89/antieco-bot.git
cd antieco-bot
pip install -r requirements.txt
cp .env.example .env
# edite .env com seu TELEGRAM_BOT_TOKEN
python bot.py
```

Sem `DATABASE_URL` no `.env`, usa SQLite automaticamente (`mensagens.db`).

---

## Variaveis de ambiente

| Variavel | Padrao | Descricao |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | Token do bot (obrigatorio) |
| `DATABASE_URL` | `""` | URL PostgreSQL; vazio = SQLite |
| `RENDER_EXTERNAL_URL` | `""` | URL publica do Render (ativa modo webhook) |
| `PORT` | `8080` | Porta do servidor webhook |
| `DUPLICATE_WINDOW_DAYS` | `30` | Janela de memoria em dias (0 = sem limite) |
| `TEXT_MIN_LENGTH` | `20` | Minimo de chars para checar texto |
| `TEXT_MAX_REPEATS` | `3` | Repeticoes permitidas antes de bloquear |

---

## Comandos

| Comando | Descricao |
|---|---|
| `/ping` | Verifica status, modo do banco e total de registros |

---

## Estrutura

```
antieco-bot/
├── bot.py              # toda a logica (banco, handlers, webhook/polling)
├── render.yaml         # config Render
├── requirements.txt
├── .env.example
└── .github/
    └── workflows/
        └── keepalive.yml   # ping a cada 5 min para evitar sleep
```

---

## Licenca

MIT
