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

# ================= 自定义名称注入字典 =================
# 键(Key)必须是你在 XML 源里抓取到的对应频道的真实 tvg-id (channel id)
CUSTOM_NAME_INJECTIONS = {
    # ⚠️ 请把下方的 "8tv_real_id" 替换成你的数据源里 8TV 真正的 id 字符串
    "8tv_real_id": ["八度空间"], 
    
    # 示例：你可以继续添加其他需要补充中文名的频道
    # "astro_aec_id": ["Astro AEC", "AEC 频道"],
}

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
    Pass 2: 精准提取与有效 ID 匹配的 Programme 节点
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
                # set 会自动处理去重，完美追加中文名
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
                # 为了简化，这里统一种为 lang="en" 和 lang="zh" 也可以，
                # 但直接写入多个 display-name 播放器都能识别。
                lang_attr = "zh" if any('\u4e00' <= char <= '\u9fff' for char in name) else "en"
                disp_elem = ET.SubElement(c_elem, "display-name", lang=lang_attr)
                disp_elem.text = name
                
        if data["icons"]:
            ET.SubElement(c_elem, "icon", src=list(data["icons"])[0])
            
        unified_channels.append(c_elem)

    print("\n⚙️ [第二阶段] 正在提取并过滤合法的节目单...")
    
    # --- Pass 2: 提取 Programme ---
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
                    if prog_channel_id in valid_ids:
                        unified_programmes.append(elem)
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
    
    # 2. 导出合并后的 XML
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
    if hasattr(ET, 'indent'):
        ET.indent(tree, space="\t", level=0)
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
