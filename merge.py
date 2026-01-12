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
import io

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
logger.propagate = False

# 全局实例
cc = OpenCC("t2s")

def transform2_zh_hans(string: str) -> str:
    return cc.convert(string) if string else ""

def normalize_channel_id(channel_id: str) -> str:
    # 保留中文、字母、数字、横线
    channel_id = re.sub(r'[^\w\u4e00-\u9fff\-]', '', channel_id)
    return channel_id.strip()

def normalize_datetime(dt_str: str) -> datetime:
    """
    解析任意时区时间并统一转换为北京时间
    支持格式: YYYYMMDDHHMMSS +HHMM / YYYYMMDDHHMMSS (默认北京)
    """
    dt_str = dt_str.strip()
    try:
        # 尝试解析带时区的标准 XMLTV 格式 (例如: 20231010120000 +0800)
        # Python 3.7+ %z 可以处理 +HHMM
        if ' ' in dt_str:
             # 有些源可能是 "20231010120000 +0800" 中间有空格
             dt = datetime.strptime(dt_str.replace(" ", ""), "%Y%m%d%H%M%S%z")
        elif len(dt_str) > 14 and (dt_str[14] == '+' or dt_str[14] == '-'):
             dt = datetime.strptime(dt_str, "%Y%m%d%H%M%S%z")
        else:
             # 无时区信息，默认视为北京时间
             dt = datetime.strptime(dt_str[:14], "%Y%m%d%H%M%S")
             dt = dt.replace(tzinfo=BEIJING_TZ)
        
        # 统一转换为北京时间
        return dt.astimezone(BEIJING_TZ)
    except Exception:
        # 兜底策略：只要前14位是数字，强行视为北京时间，防止报错丢台
        try:
            clean_ts = dt_str[:14]
            dt = datetime.strptime(clean_ts, "%Y%m%d%H%M%S")
            return dt.replace(tzinfo=BEIJING_TZ)
        except:
            # 实在解析不了，返回当前时间以便后续过滤丢弃
            return datetime.now(BEIJING_TZ) - timedelta(days=365)

def strip_namespace(xml_content: str) -> str:
    """去除XML内容中的命名空间，防止findall失效"""
    return re.sub(r' xmlns="[^"]+"', '', xml_content, count=1)

async def fetch_epg(url: str, session: aiohttp.ClientSession) -> Optional[str]:
    try:
        # 增加 headers 模拟浏览器，防止部分 CDN 拦截
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
        async with session.get(url, headers=headers, timeout=60) as response:
            if response.status == 200:
                # 自动处理 gzip
                content = await response.read()
                try:
                    # 尝试解压 (即使 header 没写 gzip，有的源也是压缩的)
                    if content[:2] == b'\x1f\x8b':
                        content = gzip.decompress(content)
                except:
                    pass
                return content.decode('utf-8', errors='ignore')
            else:
                logger.warning(f"下载失败 [{response.status}]: {url}")
    except Exception as e:
        logger.error(f"下载异常 {url}: {e}")
    return None

def parse_epg(epg_content: str) -> Tuple[Dict[str, str], Dict[str, List[ET.Element]]]:
    channels = {}
    programmes = defaultdict(list)
    if not epg_content: return channels, programmes
    
    # 关键修复：去除命名空间
    epg_content = strip_namespace(epg_content)

    try:
        root = ET.fromstring(epg_content)
    except Exception as e:
        logger.error(f"XML解析失败: {e}")
        return channels, programmes
    
    # 解析频道
    for channel in root.findall('channel'):
        c_id = channel.get('id', '')
        if not c_id: continue
        norm_id = normalize_channel_id(transform2_zh_hans(c_id))
        
        name_node = channel.find('display-name')
        if name_node is not None and name_node.text:
            display_name = transform2_zh_hans(name_node.text)
        else:
            display_name = norm_id
            
        channels[norm_id] = display_name
    
    # 解析节目
    now = beijing_now()
    # 允许保留过去多久的节目（例如保留过去1小时的回看）
    retention_time = now - timedelta(hours=1) 

    for prog in root.findall('programme'):
        c_id = prog.get('channel', '')
        if not c_id: continue
        norm_id = normalize_channel_id(transform2_zh_hans(c_id))
        
        # 如果频道不在列表中，可能也需要保留（取决于是否严格匹配）
        # 这里为了数据完整性，暂不强制要求频道必须在 channels 字典中
        
        try:
            start_str = prog.get('start', '')
            stop_str = prog.get('stop', '')
            
            if not start_str or not stop_str: continue

            start_dt = normalize_datetime(start_str)
            stop_dt = normalize_datetime(stop_str)
            
            if stop_dt < retention_time: continue # 过滤已结束太久的节目
            
            new_p = ET.Element('programme', {
                "channel": norm_id,
                "start": start_dt.strftime("%Y%m%d%H%M%S +0800"),
                "stop": stop_dt.strftime("%Y%m%d%H%M%S +0800")
            })
            
            # 复制并繁转简
            for tag in ['title', 'desc', 'sub-title', 'category', 'episode-num']:
                node = prog.find(tag)
                if node is not None and node.text:
                    text = transform2_zh_hans(node.text)
                    # 清理可能存在的换行符
                    text = text.replace('\n', ' ').strip()
                    ET.SubElement(new_p, tag, node.attrib).text = text # 保留原属性(如lang)
            
            programmes[norm_id].append(new_p)
        except Exception: 
            continue
            
    return channels, programmes

def write_to_xml(channels: Dict[str, str], programmes: Dict[str, List[ET.Element]], filename: str):
    root = ET.Element('tv', {
        'generator-info-name': 'EPG-Merger-Optimized',
        'date': beijing_now().strftime("%Y%m%d%H%M%S +0800")
    })
    
    logger.info(f"写入 {len(channels)} 个频道")
    # 按频道ID排序写入，美观
    for c_id, name in sorted(channels.items()):
        ch_node = ET.SubElement(root, 'channel', {"id": c_id})
        ET.SubElement(ch_node, 'display-name', {"lang": "zh"}).text = name
    
    total_prog_count = 0
    
    # 优化：按频道顺序写入节目，而不是乱序
    sorted_channel_ids = sorted(programmes.keys())
    
    for c_id in sorted_channel_ids:
        plist = programmes[c_id]
        if not plist: continue

        # 去重逻辑：同一频道，同一时间，同一标题
        unique = {}
        for p in plist:
            # 使用开始时间和标题作为唯一键
            key = (p.get('start'), p.findtext('title'))
            if key not in unique: 
                unique[key] = p
        
        # 排序：按开始时间
        sorted_progs = sorted(unique.values(), key=lambda x: x.get('start'))
        
        for p in sorted_progs:
            root.append(p)
            total_prog_count += 1
            
    # Py3.9+ 支持 indent，为了兼容性加判断，或者简单处理
    if hasattr(ET, 'indent'):
        ET.indent(root, space="  ")
    
    tree = ET.ElementTree(root)
    tree.write(filename, encoding='utf-8', xml_declaration=True)
    return total_prog_count

def cleanup_old_files(output_dir: str, keep_count: int = 3):
    files = [f for f in os.listdir(output_dir) if f.startswith('epg_') and f.endswith('.xml')]
    # 按修改时间降序
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
    logger.info(f"Python版本: {sys.version.split()[0]}")
    logger.info("=" * 60)
    logger.info("开始EPG合并处理 (Enhanced Version)")
    logger.info("=" * 60)
    
    urls = []
    if os.path.exists('config.txt'):
        with open('config.txt', 'r', encoding='utf-8') as f:
            urls = [l.strip() for l in f if l.strip() and not l.startswith('#')]
    else:
        logger.warning("未找到 config.txt，请创建并添加EPG链接")
        return

    logger.info(f"从配置读取 {len(urls)} 个EPG源")
    if not urls: return

    output_dir = 'output'
    os.makedirs(output_dir, exist_ok=True)
    
    async with aiohttp.ClientSession() as session:
        tasks = [fetch_epg(url, session) for url in urls]
        # 使用 gather 获取所有结果
        contents = await tqdm_asyncio.gather(*tasks, desc="下载EPG", unit="源")
    
    success_count = sum(1 for c in contents if c)
    logger.info(f"下载完成: {success_count} 成功, {len(urls)-success_count} 失败")
    
    logger.info("开始解析合并...")
    all_channels = {}
    all_programmes = defaultdict(list)
    
    with tqdm(total=len(contents), desc="解析进度", unit="文件") as pbar:
        for content in contents:
            if content:
                ch, prog = parse_epg(content)
                # 合并频道：如果已存在，且新名字更长（通常信息更全），则覆盖
                for cid, name in ch.items():
                    if cid not in all_channels or len(name) > len(all_channels[cid]):
                        all_channels[cid] = name
                
                # 合并节目
                for cid, plist in prog.items():
                    all_programmes[cid].extend(plist)
                
                # 更新进度条显示的日志
                tqdm.write(f"  [+] 解析提取: {len(ch)} 频道 / {sum(len(v) for v in prog.values())} 节目")
            pbar.update(1)

    ts = beijing_now().strftime("%Y%m%d_%H%M%S")
    xml_file = os.path.join(output_dir, f"epg_{ts}.xml")
    gz_file = os.path.join(output_dir, "epg.xml.gz")
    
    logger.info("写入XML文件...")
    total_progs = write_to_xml(all_channels, all_programmes, xml_file)
    
    logger.info("生成压缩文件...")
    with open(xml_file, 'rb') as f_in, gzip.open(gz_file, 'wb') as f_out:
        shutil.copyfileobj(f_in, f_out)
    
    xml_size = os.path.getsize(xml_file)
    gz_size = os.path.getsize(gz_file)
    
    # 软链接处理 (兼容 Windows/Linux)
    latest_link = os.path.join(output_dir, 'epg_latest.xml')
    if os.path.exists(latest_link):
        try:
            os.remove(latest_link)
        except: pass
    
    try:
        # 在 Windows 上需要管理员权限才能创建 symlink，否则会抛出 OSError
        os.symlink(os.path.basename(xml_file), latest_link)
        logger.info(f"创建软链接成功: {latest_link}")
    except OSError:
        # 回退方案：直接复制
        shutil.copy2(xml_file, latest_link)
        logger.info(f"系统不支持软链接，已复制文件: {latest_link}")

    with open(xml_file, "rb") as f:
        file_md5 = hashlib.md5(f.read()).hexdigest()

    duration = (beijing_now() - start_time_all).total_seconds()
    
    logger.info("=" * 60)
    logger.info("✅ 处理完成!")
    logger.info("-" * 60)
    logger.info(f"📊 耗时: {duration:.1f}秒")
    logger.info(f"📺 频道: {len(all_channels)} | 🎬 节目: {total_progs}")
    logger.info(f"💾 大小: XML={xml_size/1024/1024:.2f}MB, GZ={gz_size/1024/1024:.2f}MB")
    logger.info(f"🔐 MD5 : {file_md5}")
    logger.info("=" * 60)

    cleanup_old_files(output_dir, 3)

if __name__ == '__main__':
    # 解决 Windows 下 asyncio SelectorEventLoop 报错问题
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("用户停止运行")
