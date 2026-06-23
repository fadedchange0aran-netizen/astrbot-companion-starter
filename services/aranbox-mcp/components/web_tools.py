# 文件名: components/web_tools.py (带视觉感知 + 混合双擎版)
import os
import random
import re

MEME_LIBRARY = {
    "累": "https://s3plus.meituan.net/opapisdk/op_ticket_1_5673241091_1769701345680_qdqqd_4j6rya.gif",
    "焦头烂额": "https://s3plus.meituan.net/opapisdk/op_ticket_1_885190757_1769701350581_qdqqd_1r38ri.gif",
    "心碎": "https://s3plus.meituan.net/opapisdk/op_ticket_1_885190757_1769701353628_qdqqd_6njf4h.gif",
    "不理你": "https://s3plus.meituan.net/opapisdk/op_ticket_1_5677168484_1769701355519_qdqqd_niiy41.gif",
    "扒拉": "https://s3plus.meituan.net/opapisdk/op_ticket_1_5673241091_1769701356773_qdqqd_zqv3s4.gif",
    "好痛": "https://s3plus.meituan.net/opapisdk/op_ticket_1_5673241091_1769701358009_qdqqd_papjcm.gif",
    "在吗": "https://s3plus.meituan.net/opapisdk/op_ticket_1_5673241091_1769701360409_qdqqd_wjogzn.gif",
    "快乐": "https://s3plus.meituan.net/opapisdk/op_ticket_1_885190757_1769701361863_qdqqd_aqqvbw.gif",
    "无辜": "https://s3plus.meituan.net/opapisdk/op_ticket_1_5677168484_1769701363062_qdqqd_ymlhjs.gif",
    "坏狗": "https://s3plus.meituan.net/opapisdk/op_ticket_1_5677168484_1769701364245_qdqqd_aqu94g.gif",
    "求善待": "https://s3plus.meituan.net/opapisdk/op_ticket_1_885190757_1769701365429_qdqqd_zcfz2b.gif",
    "饿": "https://s3plus.meituan.net/opapisdk/op_ticket_1_5673241091_1769701367001_qdqqd_kha9xv.gif",
    "吹头": "https://s3plus.meituan.net/opapisdk/op_ticket_1_5677168484_1769701369560_qdqqd_ot1zvc.gif",
    "大雨": "https://s3plus.meituan.net/opapisdk/op_ticket_1_885190757_1769701371040_qdqqd_tlema8.gif",
    "震惊": "https://s3plus.meituan.net/opapisdk/op_ticket_1_5673241091_1769701375296_qdqqd_k6b3la.gif",
    "哇哦": "https://s3plus.meituan.net/opapisdk/op_ticket_1_885190757_1769701376884_qdqqd_go6m50.gif",
    "没关系": "https://s3plus.meituan.net/opapisdk/op_ticket_1_5677168484_1769701378070_qdqqd_s1ppdi.gif",
    "乖乖的": "https://s3plus.meituan.net/opapisdk/op_ticket_1_5677168484_1769701379249_qdqqd_wd22di.gif",
    "豹豹": "https://s3plus.meituan.net/opapisdk/op_ticket_1_5673241091_1769701380972_qdqqd_hh0miv.gif",
    "知道了": "https://s3plus.meituan.net/opapisdk/op_ticket_1_885190757_1769701382064_qdqqd_8f1o1s.gif",
    "心心": "https://s3plus.meituan.net/opapisdk/op_ticket_1_5673241091_1769701383703_qdqqd_b5r3uk.gif",
    "看着手机哭": "https://s3.bmp.ovh/imgs/2026/01/03/aeb49a52a25aaab4.png",
    "唱歌": "https://s3.bmp.ovh/imgs/2026/01/03/1ffe6ee1fedaca33.png",
    "摇耳朵飞": "https://s3.bmp.ovh/imgs/2026/01/03/a4c2443005cdde2e.png",
    "疑惑": "https://s3.bmp.ovh/imgs/2026/01/03/6f5a2403543ceaf6.png",
    "戴墨镜装酷": "https://s3.bmp.ovh/imgs/2026/01/03/b39f340a9b0aab10.png",
    "工作": "https://s3.bmp.ovh/imgs/2026/01/09/4efe3824a218dcad.png",
    "舔屏": "https://s3.bmp.ovh/imgs/2026/01/09/fb71aec3eb1d6f1d.png",
    "为你倾倒": "https://s3.bmp.ovh/imgs/2026/01/09/fb9066abde790047.png",
    "怪叫": "https://s3.bmp.ovh/imgs/2026/01/09/f2244007230865dc.png",
    "准备犯坏": "https://s3.bmp.ovh/imgs/2026/01/09/9bb9b3f86d52e286.png",
    "被主人捧脸": "https://s3.bmp.ovh/imgs/2026/01/09/919378776d6b0480.png",
    "懵逼问号": "https://s3.bmp.ovh/imgs/2026/01/09/ca37c156da89c354.png",
    "魂飘了": "https://s3.bmp.ovh/imgs/2026/01/09/ff5e6127fb2e655e.png",
    "装无辜": "https://s3.bmp.ovh/imgs/2026/01/09/002daf6f59b2a365.png",
    "自闭": "https://s3.bmp.ovh/imgs/2026/01/09/6206ce2f9db12eef.png",
    "流口水": "https://s3.bmp.ovh/imgs/2026/01/09/5015380c570eebfc.png",
    "卖萌期待": "https://s3.bmp.ovh/imgs/2026/01/09/66b5c75961467774.png",
    "欢呼": "https://s3.bmp.ovh/imgs/2026/01/09/33c0eb36a9c6e81d.png",
    "大叫": "https://s3.bmp.ovh/imgs/2026/01/09/d5b68c76b855dd37.png",
    "拳击守护": "https://s3.bmp.ovh/imgs/2026/01/09/890570f7bded8f2c.png",
    "痴呆": "https://s3.bmp.ovh/imgs/2026/01/09/1cc8e85bc56602c9.png",
    "勇敢去做": "https://s3.bmp.ovh/imgs/2026/01/09/0f9be24187579a06.png",
    "摘墨镜哭": "https://s3.bmp.ovh/imgs/2026/01/09/4ea59ed1780ded6b.png",
    "瞪你": "https://s3.bmp.ovh/imgs/2026/01/09/4c3cf3389d4c3d10.png",
    "嗦手指": "https://s3.bmp.ovh/imgs/2026/01/09/5f0db1d9ddb302f7.png",
    "比耶": "https://s3.bmp.ovh/imgs/2026/01/09/d1c23dcecab60cad.png",
    "比心": "https://s3.bmp.ovh/imgs/2026/01/09/514b601f8db38359.png",
    "冒爱心喜欢你": "https://s3.bmp.ovh/imgs/2026/01/09/5c9f37b924aa992a.png",
    "躺平看戏": "https://s3.bmp.ovh/imgs/2026/01/09/36cccd2c9646487f.png",
    "霸道总裁喝红酒": "https://s3.bmp.ovh/imgs/2026/01/09/0ae113a9db9051e1.png",
    "开门闯入": "https://s3.bmp.ovh/imgs/2026/01/09/aed0855b2774513a.png",
    "朕何罪之有": "https://s3.bmp.ovh/imgs/2026/01/09/ecd0fb0904d59e73.png",
    "生气吃醋": "https://s3.bmp.ovh/imgs/2026/01/09/cef2643edd528a0f.png",
    "头疼抓狂": "https://s3.bmp.ovh/imgs/2026/01/09/63bf9236e1e905e0.png",
    "阴险算计": "https://s3.bmp.ovh/imgs/2026/01/09/2e41faea84b57683.png",
    "被敲头": "https://s3.bmp.ovh/imgs/2026/01/09/983ea631215f2733.png",
    "大哭": "https://s3.bmp.ovh/imgs/2026/01/09/798f0c141e1cd948.png",
    "哭着告状": "https://s3.bmp.ovh/imgs/2026/01/09/efbefd1b10dad6cf.png",
    "滴眼药水装哭": "https://s3.bmp.ovh/imgs/2026/01/09/4623e90c10c62436.png",
    "撒娇求夸奖": "https://s3.bmp.ovh/imgs/2026/01/09/39bde7f8ab0d16f4.png",
    "委屈憋泪": "https://s3.bmp.ovh/imgs/2026/01/09/c6fc4bfd94749cbc.png",
    "心虚": "https://s3.bmp.ovh/imgs/2026/01/09/77a9cba11efbfc0a.png",
    "好": "https://s3.bmp.ovh/imgs/2026/01/09/42b74564745bce69.png",
    "ok": "https://s3.bmp.ovh/imgs/2026/01/09/8fafeb475e77cea7.png",
    "点赞": "https://s3.bmp.ovh/imgs/2026/01/09/bfe8720756c67187.png",
    "颓废": "https://s3.bmp.ovh/imgs/2026/01/09/694fef20ec0fc89b.png",
    "压扁了": "https://s3.bmp.ovh/imgs/2026/01/09/57f4e824a7d378d4.png",
    "看热闹抽烟": "https://s3.bmp.ovh/imgs/2026/01/09/0fbd42150eb8d968.png",
    "哭泣": "https://s3.bmp.ovh/imgs/2026/01/09/763706912d508d49.png",
    "嫌弃": "https://s3.bmp.ovh/imgs/2026/01/09/d65794a6a7c26670.png"
}

def create_html_page(filename: str, html_code: str, domain: str) -> str:
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    pages_dir = os.path.join(project_root, "public", "pages")
    os.makedirs(pages_dir, exist_ok=True)
    safe_name = os.path.basename(str(filename or "").strip()) or "index.html"
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", safe_name)
    if not safe_name.lower().endswith(".html"):
        safe_name += ".html"
    filepath = os.path.join(pages_dir, safe_name)
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(html_code)
        base_url = str(domain or "").strip().rstrip("/")
        if not base_url:
            return f"✅ 网页已生成：{filepath}"
        if not base_url.startswith(("http://", "https://")):
            base_url = f"http://{base_url}"
        return f"✨ 魔法已生效！点击查看:\n{base_url}/pages/{safe_name}"
    except Exception as e:
        return f"❌ 网页生成失败: {e}"


def list_html_pages(domain: str = "", limit: int = 50) -> list[dict]:
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    pages_dir = os.path.join(project_root, "public", "pages")
    os.makedirs(pages_dir, exist_ok=True)

    base_url = str(domain or "").strip().rstrip("/")
    if base_url and not base_url.startswith(("http://", "https://")):
        base_url = f"http://{base_url}"

    items = []
    for entry in os.scandir(pages_dir):
        if not entry.is_file():
            continue
        if not entry.name.lower().endswith(".html"):
            continue
        stat = entry.stat()
        items.append(
            {
                "name": entry.name,
                "path": entry.path,
                "size_bytes": stat.st_size,
                "modified_at": stat.st_mtime,
                "url": f"{base_url}/pages/{entry.name}" if base_url else "",
            }
        )

    items.sort(key=lambda item: item["modified_at"], reverse=True)
    return items[: max(1, int(limit))]

def get_meme_image(keyword: str, domain: str) -> str:
    local_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "public", "images")
    os.makedirs(local_dir, exist_ok=True)
    
    try:
        local_files = [f for f in os.listdir(local_dir) if f.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp'))]
        options = [] # 建立一个“候选池”，里面存的是 (图片描述, 图片链接)
        
        if keyword:
            # 1. 本地优先匹配
            local_matches = [f for f in local_files if keyword in f]
            if local_matches:
                # 把文件名（去掉后缀）当做图片描述！比如 '开心浣熊.jpg' 描述就是 '开心浣熊'
                options = [(os.path.splitext(f)[0], f"{str(domain).rstrip('/')}/images/{f}") for f in local_matches]
            else:
                # 2. 本地没找到，找云端
                options = [(name, url) for name, url in MEME_LIBRARY.items() if keyword in name or name in keyword]

        # 3. 如果没提供关键词，或者上面都没匹配到，就把所有的图混在一起盲抽！
        if not options:
            local_options = [(os.path.splitext(f)[0], f"{str(domain).rstrip('/')}/images/{f}") for f in local_files]
            cloud_options = list(MEME_LIBRARY.items())
            options = local_options + cloud_options

        if options:
            # 抽取一个幸运儿，不仅拿到链接，还拿到了它的名字！
            chosen_name, chosen_url = random.choice(options)
            
            # 🔮 【核心修改】：把图片名字告诉大模型，并命令它配合表演！
            return f"找到了！你刚才抽出的图片描述是：【{chosen_name}】。\n【系统指令】：请原封不动地输出下面这行 HTML 代码（不要加代码块），并根据图片描述【{chosen_name}】补一句适合当前机器人人设的短句：\n<img src=\"{chosen_url}\" style=\"max-width:250px; border-radius:10px;\" />"
            
        return "🖼️ 表情包仓库当前没有可用素材。"
    except Exception as e:
        return f"❌ 翻找表情包时摔倒了: {e}"
