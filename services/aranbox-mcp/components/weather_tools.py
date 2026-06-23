from components.weather_service import get_weather


async def get_weather_function(city: str = None, date: str = None) -> str:
    """
    获取指定城市的实时天气预报。
    这是一个供主服务调用的普通函数，不会自动注册为 MCP 工具。
    """
    if not city:
        city = "西安"
    return get_weather(city, date, persona="Companion")
