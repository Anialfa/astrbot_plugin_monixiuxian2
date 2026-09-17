"""Check inventory conservation, rejection, rollback and concurrent settlement."""

import asyncio
import importlib
import json
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

HERB = "\u7075\u8349"
IRON = "\u7cbe\u94c1"
GOLD = "\u7075\u77f3"
PILL = "\u4e09\u54c1\u51dd\u795e\u589e\u76ca\u4e39"


class Event:
    def __init__(self, user_id, message=""):
        self.user_id = user_id
        self.message = message

    def get_sender_id(self):
        return self.user_id

    def get_sender_name(self):
        return "audit-player"

    def get_message_str(self):
        return self.message

    def plain_result(self, text):
        return text


async def invoke(handler, method, player, *args):
    function = getattr(type(handler), method).__wrapped__
    return [text async for text in function(handler, player, Event(player.user_id), *args)]


async def main(root):
    sys.path.insert(0, str(root.parent))

    def module(name):
        loaded = importlib.import_module(root.name + "." + name)
        assert Path(loaded.__file__).resolve().is_relative_to(root.resolve())
        return loaded

    data = module("data.data_manager")
    migration = module("data.migration")
    models = module("models")
    extended = module("models_extended")
    config = module("config_manager").ConfigManager(root)
    storage_class = module("core.storage_ring_manager").StorageRingManager
    adventure_module = module("managers.adventure_manager")
    boss_module = module("managers.boss_manager")
    alchemy_module = module("managers.alchemy_manager")
    rift_module = module("managers.rift_manager")
    bounty_module = module("managers.bounty_manager")
    farm_module = module("managers.spirit_farm_manager")
    shop_class = module("handlers.shop_handler").ShopHandler
    equipment_class = module("handlers.equipment_handler").EquipmentHandler
    ring_class = module("handlers.storage_ring_handler").StorageRingHandler
    results = {}

    def record(name, reproduced=None, **details):
        results[name] = {"passed": True, **details}
        print("CASE=" + json.dumps({name: results[name]}, ensure_ascii=True), flush=True)

    with tempfile.TemporaryDirectory(prefix="inventory-db-") as temporary:
        db = data.DataBase(str(Path(temporary) / "isolated.db"))
        await db.connect()
        try:
            await migration.MigrationManager(db.conn, None).migrate()
            storage = storage_class(db, config)
            capacity = storage.get_ring_capacity(models.Player(user_id="test").storage_ring)
            full = {"audit_slot_" + str(i): 1 for i in range(capacity)}

            async def player(name, items=None, **fields):
                value = models.Player(user_id="__inventory_audit_" + name,
                                      experience=1000, gold=1000, hp=100, mp=100, **fields)
                value.set_storage_ring_items(items or {})
                await db.create_player(value)
                return value

            async def saved(value):
                return await db.get_player_by_id(value.user_id)

            # Keep reward generation deterministic; exercise the real persistence paths.
            adventure = adventure_module.AdventureManager(db, storage)
            route = adventure.routes[adventure.default_route_key]
            herb_drop = {"name": HERB, "weight": 1, "min": 3, "max": 3}
            adventure.drop_tables["audit"] = [herb_drop]
            p = await player("adventure", {HERB: 2})
            await db.ext.set_user_busy(p.user_id, extended.UserStatus.ADVENTURING,
                                      int(time.time()) - 1, {"route_key": route["key"]})
            event = {"desc": "audit", "item_chance": 100, "drop_tier": "audit"}
            with patch.object(adventure, "_trigger_route_event", return_value=event), \
                 patch.object(adventure, "_calculate_rewards", return_value={"exp": 120, "gold": 50}), \
                 patch.object(adventure_module.random, "randint", side_effect=[1, 1, 3]):
                ok, msg, reward = await adventure.finish_adventure(p.user_id)
            after = await saved(p)
            assert ok and reward["items"] == [(HERB, 3)] and after.gold == 1050
            assert after.get_storage_ring_items() == {HERB: 5}
            record("adventure_reward_persists", before=2, awarded=3, after=5)

            p = await player("boss", {IRON: 2})
            boss = extended.Boss(boss_id=0, boss_name="audit", boss_level="audit", hp=10,
                                 max_hp=10, atk=1, stone_reward=50, create_time=int(time.time()), status=1)
            await db.ext.create_boss(boss)
            combat = SimpleNamespace(player_vs_boss=lambda *_: {
                "winner": p.user_id, "reward": 50, "rounds": 1, "player_final_hp": 80,
                "player_final_mp": 70, "boss_final_hp": 0, "combat_log": []})
            manager = boss_module.BossManager(db, combat, config, storage)
            with patch.object(manager, "_roll_boss_drops", new=AsyncMock(return_value=[(IRON, 3)])):
                ok, msg, _ = await manager.challenge_boss(p.user_id)
            after = await saved(p)
            assert ok and after.gold == 1050 and after.get_storage_ring_items() == {IRON: 5}
            record("boss_reward_persists", before=2, awarded=3, after=5)

            shop = shop_class(db, {}, config)
            for item_type in ["material", "weapon", "armor", "main_technique", "technique", "accessory", "\u529f\u6cd5", "pill"]:
                name = PILL if item_type == "pill" else HERB if item_type == "material" else "audit_" + item_type
                p = await player("shop_" + item_type)
                await db.update_shop_data("treasure_pavilion", int(time.time()),
                                          [{"name": name, "type": item_type, "price": 10, "stock": 5}])
                messages = await invoke(shop, "handle_buy", p, name + " 2")
                after = await saved(p)
                _, stock = await db.get_shop_data("treasure_pavilion")
                assert after.gold == 980 and stock[0]["stock"] == 3, messages
                if item_type == "pill":
                    assert after.get_pills_inventory().get(name) == 2
                    record("control_shop_pills", False, control_passed=True, gold=after.gold, count=2)
                else:
                    assert after.get_storage_ring_items().get(name, 0) == 2
                    record("shop_" + item_type + "_persists", gold_before=1000, gold_after=after.gold,
                           stock_after=3, item_count=2)

            p = await player("shop_full", full)
            await db.update_shop_data("treasure_pavilion", int(time.time()),
                                      [{"name": HERB, "type": "material", "price": 10, "stock": 5}])
            messages = await invoke(shop, "handle_buy", p, HERB + " 2")
            after = await saved(p)
            _, stock = await db.get_shop_data("treasure_pavilion")
            assert after.gold == 1000 and after.get_storage_ring_items() == full and stock[0]["stock"] == 5
            record("shop_full_no_charge", gold_after=after.gold, stock_after=5, message=messages)

            p = await player("shop_rollback")
            await db.update_shop_data("treasure_pavilion", int(time.time()),
                                      [{"name": HERB, "type": "material", "price": 10, "stock": 5}])
            original_update = db.update_player
            calls = 0

            async def fail_second_update(value):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise RuntimeError("audit injected failure before charging")
                await original_update(value)

            with patch.object(db, "update_player", new=fail_second_update):
                try:
                    await invoke(shop, "handle_buy", p, HERB + " 2")
                    raise AssertionError("injected failure not reached")
                except RuntimeError as error:
                    assert str(error) == "audit injected failure before charging"
            after = await saved(p)
            _, stock = await db.get_shop_data("treasure_pavilion")
            assert after.gold == 1000 and after.get_storage_ring_items() == {} and stock[0]["stock"] == 5
            record("shop_error_rolls_back_all", gold_after=1000, item_count=0, stock_after=5)

            weapon = next(name for name, item in {**config.weapons_data, **config.items_data}.items()
                          if item.get("type") == "weapon" and item.get("required_level_index", 0) <= 0)
            equipment = equipment_class(db, config)
            p = await player("equip", {weapon: 1})
            messages = await invoke(equipment, "handle_equip_item", p, weapon)
            after = await saved(p)
            assert after.weapon == weapon and after.get_storage_ring_items().get(weapon, 0) == 0
            record("equip_conserves_item", weapon=weapon, equipped=True, still_in_ring=0)
            messages = await invoke(equipment, "handle_unequip_item", after, "weapon")
            after = await saved(p)
            assert not after.weapon and after.get_storage_ring_items().get(weapon) == 1
            record("equip_then_unequip_conserves_item", before=1, after=1)

            p = await player("unequip_full", full, weapon=weapon)
            messages = await invoke(equipment, "handle_unequip_item", p, "weapon")
            after = await saved(p)
            assert after.weapon == weapon and after.get_storage_ring_items().get(weapon, 0) == 0
            record("unequip_full_retains_equipment", equipped_after=True, in_ring=0, message=messages)

            alchemy = alchemy_module.AlchemyManager(db, config, storage)
            recipe = next(r for r in alchemy.recipes.values()
                          if any(key != GOLD for key in r["materials"]) and r["materials"].get(GOLD, 0) <= 1000)
            materials = {name: count * 2 for name, count in recipe["materials"].items() if name != GOLD}
            for roll, success in [(1, True), (100, False)]:
                p = await player("alchemy_" + str(roll), materials, level_index=recipe["level_required"])
                with patch.object(alchemy_module.random, "randint", return_value=roll):
                    ok, msg, result = await alchemy.craft_pill(p.user_id, int(recipe["id"]))
                after = await saved(p)
                expected_materials = {name: count - recipe["materials"][name] for name, count in materials.items()}
                assert ok and result["success"] == success and after.get_storage_ring_items() == expected_materials
                assert after.get_pills_inventory().get(recipe["name"], 0) == int(success)
                assert after.gold == 1000 - recipe["materials"].get(GOLD, 0)
                record("alchemy_" + ("success" if success else "failure") + "_consumes_materials", False,
                       recipe=recipe["name"], required=recipe["materials"], before=materials,
                       after=after.get_storage_ring_items(), pills=after.get_pills_inventory(), message=msg)

            ring = ring_class(db, config)
            p = await player("retrieve", {HERB: 3})
            messages = await invoke(ring, "handle_retrieve_item", p, HERB + " 2")
            after = await saved(p)
            assert after.get_storage_ring_items() == {HERB: 3} and after.get_pills_inventory() == {}
            record("retrieve_retains_items", before=3, after=3, message=messages)
            p = await player("retrieve_all", {HERB: 3, IRON: 2})
            messages = await invoke(ring, "handle_retrieve_all", p, "\u6750\u6599")
            after = await saved(p)
            assert after.get_storage_ring_items() == {HERB: 3, IRON: 2} and after.get_pills_inventory() == {}
            record("retrieve_all_retains_items", after=after.get_storage_ring_items(), message=messages)

            for action in ["reject", "accept_full", "expire", "accept_normal"]:
                sender = await player("sender_" + action, full if action in ["reject", "accept_full"] else {})
                receiver = await player("receiver_" + action, full if action == "accept_full" else {})
                # Fixture represents an already-sent gift: the sender's items were removed.
                gift_id = await db.ext.create_pending_gift(receiver.user_id, sender.user_id, "audit-sender",
                                                           HERB, 3, -1 if action == "expire" else 24)
                if action == "expire":
                    await db.ext.get_pending_gift(receiver.user_id)
                    messages = []
                else:
                    method = "handle_reject_gift" if action == "reject" else "handle_accept_gift"
                    messages = await invoke(ring, method, receiver)
                sender_count = (await saved(sender)).get_storage_ring_items().get(HERB, 0)
                receiver_count = (await saved(receiver)).get_storage_ring_items().get(HERB, 0)
                async with db.conn.execute("SELECT count(*) FROM pending_gifts WHERE id = ?", (gift_id,)) as cursor:
                    pending = (await cursor.fetchone())[0]
                assert pending == (0 if action == "accept_normal" else 1) and sender_count == 0
                assert receiver_count == (3 if action == "accept_normal" else 0)
                record("gift_" + action, False, sender_count=sender_count,
                       receiver_count=receiver_count, pending=pending, message=messages,
                       control_passed=action == "accept_normal")

                if action == "expire":
                    returned, waiting = await storage.recover_expired_gifts(sender.user_id)
                    assert (returned, waiting) == (1, 0)
                    assert (await saved(sender)).get_storage_ring_items() == {HERB: 3}
                    assert await storage.recover_expired_gifts(sender.user_id) == (0, 0)
                    record("expired_gift_recovered_once")

            p = await player("bounty", {HERB: 2})
            await db.ext.create_bounty(p.user_id, 1, "audit", "gather", 1,
                                      json.dumps({"stone": 50, "exp": 120}), int(time.time()) + 3600)
            await db.ext.update_bounty_progress(p.user_id, 1)
            bounty = bounty_module.BountyManager(db, storage)
            with patch.object(bounty, "_roll_bounty_items", new=AsyncMock(return_value=[(HERB, 3)])):
                ok, msg = await bounty.complete_bounty(p)
            after = await saved(p)
            assert ok and after.get_storage_ring_items() == {HERB: 5} and after.gold == 1050
            record("control_bounty", False, control_passed=True, before=2, awarded=3, after=5)

            p = await player("farm", {HERB: 2})
            crops = [{"name": HERB, "plant_time": int(time.time()) - 3600, "mature_time": int(time.time()) - 1}]
            await db.conn.execute("INSERT INTO spirit_farms (user_id, level, crops) VALUES (?, 1, ?)",
                                  (p.user_id, json.dumps(crops)))
            await db.conn.commit()
            ok, msg = await farm_module.SpiritFarmManager(db, storage).harvest(p)
            after = await saved(p)
            assert ok and after.get_storage_ring_items() == {HERB: 3}
            record("control_farm", False, control_passed=True, before=2, awarded=1, after=3)

            p = await player("rift", {HERB: 2})
            await db.ext.set_user_busy(p.user_id, extended.UserStatus.EXPLORING,
                                      int(time.time()) - 1, {"rift_id": 1, "rift_level": 1})
            rift = rift_module.RiftManager(db, config, storage)
            with patch.object(rift, "_roll_rift_drops", new=AsyncMock(return_value=[(HERB, 3), (PILL, 1)])), \
                 patch.object(rift_module.random, "randint", side_effect=[120, 50]):
                ok, msg, _ = await rift.finish_exploration(p.user_id)
            after = await saved(p)
            assert ok and after.get_storage_ring_items() == {HERB: 5} and after.get_pills_inventory() == {PILL: 1}
            record("control_rift_fixed", False, control_passed=True, before=2, awarded=3, after=5, pill_count=1)
            rift_player = p

            p = await player("stale_snapshot")
            stale = await saved(p)
            await storage.store_item(p, HERB, 2)
            stale.gold += 10
            await db.update_player(stale)
            assert (await saved(p)).get_storage_ring_items() == {HERB: 2}
            other = await saved(p)
            await storage.store_item(other, HERB, 3)
            p.gold += 20
            await db.update_player(p)
            assert (await saved(p)).get_storage_ring_items() == {HERB: 5}
            record("unrelated_stale_save_preserves_inventory")

            p = await player("invalid_quantities", {HERB: 3})
            for count in [0, -1, 1.5]:
                for operation in [storage.store_item, storage.retrieve_item, storage.discard_item]:
                    ok, _ = await operation(p, HERB, count)
                    assert not ok
            assert (await saved(p)).get_storage_ring_items() == {HERB: 3}
            record("invalid_quantities_rejected")

            p = await player("storage_concurrent")
            answers = await asyncio.gather(*[storage.store_item(p, HERB, 1) for _ in range(6)])
            assert all(answer[0] for answer in answers)
            assert (await saved(p)).get_storage_ring_items() == {HERB: 6}
            record("concurrent_stores_conserve_total")

            p = await player("shop_concurrent")
            await db.update_shop_data("treasure_pavilion", int(time.time()),
                                      [{"name": HERB, "type": "material", "price": 10, "stock": 2}])
            await asyncio.gather(*[invoke(shop, "handle_buy", p, HERB + " 2") for _ in range(2)])
            after = await saved(p)
            _, stock = await db.get_shop_data("treasure_pavilion")
            assert after.gold == 980 and after.get_storage_ring_items() == {HERB: 2} and stock[0]["stock"] == 0
            record("concurrent_purchase_stock_and_charge_once")

            p = await player("shop_cancelled")
            await db.update_shop_data("treasure_pavilion", int(time.time()),
                                      [{"name": HERB, "type": "material", "price": 10, "stock": 2}])
            reached = asyncio.Event()
            async def suspended_update(value):
                reached.set()
                await asyncio.Event().wait()
            with patch.object(db, "update_player", new=suspended_update):
                task = asyncio.create_task(invoke(shop, "handle_buy", p, HERB + " 2"))
                await asyncio.wait_for(reached.wait(), 10)
                task.cancel()
                try:
                    await task
                    raise AssertionError("expected cancellation")
                except asyncio.CancelledError:
                    pass
            after = await saved(p)
            _, stock = await db.get_shop_data("treasure_pavilion")
            assert after.gold == 1000 and after.get_storage_ring_items() == {} and stock[0]["stock"] == 2
            record("cancelled_purchase_rolls_back")

            replacement = "audit_old_weapon"
            p = await player("replace_full", {**dict(list(full.items())[:-1]), weapon: 1}, weapon=replacement)
            await invoke(equipment, "handle_equip_item", p, weapon)
            after = await saved(p)
            assert after.weapon == weapon and after.get_storage_ring_items().get(replacement) == 1
            assert after.get_storage_ring_items().get(weapon, 0) == 0
            record("full_ring_replacement_reuses_freed_slot")
            p = await player("replace_full_stack", {**dict(list(full.items())[:-1]), weapon: 2}, weapon=replacement)
            before = (await saved(p)).get_storage_ring_items()
            await invoke(equipment, "handle_equip_item", p, weapon)
            after = await saved(p)
            assert after.weapon == replacement and after.get_storage_ring_items() == before
            record("replacement_without_space_rolls_back")
            p = await player("unequip_full_stack", {**dict(list(full.items())[:-1]), weapon: 1}, weapon=weapon)
            await invoke(equipment, "handle_unequip_item", p, "weapon")
            after = await saved(p)
            assert not after.weapon and after.get_storage_ring_items()[weapon] == 2
            record("full_ring_unequip_stacks_existing_item")

            p = await player("equip_error", {weapon: 1})
            with patch.object(db, "update_player", new=AsyncMock(side_effect=RuntimeError("injected"))):
                try:
                    await invoke(equipment, "handle_equip_item", p, weapon)
                    raise AssertionError("expected error")
                except RuntimeError:
                    pass
            after = await saved(p)
            assert not after.weapon and after.get_storage_ring_items() == {weapon: 1}
            record("equip_error_rolls_back_retrieval")

            p = await player("alchemy_error", materials, level_index=recipe["level_required"])
            with patch.object(db, "update_player", new=AsyncMock(side_effect=RuntimeError("injected"))):
                try:
                    await alchemy.craft_pill(p.user_id, int(recipe["id"]))
                    raise AssertionError("expected error")
                except RuntimeError:
                    pass
            after = await saved(p)
            assert after.gold == 1000 and after.get_storage_ring_items() == materials and after.get_pills_inventory() == {}
            record("alchemy_error_restores_materials_and_gold")

            sender = await player("expired_full_sender", full)
            receiver = await player("expired_full_receiver")
            await db.ext.create_pending_gift(receiver.user_id, sender.user_id, "sender", HERB, 3, -1)
            assert await storage.recover_expired_gifts(sender.user_id) == (0, 1)
            await storage.discard_item(sender, next(iter(full)), 1)
            assert await storage.recover_expired_gifts(sender.user_id) == (1, 0)
            assert (await saved(sender)).get_storage_ring_items()[HERB] == 3
            record("expired_full_gift_retained_and_retried")

            sender = await db.get_player_by_id("__inventory_audit_sender_reject")
            receiver = await db.get_player_by_id("__inventory_audit_receiver_reject")
            await storage.discard_item(sender, next(iter(full)), 1)
            await invoke(ring, "handle_reject_gift", receiver)
            assert (await saved(sender)).get_storage_ring_items()[HERB] == 3
            assert await db.ext.get_pending_gift(receiver.user_id) is None
            record("rejected_gift_retry_returns_once")

            sender = await player("concurrent_gift_sender")
            receiver = await player("concurrent_gift_receiver")
            await db.ext.create_pending_gift(receiver.user_id, sender.user_id, "sender", HERB, 3)
            await asyncio.gather(*[invoke(ring, "handle_accept_gift", receiver) for _ in range(2)])
            assert (await saved(receiver)).get_storage_ring_items() == {HERB: 3}
            assert await db.ext.get_pending_gift(receiver.user_id) is None
            record("concurrent_gift_claim_once")

            sender = await player("gift_error_sender")
            receiver = await player("gift_error_receiver")
            await db.ext.create_pending_gift(receiver.user_id, sender.user_id, "sender", HERB, 3)
            with patch.object(extended_module := module("data.database_extended").DatabaseExtended,
                              "delete_pending_gift", new=AsyncMock(side_effect=RuntimeError("injected"))):
                try:
                    await invoke(ring, "handle_accept_gift", receiver)
                    raise AssertionError("expected error")
                except RuntimeError:
                    pass
            assert (await saved(receiver)).get_storage_ring_items() == {}
            assert await db.ext.get_pending_gift(receiver.user_id) is not None
            record("gift_claim_error_restores_pending_record")

            from astrbot.api.all import Plain
            sender = await player("gift_send_error", {HERB: 3})
            receiver = models.Player(user_id="909090909")
            await db.create_player(receiver)
            event = Event(sender.user_id)
            event.message_obj = SimpleNamespace(message=[Plain("\u8d60\u4e88 " + receiver.user_id + " " + HERB + " 2")])
            with patch.object(extended_module, "create_pending_gift", new=AsyncMock(side_effect=RuntimeError("injected"))):
                try:
                    _ = [message async for message in type(ring).handle_gift_item.__wrapped__(ring, sender, event, "")]
                    raise AssertionError("expected error")
                except RuntimeError:
                    pass
            assert (await saved(sender)).get_storage_ring_items() == {HERB: 3}
            assert await db.ext.get_pending_gift(receiver.user_id) is None
            record("gift_send_error_restores_inventory")

            p = await player("rift_concurrent")
            await db.ext.set_user_busy(p.user_id, extended.UserStatus.EXPLORING,
                                      int(time.time()) - 1, {"rift_id": 1, "rift_level": 1})
            with patch.object(rift, "_roll_rift_drops", new=AsyncMock(return_value=[(HERB, 3)])), \
                 patch.object(rift_module.random, "randint", side_effect=[120, 50]):
                outcomes = await asyncio.gather(*[rift.finish_exploration(p.user_id) for _ in range(2)])
            assert sum(outcome[0] for outcome in outcomes) == 1
            after = await saved(p)
            assert after.gold == 1050 and after.experience == 1120 and after.get_storage_ring_items() == {HERB: 3}
            record("concurrent_rift_completion_claims_once")

            p = await player("adventure_error")
            await db.ext.set_user_busy(p.user_id, extended.UserStatus.ADVENTURING,
                                      int(time.time()) - 1, {"route_key": route["key"]})
            with patch.object(adventure, "_trigger_route_event", return_value={"desc": "audit", "item_chance": 100, "drop_tier": "audit"}), \
                 patch.object(adventure_module.random, "randint", side_effect=[1, 1, 3]), \
                 patch.object(db, "update_player", new=AsyncMock(side_effect=RuntimeError("injected"))):
                try:
                    await adventure.finish_adventure(p.user_id)
                    raise AssertionError("expected error")
                except RuntimeError:
                    pass
            after = await saved(p)
            assert after.gold == 1000 and after.get_storage_ring_items() == {}
            assert (await db.ext.get_user_cd(p.user_id)).type == extended.UserStatus.ADVENTURING
            record("adventure_error_leaves_reward_claimable")

            await db.close()
            await db.connect()
            after = await saved(rift_player)
            assert after.get_storage_ring_items() == {HERB: 5}
            async with db.conn.execute("PRAGMA integrity_check") as cursor:
                assert (await cursor.fetchone())[0] == "ok"
            record("control_reconnect_integrity", False, control_passed=True)
        finally:
            await db.close()
    print("AUDIT_RESULTS=" + json.dumps(results, ensure_ascii=True), flush=True)


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1])))
