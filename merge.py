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
from typing import Dict, List, Tuple, Optional, Set
import hashlib
import sys

# ================= 配置常量 =================
CONFIG = {
    "SOURCE_FILE": "config.txt",
    "OUTPUT_DIR": "output",
    "MAX_CONCURRENT_REQUESTS": 5,  # 限制并发数，防止被封
    "REQUEST_TIMEOUT": 30,         # 请求超时秒数
    "KEEP_OLD_FILES": 3,           # 保留旧文件数量
    "RETENTION_HOURS": 1,          # 保留过去多少小时的节目
    "USER_AGENT": 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
}

# ================= 环境与日志配置 =================
BEIJING_TZ = timezone(timedelta(hours=8))

def beijing_now() -> datetime:
    """获取当前的北京时间"""
    return datetime.now(BEIJING_TZ)

class BeijingFormatter(logging.Formatter):
    """强制日志使用北京时间"""
    def converter(self, timestamp):
        dt = datetime.fromtimestamp(timestamp, tz=BEIJING_TZ)
        return dt.timetuple()

    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, tz=BEIJING_TZ)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.strftime('%Y-%m-%d %H:%M:%S')

# 配置日志
logger = logging.getLogger("EPG_Merger")
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    formatter = BeijingFormatter('%(asctime)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)

# 全局转换器实例
cc = OpenCC("t2s")

# ================= 核心工具函数 =================

def transform2_zh_hans(string: Optional[str]) -> str:
    """繁体转简体"""
    return cc.convert(string) if string else ""

def normalize_channel_id(channel_id: str) -> str:
    """标准化频道ID：仅保留中文、字母、数字、横线"""
    if not channel_id:
        return "unknown"
    # 预编译正则可略微提升性能，但此处调用频率不高，直接调用亦可
    return re.sub(r'[^\w\u4e00-\u9fff\-]', '', channel_id).strip()

def normalize_datetime(dt_str: str) -> datetime:
    """
    解析任意时区时间并统一转换为北京时间
    """
    dt_str = dt_str.strip()
    try:
        # 常见格式处理
        # 格式: YYYYMMDDHHMMSS +HHMM 或 YYYYMMDDHHMMSS
        if len(dt_str) >= 14:
            base_time = dt_str[:14]
            dt = datetime.strptime(base_time, "%Y%m%d%H%M%S")
            
            # 检查时区后缀
            if len(dt_str) > 15 and ('+' in dt_str or '-' in dt_str):
                # 简单处理 +0800 这种格式
                tz_part = dt_str[-5:]
                if re.match(r'[+\-]\d{4}', tz_part):
                    hours = int(tz_part[:3])
                    minutes = int(tz_part[3:])
                    # 构建原始时区
                    original_tz = timezone(timedelta(hours=hours, minutes=minutes))
                    dt = dt.replace(tzinfo=original_tz)
                else:
                    dt = dt.replace(tzinfo=BEIJING_TZ)
            else:
                # 默认北京时间
                dt = dt.replace(tzinfo=BEIJING_TZ)
            
            return dt.astimezone(BEIJING_TZ)
            
    except ValueError:
        pass
    
    # 兜底：返回一年前的时间，确保后续逻辑能将其过滤
    return beijing_now() - timedelta(days=365)

def strip_namespace(xml_content: str) -> str:
    """
    去除XML内容中的命名空间
    优化：只替换第一次出现的 xmlns="..."，避免全量扫描带来的性能损耗
    """
    # 仅匹配根节点的 xmlns 定义
    return re.sub(r'\sxmlns="[^"]+"', '', xml_content, count=1)

# ================= 业务逻辑函数 =================

async def fetch_epg(url: str, session: aiohttp.ClientSession, semaphore: asyncio.Semaphore) -> Optional[str]:
    """异步下载 EPG 数据，带并发限制"""
    async with semaphore:  # 使用信号量限制并发
        try:
            headers = {'User-Agent': CONFIG["USER_AGENT"]}
            timeout = aiohttp.ClientTimeout(total=CONFIG["REQUEST_TIMEOUT"])
            
            async with session.get(url, headers=headers, timeout=timeout) as response:
                if response.status == 200:
                    content = await response.read()
                    
                    # 处理 Gzip (即使 Header 没声明)
                    if content.startswith(b'\x1f\x8b'):
                        try:
                            content = gzip.decompress(content)
                        except Exception as e:
                            logger.warning(f"解压异常 {url}: {e}")
                            
                    return content.decode('utf-8', errors='ignore')
                else:
                    logger.warning(f"下载失败 [{response.status}]: {url}")
        except asyncio.TimeoutError:
            logger.error(f"下载超时: {url}")
        except Exception as e:
            logger.error(f"下载异常 {url}: {e}")
    return None

def parse_epg_content(epg_content: str) -> Tuple[Dict[str, str], Dict[str, List[ET.Element]]]:
    """解析单个 EPG 内容"""
    channels = {}
    programmes = defaultdict(list)
    
    if not epg_content:
        return channels, programmes

    # 清理命名空间
    epg_content = strip_namespace(epg_content)

    try:
        # 使用 safe parsing 避免某些非法字符导致崩溃
        parser = ET.XMLParser(encoding="utf-8")
        root = ET.fromstring(epg_content, parser=parser)
    except Exception as e:
        logger.error(f"XML解析失败: {e}")
        return channels, programmes

    # 1. 解析频道
    for channel in root.findall('channel'):
        c_id = channel.get('id')
        if not c_id: continue
        
        # 转换 ID 和 名称
        norm_id = normalize_channel_id(transform2_zh_hans(c_id))
        
        display_name_node = channel.find('display-name')
        if display_name_node is not None and display_name_node.text:
            display_name = transform2_zh_hans(display_name_node.text)
        else:
            display_name = norm_id
            
        channels[norm_id] = display_name

    # 2. 解析节目
    retention_threshold = beijing_now() - timedelta(hours=CONFIG["RETENTION_HOURS"])
    
    for prog in root.findall('programme'):
        c_id = prog.get('channel')
        if not c_id: continue
        
        norm_id = normalize_channel_id(transform2_zh_hans(c_id))
        
        start_str = prog.get('start')
        stop_str = prog.get('stop')
        
        if not start_str or not stop_str: continue
        
        start_dt = normalize_datetime(start_str)
        stop_dt = normalize_datetime(stop_str)
        
        # 过滤过期的节目
        if stop_dt < retention_threshold:
            continue
            
        # 构建新节点
        new_p = ET.Element('programme', {
            "channel": norm_id,
            "start": start_dt.strftime("%Y%m%d%H%M%S +0800"),
            "stop": stop_dt.strftime("%Y%m%d%H%M%S +0800")
        })
        
        # 处理子节点 (标题、描述等)
        for tag in ['title', 'desc', 'sub-title', 'category', 'episode-num', 'icon']:
            node = prog.find(tag)
            if node is not None:
                # 只有文本类标签需要繁简转换
                if tag != 'icon' and node.text:
                    text = transform2_zh_hans(node.text).replace('\n', ' ').strip()
                    sub_node = ET.SubElement(new_p, tag, node.attrib)
                    sub_node.text = text
                elif tag == 'icon':
                    # 图标保留原样
                    ET.SubElement(new_p, tag, node.attrib)

        programmes[norm_id].append(new_p)

    return channels, programmes

def write_xml_file(channels: Dict[str, str], programmes: Dict[str, List[ET.Element]], filename: str) -> int:
    """构建并写入 XML 文件"""
    root = ET.Element('tv', {
        'generator-info-name': 'EPG-Merger-Optimized',
        'date': beijing_now().strftime("%Y%m%d%H%M%S +0800")
    })

    logger.info(f"正在构建 XML 树: {len(channels)} 个频道")
    
    # 写入频道 (按ID排序)
    for c_id in sorted(channels.keys()):
        ch_node = ET.SubElement(root, 'channel', {"id": c_id})
        ET.SubElement(ch_node, 'display-name', {"lang": "zh"}).text = channels[c_id]

    total_prog_count = 0
    
    # 写入节目 (按频道ID排序)
    for c_id in sorted(programmes.keys()):
        plist = programmes[c_id]
        if not plist: continue
        
        # 节目去重: 使用 (start_time, title) 作为指纹
        unique_progs = {}
        for p in plist:
            start_val = p.get('start')
            title = p.findtext('title', '')
            key = (start_val, title)
            unique_progs[key] = p
        
        # 节目排序: 按开始时间
        sorted_progs = sorted(unique_progs.values(), key=lambda x: x.get('start', ''))
        
        root.extend(sorted_progs)
        total_prog_count += len(sorted_progs)

    # 格式化 XML (Python 3.9+)
    if hasattr(ET, 'indent'):
        ET.indent(root, space="  ")

    # 写入文件
    tree = ET.ElementTree(root)
    tree.write(filename, encoding='utf-8', xml_declaration=True)
    
    return total_prog_count

def create_symlink_or_copy(src: str, dst: str):
    """创建软链接，失败则复制"""
    if os.path.exists(dst):
        try:
            os.remove(dst)
        except OSError:
            pass
            
    try:
        if sys.platform == 'win32':
             # Windows 可能会报权限错误，直接复制更稳妥
             shutil.copy2(src, dst)
        else:
            os.symlink(os.path.basename(src), dst)
            logger.info(f"软链接更新: {dst}")
    except OSError:
        shutil.copy2(src, dst)
        logger.info(f"软链接失败，已复制: {dst}")

def cleanup_old_files(output_dir: str):
    """清理旧文件"""
    files = [f for f in os.listdir(output_dir) if f.startswith('epg_') and f.endswith('.xml')]
    files.sort(key=lambda x: os.path.getmtime(os.path.join(output_dir, x)), reverse=True)
    
    for old_file in files[CONFIG["KEEP_OLD_FILES"]:]:
        try:
            os.remove(os.path.join(output_dir, old_file))
            logger.info(f"清理旧文件: {old_file}")
        except OSError as e:
            logger.error(f"清理失败 {old_file}: {e}")

# ================= 主流程 =================

async def main():
    start_time_all = beijing_now()
    
    print(r"""
   ___  ___  ___   __  __                             
  / _ \/ _ \/ _ \ /  \/  \ ___  _ _ __ _  ___  _ _    
 |  __/  __/ (_| | |\/| | / -_)| '_/ _` |/ -_)| '_|   
  \___|_|   \__, |_|  |_| \___||_| \__, |\___||_|     
            |___/                  |___/              
    """)
    logger.info("启动 EPG 合并程序 (Optimized)")
    
    # 1. 读取配置
    urls = []
    if os.path.exists(CONFIG["SOURCE_FILE"]):
        with open(CONFIG["SOURCE_FILE"], 'r', encoding='utf-8') as f:
            urls = [line.strip() for line in f if line.strip() and not line.startswith('#')]
    
    if not urls:
        logger.error(f"未找到有效配置或 {CONFIG['SOURCE_FILE']} 为空")
        return

    os.makedirs(CONFIG["OUTPUT_DIR"], exist_ok=True)
    logger.info(f"加载 {len(urls)} 个数据源")

    # 2. 并发下载
    semaphore = asyncio.Semaphore(CONFIG["MAX_CONCURRENT_REQUESTS"])
    async with aiohttp.ClientSession() as session:
        tasks = [fetch_epg(url, session, semaphore) for url in urls]
        # tqdm_asyncio 自动显示进度条
        contents = await tqdm_asyncio.gather(*tasks, desc="📥 下载进度", unit="源")

    success_count = sum(1 for c in contents if c)
    logger.info(f"下载完成: 成功 {success_count} / 总计 {len(urls)}")

    # 3. 解析与合并
    all_channels: Dict[str, str] = {}
    all_programmes: Dict[str, List[ET.Element]] = defaultdict(list)
    
    logger.info("开始解析与合并数据...")
    
    # 使用 tqdm 显示解析进度
    for content in tqdm(contents, desc="🔄 解析进度", unit="文件"):
        if not content: continue
        
        ch_dict, prog_dict = parse_epg_content(content)
        
        # 合并频道 (保留名字较长的那个，通常信息更全)
        for cid, name in ch_dict.items():
            if cid not in all_channels or len(name) > len(all_channels[cid]):
                all_channels[cid] = name
        
        # 合并节目
        for cid, plist in prog_dict.items():
            all_programmes[cid].extend(plist)

    # 4. 写入文件
    ts = beijing_now().strftime("%Y%m%d_%H%M%S")
    xml_file = os.path.join(CONFIG["OUTPUT_DIR"], f"epg_{ts}.xml")
    gz_file = os.path.join(CONFIG["OUTPUT_DIR"], "epg.xml.gz")
    
    total_progs = write_xml_file(all_channels, all_programmes, xml_file)
    
    # 5. 压缩与后处理
    logger.info("正在生成 GZIP 压缩包...")
    with open(xml_file, 'rb') as f_in, gzip.open(gz_file, 'wb') as f_out:
        shutil.copyfileobj(f_in, f_out)

    # 计算信息
    xml_size_mb = os.path.getsize(xml_file) / (1024 * 1024)
    gz_size_mb = os.path.getsize(gz_file) / (1024 * 1024)
    
    # MD5 计算
    with open(xml_file, "rb") as f:
        file_md5 = hashlib.md5(f.read()).hexdigest()

    # 更新 latest 链接
    create_symlink_or_copy(xml_file, os.path.join(CONFIG["OUTPUT_DIR"], 'epg_latest.xml'))
    
    # 清理
    cleanup_old_files(CONFIG["OUTPUT_DIR"])

    # 6. 总结报告
    duration = (beijing_now() - start_time_all).total_seconds()
    
    logger.info("=" * 60)
    logger.info("✅ 处理完成 Summary")
    logger.info("-" * 60)
    logger.info(f"⏱️  耗时: {duration:.2f} 秒")
    logger.info(f"📺 频道: {len(all_channels)} 个")
    logger.info(f"🎬 节目: {total_progs} 条")
    logger.info(f"📦 体积: XML={xml_size_mb:.2f}MB | GZ={gz_size_mb:.2f}MB")
    logger.info(f"🔑 MD5 : {file_md5}")
    logger.info("=" * 60)

if __name__ == '__main__':
    # Windows 平台下的 asyncio 策略修复
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.warning("🚫 用户手动终止程序")
    except Exception as e:
        logger.critical(f"❌ 程序发生未捕获异常: {e}", exc_info=True)
