import asyncio
import subprocess


def run_alembic():
    # In Windows/Linux, we run alembic command to run migrations cleanly.
    # Using shell=True guarantees that alembic is found on the PATH of virtualenv/host OS.
    subprocess.run("alembic upgrade head", shell=True, check=True)


async def init_db():
    await asyncio.to_thread(run_alembic)
    print("Database migrated to head successfully!")


if __name__ == "__main__":
    asyncio.run(init_db())
