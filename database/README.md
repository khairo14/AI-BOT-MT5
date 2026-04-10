# PostgreSQL Database Setup

## Quick Start

### 1. Install Docker Desktop
Download from: https://docker.com/products/docker-desktop

### 2. Generate Encryption Key
```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```
Copy output to `.env` → `ENCRYPTION_KEY=...`

### 3. Install Python Dependencies
```bash
.venv\Scripts\activate
pip install -r requirements.txt
```

### 4. Start Database
```bash
.\db.bat start
```

### 5. Initialize Tables
```bash
.\db.bat init
```

### 6. Verify Connection
```bash
.\db.bat test
```

---

## Usage

### Start Bot (includes database)
```bash
.\start.bat
```

### Stop Bot (includes database)
```bash
.\stop.bat
```

### Database CLI Commands
```bash
.\db.bat start     # Start database only
.\db.bat stop      # Stop database only
.\db.bat logs      # View logs
.\db.bat shell     # Open psql shell
.\db.bat init      # Create tables
.\db.bat migrate   # Run migrations
.\db.bat test      # Test connection
```

---

## Connection Details

**PostgreSQL:**
- Host: `localhost`
- Port: `5433` (to avoid conflicts with native PostgreSQL on 5432)
- Database: `aibot_mt5`
- User: `aibot`
- Password: `changeme123` (change in `.env` → `POSTGRES_PASSWORD`)

**pgAdmin (GUI):**
- URL: http://localhost:5050
- Email: `admin@aibot.local`
- Password: `admin123` (change in `.env` → `PGADMIN_PASSWORD`)

---

## Migrations (Alembic)

### Create New Migration (after model changes)
```bash
.venv\Scripts\python -m alembic revision --autogenerate -m "Description of changes"
```

### Apply Migrations
```bash
.venv\Scripts\python -m alembic upgrade head
```

### Rollback Migration
```bash
.venv\Scripts\python -m alembic downgrade -1
```

### Migration History
```bash
.venv\Scripts\python -m alembic history
```

---

## Troubleshooting

### Port 5433 Already in Use
```bash
# Check what's using the port
netstat -ano | findstr :5433

# Kill the process (replace PID)
taskkill /PID <pid> /F

# Or change port in docker-compose.yml
ports:
  - "5434:5432"  # Use 5434 instead
```

### Database Not Starting
```bash
# Check Docker is running
docker ps

# View container logs
docker-compose logs postgres

# Restart Docker Desktop
# Then: .\db.bat start
```

### Connection Refused
1. Check `.env` → `DATABASE_URL` matches `docker-compose.yml`
2. Verify port: `localhost:5433` (not 5432)
3. Test with: `.\db.bat test`

### Reset Database (DANGER: deletes all data)
```bash
docker-compose down -v
.\db.bat start
.\db.bat init
```

---

## Data Persistence

Data is stored in Docker volume `postgres_data`:
```bash
# View volumes
docker volume ls

# Backup database
docker exec ai-bot-mt5-db pg_dump -U aibot aibot_mt5 > backup.sql

# Restore database
docker exec -i ai-bot-mt5-db psql -U aibot aibot_mt5 < backup.sql
```

---

## Migration to Supabase (Future)

When ready to migrate to Supabase:

1. Export local data:
```bash
docker exec ai-bot-mt5-db pg_dump -U aibot --clean --no-owner aibot_mt5 > export.sql
```

2. Import to Supabase:
```bash
psql -h db.xxx.supabase.co -U postgres -d postgres < export.sql
```

3. Update `.env`:
```env
DATABASE_URL=postgresql://postgres:[PASSWORD]@db.xxx.supabase.co:5432/postgres
```

4. No code changes needed!

---

## Architecture

```
AI-BOT-MT5/
├── database/
│   ├── __init__.py
│   ├── connection.py       # SQLAlchemy engine + session
│   ├── models.py           # ORM table definitions
│   ├── init/
│   │   └── 01_extensions.sql  # Auto-run on first start
│   └── migrations/         # Alembic version history
│       ├── env.py
│       ├── script.py.mako
│       └── versions/       # Auto-generated migrations
├── docker-compose.yml      # PostgreSQL + pgAdmin containers
├── alembic.ini            # Migration config
└── db.bat                 # Database CLI helper
```
