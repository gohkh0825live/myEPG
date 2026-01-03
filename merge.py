import xml.etree.ElementTree as ET
from collections import defaultdict
import aiohttp
import asyncio
from tqdm.asyncio import tqdm_asyncio
from datetime import datetime, timezone, timedelta
import gzip
import shutil
import re
from opencc import OpenCC
import os
from tqdm import tqdm
import logging
from typing import Dict, List, Tuple, Optional
import hashlib
import sys

# ================= 强制北京时间配置 =================
BEIJING_TZ = timezone(timedelta(hours=8))

def beijing_now():
    """获取当前的北京时间"""
    return datetime.now(BEIJING_TZ)

# 强制日志使用北京时间
class BeijingFormatter(logging.Formatter):
    def converter(self, timestamp):
        dt = datetime.fromtimestamp(timestamp, tz=BEIJING_TZ)
        return dt.timetuple()
    
    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, tz=BEIJING_TZ)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.strftime('%Y-%m-%d %H:%M:%S,%f')[:-3]

# 配置日志
handler = logging.StreamHandler(sys.stdout)
formatter = BeijingFormatter('%(asctime)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)

logger = logging.getLogger(__name__)
logger.addHandler(handler)
logger.setLevel(logging.INFO)
logger.propagate = False # 防止重复打印

# 全局实例
cc = OpenCC("t2s")

def transform2_zh_hans(string: str) -> str:
    return cc.convert(string) if string else ""

def normalize_channel_id(channel_id: str) -> str:
    channel_id = re.sub(r'[^\w\u4e00-\u9fff\-]', '', channel_id)
    return channel_id.strip()

def normalize_datetime(dt_str: str) -> datetime:
    """解析任意时区时间并统一转换为北京时间"""
    dt_str = re.sub(r'\s+', '', dt_str)
    
    # 提取前14位数字
    clean_ts = dt_str[:14]
    dt = datetime.strptime(clean_ts, "%Y%m%d%H%M%S")
    
    # 判断原始时区
    if 'Z' in dt_str.upper():
        dt = dt.replace(tzinfo=timezone.utc)
    elif '+08' in dt_str:
        dt = dt.replace(tzinfo=BEIJING_TZ)
    else:
        # 默认假设为北京时间，如果不是，请根据源调整
        dt = dt.replace(tzinfo=BEIJING_TZ)
    
    # 统一转换为北京时间
    return dt.astimezone(BEIJING_TZ)

async def fetch_epg(url: str, session: aiohttp.ClientSession) -> Optional[str]:
    try:
        async with session.get(url, timeout=60) as response:
            if response.status == 200:
                return await response.text(encoding='utf-8', errors='ignore')
    except Exception:
        pass
    return None

def parse_epg(epg_content: str) -> Tuple[Dict[str, str], Dict[str, List[ET.Element]]]:
    channels = {}
    programmes = defaultdict(list)
    if not epg_content: return channels, programmes
    
    try:
        root = ET.fromstring(epg_content)
    except Exception:
        return channels, programmes
    
    # 解析频道
    for channel in root.findall('channel'):
        c_id = channel.get('id', '')
        if not c_id: continue
        norm_id = normalize_channel_id(transform2_zh_hans(c_id))
        name_node = channel.find('display-name')
        display_name = transform2_zh_hans(name_node.text) if name_node is not None else norm_id
        channels[norm_id] = display_name
    
    # 解析节目
    now = beijing_now()
    for prog in root.findall('programme'):
        c_id = prog.get('channel', '')
        if not c_id: continue
        norm_id = normalize_channel_id(transform2_zh_hans(c_id))
        
        try:
            start_dt = normalize_datetime(prog.get('start', ''))
            stop_dt = normalize_datetime(prog.get('stop', ''))
            if stop_dt < now: continue # 过滤过期
            
            new_p = ET.Element('programme', {
                "channel": norm_id,
                "start": start_dt.strftime("%Y%m%d%H%M%S +0800"),
                "stop": stop_dt.strftime("%Y%m%d%H%M%S +0800")
            })
            for tag in ['title', 'desc', 'sub-title', 'category', 'episode-num']:
                node = prog.find(tag)
                if node is not None and node.text:
                    ET.SubElement(new_p, tag).text = transform2_zh_hans(node.text)
            programmes[norm_id].append(new_p)
        except: continue
            
    return channels, programmes

def write_to_xml(channels: Dict[str, str], programmes: Dict[str, List[ET.Element]], filename: str):
    root = ET.Element('tv', {
        'generator-info-name': 'EPG-Merger-Optimized',
        'date': beijing_now().strftime("%Y%m%d%H%M%S +0800")
    })
    
    logger.info(f"写入 {len(channels)} 个频道")
    for c_id, name in sorted(channels.items()):
        ch_node = ET.SubElement(root, 'channel', {"id": c_id})
        ET.SubElement(ch_node, 'display-name', {"lang": "zh"}).text = name
    
    total_prog_count = 0
    for c_id in programmes:
        unique = {}
        for p in programmes[c_id]:
            key = (p.get('start'), p.findtext('title'))
            if key not in unique: unique[key] = p
        
        sorted_progs = sorted(unique.values(), key=lambda x: x.get('start'))
        for p in sorted_progs:
            root.append(p)
            total_prog_count += 1
            
    ET.indent(root, space="  ")
    tree = ET.ElementTree(root)
    tree.write(filename, encoding='utf-8', xml_declaration=True)
    return total_prog_count

def cleanup_old_files(output_dir: str, keep_count: int = 3):
    files = [f for f in os.listdir(output_dir) if f.startswith('epg_') and f.endswith('.xml')]
    files.sort(key=lambda x: os.path.getmtime(os.path.join(output_dir, x)), reverse=True)
    
    removed = 0
    for old_file in files[keep_count:]:
        try:
            os.remove(os.path.join(output_dir, old_file))
            logger.info(f"清理旧文件: {old_file}")
            removed += 1
        except: pass
    return removed

async def main():
    start_time_all = beijing_now()
    logger.info("检查运行环境...")
    logger.info(f"Python版本: {sys.version}")
    logger.info("=" * 60)
    logger.info("开始EPG合并处理")
    logger.info("=" * 60)
    
    urls = []
    if os.path.exists('config.txt'):
        with open('config.txt', 'r', encoding='utf-8') as f:
            urls = [l.strip() for l in f if l.strip() and not l.startswith('#')]
    logger.info(f"从配置读取 {len(urls)} 个EPG源")
    
    if not urls: return

    output_dir = 'output'
    os.makedirs(output_dir, exist_ok=True)
    logger.info(f"开始从 {len(urls)} 个源获取EPG数据...")
    
    async with aiohttp.ClientSession() as session:
        tasks = [fetch_epg(url, session) for url in urls]
        contents = await tqdm_asyncio.gather(*tasks, desc="获取EPG", unit="源")
    
    success_count = sum(1 for c in contents if c)
    logger.info(f"EPG获取完成: {success_count} 成功, {len(urls)-success_count} 失败")
    
    logger.info("开始解析EPG数据...")
    all_channels = {}
    all_programmes = defaultdict(list)
    
    with tqdm(total=len(contents), desc="解析EPG", unit="文件") as pbar:
        for content in contents:
            if content:
                ch, prog = parse_epg(content)
                for cid, name in ch.items():
                    if cid not in all_channels or len(name) > len(all_channels[cid]):
                        all_channels[cid] = name
                for cid, plist in prog.items():
                    all_programmes[cid].extend(plist)
                
                # 打印日志时也使用北京时间格式
                tqdm.write(f"{beijing_now().strftime('%Y-%m-%d %H:%M:%S,%f')[:-3]} - INFO - 解析完成: {len(ch)} 频道, {sum(len(v) for v in prog.values())} 节目")
            pbar.update(1)

    ts = beijing_now().strftime("%Y%m%d_%H%M%S")
    xml_file = os.path.join(output_dir, f"epg_{ts}.xml")
    gz_file = os.path.join(output_dir, "epg.xml.gz")
    
    logger.info("写入XML文件...")
    total_progs = write_to_xml(all_channels, all_programmes, xml_file)
    logger.info(f"写入完成: {xml_file}, {total_progs} 个节目")
    
    logger.info("生成压缩文件...")
    with open(xml_file, 'rb') as f_in, gzip.open(gz_file, 'wb') as f_out:
        shutil.copyfileobj(f_in, f_out)
    
    xml_size = os.path.getsize(xml_file)
    gz_size = os.path.getsize(gz_file)
    logger.info(f"压缩完成: {gz_file}")
    logger.info(f"压缩率: {gz_size/xml_size:.1%}")

    latest_link = os.path.join(output_dir, 'epg_latest.xml')
    if os.path.exists(latest_link):
        os.remove(latest_link)
        logger.info(f"已移除旧的软链接: {latest_link}")
    
    try:
        os.symlink(os.path.basename(xml_file), latest_link)
        logger.info(f"创建软链接: {os.path.basename(xml_file)} -> {latest_link}")
    except:
        shutil.copy2(xml_file, latest_link)
        logger.info(f"创建文件副本: {os.path.basename(xml_file)} -> {latest_link}")

    with open(xml_file, "rb") as f:
        file_md5 = hashlib.md5(f.read()).hexdigest()

    duration = (beijing_now() - start_time_all).total_seconds()
    logger.info("=" * 60)
    logger.info("✅ EPG合并处理完成!")
    logger.info("-" * 60)
    logger.info("📊 统计数据:")
    logger.info(f"   ⏱️  处理时间: {duration:.1f}秒")
    logger.info(f"   📡 数据源: {success_count}成功/{len(urls)-success_count}失败")
    logger.info(f"   📺 频道数: {len(all_channels)}")
    logger.info(f"   🎬 节目数: {total_progs}")
    logger.info(f"   💾 文件大小: XML={xml_size/1024/1024:.1f}MB, GZ={gz_size/1024/1024:.1f}MB")
    logger.info(f"   📁 原始文件: {xml_file}")
    logger.info(f"   📦 压缩文件: {gz_file}")
    logger.info(f"   🔗 最新链接: {latest_link}")
    logger.info(f"   🔐 文件校验: {file_md5}")
    logger.info("=" * 60)

    removed_count = cleanup_old_files(output_dir, 3)
    logger.info(f"🗑️  清理完成: 删除了 {removed_count} 个旧文件")

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("👋 用户中断程序")
