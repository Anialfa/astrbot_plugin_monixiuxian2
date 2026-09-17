"""Self-update support using AstrBot's repository updater."""

import asyncio
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
from uuid import uuid4

from astrbot.api import logger, sp


PLUGIN_NAME = "astrbot_plugin_monixiuxian2"
UPDATE_REPO = "https://github.com/wearshoes/astrbot_plugin_monixiuxian2"
TASK_ATTRIBUTE = "_xiuxian_update_task"
MAINTENANCE_ATTRIBUTE = "_xiuxian_maintenance"


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".update-", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class UpdateManager:
    def __init__(self, plugin, plugin_dir: Path, data_dir: Path):
        self.plugin = plugin
        self.context = plugin.context
        self.plugin_dir = Path(plugin_dir).resolve()
        self.data_dir = Path(data_dir).resolve()
        self.status_path = self.data_dir / "update_status.json"

    def allowed(self, event):
        admins = self.plugin.config.get("ACCESS_CONTROL", {}).get("UPDATE_ADMINS", [])
        return event.is_admin() or str(event.get_sender_id()) in {str(value) for value in admins}

    def start(self, event):
        if not self.allowed(event):
            return "无更新权限。仅 AstrBot 管理员或修仙更新管理员可使用。"
        task = getattr(self.context, TASK_ATTRIBUTE, None)
        if task is not None and not task.done():
            return "修仙插件正在更新，请勿重复执行，也请勿同时点击网页更新。"
        manager = getattr(self.context, "_star_manager", None)
        if manager is None or not all(callable(getattr(manager, name, None)) for name in
                                      ("update_plugin", "turn_off_plugin", "turn_on_plugin", "load", "reload")):
            return "当前 AstrBot 版本不支持此更新入口，请使用控制台更新。"
        expected_dir = Path(manager.plugin_store_path).resolve() / PLUGIN_NAME
        if self.plugin_dir != expected_dir or self.data_dir.is_relative_to(self.plugin_dir):
            return "插件安装路径异常，已取消更新。"
        # Context survives the module purge during self-reload; terminate() must not cancel this task.
        task = asyncio.create_task(self._run(event, manager), name="xiuxian-self-update")
        setattr(self.context, TASK_ATTRIBUTE, task)
        return "开始更新修仙插件：备份后从 fork 拉取并重载，完成后会通知。请勿同时点击网页更新。"

    def status(self, event):
        if not self.allowed(event):
            return "无更新权限。仅 AstrBot 管理员或修仙更新管理员可使用。"
        current = self.context.get_registered_star(PLUGIN_NAME)
        version = getattr(current, "version", "未知")
        try:
            record = json.loads(self.status_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return f"修仙插件版本：{version}\n尚未通过修仙更新命令执行更新。"
        except (OSError, ValueError):
            return f"修仙插件版本：{version}\n更新记录读取失败，请检查服务日志。"
        running = getattr(self.context, TASK_ATTRIBUTE, None)
        state = record.get("state", "未知")
        if state in ("准备中", "备份中", "更新中", "检查中") and (running is None or running.done()):
            state = "上次更新中断，请检查控制台和备份"
        lines = [f"修仙插件版本：{version}", f"最近更新：{state}"]
        if record.get("backup"):
            lines.append("备份编号：" + Path(record["backup"]).name)
        return "\n".join(lines)

    def _record(self, state, **fields):
        self.record.update(state=state, updated_at=int(time.time()), **fields)
        write_json(self.status_path, self.record)

    async def _wait_idle(self):
        while self.plugin._active_handlers:
            await asyncio.sleep(0.05)

    def _database_check(self, backup=None):
        with sqlite3.connect(self.plugin.db.db_path.resolve().as_uri() + "?mode=ro", uri=True) as conn:
            conn.execute("PRAGMA query_only=ON")
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Database integrity check failed")
            version = conn.execute("SELECT version FROM db_info").fetchone()[0]
            if backup is not None:
                with sqlite3.connect(backup) as target:
                    conn.backup(target)
        return version

    def _backup(self, directory):
        directory.mkdir(mode=0o700, parents=True)
        shutil.copytree(self.plugin_dir, directory / "plugin",
                        ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))
        overrides = self.data_dir / "config"
        if overrides.exists():
            shutil.copytree(overrides, directory / "config")
        config_file = getattr(self.plugin.config, "config_path", None)
        if config_file and Path(config_file).is_file():
            shutil.copy2(config_file, directory / "plugin-settings.json")
        else:
            write_json(directory / "plugin-settings.json", dict(self.plugin.config))
        version = self._database_check(directory / "database.db")
        write_json(directory / "manifest.json", {**self.record, "database_version": version})
        return version

    def _restore_code(self, backup):
        # Stage the entire old tree before replacing anything; keep the failed tree for inspection.
        staged = backup / "restore-staging"
        shutil.copytree(backup / "plugin", staged)
        if self.plugin_dir.exists():
            shutil.move(str(self.plugin_dir), str(backup / "failed-plugin"))
        shutil.move(str(staged), str(self.plugin_dir))

    async def _recover(self, manager, backup, database_version, replaced):
        current = self.context.get_registered_star(PLUGIN_NAME)
        if current is not None and current.activated:
            await manager.turn_off_plugin(PLUGIN_NAME)
        if replaced:
            if await asyncio.to_thread(self._database_check) != database_version:
                raise RuntimeError("Database schema changed; manual recovery required")
            await asyncio.to_thread(self._restore_code, backup)
        # reload(name) reloads ALL plugins if the name disappeared after a failed import.
        if self.context.get_registered_star(PLUGIN_NAME) is None:
            ok, message = await manager.load(specified_dir_name=PLUGIN_NAME)
        else:
            ok, message = await manager.reload(PLUGIN_NAME)
        if not ok:
            raise RuntimeError(message or "Recovery load failed")
        await manager.turn_on_plugin(PLUGIN_NAME)
        current = self.context.get_registered_star(PLUGIN_NAME)
        if current is None or not current.activated or current.star_cls is None:
            raise RuntimeError("Recovery activation failed")

    async def _notify(self, event, text):
        try:
            await event.send(event.plain_result(text))
        except Exception:
            logger.exception("修仙更新结果发送失败，可用修仙更新状态查询")

    async def _run(self, event, manager):
        backup = self.data_dir / "backups" / (time.strftime("update-%Y%m%d-%H%M%S") + "-" + uuid4().hex[:8])
        self.record = {"started_at": int(time.time()), "repo": UPDATE_REPO}
        disabled = replaced = False
        database_version = None
        message = "修仙更新未完成，请检查控制台。"
        try:
            self._record("准备中")
            sources = await sp.global_get("plugin_install_sources", {})
            source = sources.get(PLUGIN_NAME, {})
            if source.get("install_method") != "repository" or source.get("repo", "").rstrip("/").removesuffix(".git") != UPDATE_REPO:
                raise ValueError("请先在控制台将修仙插件的安装源绑定到本 fork 仓库。")
            if self.context.get_registered_star(PLUGIN_NAME) is None:
                raise RuntimeError("修仙插件未注册")
            setattr(self.context, MAINTENANCE_ATTRIBUTE, True)
            await asyncio.wait_for(self._wait_idle(), timeout=30)
            disabled = True
            await manager.turn_off_plugin(PLUGIN_NAME)
            self._record("备份中", backup=str(backup))
            database_version = await asyncio.to_thread(self._backup, backup)
            self._record("更新中")
            replaced = True
            await manager.update_plugin(PLUGIN_NAME, repo_url=UPDATE_REPO)
            current = self.context.get_registered_star(PLUGIN_NAME)
            if current is None:
                raise RuntimeError("新版本导入失败")
            self._record("检查中")
            await manager.turn_on_plugin(PLUGIN_NAME)
            current = self.context.get_registered_star(PLUGIN_NAME)
            if current is None or not current.activated or current.star_cls is None:
                raise RuntimeError("新版本未正常启用")
            await asyncio.to_thread(self._database_check)
            self._record("成功", version=current.version)
            message = f"修仙插件更新成功，当前版本：{current.version}。配置和玩家数据已保留。"
        except Exception as exc:
            logger.exception("修仙插件更新失败")
            state = "失败，未替换代码"
            message = f"修仙更新失败：{exc}" if not disabled else "修仙更新失败。"
            if disabled:
                try:
                    await self._recover(manager, backup, database_version, replaced)
                    state = "失败，已恢复旧版本" if replaced else "失败，已重新启用原版本"
                    message += state + "，数据库未回滚。"
                except Exception:
                    logger.exception("修仙插件自动恢复失败")
                    state = "失败，需要人工恢复"
                    message += "自动恢复未完成，请在控制台检查日志；备份已保留，数据库未回滚。"
            try:
                self._record(state, error_type=type(exc).__name__)
            except OSError:
                logger.exception("修仙更新状态无法写入")
                message += "更新状态记录未能写入，请检查磁盘空间及目录权限。"
        finally:
            setattr(self.context, MAINTENANCE_ATTRIBUTE, False)
        await self._notify(event, message)
