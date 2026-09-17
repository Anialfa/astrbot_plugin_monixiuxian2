"""Exercise fresh install, player lifecycle, and missing-record recovery."""

import argparse
import ast
import asyncio
import importlib
import json
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import time


async def fetch(connection, sql, parameters=()):
    async with connection.execute(sql, parameters) as cursor:
        return [tuple(row) for row in await cursor.fetchall()]


async def contents(connection):
    result = {}
    for (table,) in await fetch(connection, "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
        result[table] = sorted(await fetch(connection, f'SELECT * FROM "{table}"'), key=repr)
    return result


class Event:
    def __init__(self, command):
        self.command = command

    def get_sender_id(self):
        return '__database_test__'

    def get_message_str(self):
        return self.command

    def plain_result(self, message):
        return message


async def main(root, live_db=None):
    sys.path.insert(0, str(root.parent))
    pkg = root.name
    data_module = importlib.import_module(pkg + '.data.data_manager')
    migrations = importlib.import_module(pkg + '.data.migration')
    models = importlib.import_module(pkg + '.models')
    handler_module = importlib.import_module(pkg + '.handlers.player_handler')
    config_module = importlib.import_module(pkg + '.config_manager')
    assert Path(data_module.__file__).resolve().is_relative_to(root.resolve())
    results = {}
    with tempfile.TemporaryDirectory(prefix='xiuxian-db-tests-') as temporary:
        path = Path(temporary)
        db = data_module.DataBase(str(path / 'fresh.db'))
        await db.connect()
        try:
            manager = migrations.MigrationManager(db.conn, None)
            await manager.migrate()
            await db.ext.ensure_system_config_table()
            assert await fetch(db.conn, 'SELECT version FROM db_info') == [(20,)]
            assert await fetch(db.conn, 'SELECT COUNT(*) FROM rifts') == [(5,)]
            assert await fetch(db.conn, 'SELECT COUNT(*) FROM spirit_eyes') == [(3,)]
            for table in ('blessed_lands', 'spirit_farms', 'dual_cultivation', 'dual_cultivation_requests', 'combat_cooldowns'):
                await fetch(db.conn, f'SELECT * FROM {table}')
            await fetch(db.conn, 'SELECT extra_data FROM user_cd')
            await fetch(db.conn, 'SELECT last_collect_time FROM spirit_eyes')
            initial = await contents(db.conn)
            await manager.migrate()
            assert initial == await contents(db.conn)
            results['fresh_install_and_restart'] = 'passed'

            player = models.Player(user_id='__database_test__', spiritual_root='金灵根')
            await db.create_player(player)
            for table in ('user_cd', 'buff_info', 'impart_info'):
                assert await fetch(db.conn, f'SELECT COUNT(*) FROM {table} WHERE user_id=?', (player.user_id,)) == [(1,)]
            handler = handler_module.PlayerHandler(db, {'VALUES': {}}, config_module.ConfigManager(root))
            responses = [response async for response in handler.handle_start_cultivation(Event('闭关'))]
            assert any('已进入闭关状态' in response for response in responses)
            player = await db.get_player_by_id(player.user_id)
            assert player.state == '修炼中'
            assert (await db.ext.get_user_cd(player.user_id)).type == 1
            player.cultivation_start_time = int(time.time()) - 120
            await db.update_player(player)
            responses = [response async for response in handler.handle_end_cultivation(Event('出关'))]
            assert any('出关成功' in response for response in responses)
            player = await db.get_player_by_id(player.user_id)
            assert player.state == '空闲' and player.experience > 0
            assert (await db.ext.get_user_cd(player.user_id)).type == 0
            results['new_player_cultivation_start_and_end'] = 'passed'

            await db.conn.execute('INSERT INTO combat_cooldowns (user_id, last_duel_time) VALUES (?, ?)', (player.user_id, 123))
            await db.conn.execute('INSERT INTO combat_cooldowns (user_id, last_duel_time) VALUES (?, ?)', ('__other_player__', 456))
            await db.conn.commit()
            await db.delete_player_cascade(player.user_id)
            for table in ('players', 'user_cd', 'buff_info', 'impart_info', 'combat_cooldowns'):
                assert await fetch(db.conn, f'SELECT COUNT(*) FROM {table} WHERE user_id=?', (player.user_id,)) == [(0,)]
            assert await fetch(db.conn, 'SELECT last_duel_time FROM combat_cooldowns WHERE user_id=?', ('__other_player__',)) == [(456,)]
            results['targeted_player_and_cooldown_deletion'] = 'passed'

            check = sqlite3.connect(':memory:')
            await db.conn.backup(check)
            count = 0
            for file in root.rglob('*.py'):
                if file.name == 'migration.py' or file.is_relative_to(root / 'tests'):
                    continue
                tree = ast.parse(file.read_text(encoding='utf-8-sig'))
                for node in ast.walk(tree):
                    if isinstance(node, ast.Constant) and isinstance(node.value, str) and re.match(r'^\s*(SELECT|INSERT|REPLACE|UPDATE|DELETE|CREATE|ALTER|DROP|PRAGMA)\s', node.value, re.I):
                        sql = 'EXPLAIN ' + node.value.strip()
                        try:
                            check.execute(sql)
                        except sqlite3.ProgrammingError as error:
                            match = re.search(r'uses (\d+)', str(error))
                            assert match, str(error)
                            check.execute(sql, (None,) * int(match.group(1)))
                        count += 1
            check.close()
            results['business_sql_compiled'] = count
            if live_db is None:
                for index, state in enumerate(('空闲', '修炼中')):
                    fixture = models.Player(user_id='__recovery_test_' + str(index),
                                            gold=1234, experience=5678, state=state,
                                            cultivation_start_time=int(time.time()) - 120 if index else 0)
                    await db.create_player(fixture)
                    for table in ('user_cd', 'buff_info', 'impart_info'):
                        await db.conn.execute(f'DELETE FROM {table} WHERE user_id=?', (fixture.user_id,))
                await db.conn.commit()
                live_db = path / 'fresh.db'
        finally:
            await db.close()

        snapshot = path / 'production-copy.db'
        with sqlite3.connect(f'file:{live_db}?mode=ro', uri=True) as source:
            with sqlite3.connect(snapshot) as destination:
                source.backup(destination)
        db = data_module.DataBase(str(snapshot))
        await db.connect()
        try:
            before = await contents(db.conn)
            await db.initialize_player_records()
            after = await contents(db.conn)
            for table, records in before.items():
                if table not in ('user_cd', 'buff_info', 'impart_info'):
                    assert after[table] == records, table
                elif table != 'user_cd':
                    assert set(records) <= set(after[table]), table
            for table in ('user_cd', 'buff_info', 'impart_info'):
                assert await fetch(db.conn, f'SELECT COUNT(*) FROM players p LEFT JOIN {table} x ON x.user_id=p.user_id WHERE x.user_id IS NULL') == [(0,)]
            assert await fetch(db.conn, "SELECT COUNT(*) FROM players p JOIN user_cd c ON c.user_id=p.user_id WHERE p.state='修炼中' AND (c.type!=1 OR c.create_time!=p.cultivation_start_time)") == [(0,)]
            await db.initialize_player_records()
            assert after == await contents(db.conn)
            assert await fetch(db.conn, 'PRAGMA integrity_check') == [('ok',)]
            results['production_copy_backfill_preserves_players_and_is_idempotent'] = 'passed'
            results['production_player_count'] = len(before['players'])
        finally:
            await db.close()
    print('TEST_RESULTS=' + json.dumps(results, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--plugin-root', type=Path, required=True)
    parser.add_argument('--live-db', type=Path, help='Optional database to copy read-only; defaults to synthetic fixtures')
    args = parser.parse_args()
    asyncio.run(main(args.plugin_root, args.live_db))
