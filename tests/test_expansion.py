"""Check v20 migration preservation, route gates and independent event rewards."""

import asyncio
import importlib
import json
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import AsyncMock, patch

from test_database_fix import contents, fetch


async def main(root):
    sys.path.insert(0, str(root.parent))
    def module(name):
        return importlib.import_module(root.name + "." + name)
    data = module("data.data_manager")
    migration = module("data.migration")
    models = module("models")
    extended = module("models_extended")
    adventure_module = module("managers.adventure_manager")
    config = module("config_manager").ConfigManager(root)
    results = {}
    assert len(config.level_data) == 44
    with tempfile.TemporaryDirectory(prefix="xiuxian-expansion-") as directory:
        db = data.DataBase(str(Path(directory) / "old.db"))
        await db.connect()
        try:
            old_tasks = {version: task for version, task in migration.MIGRATION_TASKS.items() if version <= 20}
            with patch.object(migration, "LATEST_DB_VERSION", 20), patch.object(migration, "MIGRATION_TASKS", old_tasks):
                await migration.MigrationManager(db.conn, None).migrate()
            player = models.Player(user_id="__expansion_existing", level_index=16, gold=12345, experience=45678)
            player.set_storage_ring_items({"灵草": 7})
            await db.create_player(player)
            await db.ext.set_user_busy(player.user_id, extended.UserStatus.EXPLORING, int(time.time()) + 500,
                                      {"rift_id": 1, "rift_level": 1})
            await db.conn.execute("UPDATE rifts SET rewards=? WHERE rift_id=1", ('{"gold": [123, 456]}',))
            await db.conn.commit()
            before = await contents(db.conn)
            await migration.MigrationManager(db.conn, None).migrate()
            after = await contents(db.conn)
            for table, rows in before.items():
                if table not in ("rifts", "db_info"):
                    assert after[table] == rows, table
            old_rifts = await fetch(db.conn, "SELECT * FROM rifts WHERE rift_id <= 5 ORDER BY rift_id")
            assert sorted([r[:-1] for r in old_rifts], key=repr) == before["rifts"]
            assert all(r[-1] == 0 for r in old_rifts)
            assert await fetch(db.conn, "SELECT version FROM db_info") == [(21,)]
            assert len(after["rifts"]) == 15
            await migration.MigrationManager(db.conn, None).migrate()
            assert await contents(db.conn) == after
            results["v20_upgrade_preserves_players_inventory_cd_and_custom_rifts"] = True
            results["v21_restart_idempotent"] = True

            adventure = adventure_module.AdventureManager(db, config_manager=config)
            assert len(adventure.routes) == 14 and len(adventure.special_events) == 5
            for route in adventure.routes.values():
                assert 1200 <= route["duration"] <= 3600
                assert 300 <= route["fatigue_cooldown"] <= 1200
                visible = route.get("visible_level", 0)
                required = route.get("min_level", 0)
                gate = models.Player(user_id="__expansion_gate_" + route["key"], level_index=max(0, visible - 1))
                await db.create_player(gate)
                if visible:
                    assert route["key"] not in {r["key"] for r in adventure.get_route_overview(gate.level_index)}
                    assert not (await adventure.start_adventure(gate.user_id, route["key"]))[0]
                if required > visible:
                    gate.level_index = visible
                    await db.update_player(gate)
                    assert route["key"] in {r["key"] for r in adventure.get_route_overview(gate.level_index)}
                    assert not (await adventure.start_adventure(gate.user_id, route["key"]))[0]
                gate.level_index = required
                await db.update_player(gate)
                assert (await adventure.start_adventure(gate.user_id, route["key"]))[0]
            results["all_14_routes_visibility_entry_and_duration"] = True

            with patch.object(adventure_module.random, "randint", return_value=1) as roll:
                events = adventure._trigger_special_events()
                assert len(events) == 5 and roll.call_count == 5
            with patch.object(adventure_module.random, "randint", return_value=100):
                assert adventure._trigger_special_events() == []
            route = adventure.routes[adventure.default_route_key]
            fixed_event = {"desc": "test", "exp_mult": 1.0, "gold_mult": 1.0}
            base = adventure._calculate_rewards(player, route, 1, fixed_event, [])
            await db.ext.set_user_busy(player.user_id, extended.UserStatus.ADVENTURING, int(time.time()) - 1,
                                      {"route_key": route["key"]})
            with patch.object(adventure, "_trigger_special_events", return_value=events), \
                 patch.object(adventure, "_trigger_route_event", return_value=fixed_event), \
                 patch.object(adventure, "_handle_drops", new=AsyncMock(return_value=([], ""))):
                ok, _, rewards = await adventure.finish_adventure(player.user_id)
            assert ok and rewards["special_event_keys"] == [event["key"] for event in events]
            saved = await db.get_player_by_id(player.user_id)
            assert saved.experience == player.experience + base["exp"] + sum(e["bonus_exp"] for e in events)
            assert saved.gold == player.gold + base["gold"] + sum(e["bonus_gold"] for e in events)
            assert saved.get_storage_ring_items() == {"灵草": 7}
            assert not (await adventure.finish_adventure(player.user_id))[0]
            results["multiple_independent_events_awarded_once"] = True
        finally:
            await db.close()
    print("EXPANSION_RESULTS=" + json.dumps(results), flush=True)


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1]).resolve()))
