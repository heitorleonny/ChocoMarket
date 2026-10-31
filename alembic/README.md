# ChocoMarket migrations

Run migrations with:

```bash
alembic upgrade head
```

The database URL comes from `CHOCOMARKET_DATABASE_URL`; when unset, the
application development database (`chocomarket.db`) is used. The initial
revision is deliberately non-destructive for an existing database: it does not
drop or recreate `users`, and it refuses to guess values for missing identity
or credential columns.
