import asyncio
import subprocess
import sys


def run_alembic():
    # Run alembic upgrade head using the active Python interpreter/venv cleanly
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True, shell=False)


async def init_db():
    await asyncio.to_thread(run_alembic)
    print("Database migrated to head successfully!")


if __name__ == "__main__":
    asyncio.run(init_db())
