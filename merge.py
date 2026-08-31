import os
import sys
import gzip
import shutil
import json
import asyncio
import aiohttp
import xml.etree.ElementTree as ET
import io
from collections import defaultdict
from datetime import datetime, timezone, timedelta

# ================= 配置常量 =================
OUTPUT_DIR = 'output'
CONFIG_FILE = 'config.txt'
TZ_UTC_PLUS_8 = timezone(timedelta(hours=8))

# 建议保留的天数：保留今天（当天）及未来 2 天的节目单，过滤掉过期的历史节目
KEEP_DAYS_PAST = 0    # 0 表示不过滤过去的节目，如果设为 0 但结合 KEEP_DAYS_FUTURE 可以大幅缩减体积；建议只抓今天起的节目
KEEP_DAYS_FUTURE = 2  # 保留未来多少天的节目单 (今天 + 未来2天 = 3天)

# ================= 自定义名称注入字典 =================
CUSTOM_NAME_INJECTIONS = {
    # ⚠️ 请把下方的 "8tv_real_id" 替换成你的数据源里 8TV 真正的 id 字符串
    "8tv_real_id": ["八度空间"], 
}

# ================= 辅助函数 =================

def parse_xmltv_date(date_str):
    """解析 XMLTV 格式的时间字符串 (例如 20260901023000 +0800)"""
    if not date_str or len(date_str) < 8:
        return None
    try:
        # 取前 8 位年月日 YYYYMMDD
        clean_date = date_str.split()[0][:8]
        return datetime.strptime(clean_date, "%Y%m%d").date()
    except Exception:
        return None

# ================= 核心处理引擎 =================

async def fetch_epg(url, session):
    """异步下载 EPG 文件，支持 GZIP 实时解压"""
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        async with session.get(url, headers=headers, timeout=30) as response:
            if response.status == 200:
                data = await response.read()
                if url.endswith('.gz') or data.startswith(b'\x1f\x8b'):
                    return url, gzip.decompress(data).decode('utf-8', errors='ignore')
                return url, data.decode('utf-8', errors='ignore')
    except Exception as e:
        print(f"❌ 下载失败 {url}: {e}")
    return url, None

def process_and_merge(results):
    """
    双重遍历解析器 (以 ID 为唯一标识的版本)
    Pass 1: 以 Channel ID 为主键，收集去重所有的名称和图标，并注入自定义名称
    Pass 2: 精准提取与有效 ID 匹配且在有效时间段内的 Programme 节点
    """
    channel_groups = defaultdict(lambda: {
        "display_names": set(),
        "icons": set()
    })
    
    print("\n⚙️ [第一阶段] 正在以固定的 Channel ID 为基准聚合频道数据...")
    
    # --- Pass 1: 建立频道档案 ---
    for url, content in results:
        if not content: continue
        
        content = content.replace(' xmlns="', ' dummy="')
        stream = io.BytesIO(content.encode('utf-8'))
        
        try:
            context = ET.iterparse(stream, events=("end",))
            for event, elem in context:
                if elem.tag == 'channel':
                    channel_id = elem.get('id')
                    if not channel_id:
                        elem.clear()
                        continue
                    
                    for dn in elem.findall('display-name'):
                        if dn.text:
                            channel_groups[channel_id]["display_names"].add(dn.text.strip())
                    
                    icon_node = elem.find('icon')
                    if icon_node is not None and icon_node.get('src'):
                        channel_groups[channel_id]["icons"].add(icon_node.get('src'))
                        
                    elem.clear()
        except ET.ParseError as e:
            print(f"⚠️ XML解析跳过 ({url}): {e}")

    # ================= 自动注入自定义名称 =================
    for cid, custom_names in CUSTOM_NAME_INJECTIONS.items():
        if cid in channel_groups: # 确保网络源里抓到了这个台
            for name in custom_names:
                channel_groups[cid]["display_names"].add(name)
    # ==========================================================

    unified_channels = []
    valid_ids = set(channel_groups.keys())
    
    for cid, data in channel_groups.items():
        c_elem = ET.Element("channel", id=cid)
        
        if not data["display_names"]:
            disp_elem = ET.SubElement(c_elem, "display-name", lang="en")
            disp_elem.text = cid
        else:
            for name in data["display_names"]:
                lang_attr = "zh" if any('\u4e00' <= char <= '\u9fff' for char in name) else "en"
                disp_elem = ET.SubElement(c_elem, "display-name", lang=lang_attr)
                disp_elem.text = name
                
        if data["icons"]:
            ET.SubElement(c_elem, "icon", src=list(data["icons"])[0])
            
        unified_channels.append(c_elem)

    print("\n⚙️ [第二阶段] 正在提取并过滤合法的节目单 (含过期节目清理)...")
    
    # 计算有效节目单的时间区间
    today = datetime.now(TZ_UTC_PLUS_8).date()
    min_date = today - timedelta(days=KEEP_DAYS_PAST)
    max_date = today + timedelta(days=KEEP_DAYS_FUTURE)
    
    # --- Pass 2: 提取 Programme 并过滤时间 ---
    unified_programmes = []
    
    for url, content in results:
        if not content: continue
        content = content.replace(' xmlns="', ' dummy="')
        stream = io.BytesIO(content.encode('utf-8'))
        
        try:
            context = ET.iterparse(stream, events=("end",))
            for event, elem in context:
                if elem.tag == 'programme':
                    prog_channel_id = elem.get('channel')
                    start_str = elem.get('start')
                    
                    # 校验 1: 必须是有效频道 ID
                    if prog_channel_id in valid_ids:
                        # 校验 2: 过滤过期或过远的节目单
                        prog_date = parse_xmltv_date(start_str)
                        if prog_date and (min_date <= prog_date <= max_date):
                            unified_programmes.append(elem)
                        else:
                            elem.clear()
                    else:
                        elem.clear() 
        except ET.ParseError:
            pass

    return channel_groups, unified_channels, unified_programmes

def export_results(channel_groups, channels, programmes):
    """序列化导出引擎"""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    json_file = os.path.join(OUTPUT_DIR, 'unified_channels.json')
    xml_file = os.path.join(OUTPUT_DIR, 'epg.xml')
    gz_file = os.path.join(OUTPUT_DIR, 'epg.xml.gz')

    # 1. 导出 JSON 映射表
    json_export_data = {}
    for cid, data in channel_groups.items():
        json_export_data[cid] = {
            "display_names": list(data["display_names"]),
            "icons": list(data["icons"])
        }
    with open(json_file, 'w', encoding='utf-8') as jf:
        json.dump(json_export_data, jf, indent=4, ensure_ascii=False)
    
    # 2. 导出合并后的 XML（移除 ET.indent 以极大减小 XML 体积）
    current_time = datetime.now(TZ_UTC_PLUS_8).strftime("%Y%m%d%H%M%S %z")
    root = ET.Element('tv', attrib={
        'date': current_time, 
        'generator-info-name': "Python-ID-Driven-EPG-Aggregator"
    })
    
    for c in channels:
        root.append(c)
    for p in programmes:
        root.append(p)
        
    tree = ET.ElementTree(root)
    # 取消 ET.indent(tree, space="\t", level=0)，避免写入数百万个缩进换行符
    tree.write(xml_file, encoding='utf-8', xml_declaration=True)

    # 3. 生成 GZ 压缩包
    with open(xml_file, 'rb') as f_in, gzip.open(gz_file, 'wb') as f_out:
        shutil.copyfileobj(f_in, f_out)

    print(f"\n✅ 处理完成!")
    print(f"📊 唯一频道 ID 数: {len(channels)}")
    print(f"🎬 合规节目单数: {len(programmes)}")
    print(f"💾 XML 文件大小: {os.path.getsize(xml_file)/1024/1024:.2f} MB")
    print(f"📂 输出目录: ./{OUTPUT_DIR}/")

async def main():
    if not os.path.exists(CONFIG_FILE):
        print(f"❌ 找不到配置文件: {CONFIG_FILE}")
        return
        
    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        urls = [line.strip() for line in f if line.strip() and not line.startswith('#')]

    print("📡 正在并发获取 EPG 数据...")
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as session:
        tasks = [fetch_epg(url, session) for url in urls]
        results = await asyncio.gather(*tasks)

    channel_groups, final_channels, final_programmes = process_and_merge(results)
    
    if final_channels:
        print("\n💾 正在输出文件...")
        export_results(channel_groups, final_channels, final_programmes)
    else:
        print("\n⚠️ 没有提取到任何有效频道，中止输出。")

if __name__ == '__main__':
    print("==================================================")
    print("      EPG 聚合器 - 终极版 (固定 ID + 名称注入)      ")
    print("==================================================")
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
