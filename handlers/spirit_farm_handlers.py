# handlers/spirit_farm_handlers.py
"""灵田处理器"""
from astrbot.api.event import AstrMessageEvent
from ..data import DataBase
from ..managers.spirit_farm_manager import SpiritFarmManager
from ..models import Player
from .utils import player_required

__all__ = ["SpiritFarmHandlers"]


class SpiritFarmHandlers:
    """灵田处理器"""
    
    def __init__(self, db: DataBase, farm_mgr: SpiritFarmManager):
        self.db = db
        self.mgr = farm_mgr
    
    @player_required
    async def handle_farm_info(self, player: Player, event: AstrMessageEvent):
        """查看灵田信息"""
        info = await self.mgr.get_farm_info(player.user_id)
        yield event.plain_result(info)
    
    @player_required
    async def handle_create_farm(self, player: Player, event: AstrMessageEvent):
        """开垦灵田"""
        success, msg = await self.mgr.create_farm(player)
        yield event.plain_result(msg)
    
    @player_required
    async def handle_plant(self, player: Player, event: AstrMessageEvent, herb_name: str = ""):
        """种植灵草"""
        if not herb_name.strip():
            yield event.plain_result(
                "🌱 可种植的灵草\n"
                "━━━━━━━━━━━━━━━\n"
                "灵草 - 45分钟（每株收获4份，修为+500）\n"
                "血灵草 - 45分钟（每株收获3份，修为+1500）\n"
                "冰心草 - 45分钟（每株收获3份，修为+4000）\n"
                "火焰花 - 45分钟（每株收获2份，修为+10000）\n"
                "九叶灵芝 - 45分钟（每株收获2份，修为+30000）\n"
                "━━━━━━━━━━━━━━━\n"
                "💡 使用 /种植 <灵草名> [数量]，例如 /种植 灵草 3"
            )
            return
        
        parts = herb_name.strip().rsplit(maxsplit=1)
        herb_name = parts[0]
        quantity = 1
        if len(parts) == 2:
            try:
                quantity = int(parts[1])
            except ValueError:
                yield event.plain_result("❌ 种植数量必须是大于 0 的整数。")
                return
            if quantity <= 0:
                yield event.plain_result("❌ 种植数量必须是大于 0 的整数。")
                return
        success, msg = await self.mgr.plant_herbs(player, herb_name, quantity)
        yield event.plain_result(msg)
    
    @player_required
    async def handle_harvest(self, player: Player, event: AstrMessageEvent):
        """收获灵草"""
        success, msg = await self.mgr.harvest(player)
        yield event.plain_result(msg)
    
    @player_required
    async def handle_upgrade_farm(self, player: Player, event: AstrMessageEvent):
        """升级灵田"""
        success, msg = await self.mgr.upgrade_farm(player)
        yield event.plain_result(msg)
