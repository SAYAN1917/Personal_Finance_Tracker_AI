# Production deployment

How to run the finance tracker in production. The app is a single-user
personal tracker, so this guide favors **data safety and fail-closed
security** over high availability: one API process, one bot process, Postgres
(or SQLite + WAL), nightly backups, migrations.

## 1. Secrets (fail closed)

Copy `service/.env.example` to `/etc/finance/finance.env`, fill in real
values, and set `FINANCE_ENV=prod`:

| Var | Why it must be set in prod |
|-----|----------------------------|
| `WEBHOOK_SECRET` | Every `/webhook/*` call is rejected without the matching header |
| `ADMIN_USER_ID` | Without it the Telegram bot rejects **every** user |
| `TELEGRAM_BOT_TOKEN` | Required by the bot process |
| `DATABASE_URL` | Point at your Postgres or SQLite file |

With `FINANCE_ENV=prod` the API and bot **refuse to boot** on a missing or
placeholder secret (`settings.validate()` in `app/config.py`).

Never commit `/etc/finance/finance.env` or a `.env` with real values.

## 2. Database + migrations

Schema changes use Alembic (the repo contains the initial migration):

```bash
# from service/
DATABASE_URL=postgresql://finance:***@localhost:5432/finance alembic upgrade head
```

- The API's `init_db()` still `create_all`s for dev convenience; in prod rely
  on `alembic upgrade head` (run as a `ExecStartPre`, see the systemd units).
- `GET /health` is cheap liveness; `GET /ready` returns `503` while
  migrations are pending.

## 3. Reverse proxy (Nginx + TLS)

The API listens on `127.0.0.1:8000`. Expose ONLY the webhook path to the
internet through Nginx with TLS. Telegram webhooks must be HTTPS.

```nginx
server {
    listen 443 ssl;
    server_name finance.example.com;

    ssl_certificate     /etc/letsencrypt/live/finance.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/finance.example.com/privkey.pem;

    # Only the webhook endpoints are public (N8N -> Core).
    location /webhook/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }

    # Everything else stays private (ledger, balances, reports).
    location / {
        deny all;
    }
}
```

Rate limiting can be enforced here instead of the app:

```nginx
limit_req_zone $binary_remote_addr zone=webhook:10m rate=30r/m;

location /webhook/ {
    limit_req zone=webhook burst=10 nodelay;
    proxy_pass http://127.0.0.1:8000;
}
```

If you run multiple uvicorn workers, put the rate limit here: the in-app
limiter (`app/ratelimit.py`) is per-process.

## 4. Bot

The bot long-polls Telegram (no public webhook needed). Run it via the
systemd unit with `TELEGRAM_BOT_TOKEN` and `ADMIN_USER_ID` set. Restarting is
safe: the poll offset is kept in memory and the ledger is the source of truth.

## 5. Backup

`service/scripts/backup.sh` supports both backends and ships the dump to your
Telegram Saved Messages:

```cron
0 3 * * * TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... /opt/finance/service/scripts/backup.sh
```

- SQLite: `sqlite3 finance.db ".backup ..."` (binary, safe on a live DB).
- Postgres: `pg_dump "$DATABASE_URL"`.

Test a restore at least once; a backup you never restored is a hope, not a
backup.

## 6. CI

GitHub Actions (`.github/workflows/ci.yml`) runs ruff (F-class) and the full
pytest suite on every push/PR to `master`.

## 7. Operations checklist

- [ ] `FINANCE_ENV=prod` and real secrets in `/etc/finance/finance.env`
- [ ] `alembic upgrade head` succeeds against the production DB
- [ ] Bot allowlist set (`ADMIN_USER_ID`), bot rejects unknown users
- [ ] Nginx exposes only `/webhook/`, everything else denied, TLS on
- [ ] Backup cron running and restore tested
- [ ] `/health` and `/ready` reachable by your uptime monitor
- [ ] Logs rotating in `LOG_DIR` (or journald)
