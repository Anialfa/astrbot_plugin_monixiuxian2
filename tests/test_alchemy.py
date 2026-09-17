"""Exercise the expanded catalog against an isolated real SQLite database."""

import asyncio
from collections import Counter
import copy
import importlib
import json
from pathlib import Path
import re
import sys
import tempfile
import time
from unittest.mock import AsyncMock, patch


class Event:
    def __init__(self, user_id):
        self.user_id = user_id

    def get_sender_id(self):
        return self.user_id

    def get_sender_name(self):
        return "alchemy-test"

    def get_message_str(self):
        return ""

    def plain_result(self, text):
        return text


async def main(root):
    sys.path.insert(0, str(root.parent))

    def module(name):
        value = importlib.import_module(root.name + "." + name)
        assert Path(value.__file__).resolve().is_relative_to(root.resolve())
        return value

    module("data.data_manager")
    config = module("config_manager").ConfigManager(root)
    models = module("models")
    alchemy_module = module("managers.alchemy_manager")
    breakthrough_module = module("core.breakthrough_manager")
    results = {}

    def record(name, **details):
        results[name] = {"passed": True, **details}
        print("CASE=" + json.dumps({name: results[name]}), flush=True)

    with tempfile.TemporaryDirectory(prefix="alchemy-db-") as tmp:
        db = module("data.data_manager").DataBase(str(Path(tmp) / "isolated.db"))
        await db.connect()
        try:
            await module("data.migration").MigrationManager(db.conn, None).migrate()
            storage = module("core.storage_ring_manager").StorageRingManager(db, config)
            alchemy = alchemy_module.AlchemyManager(db, config, storage)
            pills = module("core.pill_manager").PillManager(db, config)
            breakthrough = breakthrough_module.BreakthroughManager(db, config, {})
            recipes = alchemy.recipes
            assert Counter(r["category"] for r in recipes.values()) == {"修为": 30, "破境": 26, "回复": 12}
            assert sorted(recipes) == list(range(1, 69))
            assert [recipes[i]["name"] for i in range(1, 6)] == ["炼气丹", "聚灵丹", "凝气丹", "培元丹", "玄灵丹"]
            for recipe in recipes.values():
                pill = pills.get_pill_by_name(recipe["name"])
                assert pill and all(isinstance(n, int) and n > 0 for n in recipe["materials"].values())
                assert 0 < recipe["success_rate"] <= 95
                for material in recipe["materials"]:
                    assert material == "灵石" or material in config.alchemy_config["material_sources"]
                expected_gold = pill["price"] * recipe["success_rate"] * 60 // 10000
                assert recipe["materials"]["灵石"] == max(1, expected_gold)
                if recipe["category"] == "破境":
                    assert recipe["level_required"] == pill["target_level_index"] - 1
            record("catalog_68_counts_costs_unlocks_and_legacy_ids")

            serial = 0

            async def player(**fields):
                nonlocal serial
                serial += 1
                values = dict(level_index=35, gold=10**10, experience=0,
                              cultivation_type="灵修", hp=100, mp=100,
                              spiritual_qi=10, max_spiritual_qi=1000000,
                              blood_qi=10, max_blood_qi=1000000)
                values.update(fields)
                value = models.Player(user_id="__alchemy_audit_" + str(serial), **values)
                await db.create_player(value)
                return value

            async def saved(value):
                return await db.get_player_by_id(value.user_id)

            for recipe in recipes.values():
                for roll in (1, 100):
                    p = await player(level_index=recipe["level_required"],
                                     cultivation_type=recipe["cultivation_type"] or "灵修")
                    materials = {n: c * 2 for n, c in recipe["materials"].items() if n != "灵石"}
                    p.set_storage_ring_items({**materials, "audit_sentinel": 7})
                    p.set_pills_inventory({"audit_pill": 3})
                    await db.update_player(p)
                    with patch.object(alchemy_module.random, "randint", return_value=roll):
                        ok, msg, result = await alchemy.craft_pill(p.user_id, recipe["id"])
                    after = await saved(p)
                    assert ok and result["success"] == (roll == 1), (recipe, msg)
                    assert after.gold == p.gold - recipe["materials"]["灵石"]
                    assert after.get_storage_ring_items() == {
                        **{n: c // 2 for n, c in materials.items()}, "audit_sentinel": 7}
                    assert after.get_pills_inventory() == {
                        "audit_pill": 3, **({recipe["name"]: 1} if roll == 1 else {})}
                    assert f"成功率：{recipe['success_rate']}%" in msg
                    record(f"craft_{recipe['id']}_{roll}")
                    if roll == 100:
                        continue
                    pill = pills.get_pill_by_name(recipe["name"])
                    if recipe["category"] == "修为":
                        ok, msg = await pills.use_pill(after, recipe["name"])
                        final = await saved(p)
                        assert ok and final.experience == pill["exp_gain"], msg
                        assert final.get_pills_inventory() == {"audit_pill": 3}
                        record(f"crafted_exp_use_{recipe['id']}")
                    elif recipe["category"] == "回复":
                        ok, msg = await pills.use_pill(after, recipe["name"])
                        final = await saved(p)
                        blood = p.cultivation_type == "体修"
                        gain = pill.get("blood_qi_restore") if blood else pill.get("spiritual_qi_restore")
                        energy = final.blood_qi if blood else final.spiritual_qi
                        assert ok and energy == (1000000 if gain == -1 else min(1000000, 10 + gain)), msg
                        assert final.get_pills_inventory() == {"audit_pill": 3}
                        record(f"crafted_restore_use_{recipe['id']}")
                    else:
                        before_inventory = after.get_pills_inventory()
                        ok, msg = await pills.use_pill(after, recipe["name"])
                        assert not ok and "突破 " in msg
                        assert (await saved(p)).get_pills_inventory() == before_inventory
                        after.experience = config.level_data[after.level_index + 1]["exp_needed"]
                        await db.update_player(after)
                        with patch.object(breakthrough_module.random, "random", return_value=0):
                            ok, msg, died = await breakthrough.execute_breakthrough(after, recipe["name"])
                        final = await saved(p)
                        assert ok and not died and final.level_index == recipe["level_required"] + 1, msg
                        assert final.get_pills_inventory() == {"audit_pill": 3}
                        record(f"crafted_breakthrough_use_{recipe['id']}")

            for recipe in (r for r in recipes.values() if r["category"] == "破境"):
                p = await player(level_index=recipe["level_required"], cultivation_type="体修")
                p.experience = config.body_level_data[p.level_index + 1]["exp_needed"]
                await db.update_player(p)
                with patch.object(breakthrough_module.random, "random") as random_roll:
                    ok, msg, died = await breakthrough.execute_breakthrough(p, recipe["name"])
                    random_roll.assert_not_called()
                assert not ok and "没有" in msg and not died
                p.set_pills_inventory({recipe["name"]: 2})
                await db.update_player(p)
                with patch.object(breakthrough_module.random, "random", return_value=1):
                    ok, msg, died = await breakthrough.execute_breakthrough(p, recipe["name"])
                final = await saved(p)
                assert not ok and not died and final.get_pills_inventory() == {recipe["name"]: 1}
                assert final.level_index == recipe["level_required"]
                record(f"breakthrough_ownership_failure_body_{recipe['id']}")

            r = recipes[31]
            p = await player(level_index=r["level_required"])
            p.set_pills_inventory({r["name"]: 1})
            effect = {"subtype": "breakthrough_boost", "breakthrough_bonus": .05,
                      "expiry_time": int(time.time()) + 3600}
            p.set_active_pill_effects([effect])
            await db.update_player(p)
            before = (await saved(p)).__dict__.copy()
            ok, msg, died = await breakthrough.execute_breakthrough(p, r["name"])
            assert not ok and (await saved(p)).__dict__ == before
            p.level_index -= 1
            await db.update_player(p)
            before = (await saved(p)).__dict__.copy()
            ok, msg, died = await breakthrough.execute_breakthrough(p, r["name"])
            assert not ok and (await saved(p)).__dict__ == before
            record("invalid_breakthrough_preserves_pill_and_temporary_effects")

            p.level_index = r["level_required"]
            p.experience = config.level_data[p.level_index + 1]["exp_needed"]
            await db.update_player(p)
            before = (await saved(p)).__dict__.copy()
            update = db.update_player

            async def fail_after_save(value):
                await update(value)
                raise RuntimeError("injected")

            with patch.object(db, "update_player", new=fail_after_save):
                try:
                    await breakthrough.execute_breakthrough(p, r["name"])
                    raise AssertionError("expected injected failure")
                except RuntimeError as error:
                    assert str(error) == "injected"
            assert (await saved(p)).__dict__ == before
            record("breakthrough_rollback_restores_pill_effects_and_player")

            stale = await saved(p)
            with patch.object(breakthrough_module.random, "random", return_value=0):
                responses = await asyncio.gather(breakthrough.execute_breakthrough(p, r["name"]),
                                                breakthrough.execute_breakthrough(stale, r["name"]))
            assert sum(result[0] for result in responses) == 1
            final = await saved(p)
            assert final.get_pills_inventory() == {} and final.get_active_pill_effects() == []
            record("concurrent_breakthrough_consumes_once")

            for resurrection in (False, True):
                p = await player(level_index=r["level_required"], has_resurrection_pill=resurrection)
                p.experience = config.level_data[p.level_index + 1]["exp_needed"]
                p.set_pills_inventory({r["name"]: 2})
                await db.update_player(p)
                with patch.object(breakthrough_module.random, "random", side_effect=[1, 0]):
                    ok, msg, died = await breakthrough.execute_breakthrough(p, r["name"])
                final = await saved(p)
                assert not ok and died == (not resurrection)
                if resurrection:
                    assert final and not final.has_resurrection_pill
                    assert final.get_pills_inventory() == {r["name"]: 1}
                else:
                    assert final is None
                record("breakthrough_death_resurrection_" + str(resurrection))

            p = await player()
            name = recipes[1]["name"]
            p.set_pills_inventory({name: 1})
            await db.update_player(p)
            stale = await saved(p)
            responses = await asyncio.gather(pills.use_pill(p, name), pills.use_pill(stale, name))
            assert sum(result[0] for result in responses) == 1
            assert (await saved(p)).experience == config.exp_pills_data[name]["exp_gain"]
            record("concurrent_pill_use_gains_once")

            for cultivation in ("灵修", "体修"):
                p = await player(cultivation_type=cultivation)
                expected = {r["id"] for r in recipes.values()
                            if not r["cultivation_type"] or r["cultivation_type"] == cultivation}
                found = []
                for page in range(1, (len(expected) + 4) // 5 + 1):
                    ok, msg = await alchemy.get_available_recipes(p.user_id, page)
                    ids = [int(x) for x in re.findall(r"ID:(\d+)", msg)]
                    assert ok and 1 <= len(ids) <= 5
                    assert config.get_level_data(cultivation)[35]["level_name"] in msg
                    found.extend(ids)
                assert found == sorted(expected)
                for category in ("修为", "破境", "回复"):
                    ok, msg = await alchemy.get_available_recipes(p.user_id, 1, category)
                    assert ok and all(recipes[int(i)]["category"] == category for i in re.findall(r"ID:(\d+)", msg))
                for page, category in [(0, "全部"), (999, "全部"), (1, "bad")]:
                    assert not (await alchemy.get_available_recipes(p.user_id, page, category))[0]
                assert alchemy._success_rate(p, recipes[1]) == 95
                p.level_index = 0
                await db.update_player(p)
                ok, msg = await alchemy.get_available_recipes(p.user_id, 1, "未解锁")
                assert ok and all(recipes[int(i)]["level_required"] > 0 for i in re.findall(r"ID:(\d+)", msg))
                record("pagination_filters_rates_realms_" + cultivation)

            p = await player(level_index=0)
            for recipe_id in (999, 30, 1):
                before = (await saved(p)).__dict__.copy()
                assert not (await alchemy.craft_pill(p.user_id, recipe_id))[0]
                assert (await saved(p)).__dict__ == before
            blood_recipe = next(r for r in recipes.values() if r["cultivation_type"])
            assert not (await alchemy.craft_pill(p.user_id, blood_recipe["id"]))[0]
            p.set_pills_inventory({blood_recipe["name"]: 1})
            await db.update_player(p)
            assert not (await pills.use_pill(p, blood_recipe["name"]))[0]
            assert (await saved(p)).get_pills_inventory() == {blood_recipe["name"]: 1}
            record("invalid_recipe_level_materials_and_cultivation_rejected")

            shop = module("handlers.shop_handler").ShopHandler(db, {}, config)
            changed = [pill for pill in config.exp_pills_data.values() if pill["required_level_index"] >= 22]
            for pill in changed:
                cached = [{"name": pill["name"], "type": "exp_pill", "price": 8,
                           "original_price": 10, "discount": .8, "stock": 7,
                           "data": {"price": 10, "exp_gain": 100}}]
                await db.update_shop_data("pill_pavilion", int(time.time()), cached)
                p = await player()
                messages = [msg async for msg in type(shop).handle_buy.__wrapped__(
                    shop, p, Event(p.user_id), pill["name"] + " 2")]
                final = await saved(p)
                _, items = await db.get_shop_data("pill_pavilion")
                assert final.gold == p.gold - int(pill["price"] * .8) * 2, messages
                assert final.get_pills_inventory() == {pill["name"]: 2}
                assert items[0]["stock"] == 5 and items[0]["discount"] == .8
                assert items[0]["data"] == pill
                assert not shop.shop_manager.ensure_items_have_stock(items)
                record("cached_shop_reprice_" + pill["name"])

            await db.update_shop_data("pill_pavilion", int(time.time()), cached)
            p = await player()
            reached = asyncio.Event()
            release = asyncio.Event()
            update_shop = db.update_shop_data

            async def pause_reprice(*args):
                reached.set()
                await release.wait()
                await update_shop(*args)

            async def buy():
                return [msg async for msg in type(shop).handle_buy.__wrapped__(
                    shop, p, Event(p.user_id), pill["name"] + " 2")]

            with patch.object(db, "update_shop_data", new=pause_reprice):
                refresh = asyncio.create_task(shop._ensure_pavilion_refreshed(
                    "pill_pavilion", shop.shop_manager.get_pills_for_display, 10))
                await asyncio.wait_for(reached.wait(), 5)
                purchase = asyncio.create_task(buy())
                await asyncio.sleep(.05)
                release.set()
                await asyncio.wait_for(asyncio.gather(refresh, purchase), 10)
            _, items = await db.get_shop_data("pill_pavilion")
            assert items[0]["stock"] == 5
            assert (await saved(p)).get_pills_inventory() == {pill["name"]: 2}
            record("concurrent_shop_reprice_and_purchase_preserves_stock")
        finally:
            await db.close()
    print("ALCHEMY_RESULTS=" + json.dumps(results), flush=True)


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1]).resolve()))
