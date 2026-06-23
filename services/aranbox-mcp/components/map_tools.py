# 文件名: components/map_tools.py
import os
import requests

DEFAULT_CITY = "西安"
DEFAULT_CENTER_CANDIDATES = [
    "西安交通大学雁塔校区财经学院",
    "小寨",
]


def _geocode_address(api_key: str, address: str):
    geo_url = "https://restapi.amap.com/v3/geocode/geo"
    geo_res = requests.get(geo_url, params={"key": api_key, "address": address}, timeout=5).json()
    if geo_res.get("status") == "1" and geo_res.get("geocodes"):
        return geo_res["geocodes"][0]["location"], geo_res["geocodes"][0]
    return None, None


def resolve_search_center(api_key: str, city: str = None, center: str = None):
    explicit_center = (center or "").strip()
    target_city = (city or "").strip() or DEFAULT_CITY

    candidates = []
    if explicit_center:
        candidates.append(explicit_center)
    else:
        env_center = (os.getenv("AMAP_DEFAULT_ADDRESS") or "").strip()
        if env_center:
            candidates.append(env_center)
        if target_city == DEFAULT_CITY:
            candidates.extend(DEFAULT_CENTER_CANDIDATES)
        candidates.append(target_city)

    tried = []
    for candidate in candidates:
        if not candidate or candidate in tried:
            continue
        tried.append(candidate)
        location, geocode = _geocode_address(api_key, candidate)
        if location:
            formatted = geocode.get("formatted_address") or candidate
            return {
                "location": location,
                "resolved_center": formatted,
                "requested_center": candidate,
                "city": target_city,
            }

    return {
        "location": None,
        "resolved_center": None,
        "requested_center": explicit_center or target_city,
        "city": target_city,
    }


def search_nearby_places(keywords: str, city: str = None, center: str = None) -> str:
    """
    【周边搜索】帮你找附近的好吃的好玩的。
    keywords: 想找什么？(比如 '火锅', '商场', '公园', '电影院')
    city: 在哪个城市找？(可选，默认搜 '西安'，如果不填的话)
    center: 从哪个更具体的地点附近搜？(可选，例如 '西安交通大学雁塔校区'、'小寨')
    """
    api_key = os.getenv("AMAP_API_KEY")
    if not api_key: 
        return "❌ 报告：还没有高德地图的钥匙 (AMAP_API_KEY)，没法帮你找路。"

    try:
        center_info = resolve_search_center(api_key, city, center)
        location = center_info["location"]
        if not location:
            return f"❌ 找不到 '{center_info['requested_center']}' 这个地方在哪..."

        search_url = "https://restapi.amap.com/v3/place/around"
        params = {
            "key": api_key,
            "location": location,
            "keywords": keywords,
            "radius": 5000,
            "offset": 5,
            "sortrule": "distance"
        }
        res = requests.get(search_url, params=params, timeout=5).json()

        if res.get("status") == "1" and res.get("pois"):
            resolved_center = center_info["resolved_center"] or center_info["city"]
            ans = (
                f"📍 已按 {resolved_center} 为圆心，在 {center_info['city']} 附近找 '{keywords}'：\n\n"
            )
            for i, p in enumerate(res["pois"], 1):
                ans += f"{i}. 【{p['name']}】\n   距离: {p['distance']}米 | 地址: {p['address']}\n   类型: {p['type']}\n\n"
            return ans
        
        resolved_center = center_info["resolved_center"] or center_info["city"]
        return f"我以 {resolved_center} 为圆心，在附近转了一圈，没找到 '{keywords}' 哎。"
            
    except Exception as e:
        return f"❌ 导航卫星信号中断了: {e}"
