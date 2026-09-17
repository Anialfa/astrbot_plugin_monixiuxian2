# managers/alchemy_manager.py
"""
炼丹系统管理器 - 处理炼丹与分境界配方
"""

import random
from typing import Tuple, List, Dict, Optional, TYPE_CHECKING
from ..data.data_manager import DataBase
from ..models import Player
from ..data.transaction import atomic_operation
from ..models_extended import UserStatus

if TYPE_CHECKING:
    from ..config_manager import ConfigManager
    from ..core import StorageRingManager


class AlchemyManager:
    """炼丹系统管理器（简化版）"""
    
    def __init__(self, db: DataBase, config_manager: "ConfigManager" = None, storage_ring_manager: "StorageRingManager" = None):
        self.db = db
        self.config_manager = config_manager
        self.storage_ring_manager = storage_ring_manager
        self.config = config_manager.alchemy_config if config_manager else {}
        
        raw_recipes = {}
        if config_manager and hasattr(config_manager, 'alchemy_recipes') and config_manager.alchemy_recipes:
            raw_recipes = config_manager.alchemy_recipes
        
        self.recipes = {}
        for recipe in raw_recipes.values():
            if isinstance(recipe, dict) and recipe.get("id"):
                recipe_id = int(recipe["id"])
                self.recipes[recipe_id] = self._normalize_recipe(recipe_id, recipe)
    
    def _normalize_recipe(self, recipe_id: int, recipe: Dict) -> Dict:
        """标准化配方字段，兼容不同格式的配置"""
        name = recipe.get("name", f"丹药{recipe_id}")
        
        desc = recipe.get("desc", None)
        if not desc and self.config_manager:
            pill_config = self._get_pill_config_by_name(name)
            if pill_config:
                desc = self._generate_pill_desc(pill_config)
        if not desc:
            desc = "丹药效果"
        
        return {
            "id": recipe.get("id", recipe_id),
            "name": name,
            "level_required": recipe.get("level_required", recipe.get("level", 0)),
            "materials": recipe.get("materials", recipe.get("cost", {})),
            "success_rate": recipe.get("success_rate", recipe.get("success", 50)),
            "desc": desc,
            "category": recipe.get("category", "修为"),
            "cultivation_type": recipe.get("cultivation_type", "")
        }
    
    def _generate_pill_desc(self, pill_config: Dict) -> str:
        """根据丹药配置生成描述"""
        rank = pill_config.get("rank", "")
        
        if pill_config.get("exp_gain"):
            return f"增加{pill_config['exp_gain']}修为（{rank}修为丹）"
        
        if pill_config.get("breakthrough_bonus"):
            bonus = int(pill_config["breakthrough_bonus"] * 100)
            return f"提升{bonus}%突破成功率（{rank}破境丹）"
        
        if pill_config.get("description"):
            return pill_config["description"]
        
        effect = pill_config.get("effect", {})
        if effect:
            effects = []
            if effect.get("add_hp"):
                effects.append(f"恢复{effect['add_hp']}气血")
            if effect.get("add_experience"):
                effects.append(f"增加{effect['add_experience']}修为")
            if effect.get("add_breakthrough_bonus"):
                bonus = int(effect["add_breakthrough_bonus"] * 100)
                effects.append(f"提升{bonus}%突破率")
            if effects:
                return f"{'，'.join(effects)}（{rank}）"
        
        return f"{rank}丹药"
    
    def _get_pill_config_by_name(self, name: str) -> Optional[Dict]:
        """根据丹药名称从配置中获取丹药信息"""
        if not self.config_manager:
            return None
        
        if hasattr(self.config_manager, 'exp_pills_data'):
            pill = self.config_manager.exp_pills_data.get(name)
            if pill:
                return pill
        
        if hasattr(self.config_manager, 'utility_pills_data'):
            pill = self.config_manager.utility_pills_data.get(name)
            if pill:
                return pill
        
        if hasattr(self.config_manager, 'pills_data'):
            pill = self.config_manager.pills_data.get(name)
            if pill:
                return pill
        
        if hasattr(self.config_manager, 'items_data'):
            item = self.config_manager.items_data.get(name)
            if item and item.get("type") == "丹药":
                return item
        
        return None
    
    def _success_rate(self, player: Player, recipe: Dict) -> int:
        return min(95, recipe["success_rate"] + max(0, player.level_index - recipe["level_required"]) * 2)

    def _level_name(self, level: int, player: Player) -> str:
        levels = self.config_manager.get_level_data(player.cultivation_type) if self.config_manager else []
        return levels[level]["level_name"] if 0 <= level < len(levels) else f"境界{level}"

    def _recipe_effect(self, recipe: Dict, player: Player) -> str:
        pill = self._get_pill_config_by_name(recipe["name"]) or {}
        if pill.get("subtype") == "breakthrough":
            target = self._level_name(pill.get("target_level_index", -1), player)
            return (f"突破至{target}，成功率+{pill.get('breakthrough_bonus', 0):.2%}，"
                    f"上限{pill.get('max_success_rate', 1):.2%}")
        return recipe["desc"]

    async def get_available_recipes(self, user_id: str, page: int = 1, category: str = "全部") -> Tuple[bool, str]:
        """
        获取可用的丹药配方
        
        Args:
            user_id: 用户ID
            
        Returns:
            (成功标志, 消息)
        """
        player = await self.db.get_player_by_id(user_id)
        if not player:
            return False, "❌ 你还未踏入修仙之路！"
        
        category = category.strip() or "全部"
        if category not in ("全部", "修为", "破境", "回复", "未解锁"):
            return False, "分类不存在，可选：全部、修为、破境、回复、未解锁。"
        if not isinstance(page, int) or page < 1:
            return False, "页码必须为正整数。"
        compatible = [r for r in self.recipes.values()
                      if not r["cultivation_type"] or r["cultivation_type"] == player.cultivation_type]
        unlocked = [r for r in compatible if player.level_index >= r["level_required"]]
        if category == "未解锁":
            available = sorted([r for r in compatible if player.level_index < r["level_required"]],
                               key=lambda r: int(r["id"]))
        else:
            available = sorted([r for r in unlocked if category == "全部" or r["category"] == category],
                               key=lambda r: int(r["id"]))
        if not available:
            return False, f"当前没有{category}配方。"
        page_size = max(1, min(8, int(self.config.get("recipe_page_size", 5))))
        pages = (len(available) + page_size - 1) // page_size
        if page > pages:
            return False, f"页码超出范围，当前分类共 {pages} 页。"
        lines = [f"丹药配方 · {category} · {page}/{pages}页",
                 f"当前境界：{self._level_name(player.level_index, player)}",
                 f"已解锁 {len(unlocked)} 种 · 配方总数 {len(self.recipes)} 种"]
        sources = self.config.get("material_sources", {})
        for recipe in available[(page - 1) * page_size:page * page_size]:
            level = self._level_name(recipe["level_required"], player)
            rate = recipe["success_rate"] if category == "未解锁" else self._success_rate(player, recipe)
            rate_label = "解锁时炼制成功率" if category == "未解锁" else "当前炼制成功率"
            materials = "、".join(f"{name}×{count:,}" for name, count in recipe["materials"].items())
            origins = "；".join(f"{name}：{sources.get(name, '暂无来源信息')}"
                                for name in recipe["materials"] if name != "灵石")
            restriction = " · 体修专用" if recipe["cultivation_type"] == "体修" else ""
            lines.extend([f"\n【{recipe['name']}】ID:{recipe['id']} · {recipe['category']}{restriction}",
                          f"需求境界：{level} | {rate_label}：{rate}%", f"材料：{materials}",
                          f"效果：{self._recipe_effect(recipe, player)}", f"来源：{origins}"])
        if page < pages:
            lines.append(f"\n下一页：丹药配方 {page + 1} {category}")
        lines.append("炼制：炼丹 <ID> | 分类：丹药配方 1 修为/破境/回复/未解锁")
        return True, "\n".join(lines)
    
    @atomic_operation
    async def craft_pill(
        self,
        user_id: str,
        pill_id: int
    ) -> Tuple[bool, str, Optional[Dict]]:
        """
        炼制丹药
        
        Args:
            user_id: 用户ID
            pill_id: 丹药ID
            
        Returns:
            (成功标志, 消息, 结果数据)
        """
        # 1. 检查用户
        player = await self.db.get_player_by_id(user_id)
        if not player:
            return False, "❌ 你还未踏入修仙之路！", None
        
        # 2. 检查用户状态（状态互斥）
        user_cd = await self.db.ext.get_user_cd(user_id)
        if user_cd and user_cd.type != UserStatus.IDLE:
            current_status = UserStatus.get_name(user_cd.type)
            return False, f"❌ 你当前正{current_status}，无法炼丹！", None
        
        # 3. 检查配方
        if pill_id not in self.recipes:
            return False, "❌ 无效的丹药ID！", None
        
        recipe = self.recipes[pill_id]
        if recipe["cultivation_type"] and recipe["cultivation_type"] != player.cultivation_type:
            return False, f"{recipe['name']}为{recipe['cultivation_type']}专用配方。", None
        if not self._get_pill_config_by_name(recipe["name"]):
            return False, "丹药配置不存在，请联系管理员。", None
        
        # 3. 检查境界要求
        if player.level_index < recipe["level_required"]:
            return False, f"❌ 炼制{recipe['name']}需要达到{self._level_name(recipe['level_required'], player)}！", None
        
        # 4. 检查所有材料
        materials = recipe["materials"]
        missing_materials = []
        
        # 检查灵石
        required_gold = materials.get("灵石", 0)
        if player.gold < required_gold:
            missing_materials.append(f"灵石（需要{required_gold}，拥有{player.gold}）")
        
        # 检查储物戒中的材料
        if self.storage_ring_manager:
            for material_name, required_count in materials.items():
                if material_name == "灵石":
                    continue
                current_count = self.storage_ring_manager.get_item_count(player, material_name)
                if current_count < required_count:
                    missing_materials.append(f"{material_name}（需要{required_count}，拥有{current_count}）")
        else:
            return False, "储物戒暂不可用，未消耗材料或灵石。", None
        
        if missing_materials:
            return False, f"❌ 材料不足！\n" + "\n".join(f"  · {m}" for m in missing_materials), None
        
        # 5. 扣除所有材料
        player.gold -= required_gold
        
        # 扣除储物戒中的材料
        consumed_materials = []
        if self.storage_ring_manager:
            for material_name, required_count in materials.items():
                if material_name == "灵石":
                    continue
                success, reason = await self.storage_ring_manager.retrieve_item(player, material_name, required_count)
                if success:
                    consumed_materials.append(f"{material_name}×{required_count}")
                else:
                    await self.db.conn.rollback()
                    return False, f"炼丹取消：{reason}，未消耗材料或灵石。", None
        
        # 6. 判断成功率
        final_success_rate = self._success_rate(player, recipe)
        
        roll = random.randint(1, 100)
        is_success = roll <= final_success_rate
        
        if is_success:
            # 炼制成功 - 丹药存入丹药背包
            pill_name = recipe["name"]
            
            # 将丹药存入丹药背包
            inventory = player.get_pills_inventory()
            inventory[pill_name] = inventory.get(pill_name, 0) + 1
            player.set_pills_inventory(inventory)
            
            await self.db.update_player(player)
            
            # 构建消耗材料显示
            cost_lines = []
            if required_gold > 0:
                cost_lines.append(f"灵石 -{required_gold}")
            cost_lines.extend(consumed_materials)
            cost_str = "、".join(cost_lines) if cost_lines else "无"
            use_command = "突破" if recipe["category"] == "破境" else "服用丹药"
            
            msg = f"""
🎉 炼丹成功！
━━━━━━━━━━━━━━━

你成功炼制了【{pill_name}】！
丹药已存入丹药背包

消耗：{cost_str}
成功率：{final_success_rate}%

💡 使用 /{use_command} {pill_name}
💡 使用 /丹药背包 查看所有丹药
            """.strip()
            
            result_data = {
                "success": True,
                "pill_name": pill_name,
                "cost": required_gold,
                "materials_consumed": consumed_materials
            }
        else:
            # 炼制失败
            await self.db.update_player(player)
            
            # 构建消耗材料显示
            cost_lines = []
            if required_gold > 0:
                cost_lines.append(f"灵石 -{required_gold}")
            cost_lines.extend(consumed_materials)
            cost_str = "、".join(cost_lines) if cost_lines else "无"
            
            msg = f"""
💔 炼丹失败
━━━━━━━━━━━━━━━

炼制【{recipe['name']}】失败了...

材料已消耗
消耗：{cost_str}
成功率：{final_success_rate}%

再接再厉！
            """.strip()
            
            result_data = {
                "success": False,
                "pill_name": recipe["name"],
                "cost": required_gold,
                "materials_consumed": consumed_materials
            }
        
        return True, msg, result_data
