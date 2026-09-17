"""Exercise persistent overrides and self-update failure recovery in isolation."""

import asyncio
import importlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


class Event:
    def __init__(self, user="ordinary", admin=False):
        self.user = user
        self.admin = admin
        self.messages = []

    def is_admin(self):
        return self.admin

    def get_sender_id(self):
        return self.user

    def plain_result(self, text):
        return text

    async def send(self, text):
        self.messages.append(text)


async def main(root):
    sys.path.insert(0, str(root.parent))
    importlib.import_module(root.name + ".data.data_manager")
    config_module = importlib.import_module(root.name + ".config_manager")
    update = importlib.import_module(root.name + ".managers.update_manager")
    adventure = importlib.import_module(root.name + ".managers.adventure_manager")
    bounty = importlib.import_module(root.name + ".managers.bounty_manager")
    results = {}

    def record(name):
        results[name] = {"passed": True}

    with tempfile.TemporaryDirectory(prefix="xiuxian-config-") as directory:
        base = Path(directory) / "plugin"
        data = Path(directory) / "data"
        shutil.copytree(root / "config", base / "config")
        config = config_module.ConfigManager(base, data)
        assert not list((data / "config").iterdir())
        assert not (base / "config" / "boss_config.json").exists()
        record("default_load_does_not_freeze_or_write_defaults")

        custom = json.loads((base / "config/alchemy_recipes.json").read_text(encoding="utf-8"))
        custom[0]["success_rate"] = 42
        (data / "config/alchemy_recipes.json").write_text(json.dumps(custom), encoding="utf-8-sig")
        (data / "config/boss_config.json").write_text('{"spawn_interval": 1234}', encoding="utf-8")
        cfg = config_module.ConfigManager(base, data)
        assert cfg.alchemy_recipes[custom[0]["name"]]["success_rate"] == 42
        assert cfg.boss_config["spawn_interval"] == 1234
        shutil.rmtree(base)
        shutil.copytree(root / "config", base / "config")
        default_pills = json.loads((base / "config/exp_pills.json").read_text(encoding="utf-8"))
        default_pills[0]["price"] = 654321
        (base / "config/exp_pills.json").write_text(json.dumps(default_pills), encoding="utf-8")
        cfg = config_module.ConfigManager(base, data)
        assert cfg.alchemy_recipes[custom[0]["name"]]["success_rate"] == 42
        assert cfg.exp_pills_data[default_pills[0]["name"]]["price"] == 654321
        record("overrides_survive_directory_replacement_new_defaults_follow_release")
        (data / "config/alchemy_recipes.json").unlink()
        assert config_module.ConfigManager(base, data).alchemy_recipes[custom[0]["name"]]["success_rate"] != 42
        record("removing_override_restores_shipped_default")

        routes = json.loads((base / "config/adventure_config.json").read_text(encoding="utf-8"))
        routes["routes"][0]["duration"] = 7654
        (data / "config/adventure_config.json").write_text(json.dumps(routes), encoding="utf-8")
        templates = json.loads((base / "config/bounty_templates.json").read_text(encoding="utf-8"))
        templates["templates"][0]["min_target"] = 987
        (data / "config/bounty_templates.json").write_text(json.dumps(templates), encoding="utf-8")
        cfg = config_module.ConfigManager(base, data)
        am = adventure.AdventureManager(None, None, cfg)
        bm = bounty.BountyManager(None, None, cfg)
        route = routes["routes"][0]
        assert am.routes[route["key"]]["duration"] == 7654
        assert bm.adventure_tag_meta[route["bounty_tag"]]["duration"] == 7654
        assert bm.templates_by_id[templates["templates"][0]["id"]]["min_target"] == 987
        record("adventure_bounty_and_route_metadata_use_overrides")
        for malformed in ("not-json", "[]"):
            (data / "config/boss_config.json").write_text(malformed, encoding="utf-8")
            try:
                config_module.ConfigManager(base, data)
                raise AssertionError("Invalid override must fail")
            except ValueError:
                pass
            assert (data / "config/boss_config.json").read_text() == malformed
        record("invalid_overrides_are_reported_without_overwriting")

    class Manager:
        def __init__(self, directory, mode):
            self.mode = mode
            self.plugin_store_path = str(directory / "plugins")
            self.current = SimpleNamespace(activated=True, version="old", star_cls=object())
            self.calls = []
            self.context = SimpleNamespace(get_registered_star=lambda name: self.current if name == update.PLUGIN_NAME else None)
            self.context._star_manager = self
            self.plugin_dir = Path(self.plugin_store_path) / update.PLUGIN_NAME
            self.plugin_dir.mkdir(parents=True)
            (self.plugin_dir / "code.py").write_text("original\n")
            self.data_dir = directory / "data"
            self.data_dir.mkdir()
            (self.data_dir / "config").mkdir()
            (self.data_dir / "config/custom.json").write_text('{"value": 91}')
            self.db_path = self.data_dir / "game.db"
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("CREATE TABLE db_info(version INTEGER)")
                conn.execute("INSERT INTO db_info VALUES(20)")
                conn.execute("CREATE TABLE players(id TEXT, gold INTEGER)")
                conn.execute("INSERT INTO players VALUES('existing',12345)")
            self.plugin = SimpleNamespace(context=self.context, _active_handlers=0,
                config={"ACCESS_CONTROL": {"UPDATE_ADMINS": ["updater"], "BOSS_ADMINS": ["boss"], "SHOP_MANAGERS": ["shop"]}},
                db=SimpleNamespace(db_path=self.db_path))

        async def turn_off_plugin(self, name):
            assert name == update.PLUGIN_NAME
            self.calls.append("off")
            self.current.activated = False

        async def turn_on_plugin(self, name):
            assert name == update.PLUGIN_NAME
            self.calls.append("on")
            self.current.activated = True
            self.current.star_cls = object()

        async def update_plugin(self, name, repo_url):
            assert name == update.PLUGIN_NAME and repo_url == update.UPDATE_REPO
            self.calls.append("update")
            assert not self.current.activated
            (self.plugin_dir / "code.py").write_text("new\n")
            if self.mode == "failed":
                raise OSError("download or extraction failed")
            if self.mode == "schema_changed":
                with sqlite3.connect(self.db_path) as conn:
                    conn.execute("UPDATE db_info SET version=21")
                raise RuntimeError("new migration failed")
            if self.mode == "disappeared":
                self.current = None
                return
            self.current.version = "new"

        async def reload(self, name):
            assert self.current is not None, "Never reload(name) when missing: AstrBot reloads everything"
            self.calls.append("reload")
            self.current.version = "old"
            return True, None

        async def load(self, *, specified_dir_name):
            assert specified_dir_name == update.PLUGIN_NAME
            self.calls.append("load-only-this-plugin")
            self.current = SimpleNamespace(activated=False, version="old", star_cls=object())
            return True, None

    source = {update.PLUGIN_NAME: {"install_method": "repository", "repo": update.UPDATE_REPO}}
    for mode in ("success", "failed", "disappeared", "schema_changed", "backup_failed", "wrong_source"):
        with tempfile.TemporaryDirectory(prefix="xiuxian-update-") as directory:
            manager = Manager(Path(directory), mode)
            updater = update.UpdateManager(manager.plugin, manager.plugin_dir, manager.data_dir)
            for user in ("ordinary", "boss", "shop"):
                assert "无更新权限" in updater.start(Event(user))
                assert "无更新权限" in updater.status(Event(user))
                assert not manager.calls
            assert updater.allowed(Event(admin=True))
            assert updater.allowed(Event("updater"))
            record("only_astrbot_or_explicit_update_admins")
            event = Event("updater")
            chosen_source = {} if mode == "wrong_source" else source
            with patch.object(update.sp, "global_get", new=AsyncMock(return_value=chosen_source)):
                if mode == "backup_failed":
                    updater._backup = lambda _: (_ for _ in ()).throw(OSError("disk full"))
                assert "开始更新" in updater.start(event)
                other_instance = update.UpdateManager(manager.plugin, manager.plugin_dir, manager.data_dir)
                assert "请勿重复执行" in other_instance.start(event)
                await getattr(manager.context, update.TASK_ATTRIBUTE)
            status = json.loads(updater.status_path.read_text(encoding="utf-8"))
            assert not getattr(manager.context, update.MAINTENANCE_ATTRIBUTE)
            assert len(event.messages) == 1
            with sqlite3.connect(manager.db_path) as conn:
                assert conn.execute("SELECT gold FROM players").fetchone()[0] == 12345
            assert (manager.data_dir / "config/custom.json").read_text() == '{"value": 91}'
            if mode == "success":
                assert status["state"] == "成功" and manager.current.activated
                backup = Path(status["backup"])
                assert (backup / "plugin/code.py").read_text() == "original\n"
                assert (backup / "config/custom.json").exists()
                with sqlite3.connect(backup / "database.db") as conn:
                    assert conn.execute("SELECT gold FROM players").fetchone()[0] == 12345
            elif mode == "schema_changed":
                assert status["state"] == "失败，需要人工恢复" and not manager.current.activated
                with sqlite3.connect(manager.db_path) as conn:
                    assert conn.execute("SELECT version FROM db_info").fetchone()[0] == 21
            elif mode == "wrong_source":
                assert not manager.calls and status["state"] == "失败，未替换代码"
            else:
                assert manager.current.activated and (manager.plugin_dir / "code.py").read_text() == "original\n"
                assert status["state"].startswith("失败，已")
                if mode == "backup_failed":
                    assert "update" not in manager.calls
                if mode == "disappeared":
                    assert "load-only-this-plugin" in manager.calls
            record("update_" + mode)
    with tempfile.TemporaryDirectory(prefix="xiuxian-no-status-") as directory:
        manager = Manager(Path(directory), "success")
        updater = update.UpdateManager(manager.plugin, manager.plugin_dir, manager.data_dir)
        event = Event(admin=True)
        with patch.object(update, "write_json", side_effect=OSError("disk full")):
            updater.start(event)
            await getattr(manager.context, update.TASK_ATTRIBUTE)
        assert not manager.calls and len(event.messages) == 1
        assert "磁盘空间" in event.messages[0]
        record("unwritable_status_aborts_and_notifies")
    print("UPDATE_RESULTS=" + json.dumps(results), flush=True)


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1]).resolve()))
