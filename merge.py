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
import logging
import hashlib
from typing import Dict, List, Tuple, Optional, AsyncGenerator

# 配置日志 - 使用更清晰的格式
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

# 全局转换器
cc = OpenCC("t2s")

def transform_to_hans(text: str) -> str:
    """转换为简体中文，增加空值处理"""
    return cc.convert(text) if text else ""

def normalize_channel_id(channel_id: str) -> str:
    """更高效的频道ID规范化"""
    if not channel_id: return ""
    # 仅保留中英文字符、数字、横杠和下划线
    return re.sub(r'[^\w\u4e00-\u9fff\-]', '', channel_id).strip()

def parse_epg_datetime(dt_str: str) -> datetime:
    """
    解析 EPG 时间字符串。
    支持格式: 20231024120000 +0800, 20231024120000 Z, 20231024120000
    """
    if not dt_str:
        return datetime.now(timezone.utc)
    
    # 清理空白
    dt_str = dt_str.strip()
    
    # 提取前14位数字
    clean_ts = dt_str[:14]
    try:
        dt = datetime.strptime(clean_ts, "%Y%m%d%H%M%S")
    except ValueError:
        return datetime.now(timezone.utc)

    # 时区处理
    if "+08" in dt_str:
        tz = timezone(timedelta(hours=8))
    elif "Z" in dt_str.upper():
        tz = timezone.utc
    else:
        tz = timezone(timedelta(hours=8)) # 默认东八区
        
    return dt.replace(tzinfo=tz)

async def download_and_parse(url: str, session: aiohttp.ClientSession) -> Tuple[Dict, Dict]:
    """下载并立即解析，减少内存占用"""
    try:
        async with session.get(url, timeout=60) as response:
            response.raise_for_status()
            # 针对大文件，可以在这里改用流式解析(iterparse)
            content = await response.text(encoding='utf-8', errors='ignore')
            return parse_epg_logic(content, url)
    except Exception as e:
        logger.error(f"源获取失败 {url}: {e}")
        return {}, {}

def parse_epg_logic(content: str, source_url: str) -> Tuple[Dict[str, str], Dict[str, List[ET.Element]]]:
    """核心解析逻辑"""
    channels = {}
    programmes = defaultdict(list)
    
    try:
        root = ET.fromstring(content)
    except Exception as e:
        logger.error(f"XML 语法错误 {source_url}: {e}")
        return channels, programmes

    # 1. 解析频道
    for ch in root.findall('channel'):
        raw_id = ch.get('id', '')
        if not raw_id: continue
        
        norm_id = normalize_channel_id(transform_to_hans(raw_id))
        disp_name = ch.findtext('display-name', default=raw_id)
        channels[norm_id] = transform_to_hans(disp_name)

    # 2. 解析节目
    now = datetime.now(timezone.utc)
    for prog in root.findall('programme'):
        raw_ch_id = prog.get('channel', '')
        if not raw_ch_id: continue
        
        norm_id = normalize_channel_id(transform_to_hans(raw_ch_id))
        start_raw = prog.get('start')
        stop_raw = prog.get('stop')
        
        if not start_raw or not stop_raw: continue
        
        start_dt = parse_epg_datetime(start_raw)
        stop_dt = parse_epg_datetime(stop_raw)

        # 过滤已过期节目 (留出1小时余量)
        if stop_dt < now - timedelta(hours=1):
            continue

        # 构建新的节目元素
        new_prog = ET.Element('programme', {
            "channel": norm_id,
            "start": start_dt.strftime("%Y%m%d%H%M%S %z"),
            "stop": stop_dt.strftime("%Y%m%d%H%M%S %z")
        })
        
        # 转换文本内容
        for tag in ['title', 'desc', 'sub-title', 'category', 'episode-num']:
            elem = prog.find(tag)
            if elem is not None and elem.text:
                ET.SubElement(new_prog, tag).text = transform_to_hans(elem.text)
        
        programmes[norm_id].append(new_prog)
        
    return channels, programmes

def save_xml(channels: Dict, programmes: Dict, filename: str):
    """保存并美化 XML"""
    root = ET.Element('tv', {
        'generator-info-name': 'EPG-Optimizer',
        'date': datetime.now().strftime("%Y%m%d%H%M%S")
    })

    # 写入频道
    for ch_id, name in sorted(channels.items()):
        ch_node = ET.SubElement(root, 'channel', {"id": ch_id})
        ET.SubElement(ch_node, 'display-name', {"lang": "zh"}).text = name

    # 写入节目 (去重并排序)
    total_count = 0
    for ch_id in programmes:
        # 使用 (开始时间, 标题) 作为唯一键去重
        unique_progs = {}
        for p in programmes[ch_id]:
            key = (p.get('start'), p.findtext('title'))
            if key not in unique_progs:
                unique_progs[key] = p
        
        # 按开始时间排序
        sorted_list = sorted(unique_progs.values(), key=lambda x: x.get('start', ''))
        for p in sorted_list:
            root.append(p)
            total_count += 1

    # 美化 XML (Python 3.9+)
    ET.indent(root, space="  ")
    tree = ET.ElementTree(root)
    
    os.makedirs(os.path.dirname(filename), exist_ok=True)
    tree.write(filename, encoding='utf-8', xml_declaration=True)
    logger.info(f"文件保存成功: {filename} (节目总数: {total_count})")

async def main():
    start_time = datetime.now()
    output_dir = 'output'
    config_path = 'config.txt'
    
    # 1. 读取 URL
    if not os.path.exists(config_path):
        logger.error("config.txt 不存在")
        return
    
    with open(config_path, 'r', encoding='utf-8') as f:
        urls = [line.strip() for line in f if line.strip() and not line.startswith('#')]

    if not urls:
        logger.warning("没有可处理的 URL")
        return

    # 2. 异步下载与并行解析
    all_channels = {}
    all_programmes = defaultdict(list)
    
    timeout = aiohttp.ClientTimeout(total=300)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        tasks = [download_and_parse(url, session) for url in urls]
        
        # 使用 tqdm 监控进度
        for result in await tqdm_asyncio.gather(*tasks, desc="处理中"):
            ch_data, prog_data = result
            # 合并频道信息
            for cid, name in ch_data.items():
                if cid not in all_channels or len(name) > len(all_channels[cid]):
                    all_channels[cid] = name
            # 合并节目信息
            for cid, progs in prog_data.items():
                all_programmes[cid].extend(progs)

    if not all_channels:
        logger.error("未能获取任何有效数据")
        return

    # 3. 写入文件
    xml_name = os.path.join(output_dir, f"epg_{datetime.now().strftime('%m%d_%H%M')}.xml")
    save_xml(all_channels, all_programmes, xml_name)
    
    # 4. 压缩
    gz_name = os.path.join(output_dir, "epg.xml.gz")
    with open(xml_name, 'rb') as f_in, gzip.open(gz_name, 'wb') as f_out:
        shutil.copyfileobj(f_in, f_out)
    
    # 5. 清理旧文件 (保留最近5个)
    files = sorted([os.path.join(output_dir, f) for f in os.listdir(output_dir) if f.startswith('epg_')], 
                   key=os.path.getmtime, reverse=True)
    for old_file in files[5:]:
        os.remove(old_file)

    duration = (datetime.now() - start_time).total_seconds()
    logger.info(f"任务完成! 总耗时: {duration:.1f}s")

if __name__ == '__main__':
    asyncio.run(main())
