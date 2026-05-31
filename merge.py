import xml.etree.ElementTree as ET
import aiohttp
import asyncio
import os
import gzip
import shutil
import sys
from datetime import datetime, timezone, timedelta

# ================= 配置常量 =================
OUTPUT_DIR = 'output'
CONFIG_FILE = 'config.txt'
TZ_UTC_PLUS_8 = timezone(timedelta(hours=8))

async def fetch_epg(url, session):
    """异步下载 EPG 文件，不做任何多余处理"""
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        async with session.get(url, headers=headers, timeout=30) as response:
            if response.status == 200:
                data = await response.read()
                # 兼容 gzip 压缩的数据源
                if url.endswith('.gz') or data.startswith(b'\x1f\x8b'):
                    return url, gzip.decompress(data).decode('utf-8', errors='ignore')
                return url, data.decode('utf-8', errors='ignore')
    except Exception as e:
        print(f"❌ 下载失败 {url}: {e}")
    return url, None

def merge_xmls_raw(results):
    """
    极简拼接模式：
    不去重，不合并，不处理名字，纯粹的节点拼装。
    """
    all_channels = []
    all_programmes = []

    # 按照 gather 收集回来的顺序依次处理（与 config 顺序一致）
    for url, content in results:
        if not content: continue
        
        try:
            # 暴力移除 xmlns 避免解析报错
            content = content.replace(' xmlns="', ' dummy="')
            root = ET.fromstring(content)
        except ET.ParseError as e:
            print(f"⚠️ XML解析跳过 ({url}): {e}")
            continue

        # 简单地把每个源的节点搜集起来
        for channel in root.findall('channel'):
            all_channels.append(channel)
            
        for prog in root.findall('programme'):
            all_programmes.append(prog)

    return all_channels, all_programmes

def save_xml_raw(channels, programmes):
    """最基础的写入模式"""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    xml_file = os.path.join(OUTPUT_DIR, 'epg.xml')
    gz_file = os.path.join(OUTPUT_DIR, 'epg.xml.gz')

    # 生成根节点
    current_time = datetime.now(TZ_UTC_PLUS_8).strftime("%Y%m%d%H%M%S %z")
    root = ET.Element('tv', attrib={'date': current_time})
    
    # 按照先 channel，后 programme 的顺序插入，符合播放器标准
    for c_node in channels:
        root.append(c_node)
        
    for p_node in programmes:
        root.append(p_node)

    # 如果 Python 版本支持，进行一下基本缩进
    if hasattr(ET, 'indent'): 
        ET.indent(root, space="\t")
    
    tree = ET.ElementTree(root)
    tree.write(xml_file, encoding='utf-8', xml_declaration=True)

    # 打包 gz
    with open(xml_file, 'rb') as f_in, gzip.open(gz_file, 'wb') as f_out:
        shutil.copyfileobj(f_in, f_out)
        
    print(f"✅ 物理拼接完成! 频道数: {len(channels)}, 节目数: {len(programmes)}, XML大小: {os.path.getsize(xml_file)/1024/1024:.2f}MB")

async def main():
    if not os.path.exists(CONFIG_FILE):
        print(f"❌ 找不到配置文件: {CONFIG_FILE}")
        return
        
    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        # 读取非空且非注释的链接
        urls = [line.strip() for line in f if line.strip() and not line.startswith('#')]

    print("📡 正在并发获取 EPG 数据...")
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as session:
        tasks = [fetch_epg(url, session) for url in urls]
        # gather 返回的列表顺序与 urls 严格一致
        results = await asyncio.gather(*tasks)

    print("\n⚙️ 正在进行极简物理拼接...")
    channels, programmes = merge_xmls_raw(results)

    print("💾 正在输出文件...")
    save_xml_raw(channels, programmes)

if __name__ == '__main__':
    # 修复 Windows 下的 aiohttp 报错
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
