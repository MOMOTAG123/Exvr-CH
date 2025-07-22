import json

def load_json(file_path):
    """加载JSON文件（自动适配UTF-8和GBK编码）"""
    encodings = ['utf-8', 'gbk']  # 优先尝试的编码列表
    
    for encoding in encodings:
        try:
            with open(file_path, 'r', encoding=encoding) as file:
                return json.load(file)
        except UnicodeDecodeError:
            # 当前编码解码失败，尝试下一个
            continue
        except FileNotFoundError:
            print(f"Error: File '{file_path}' not found.")
            return {}
        except json.JSONDecodeError as e:
            print(f"JSON解析错误: {e}")
            return {}
        except Exception as e:
            print(f"未知错误: {e}")
            return {}
    
    # 所有编码尝试均失败
    print(f"Error: 文件 '{file_path}' 无法被UTF-8或GBK解码")
    return {}

def save_json(data, file_path):
    """保存JSON文件（强制使用UTF-8编码）"""
    try:
        with open(file_path, 'w', encoding='utf-8') as file:  # 明确指定UTF-8编码
            json.dump(data, file, indent=4, ensure_ascii=False)  # 确保非ASCII字符正常保存
    except Exception as e:
        print(f"保存失败: {e}")

# 以下函数保持原有逻辑不变
def get_data(data, key):
    return data.get(key, None)

def update_data(data, key, value):
    data[key] = value
