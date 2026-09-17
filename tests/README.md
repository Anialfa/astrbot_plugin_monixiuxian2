# 回归检查

已在 Python 3.12、AstrBot 4.27.4 环境验证。测试使用插件真实的 SQLite 和业务实现，不需要 QQ 平台连接。

在已经安装 AstrBot 的 Python 环境中，从仓库根目录运行：

```bash
python -m pip install -r requirements.txt
python tests/run_regressions.py
```

独立开发环境需先安装 AstrBot，例如 `python -m pip install astrbot==4.27.4`。统一入口会复制源码到临时目录，再创建测试库，避免在当前安装目录生成默认配置或测试数据。

| 脚本 | 覆盖内容 |
| --- | --- |
| `test_database_fix.py` | 建表迁移、角色创建与删除、闭关、缺失记录补齐及重复执行 |
| `test_rift_rewards.py` | 秘境材料和丹药入库、满戒、重复领奖、重新连接后的持久化 |
| `test_inventory.py` | 商店、装备、赠予、历练、Boss、悬赏、灵田的库存守恒与事务回滚 |
| `test_alchemy.py` | 68 张配方、丹药使用、破境消耗、ID 升序分页、并发购买与价格同步 |
| `test_update_config.py` | 外部配置优先、更新保留配置、未覆盖默认值随版本更新、更新权限、重复触发、备份、失败恢复及数据库不回滚 |

分项检查时传入一份可丢弃的插件副本：

```bash
python tests/test_database_fix.py --plugin-root /path/to/astrbot_plugin_monixiuxian2
python tests/test_rift_rewards.py --plugin-root /path/to/astrbot_plugin_monixiuxian2
python tests/test_inventory.py /path/to/astrbot_plugin_monixiuxian2
python tests/test_alchemy.py /path/to/astrbot_plugin_monixiuxian2
```

数据库检查默认使用合成角色。可选参数 `--live-db /path/to/database.db` 会以只读方式复制指定数据库，后续检查仅修改临时副本。测试失败时返回非零退出码并输出错误；全部通过时输出 `REGRESSION_RESULTS` 汇总。
