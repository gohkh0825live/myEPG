import xml.etree.ElementTree as ET
from collections import defaultdict
import aiohttp
import asyncio
from tqdm.asyncio import tqdm_asyncio
from datetime import datetime
import gzip
import shutil
from xml.dom import minidom
import re
from opencc import OpenCC
import os
from tqdm import tqdm

# 全局 OpenCC 实例复用
cc = OpenCC("t2s")

def transform2_zh_hans(string):
    return cc.convert(string or "")

async def fetch_epg(url):
    connector = aiohttp.TCPConnector(limit=16, ssl=False)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
    }
    try:
        async with aiohttp.ClientSession(connector=connector, trust_env=True, headers=headers) as session:
            async with session.get(url, timeout=30) as response:
                response.raise_for_status()
                return await response.text(encoding='utf-8')
    except Exception as e:
        print(f"[ERROR] 获取失败 {url} -> {e}")
        return None

def parse_epg(epg_content):
    try:
        parser = ET.XMLParser(encoding='UTF-8')
        root = ET.fromstring(epg_content, parser=parser)
    except Exception as e:
        print(f"[ERROR] 解析失败: {e}")
        return {}, defaultdict(list)

    channels = {}
    programmes = defaultdict(list)

    for channel in root.findall('channel'):
        channel_id = transform2_zh_hans(channel.get('id'))
        name_node = channel.find('display-name')
        display_name = transform2_zh_hans(name_node.text if name_node is not None else channel_id)
        channels[channel_id] = display_name

    for programme in root.findall('programme'):
        try:
            channel_id = transform2_zh_hans(programme.get('channel'))
            start = datetime.strptime(re.sub(r'\s+', '', programme.get('start')), "%Y%m%d%H%M%S%z")
            stop = datetime.strptime(re.sub(r'\s+', '', programme.get('stop')), "%Y%m%d%H%M%S%z")
            title = transform2_zh_hans(programme.findtext('title'))

            new_prog = ET.Element('programme', {
                "channel": channel_id,
                "start": start.strftime("%Y%m%d%H%M%S +0800"),
                "stop": stop.strftime("%Y%m%d%H%M%S +0800")
            })
            ET.SubElement(new_prog, 'title').text = title

            desc = programme.findtext('desc')
            if desc:
                ET.SubElement(new_prog, 'desc').text = transform2_zh_hans(desc)

            programmes[channel_id].append(new_prog)
        except Exception as e:
            print(f"[WARN] 节目解析异常: {e}")
            continue

    return channels, programmes

def write_to_xml(channels, programmes, filename):
    os.makedirs(os.path.dirname(filename), exist_ok=True)
    root = ET.Element('tv', attrib={'date': datetime.now().strftime("%Y%m%d%H%M%S +0800")})

    for channel_id, display_name in channels.items():
        ch_elem = ET.SubElement(root, 'channel', {"id": channel_id})
        ET.SubElement(ch_elem, 'display-name', {"lang": "zh"}).text = display_name
        for prog in programmes.get(display_name, []):
            root.append(prog)

    rough_string = ET.tostring(root, 'utf-8')
    pretty_xml = minidom.parseString(rough_string).toprettyxml(indent='\t', newl='\n')
    with open(filename, 'w', encoding='utf-8') as f:
        f.write(pretty_xml)

def compress_to_gz(input_file, output_file):
    with open(input_file, 'rb') as f_in, gzip.open(output_file, 'wb') as f_out:
        shutil.copyfileobj(f_in, f_out)

def get_urls(config_path='config.txt'):
    try:
        with open(config_path, 'r', encoding='utf-8') as file:
            return [line.strip() for line in file if line.strip() and not line.startswith('#')]
    except FileNotFoundError:
        print(f"[ERROR] 配置文件不存在: {config_path}")
        return []

async def main():
    urls = get_urls()
    if not urls:
        print("[ERROR] 没有任何有效的 EPG URL")
        return

    print("📡 开始获取 EPG 数据...")
    epg_contents = await tqdm_asyncio.gather(*[fetch_epg(url) for url in urls], desc="Fetching")

    all_channels = set()
    channel_name_map = {}
    all_programmes = defaultdict(list)

    print("🧠 开始解析 EPG 数据...")
    with tqdm(total=len(epg_contents), desc="Parsing", unit="EPG") as pbar:
        for content in epg_contents:
            if not content:
                pbar.update(1)
                continue
            channels, programmes = parse_epg(content)
            for ch_id, name in channels.items():
                clean_name = name.replace(' ', '')
                if ch_id not in channel_name_map and clean_name not in channel_name_map:
                    channel_name_map[ch_id] = clean_name
                    channel_name_map[clean_name] = clean_name
                    all_channels.add(clean_name)
                    all_programmes[clean_name] = programmes[ch_id]
            pbar.update(1)

    print("📦 写入 XML 文件...")
    write_to_xml(channel_name_map, all_programmes, 'output/epg.xml')

    print("🗜️ 生成 GZ 压缩...")
    compress_to_gz('output/epg.xml', 'output/epg.gz')
    print("✅ 完成")

if __name__ == '__main__':
    asyncio.run(main())
