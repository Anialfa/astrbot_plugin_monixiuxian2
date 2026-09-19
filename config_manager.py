import json
from copy import deepcopy
from pathlib import Path
from typing import List, Dict, Any

from astrbot.api import logger
from .data.default_configs import SECT_CONFIG, BOSS_CONFIG, RIFT_CONFIG, ALCHEMY_CONFIG

class ConfigManager:
    """配置管理器，加载境界、物品、武器和丹药配置"""

    def __init__(self, base_dir: Path, data_dir: Path = None):
        self._base_dir = base_dir
        self.override_dir = Path(data_dir) / "config" if data_dir is not None else None
        if self.override_dir is not None:
            self.override_dir.mkdir(parents=True, exist_ok=True)
            self._validate_overrides()
        self.level_data: List[dict] = []  # 灵修境界数据
        self.body_level_data: List[dict] = []  # 体修境界数据
        self.items_data: Dict[str, dict] = {}  # 物品数据，key为物品名称
        self.weapons_data: Dict[str, dict] = {}  # 武器数据，key为武器名称
        self.pills_data: Dict[str, dict] = {}  # 破境丹数据，key为丹药名称
        self.exp_pills_data: Dict[str, dict] = {}  # 修为丹数据，key为丹药名称
        self.utility_pills_data: Dict[str, dict] = {}  # 功能丹数据，key为丹药名称
        self.storage_rings_data: Dict[str, dict] = {}  # 储物戒数据，key为储物戒名称
        self.materials_data: Dict[str, dict] = {}  # 材料图鉴，key为材料名称
        
        # 新增系统配置
        self.sect_config: Dict[str, Any] = {}
        self.boss_config: Dict[str, Any] = {}
        self.rift_config: Dict[str, Any] = {}
        self.alchemy_config: Dict[str, Any] = {}
        
        self._load_all()

    def config_path(self, filename: str) -> Path:
        """Resolve a whole-file override outside the replaceable plugin directory."""
        if Path(filename).name != filename or not filename.endswith(".json"):
            raise ValueError("Invalid configuration filename")
        if self.override_dir is not None:
            override = self.override_dir / filename
            if override.exists():
                return override
        return self._base_dir / "config" / filename

    def _validate_overrides(self):
        item_files = {"items.json", "weapons.json", "pills.json", "exp_pills.json",
                      "utility_pills.json", "storage_rings.json", "alchemy_recipes.json",
                      "material_catalog.json"}
        list_files = {"level_config.json", "body_level_config.json"}
        for path in self.override_dir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8-sig"))
                expected = (list, dict) if path.name in item_files else (list if path.name in list_files else dict)
                if not isinstance(data, expected):
                    raise ValueError("unexpected JSON root type")
            except (OSError, ValueError) as exc:
                raise ValueError(f"自定义配置无效，请修正后重载：{path.name}") from exc

    def get_level_data(self, cultivation_type: str = "灵修") -> List[dict]:
        """根据修炼类型获取对应的境界数据"""
        if cultivation_type == "体修":
            return self.body_level_data
        return self.level_data

    def _load_json_data(self, file_path: Path) -> List[dict]:
        """加载JSON配置文件（列表格式）"""
        if not file_path.exists():
            logger.warning(f"数据文件 {file_path} 不存在，将使用空数据。")
            return []
        try:
            with open(file_path, 'r', encoding='utf-8-sig') as f:
                data = json.load(f)
                logger.info(f"成功加载 {file_path.name} (共 {len(data)} 条数据)。")
                return data
        except Exception as e:
            logger.error(f"加载数据文件 {file_path} 失败: {e}")
            return []
            
    def _load_config_with_default(self, file_path: Path, default_config: Dict) -> Dict:
        """加载配置，缺省值只在内存中使用，不改写仓库文件。"""
        if not file_path.exists():
            return deepcopy(default_config)
        
        try:
            with open(file_path, 'r', encoding='utf-8-sig') as f:
                data = json.load(f)
                logger.info(f"成功加载配置文件: {file_path.name}")
                return data
        except Exception as e:
            logger.error(f"加载配置文件 {file_path} 失败: {e}")
            return default_config

    def _load_items_data(self, file_path: Path) -> Dict[str, dict]:
        """加载物品配置文件并转换为字典（key为物品名称）"""
        if not file_path.exists():
            logger.warning(f"物品数据文件 {file_path} 不存在，将使用空数据。")
            return {}
        try:
            with open(file_path, 'r', encoding='utf-8-sig') as f:
                data = json.load(f)

                if isinstance(data, list):
                    items_dict = {item.get("name", ""): item for item in data if isinstance(item, dict) and item.get("name")}
                elif isinstance(data, dict):
                    items_dict = {}
                    for item_id, item_data in data.items():
                        if isinstance(item_data, dict) and item_data.get("name"):
                            if "id" not in item_data:
                                item_data["id"] = item_id
                            items_dict[item_data["name"]] = item_data
                else:
                    logger.error(f"物品数据文件 {file_path} 格式不正确，应该是数组或字典。")
                    return {}

                logger.info(f"成功加载 {file_path.name} (共 {len(items_dict)} 个物品)。")
                return items_dict
        except Exception as e:
            logger.error(f"加载物品数据文件 {file_path} 失败: {e}")
            return {}

    def _load_all(self):
        """加载所有配置文件"""
        config_path = self.config_path
        
        # 加载基础配置
        self.level_data = self._load_json_data(config_path("level_config.json"))
        self.body_level_data = self._load_json_data(config_path("body_level_config.json"))
        self.items_data = self._load_items_data(config_path("items.json"))
        self.weapons_data = self._load_items_data(config_path("weapons.json"))
        self.pills_data = self._load_items_data(config_path("pills.json"))
        self.exp_pills_data = self._load_items_data(config_path("exp_pills.json"))
        self.utility_pills_data = self._load_items_data(config_path("utility_pills.json"))
        self.storage_rings_data = self._load_items_data(config_path("storage_rings.json"))
        self.materials_data = self._load_items_data(config_path("material_catalog.json"))
        
        # 加载新系统配置
        self.sect_config = self._load_config_with_default(config_path("sect_config.json"), SECT_CONFIG)
        self.boss_config = self._load_config_with_default(config_path("boss_config.json"), BOSS_CONFIG)
        self.rift_config = self._load_config_with_default(config_path("rift_config.json"), RIFT_CONFIG)
        self.alchemy_config = self._load_config_with_default(config_path("alchemy_config.json"), ALCHEMY_CONFIG)
        self.alchemy_recipes = self._load_items_data(config_path("alchemy_recipes.json"))
        
        # 加载游戏配置（包含各系统的硬编码参数）
        self.game_config = self._load_config_with_default(config_path("game_config.json"), {})
        
        self._pill_names_cache = None

        logger.info(
            f"配置管理器初始化完成，"
            f"加载了 {len(self.level_data)} 个灵修境界配置，"
            f"{len(self.body_level_data)} 个体修境界配置，"
            f"以及新系统配置 (宗门/Boss/秘境/炼丹)"
        )
    
    def is_pill(self, item_name: str) -> bool:
        """检查物品是否为丹药类型（统一的丹药判断方法）"""
        if item_name in self.pills_data:
            return True
        if item_name in self.exp_pills_data:
            return True
        if item_name in self.utility_pills_data:
            return True
        
        item_config = self.items_data.get(item_name)
        if item_config and item_config.get("type") == "丹药":
            return True
        
        return False
    
    def get_all_pill_names(self) -> set:
        """获取所有注册的丹药名称"""
        if self._pill_names_cache is not None:
            return self._pill_names_cache
        
        pill_names = set()
        pill_names.update(self.pills_data.keys())
        pill_names.update(self.exp_pills_data.keys())
        pill_names.update(self.utility_pills_data.keys())
        
        for name, item in self.items_data.items():
            if isinstance(item, dict) and item.get("type") == "丹药":
                pill_names.add(name)
        
        self._pill_names_cache = pill_names
        return pill_names
    
    def invalidate_cache(self):
        """清除缓存，在配置重载时调用"""
        self._pill_names_cache = None
