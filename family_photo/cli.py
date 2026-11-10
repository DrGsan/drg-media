import argparse
import asyncio
import fcntl
import logging
import json
import os
import secrets
from contextlib import contextmanager

from .config import Config
from .db import database
from .objects import Objects
from .archive import Archive
from . import __version__


@contextmanager
def worker_lock(config):
    config.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(config.data_dir / "telegram.lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Another reader/login process is using this Telegram session")
        yield


async def telegram_command(config, command):
    from .telegram import telegram_client, Synchronizer
    client = telegram_client(config)
    try:
        if command == "login":
            await client.start()
            print("Telegram session saved. You can now start the worker.")
        elif command == "dialogs":
            await client.connect()
            if not await client.is_user_authorized():
                raise SystemExit("Run login first")
            async for dialog in client.iter_dialogs():
                if dialog.is_group:
                    kind='supergroup' if getattr(dialog.entity,'megagroup',False) else 'basic'
                    print(f"{dialog.id}\t{kind}\t{dialog.name}")
        else:
            config.validate()
            archive = Archive(database(config.database_url), Objects(config))
            from .group_management import Registry, ManagedConfig
            registry = Registry(archive, config)
            config = ManagedConfig(config, registry)
            archive.objects.config = config
            await Synchronizer(config, archive, client).run()
    finally:
        await client.disconnect()


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="DrG Media Telegram archive")
    parser.add_argument("--version", action="version", version="DrG Media " + __version__)
    parser.add_argument("command", choices=["login", "dialogs", "worker", "serve", "generate-token", "import-export"])
    parser.add_argument("path", nargs="?", help="Extracted Telegram HTML export directory")
    parser.add_argument("--timezone", default="UTC", help="Timezone of dates without an offset in the export")
    parser.add_argument("--archive-id", help="Existing export: ID for an explicitly selected offline archive")
    parser.add_argument("--dry-run", action="store_true", help="Validate export and files without writing anything")
    args = parser.parse_args()
    if args.command == "generate-token":
        print(secrets.token_urlsafe(48))
        return
    if args.command == "import-export":
        from .import_export import read_export, import_export
        if not args.path:
            parser.error("import-export requires a directory")
        try:
            export = read_export(args.path, args.timezone, args.archive_id)
            if args.dry_run:
                print(json.dumps(export.report, ensure_ascii=False))
                return
            config = Config.from_env()
            config.validate()
            config.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            with worker_lock(config):
                archive = Archive(database(config.database_url), Objects(config))
                try:
                    from .group_management import Registry
                    Registry(archive, config)
                    result = import_export(export, archive, lambda state: print(json.dumps(state), flush=True))
                    print(json.dumps(result, ensure_ascii=False))
                finally:
                    archive.engine.dispose()
        except ValueError as error:
            parser.error(str(error))
        return
    logging.basicConfig(level=logging.WARNING)
    config = Config.from_env()
    if args.command == "serve":
        from .api import create_app
        create_app(config).run(host="127.0.0.1", port=8090)
    else:
        with worker_lock(config):
            asyncio.run(telegram_command(config, args.command))


if __name__ == "__main__":
    main()
