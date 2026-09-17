"""Prove native self-update replaces cached classes and actual help output."""

import asyncio
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import zipfile

import yaml


SOURCE = Path(sys.argv[1]).resolve()
NAME = "astrbot_plugin_monixiuxian2"


async def run(directory):
    root = Path(directory)
    os.environ["ASTRBOT_ROOT"] = directory
    os.chdir(root)
    live = root / "data/plugins" / NAME
    shutil.copytree(SOURCE, live)
    (root / "data/config").mkdir(parents=True)
    metadata_path = live / "metadata.yaml"
    metadata = yaml.safe_load(metadata_path.read_text(encoding="utf-8"))
    expected_version = metadata["version"]
    old_version = "v0.0.0-cache-test"
    metadata["version"] = old_version
    metadata_path.write_text(yaml.safe_dump(metadata, allow_unicode=True), encoding="utf-8")
    help_file = live / "handlers/misc_handler.py"
    help_file.write_text(help_file.read_text(encoding="utf-8").replace(expected_version, old_version), encoding="utf-8")
    sys.path.insert(0, directory)
    from astrbot.api import sp
    from astrbot.core.star.star_manager import PluginManager, star_registry
    from astrbot.core.star.star_handler import star_handlers_registry

    context = SimpleNamespace(
        get_registered_star=lambda name: next((p for p in star_registry if p.name == name), None),
        get_all_stars=lambda: list(star_registry),
        platform_manager=SimpleNamespace(get_insts=lambda: []),
        send_message=AsyncMock(),
    )
    manager = PluginManager(context, {})
    await sp.global_put("plugin_install_sources", {NAME: {
        "install_method": "repository", "repo": "https://github.com/wearshoes/" + NAME}})
    ok, message = await manager.load(specified_dir_name=NAME)
    assert ok, message
    old = context.get_registered_star(NAME).star_cls

    archive = root / "release.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        for path in SOURCE.rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts:
                bundle.write(path, arcname=NAME + "/" + path.relative_to(SOURCE).as_posix())

    async def local_archive(plugin_path, repo_url, proxy=""):
        assert repo_url == "https://github.com/wearshoes/" + NAME
        shutil.copy2(archive, str(plugin_path) + ".zip")

    class Event:
        messages = []

        def is_admin(self):
            return True

        def get_sender_id(self):
            return "isolated-admin"

        def get_group_id(self):
            return None

        def plain_result(self, text):
            return text

        async def send(self, text):
            self.messages.append(text)

    event = Event()
    try:
        old_help = [r async for r in old.handle_help(event)]
        assert old_version in old_help[0].splitlines()[0]
        with patch.object(manager._updater, "_download_repository", new=local_archive):
            replies = [r async for r in old.handle_plugin_update(event)]
            assert len(replies) == 1 and "开始更新" in replies[0], replies
            await asyncio.wait_for(context._xiuxian_update_task, timeout=90)
        current = context.get_registered_star(NAME)
        assert current.activated and current.star_cls is not old
        assert type(current.star_cls) is not type(old), "New instance reused cached plugin class"
        assert type(current.star_cls.misc_handler) is not type(old.misc_handler)
        assert current.star_cls.db._connection_alive()
        handlers = star_handlers_registry.get_handlers_by_module_name(current.module_path)
        help_handler = next(h for h in handlers if h.handler_name == "handle_help")
        actual_help = [r async for r in help_handler.handler(event)]
        assert expected_version in actual_help[0].splitlines()[0]
        assert old_version not in actual_help[0]
        assert "修仙更新状态" in actual_help[0] and "【管理员维护】" in actual_help[0]
        status = [r async for r in current.star_cls.handle_plugin_update_status(event)]
        assert "最近更新：成功" in status[0], status
        assert len(event.messages) == 1 and "更新成功" in event.messages[0], event.messages
        assert old.db.conn is None and old._active_handlers == 0
        assert all(t.done() for t in (old.boss_task, old.loan_check_task, old.spirit_eye_task, old.bounty_check_task))
        context.send_message.assert_not_called()
        print("NATIVE_UPDATE_RESULTS=" + json.dumps({
            "plugin_and_help_classes_reimported": True,
            "registered_help_handler_returns_new_version": True,
            "new_admin_commands_in_actual_help": True,
            "old_connections_and_tasks_closed": True,
            "status_and_completion_notification": True,
        }), flush=True)
    finally:
        current = context.get_registered_star(NAME)
        if current and current.activated:
            await manager.turn_off_plugin(NAME)


with tempfile.TemporaryDirectory(prefix="xiuxian-native-update-") as directory:
    asyncio.run(run(directory))
