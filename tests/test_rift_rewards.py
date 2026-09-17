"""Check rift rewards against real SQLite and storage code in an isolated database."""

import argparse
import asyncio
import importlib
import json
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import AsyncMock, patch


HERB = "\u7075\u8349"
IRON = "\u7cbe\u94c1"
PILL = "\u4e09\u54c1\u51dd\u795e\u589e\u76ca\u4e39"


async def main(root, expect_regression):
    sys.path.insert(0, str(root.parent))
    package = root.name
    data = importlib.import_module(package + ".data.data_manager")
    migration = importlib.import_module(package + ".data.migration")
    models = importlib.import_module(package + ".models")
    extended = importlib.import_module(package + ".models_extended")
    config_module = importlib.import_module(package + ".config_manager")
    storage_module = importlib.import_module(package + ".core.storage_ring_manager")
    rift_module = importlib.import_module(package + ".managers.rift_manager")
    assert Path(rift_module.__file__).resolve().is_relative_to(root.resolve())
    config = config_module.ConfigManager(root)
    assert config.is_pill(PILL)
    results = {}

    with tempfile.TemporaryDirectory(prefix="xiuxian-rift-tests-") as temporary:
        db = data.DataBase(str(Path(temporary) / "test.db"))
        await db.connect()
        try:
            await migration.MigrationManager(db.conn, None).migrate()
            storage = storage_module.StorageRingManager(db, config)
            manager = rift_module.RiftManager(db, config, storage)
            capacity = storage.get_ring_capacity(models.Player(user_id="test").storage_ring)
            full = {"slot_" + str(i): 1 for i in range(capacity - 1)}
            full[HERB] = 2
            cases = [
                ("material_empty", {}, {}, [(HERB, 6)], {HERB: 6}, {}, False),
                ("existing_items", {HERB: 2, IRON: 4}, {}, [(HERB, 6)], {HERB: 8, IRON: 4}, {}, False),
                ("mixed_materials_and_pills", {HERB: 2}, {PILL: 2}, [(PILL, 1), (HERB, 6), (PILL, 2), (IRON, 3)], {HERB: 8, IRON: 3}, {PILL: 5}, False),
                ("repeated_material", {HERB: 2}, {}, [(HERB, 3), (HERB, 4)], {HERB: 9}, {}, False),
                ("full_ring_stacks_and_pills", full, {PILL: 1}, [(HERB, 6), (IRON, 3), (PILL, 1)], {**full, HERB: 8}, {PILL: 2}, True),
                ("pills_only", {IRON: 2}, {PILL: 2}, [(PILL, 1)], {IRON: 2}, {PILL: 3}, False),
                ("no_drops", {IRON: 2}, {PILL: 2}, [], {IRON: 2}, {PILL: 2}, False),
            ]
            for name, items, pills, drops, expected_items, expected_pills, full_warning in cases:
                player = models.Player(user_id="__rift_test_" + name, experience=500, gold=200, hp=37)
                player.set_storage_ring_items(items)
                player.set_pills_inventory(pills)
                await db.create_player(player)
                await db.ext.set_user_busy(player.user_id, extended.UserStatus.EXPLORING, int(time.time()) - 1, {"rift_id": 1, "rift_level": 1})
                with patch.object(manager, "_roll_rift_drops", new=AsyncMock(return_value=drops)), patch.object(rift_module.random, "randint", side_effect=[120, 50]):
                    success, message, rewards = await manager.finish_exploration(player.user_id)
                saved = await db.get_player_by_id(player.user_id)
                checks = {
                    "success": success,
                    "items": saved.get_storage_ring_items() == expected_items,
                    "pills": saved.get_pills_inventory() == expected_pills,
                    "currency": saved.experience == 620 and saved.gold == 250,
                    "unrelated_fields": saved.hp == 37,
                    "idle": (await db.ext.get_user_cd(player.user_id)).type == extended.UserStatus.IDLE,
                    "reward_data": rewards is not None and rewards["items"] == drops,
                    "full_warning": ("\u50a8\u7269\u6212\u5df2\u6ee1\uff0c\u4e22\u5931" in message) == full_warning,
                }
                before_repeat = vars(saved).copy()
                repeated, _, _ = await manager.finish_exploration(player.user_id)
                checks["repeat_claim_rejected"] = not repeated and vars(await db.get_player_by_id(player.user_id)) == before_repeat
                results[name] = {"passed": all(checks.values()), "failed_checks": [key for key, ok in checks.items() if not ok]}

            player = models.Player(user_id="__rift_test_not_due", experience=500, gold=200)
            await db.create_player(player)
            await db.ext.set_user_busy(player.user_id, extended.UserStatus.EXPLORING, int(time.time()) + 600, {"rift_id": 1})
            before = vars(await db.get_player_by_id(player.user_id)).copy()
            success, _, rewards = await manager.finish_exploration(player.user_id)
            results["not_due_unchanged"] = {"passed": not success and rewards is None and vars(await db.get_player_by_id(player.user_id)) == before}
            snapshots = {name: vars(await db.get_player_by_id("__rift_test_" + name)).copy() for name, *_ in cases}
            await db.close()
            await db.connect()
            results["persisted_after_reconnect"] = {"passed": all([vars(await db.get_player_by_id("__rift_test_" + name)) == value for name, value in snapshots.items()])}
            async with db.conn.execute("PRAGMA integrity_check") as cursor:
                results["sqlite_integrity"] = {"passed": (await cursor.fetchone())[0] == "ok"}
        finally:
            await db.close()

    print("TEST_RESULTS=" + json.dumps(results), flush=True)
    failures = {name for name, result in results.items() if not result["passed"]}
    if expect_regression:
        assert failures == {"material_empty", "existing_items", "mixed_materials_and_pills", "repeated_material", "full_ring_stacks_and_pills"}, failures
    else:
        assert not failures, failures


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--plugin-root", type=Path, required=True)
    parser.add_argument("--expect-regression", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args.plugin_root, args.expect_regression))
