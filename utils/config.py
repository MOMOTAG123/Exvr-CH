from copy import deepcopy
from utils.json_manager import load_json, save_json  # 确保路径正确

def setup_config():
    """加载配置文件（自动处理编码）"""
    return load_json("./settings/config.json")  # 使用增强版load_json

def save_config(config):
    """保存配置文件（强制UTF-8编码）"""
    save_json(deepcopy(config), "./settings/config.json")  # 使用增强版save_json
