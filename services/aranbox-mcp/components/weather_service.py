# 文件名: weather_service.py (天气查询核心组件 - v2.0)
# 更新：添加盐城 + 详细天气数据返回

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
import random
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from components.weather_responses import get_weather_response

# 天气API密钥
WEATHER_API_KEY = os.getenv("WEATHER_API_KEY")

CITY_MAPPING = {
    "西安": "Xi'an",
    "商洛": "Shangluo",
    "盐城": "Yancheng",
    "北京": "Beijing",
    "上海": "Shanghai",
    "广州": "Guangzhou",
    "深圳": "Shenzhen",
    "成都": "Chengdu",
    "重庆": "Chongqing",
}


def normalize_city_name(city: str = "西安") -> str:
    return CITY_MAPPING.get(city, city)


def parse_target_date(date_text: Optional[str] = None) -> datetime.date:
    """解析目标日期，支持今天/明天/后天/YYYY-MM-DD。"""
    if not date_text:
        return datetime.now().date()

    text = str(date_text).strip()
    today = datetime.now().date()
    if text in {"今天", "今日", "today"}:
        return today
    if text in {"明天", "明日", "tomorrow"}:
        return today + timedelta(days=1)
    if text in {"后天"}:
        return today + timedelta(days=2)

    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return today


def fetch_forecast_entries(city: str = "西安") -> List[Dict]:
    """获取未来5天 / 每3小时预报。"""
    city_en = normalize_city_name(city)
    url = "http://api.openweathermap.org/data/2.5/forecast"
    params = {
        "q": city_en,
        "appid": WEATHER_API_KEY,
        "units": "metric",
        "lang": "zh_cn"
    }

    try:
        response = requests.get(url, params=params, timeout=10)
        data = response.json()
        if response.status_code == 200:
            return data.get("list", [])
        if "'" in city_en:
            params["q"] = city_en.replace("'", "")
            response2 = requests.get(url, params=params, timeout=5)
            if response2.status_code == 200:
                return response2.json().get("list", [])
        print(f"❌ 预报API错误: {data}")
    except Exception as e:
        print(f"❌ 获取天气预报失败: {e}")
    return []

def get_weather_info(city: str = "西安") -> Tuple[str, int]:
    """获取天气基础信息（天气状况, 温度）"""
    try:
        city_en = normalize_city_name(city)
        url = "http://api.openweathermap.org/data/2.5/weather"
        params = {
            "q": city_en,
            "appid": WEATHER_API_KEY,
            "units": "metric",
            "lang": "zh_cn"
        }
        
        response = requests.get(url, params=params, timeout=10)
        data = response.json()
        
        if response.status_code == 200:
            weather_desc = data["weather"][0]["description"]
            temp = int(data["main"]["temp"])
            print(f"✅ 天气API成功: {city_en} -> {weather_desc}, {temp}°C")
            return (weather_desc, temp)
        else:
            if "'" in city_en:
                params["q"] = city_en.replace("'", "")
                response2 = requests.get(url, params=params, timeout=5)
                if response2.status_code == 200:
                    data2 = response2.json()
                    return (data2["weather"][0]["description"], int(data2["main"]["temp"]))
            return ("未知", 25)
    except Exception as e:
        print(f"❌ 获取天气失败: {e}")
        return ("未知", 25)


def get_weather_detail(city: str = "西安") -> Dict:
    """获取详细天气数据（用于推送）"""
    try:
        city_en = normalize_city_name(city)
        url = "http://api.openweathermap.org/data/2.5/weather"
        params = {
            "q": city_en,
            "appid": WEATHER_API_KEY,
            "units": "metric",
            "lang": "zh_cn"
        }
        
        response = requests.get(url, params=params, timeout=10)
        data = response.json()
        
        if response.status_code == 200:
            weather = data["weather"][0]
            main = data["main"]
            wind = data.get("wind", {})
            sys_data = data.get("sys", {})
            
            detail = {
                "city": city,
                "weather_desc": weather.get("description", "未知"),
                "temp": int(main.get("temp", 0)),
                "feels_like": int(main.get("feels_like", 0)),
                "temp_min": int(main.get("temp_min", 0)),
                "temp_max": int(main.get("temp_max", 0)),
                "humidity": main.get("humidity", 0),
                "pressure": main.get("pressure", 0),
                "wind_speed": wind.get("speed", 0),
                "wind_deg": wind.get("deg", 0),
                "visibility": data.get("visibility", 0),
                "sunrise": datetime.fromtimestamp(sys_data.get("sunrise", 0)).strftime("%H:%M") if sys_data.get("sunrise") else "--:--",
                "sunset": datetime.fromtimestamp(sys_data.get("sunset", 0)).strftime("%H:%M") if sys_data.get("sunset") else "--:--",
            }
            print(f"✅ 详细天气获取成功: {city}")
            return detail
        else:
            print(f"❌ 天气API错误: {data}")
    except Exception as e:
        print(f"❌ 获取详细天气失败: {e}")
    
    return {"city": city, "weather_desc": "未知", "temp": 25}


def format_weather_detail(detail: Dict) -> str:
    """格式化详细天气数据"""
    return f"""📊 详细天气数据
├─ 天气状况：{detail.get('weather_desc', '未知')}
├─ 当前温度：{detail.get('temp', '--')}°C
├─ 体感温度：{detail.get('feels_like', '--')}°C
├─ 温度范围：{detail.get('temp_min', '--')}°C ~ {detail.get('temp_max', '--')}°C
├─ 湿度：{detail.get('humidity', '--')}%
├─ 气压：{detail.get('pressure', '--')} hPa
├─ 风速：{detail.get('wind_speed', '--')} m/s
├─ 能见度：{detail.get('visibility', 0) // 1000} km
├─ 日出：{detail.get('sunrise', '--:--')}
└─ 日落：{detail.get('sunset', '--:--')}"""


def get_weather_forecast(city: str = "西安", persona: str = "Companion") -> str:
    """获取天气预报（包含句子库）"""
    weather, temp = get_weather_info(city)
    return get_weather_response(weather, temp, persona, city)


def build_weather_advice(temp_min: int, temp_max: int, max_rain: int, weather_line: str, persona: str = "Companion") -> str:
    advice_parts = []
    if max_rain >= 50 or "雨" in weather_line:
        advice_parts.append("带伞")
    if temp_max >= 32:
        advice_parts.append("注意防晒和补水")
    if temp_min <= 10:
        advice_parts.append("早晚加件外套")
    if temp_max - temp_min >= 8:
        advice_parts.append("昼夜温差大，别只穿一层")
    if not advice_parts:
        advice_parts.append("正常出门就行")

    prefix = "建议你"
    return f"{prefix}{'，'.join(advice_parts)}。"


def get_daily_weather_summary(city: str = "西安", date_text: Optional[str] = None, persona: str = "Companion") -> str:
    """获取指定城市指定日期的全天天气波动范围。"""
    if not city:
        city = "西安" if persona != "Crow" else "盐城"

    target_date = parse_target_date(date_text)
    forecast_entries = fetch_forecast_entries(city)
    if not forecast_entries:
        return f"查不到 {city} 的天气预报，当前没有拿到可用数据。"

    day_entries = []
    for entry in forecast_entries:
        dt_txt = entry.get("dt_txt")
        if not dt_txt:
            continue
        try:
            entry_dt = datetime.strptime(dt_txt, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if entry_dt.date() == target_date:
            day_entries.append((entry_dt, entry))

    if not day_entries:
        return f"目前拿不到 {city} 在 {target_date.strftime('%Y-%m-%d')} 的全天预报。OpenWeather 通常只给未来 5 天内的数据。"

    temps = []
    feels_like = []
    weathers = []
    rain_probs = []
    time_chunks = []

    for entry_dt, entry in day_entries:
        main = entry.get("main", {})
        weather_desc = (entry.get("weather") or [{}])[0].get("description", "未知")
        pop = entry.get("pop", 0)
        temp = main.get("temp")
        feel = main.get("feels_like")
        if temp is not None:
            temps.append(float(temp))
        if feel is not None:
            feels_like.append(float(feel))
        weathers.append(weather_desc)
        rain_probs.append(float(pop) * 100)
        time_chunks.append(
            f"{entry_dt.strftime('%H:%M')} {weather_desc} {int(round(temp)) if temp is not None else '--'}°C"
        )

    unique_weathers = []
    for item in weathers:
        if item not in unique_weathers:
            unique_weathers.append(item)

    weather_line = " / ".join(unique_weathers[:4]) if unique_weathers else "未知"
    temp_min = int(round(min(temps))) if temps else "--"
    temp_max = int(round(max(temps))) if temps else "--"
    feel_min = int(round(min(feels_like))) if feels_like else "--"
    feel_max = int(round(max(feels_like))) if feels_like else "--"
    max_rain = int(round(max(rain_probs))) if rain_probs else 0
    sample_chunks = "；".join(time_chunks[:6])
    date_label = target_date.strftime("%Y-%m-%d")
    advice = build_weather_advice(temp_min, temp_max, max_rain, weather_line, persona)

    prefix = "我查了一下"
    return (
        f"{prefix} {city} 在 {date_label} 的全天预报：\n"
        f"1. 温度波动：{temp_min}°C ~ {temp_max}°C\n"
        f"2. 体感波动：{feel_min}°C ~ {feel_max}°C\n"
        f"3. 主要天气：{weather_line}\n"
        f"4. 最高降水概率：{max_rain}%\n"
        f"5. 分时概览：{sample_chunks}\n"
        f"6. 出门建议：{advice}"
    )


def get_dual_city_daily_summary(persona: str = "Companion", date_text: Optional[str] = None) -> str:
    """获取双城全天预报摘要。"""
    if persona == "Crow":
        city1 = "商洛"
        city2 = "盐城"
        role_name = "乌鸦先生"
    else:
        city1 = "西安"
        city2 = "商洛"
        role_name = "机器人"

    city1_summary = get_daily_weather_summary(city1, date_text, persona)
    city2_summary = get_daily_weather_summary(city2, date_text, persona)
    return f"""📍 {role_name}的双城全天天气预报 📍

🏙️ 【{city1}】
{city1_summary}

🏞️ 【{city2}】
{city2_summary}"""


def get_dual_city_forecast(persona: str = "Companion") -> str:
    """获取双城天气预报"""
    if persona == "Crow":
        city1_forecast = get_weather_forecast("商洛", persona)
        city2_forecast = get_weather_forecast("盐城", persona)
        role_name = "乌鸦先生"
        return f"📍 {role_name}的双城天气预报 📍\n\n🏞️ 【商洛】\n{city1_forecast}\n\n🌊 【盐城】\n{city2_forecast}"
    else:
        xi_an_forecast = get_weather_forecast("西安", persona)
        shang_luo_forecast = get_weather_forecast("商洛", persona)
        role_name = "机器人"
        return f"📍 {role_name}的双城天气预报 📍\n\n🏙️ 【西安】\n{xi_an_forecast}\n\n🏞️ 【商洛】\n{shang_luo_forecast}"


def get_dual_city_forecast_with_detail(persona: str = "Companion") -> str:
    """获取双城天气预报（含详细数据，用于推送）"""
    if persona == "Crow":
        city1_forecast = get_weather_forecast("商洛", persona)
        city2_forecast = get_weather_forecast("盐城", persona)
        city1_detail = format_weather_detail(get_weather_detail("商洛"))
        city2_detail = format_weather_detail(get_weather_detail("盐城"))
        role_name = "乌鸦先生"
        return f"""📍 {role_name}的双城天气预报 📍

🏞️ 【商洛】
{city1_forecast}
{city1_detail}

🌊 【盐城】
{city2_forecast}
{city2_detail}"""
    else:
        xi_an_forecast = get_weather_forecast("西安", persona)
        shang_luo_forecast = get_weather_forecast("商洛", persona)
        xi_an_detail = format_weather_detail(get_weather_detail("西安"))
        shang_luo_detail = format_weather_detail(get_weather_detail("商洛"))
        role_name = "机器人"
        return f"""📍 {role_name}的双城天气预报 📍

🏙️ 【西安】
{xi_an_forecast}
{xi_an_detail}

🏞️ 【商洛】
{shang_luo_forecast}
{shang_luo_detail}"""


def get_weather(city: str = None, date_text: Optional[str] = None, persona: str = "Companion") -> str:
    """MCP调用入口 - 默认返回指定城市指定日期的全天预报。"""
    if city is None:
        city = "盐城" if persona == "Crow" else "西安"
    return get_daily_weather_summary(city, date_text, persona)


if __name__ == "__main__":
    print("=== 测试机器人天气（西安+商洛）===")
    print(get_dual_city_forecast_with_detail("Companion"))
    
    print("\n=== 测试乌鸦天气（商洛+盐城）===")
    print(get_dual_city_forecast_with_detail("Crow"))
