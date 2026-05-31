import xml.etree.ElementTree as ET
import aiohttp
import asyncio
import os
import gzip
import shutil
import sys

# ================= 配置常量 =================
OUTPUT_DIR = 'output'
CONFIG_FILE = 'config.txt'

async def fetch_epg(url, session):
    """异步下载 EPG 文件"""
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        async with session.get(url, headers=headers, timeout=30) as response:
            if response.status == 200:
                data = await response.read()
                # 自动识别并解压 gzip
                if url.endswith('.gz') or data.startswith(b'\x1f\x8b'):
                    return url, gzip.decompress(data).decode('utf-8', errors='ignore')
                return url, data.decode('utf-8', errors='ignore')
    except Exception as e:
        print(f"❌ 下载失败 {url}: {e}")
    return url, None

def merge_epgs(results):
    """最简单的无脑合并逻辑"""
    merged_channels = {}  # 字典在 Python 3.7+ 会自动保持插入顺序
    merged_programmes = []

    for url, content in results:
        if not content: continue
        
        try:
            # 暴力移除 xmlns 命名空间，防止 ElementTree 找不到标签
            content = content.replace(' xmlns="', ' dummy="')
            root = ET.fromstring(content)
        except Exception as e:
            print(f"⚠️ XML解析跳过 ({url}): {e}")
            continue

        # 1. 收集频道 <channel>
        for channel in root.findall('channel'):
            c_id = channel.get('id')
            if not c_id: continue
            
            if c_id not in merged_channels:
                # 第一次遇到的频道，直接存入字典，这就保证了按照 config.txt 的顺序排列
                merged_channels[c_id] = channel
            else:
                # 如果频道已存在，把新出现的 display-name 补充进去
                existing_names = [dn.text for dn in merged_channels[c_id].findall('display-name')]
                for dn in channel.findall('display-name'):
                    if dn.text not in existing_names:
                        merged_channels[c_id].append(dn)

        # 2. 收集节目 <programme>
        for prog in root.findall('programme'):
            c_id = prog.get('channel')
            # 只要节目归属于我们收集到的频道，就原封不动放进来
            if c_id in merged_channels:
                merged_programmes.append(prog)

    return merged_channels, merged_programmes

def save_xml(merged_channels, merged_programmes):
    """保存标准 XMLTV 格式并打包 GZ"""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    xml_file = os.path.join(OUTPUT_DIR, 'epg.xml')
    gz_file = os.path.join(OUTPUT_DIR, 'epg.xml.gz')

    root = ET.Element('tv')
    
    # 先写入所有的 <channel> 节点
    for c_id, channel_node in merged_channels.items():
        root.append(channel_node)
        
    # 再写入所有的 <programme> 节点
    for prog_node in merged_programmes:
        root.append(prog_node)

    # 简单的格式美化缩进
    if hasattr(ET, 'indent'): 
        ET.indent(root, space="\t")
    
    tree = ET.ElementTree(root)
    tree.write(xml_file, encoding='utf-8', xml_declaration=True)

    # 压缩为 gz
    with open(xml_file, 'rb') as f_in, gzip.open(gz_file, 'wb') as f_out:
        shutil.copyfileobj(f_in, f_out)
        
    print(f"✅ 生成完毕! XML大小: {os.path.getsize(xml_file)/1024/1024:.2f}MB")

async def main():
    if not os.path.exists(CONFIG_FILE):
        print(f"❌ 找不到配置文件: {CONFIG_FILE}")
        return
        
    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        urls = [line.strip() for line in f if line.strip() and not line.startswith('#')]

    print("📡 正在并发获取 EPG 数据...")
    # 使用 TCPConnector 避免 ssl 报错
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as session:
        tasks = [fetch_epg(url, session) for url in urls]
        # gather 会确保 results 的顺序与 config.txt 里 urls 的顺序绝对一致
        results = await asyncio.gather(*tasks)

    print("\n⚙️ 正在进行极简合并...")
    merged_channels, merged_programmes = merge_epgs(results)

    print(f"💾 准备写入 {len(merged_channels)} 个频道，{len(merged_programmes)} 条节目...")
    save_xml(merged_channels, merged_programmes)

if __name__ == '__main__':
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
