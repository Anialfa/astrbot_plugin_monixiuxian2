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
        
        def parse_herb_and_quantity(value: str):
            normalized = value.strip().replace("　", " ")
            normalized = normalized.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
            parts = normalized.rsplit(maxsplit=1)
            if len(parts) == 2:
                return parts[0], int(parts[1])
            return normalized, 1

        try:
            herb_name, quantity = parse_herb_and_quantity(herb_name)
        except ValueError:
            yield event.plain_result("❌ 种植数量必须是大于 0 的整数。")
            return

        # AstrBot 的指令绑定可能忽略多余参数，改从原始消息补解析数量，
        # 以确保“种植 灵草 3”会一次占用三个种植格。
        if quantity == 1:
            try:
                raw_message = event.get_message_str().strip().lstrip("/")
                if raw_message.startswith("种植"):
                    raw_message = raw_message[len("种植"):].strip()
                raw_herb_name, raw_quantity = parse_herb_and_quantity(raw_message)
                if raw_herb_name == herb_name:
                    quantity = raw_quantity
            except (AttributeError, ValueError):
                pass

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
