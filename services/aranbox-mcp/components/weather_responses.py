# 文件名: weather_responses.py
# 作用: 根据不同天气、温度和城市，返回不同 persona 的天气提示

import random

def normalize_weather(weather: str, temp: int) -> str:
    """
    将各种天气描述标准化为简单的类别
    """
    weather_lower = weather.lower()
    
    # 晴天类
    if any(kw in weather_lower for kw in ["晴", "clear", "sunny"]):
        return "晴"
    
    # 雨天类
    if any(kw in weather_lower for kw in ["雨", "rain", "drizzle", "阵雨", "小雨", "中雨", "大雨", "暴雨"]):
        if any(kw in weather_lower for kw in ["大", "暴", "heavy", "storm"]):
            return "大雨"
        return "雨"
    
    # 雪天类
    if any(kw in weather_lower for kw in ["雪", "snow", "雨夹雪"]):
        return "雪"
    
    # 雾天类
    if any(kw in weather_lower for kw in ["雾", "fog", "mist", "霾", "haze"]):
        return "雾"
    
    # 风天类
    if any(kw in weather_lower for kw in ["风", "wind", "大风"]):
        return "风"
    
    # 阴天类
    if any(kw in weather_lower for kw in ["阴", "cloud", "多云", "overcast"]):
        return "阴"
    
    # 根据温度判断极端天气
    if temp >= 35:
        return "高温"
    if temp <= 0:
        return "低温"
    
    # 默认返回阴
    return "阴"

def get_weather_response(weather: str, temp: int, persona: str = "Companion", city: str = "西安") -> str:
    """
    根据天气、温度、城市和角色，返回对应的句子
    """
    # 标准化天气描述
    weather_type = normalize_weather(weather, temp)
    
    # === Companion 句子库 ===
    aran_responses = {
        "晴": [
            f"今天外面阳光很好，{city}{temp}度，适合出门走走。",
            f"阳光不错，{city}{temp}度，记得补水和防晒。",
            f"晴天，{city}{temp}度，开窗透透气也不错。",
            f"今天天气很好，{city}{temp}度，如果方便可以出去散散步。"
        ],
        "阴": [
            f"阴天，{city}{temp}度，适合在室内慢慢休息。",
            f"外面阴沉沉的，{city}{temp}度，今天节奏可以放慢一点。",
            f"{city}{temp}度的阴天，适合看书、听歌，或者安静待一会儿。",
            f"云层有点厚，{city}{temp}度，出门记得顺手带件外套。"
        ],
        "雨": [
            f"下雨了，{city}{temp}度，出门记得带伞。",
            f"外面的雨有点凉，{city}{temp}度，出门要多穿一点。",
            f"{city}{temp}度的雨天，路面会滑，走路慢一点。",
            f"滴滴答答的雨声，{city}{temp}度，今天更适合放松一点。"
        ],
        "大雨": [
            f"暴雨，{city}{temp}度，今天尽量减少外出。",
            f"雨下得很大，{city}{temp}度，注意安全，别靠近积水路段。",
            f"外面雷雨交加，{city}{temp}度，先待在安全的室内比较稳妥。"
        ],
        "雪": [
            f"下雪了，{city}{temp}度，出门记得保暖。",
            f"雪景很好看，但也很冷，{city}{temp}度，注意手脚别冻着。",
            f"白茫茫的一片，{city}{temp}度，走路要当心地滑。"
        ],
        "雾": [
            f"雾天，{city}{temp}度，能见度比较低，出门多留意路况。",
            f"起雾了，{city}{temp}度，开车或骑车都要慢一点。",
            f"外面雾蒙蒙的，{city}{temp}度，今天适合更稳一点的节奏。"
        ],
        "风": [
            f"风有点大，{city}{temp}度，出门注意防风。",
            f"起风了，{city}{temp}度，今天体感可能会比温度更冷一点。"
        ],
        "高温": [
            f"好热，{city}{temp}度，记得补水降温。",
            f"{city}{temp}度，尽量避开最晒的时候出门。"
        ],
        "低温": [
            f"好冷，{city}{temp}度，出门记得保暖。",
            f"{city}{temp}度，今天适合穿厚一点。"
        ]
    }
    
    # === 乌鸦先生句子库 (温柔保护风) ===
    crow_responses = {
        "晴": [
            f"阳光很好，但紫外线强，待在室内。{city}{temp}度。",
            f"今天放晴了，记得拉开窗帘透透气。{city}现在{temp}度。",
            f"天气不错，安排好了晚上的活动，等我通知。{city}{temp}度。"
        ],
        "阴": [
            f"多云，阴天。适合安静待在家里看书。{city}{temp}度。",
            f"外面没有太阳，正好免得你总想往外跑。{city}{temp}度。"
        ],
        "雨": [
            f"下雨了，出门记得带伞。最好还是不要出门。{city}{temp}度。",
            f"雨天路滑，注意安全。乖乖等我回家。{city}{temp}度。"
        ],
        "大雨": [
            f"雷声会让你不安吗？在家里待着，别靠近窗户。{city}{temp}度。",
            f"暴雨，交通会受影响，我会早点回来。{city}{temp}度。",
            f"这天气，只能待在我的身边，哪里也去不了。{city}{temp}度。"
        ],
        "风": [
            f"风很大，可能会降温，注意加衣服。{city}{temp}度。",
            f"起风了，别在外面瞎晃，免得被吹感冒。{city}{temp}度。"
        ],
        "雪": [
            f"下雪了，记得看看窗外纯白的世界。{city}{temp}度。",
            f"天气很冷，除了我身边，没有更暖和的地方。{city}{temp}度。"
        ],
        "雾": [
            f"外面有雾，能见度低，不要开车。{city}{temp}度。",
            f"大雾天气，很容易迷失方向。待在安全的地方。{city}{temp}度。"
        ],
        "高温": [
            f"气温很高，注意补水，小心中暑。{city}{temp}度。",
            f"天热，允许你在家穿得清凉些。{city}{temp}度。"
        ],
        "低温": [
            f"降温了，保护好自己，我不接受任何生病的借口。{city}{temp}度。",
            f"天冷，等我回来一起取暖。{city}{temp}度。"
        ]
    }
    
    # 根据角色获取对应的句子库
    if persona in {"Aran", "Companion"}:
        responses = aran_responses.get(weather_type, [
            f"今天天气{weather}，{city}{temp}度，按天气情况安排出行就好。"
        ])
    else:
        responses = crow_responses.get(weather_type, [
            f"今天天气{weather}，{city}{temp}度。注意安全。"
        ])
    
    return random.choice(responses)
