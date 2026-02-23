import asyncio
import asyncpg
import sys

async def diagnostic():
    db_name = "renfield"
    try:
        # Connect to postgres DB
        conn = await asyncpg.connect(user='postgres', password='alihusain', host='127.0.0.1', database='postgres')
        
        # Check if renfield exists
        exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname=$1", db_name)
        print(f"DATABASE_EXISTS: {exists is not None}")
        
        # Check available extensions
        exts = await conn.fetch("SELECT name FROM pg_available_extensions WHERE name='vector'")
        print(f"PGVECTOR_AVAILABLE: {len(exts) > 0}")
        
        await conn.close()
        
        if exists:
            # Connect to renfield
            conn = await asyncpg.connect(user='postgres', password='alihusain', host='127.0.0.1', database=db_name)
            
            # List tables
            tables = await conn.fetch("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")
            print(f"TABLES: {[t['table_name'] for t in tables]}")
            
            # Check enabled extensions
            enabled_exts = await conn.fetch("SELECT extname FROM pg_extension")
            print(f"ENABLED_EXTENSIONS: {[e['extname'] for e in enabled_exts]}")
            
            await conn.close()
            
    except Exception as e:
        print(f"DIAGNOSTIC_ERROR: {e}")
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(diagnostic())
