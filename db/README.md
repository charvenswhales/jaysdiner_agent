# Database setup

Verified end-to-end against a local SQLite file: models create correctly,
the Alembic migration applies cleanly, ORM inserts/relationships work, and
token encryption round-trips. All of that will work identically against
Postgres, this only used SQLite because the sandbox that built this
couldn't reach a live Neon instance.

## Get a free Postgres instance

1. Sign up at [neon.tech](https://neon.tech), create a project.
2. Copy the connection string it gives you (looks like
   `postgresql://user:pass@ep-xxx.neon.tech/dbname?sslmode=require`).

## Set environment variables

Add to `.env` (alongside your existing `OPENROUTER_API_KEY` etc.):

```
DATABASE_URL=postgresql://user:pass@ep-xxx.neon.tech/dbname?sslmode=require
MONEI_TOKEN_ENCRYPTION_KEY=<generate one, see below>
```

Generate the encryption key once:
```
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```
Save it somewhere safe outside of git, losing it means every stored Monei
connection becomes unreadable and users would need to reconnect.

## Run the migration against your real database

```
alembic upgrade head
```

This creates `users`, `checkout_profiles`, `monei_connections`, and
`orders` with the same schema that was just validated locally.

## Making future schema changes

1. Edit `db/models.py`.
2. `alembic revision --autogenerate -m "describe the change"`
3. **Read the generated migration**, autogenerate is good but not
   perfect, especially for column renames (it'll generate a drop+add
   instead of a rename unless you edit it manually).
4. `alembic upgrade head`