import xml.etree.ElementTree as ET
from collections import defaultdict
import aiohttp
import asyncio
from tqdm.asyncio import tqdm_asyncio
from datetime import datetime, timezone, timedelta
import gzip
import shutil
from xml.dom import minidom
import re
from opencc import OpenCC
import os
from tqdm import tqdm
import logging
from typing import Dict, List, Tuple, Optional
import hashlib

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# 全局实例复用
cc = OpenCC("t2s")

def transform2_zh_hans(string: str) -> str:
    """转换为简体中文"""
    if not string:
        return ""
    return cc.convert(string)

def normalize_channel_id(channel_id: str) -> str:
    """规范化频道ID"""
    # 移除特殊字符，保留字母数字和中文字符
    channel_id = re.sub(r'[^\w\u4e00-\u9fff\-]', '', channel_id)
    return channel_id.strip()

def normalize_datetime(dt_str: str) -> datetime:
    """标准化时间处理"""
    # 移除空白字符
    dt_str = re.sub(r'\s+', '', dt_str)
    
    # 处理时区
    if '+0800' in dt_str or '+08:00' in dt_str:
        tz = timezone(timedelta(hours=8))
    elif 'Z' in dt_str:
        tz = timezone.utc
    else:
        tz = timezone(timedelta(hours=8))  # 默认东八区
    
    # 解析时间
    dt_format = "%Y%m%d%H%M%S"
    dt_str_clean = dt_str[:14]  # 取前14位数字（年月日时分秒）
    dt = datetime.strptime(dt_str_clean, dt_format)
    return dt.replace(tzinfo=tz)

async def fetch_epg(url: str, session: aiohttp.ClientSession) -> Optional[str]:
    """异步获取EPG数据"""
    try:
        async with session.get(url, timeout=30) as response:
            response.raise_for_status()
            content = await response.text(encoding='utf-8')
            logger.debug(f"成功获取 EPG: {url}")
            return content
    except asyncio.TimeoutError:
        logger.warning(f"获取超时: {url}")
    except aiohttp.ClientError as e:
        logger.error(f"网络错误获取 {url}: {e}")
    except Exception as e:
        logger.error(f"获取失败 {url}: {e}")
    return None

def parse_epg(epg_content: str, source_url: str = "") -> Tuple[Dict[str, str], Dict[str, List[ET.Element]]]:
    """解析EPG XML内容"""
    channels = {}
    programmes = defaultdict(list)
    
    if not epg_content:
        return channels, programmes
    
    try:
        parser = ET.XMLParser(encoding='UTF-8')
        root = ET.fromstring(epg_content, parser=parser)
    except ET.ParseError as e:
        logger.error(f"XML解析失败 {source_url}: {e}")
        return channels, programmes
    except Exception as e:
        logger.error(f"解析异常 {source_url}: {e}")
        return channels, programmes
    
    # 解析频道
    for channel in root.findall('channel'):
        try:
            channel_id = transform2_zh_hans(channel.get('id', ''))
            if not channel_id:
                continue
                
            # 获取频道名称
            name_node = channel.find('display-name')
            if name_node is not None and name_node.text:
                display_name = transform2_zh_hans(name_node.text)
            else:
                display_name = channel_id
            
            # 规范化频道ID
            normalized_id = normalize_channel_id(channel_id)
            channels[normalized_id] = display_name
        except Exception as e:
            logger.warning(f"频道解析异常: {e}")
            continue
    
    # 解析节目
    for programme in root.findall('programme'):
        try:
            channel_id = transform2_zh_hans(programme.get('channel', ''))
            if not channel_id:
                continue
            
            # 规范化频道ID
            normalized_id = normalize_channel_id(channel_id)
            
            # 解析时间
            start_str = programme.get('start')
            stop_str = programme.get('stop')
            if not start_str or not stop_str:
                continue
                
            try:
                start_dt = normalize_datetime(start_str)
                stop_dt = normalize_datetime(stop_str)
            except ValueError as e:
                logger.warning(f"时间格式错误: {start_str} - {stop_str}: {e}")
                continue
            
            # 只保留今天和未来的节目（可调整）
            if stop_dt < datetime.now(timezone.utc):
                continue
            
            # 创建节目元素
            new_prog = ET.Element('programme', {
                "channel": normalized_id,
                "start": start_dt.strftime("%Y%m%d%H%M%S %z"),
                "stop": stop_dt.strftime("%Y%m%d%H%M%S %z")
            })
            
            # 标题
            title_elem = programme.find('title')
            if title_elem is not None and title_elem.text:
                title = transform2_zh_hans(title_elem.text)
                ET.SubElement(new_prog, 'title').text = title
            
            # 描述
            desc_elem = programme.find('desc')
            if desc_elem is not None and desc_elem.text:
                desc = transform2_zh_hans(desc_elem.text)
                ET.SubElement(new_prog, 'desc').text = desc
            
            # 其他可选字段
            for field in ['sub-title', 'category', 'episode-num']:
                field_elem = programme.find(field)
                if field_elem is not None and field_elem.text:
                    field_text = transform2_zh_hans(field_elem.text)
                    ET.SubElement(new_prog, field).text = field_text
            
            programmes[normalized_id].append(new_prog)
            
        except Exception as e:
            logger.warning(f"节目解析异常: {e}")
            continue
    
    logger.info(f"解析完成: {len(channels)} 频道, {sum(len(p) for p in programmes.values())} 节目")
    return channels, programmes

def merge_channels(all_channels: Dict[str, str], new_channels: Dict[str, str]) -> Dict[str, str]:
    """合并频道信息，处理重复"""
    merged = all_channels.copy()
    
    for ch_id, name in new_channels.items():
        if ch_id not in merged:
            merged[ch_id] = name
        else:
            # 如果已存在，选择更长的名称（通常包含更多信息）
            if len(name) > len(merged[ch_id]):
                merged[ch_id] = name
    
    return merged

def merge_programmes(all_programmes: Dict[str, List[ET.Element]], 
                    new_programmes: Dict[str, List[ET.Element]]) -> Dict[str, List[ET.Element]]:
    """合并节目信息，按时间排序"""
    merged = defaultdict(list)
    
    # 合并所有节目
    for ch_id in set(all_programmes.keys()) | set(new_programmes.keys()):
        merged[ch_id] = all_programmes.get(ch_id, []) + new_programmes.get(ch_id, [])
    
    # 按开始时间排序并去重（基于开始时间、结束时间和标题）
    for ch_id in merged:
        unique_programs = {}
        for prog in merged[ch_id]:
            key = (
                prog.get('start'),
                prog.get('stop'),
                prog.findtext('title', '')
            )
            if key not in unique_programs:
                unique_programs[key] = prog
        
        # 按开始时间排序
        sorted_programs = sorted(
            unique_programs.values(),
            key=lambda x: x.get('start', '')
        )
        merged[ch_id] = sorted_programs
    
    return merged

def write_to_xml(channels: Dict[str, str], programmes: Dict[str, List[ET.Element]], 
                filename: str, max_days: int = 7):
    """写入XML文件"""
    os.makedirs(os.path.dirname(filename), exist_ok=True)
    
    root = ET.Element('tv', {
        'generator-info-name': 'EPG-Merger',
        'generator-info-url': 'https://github.com/yourusername/epg-merger',
        'date': datetime.now().strftime("%Y%m%d%H%M%S %z")
    })
    
    # 添加频道
    logger.info(f"写入 {len(channels)} 个频道")
    for channel_id, display_name in sorted(channels.items()):
        ch_elem = ET.SubElement(root, 'channel', {"id": channel_id})
        ET.SubElement(ch_elem, 'display-name', {"lang": "zh"}).text = display_name
    
    # 添加节目
    total_programs = 0
    cutoff_date = datetime.now(timezone.utc) + timedelta(days=max_days)
    
    for channel_id, prog_list in programmes.items():
        if channel_id not in channels:
            continue
            
        for prog in prog_list:
            try:
                # 过滤过远的节目
                prog_start = datetime.strptime(
                    prog.get('start')[:14], 
                    "%Y%m%d%H%M%S"
                ).replace(tzinfo=timezone(timedelta(hours=8)))
                
                if prog_start > cutoff_date:
                    continue
                    
                root.append(prog)
                total_programs += 1
            except Exception as e:
                logger.warning(f"添加节目失败 {channel_id}: {e}")
    
    # 美化输出
    rough_string = ET.tostring(root, 'utf-8')
    pretty_xml = minidom.parseString(rough_string).toprettyxml(
        indent='  ', 
        newl='\n',
        encoding='utf-8'
    )
    
    with open(filename, 'wb') as f:
        f.write(pretty_xml)
    
    logger.info(f"写入完成: {filename}, {total_programs} 个节目")

def compress_to_gz(input_file: str, output_file: str):
    """压缩为GZ文件"""
    try:
        with open(input_file, 'rb') as f_in, gzip.open(output_file, 'wb') as f_out:
            shutil.copyfileobj(f_in, f_out)
        logger.info(f"压缩完成: {output_file}")
        
        # 验证文件大小
        input_size = os.path.getsize(input_file)
        output_size = os.path.getsize(output_file)
        compression_ratio = output_size / input_size if input_size > 0 else 0
        logger.info(f"压缩率: {compression_ratio:.1%}")
    except Exception as e:
        logger.error(f"压缩失败: {e}")

def get_urls(config_path: str = 'config.txt') -> List[str]:
    """从配置文件读取URL列表"""
    urls = []
    
    if not os.path.exists(config_path):
        logger.error(f"配置文件不存在: {config_path}")
        # 创建示例配置文件
        sample_config = """# EPG源配置
# 每行一个URL，空行和#开头的行会被忽略

# 示例源
# http://example.com/epg.xml
# http://another.com/epg.gz
"""
        os.makedirs(os.path.dirname(config_path), exist_ok=True)
        with open(config_path, 'w', encoding='utf-8') as f:
            f.write(sample_config)
        logger.info(f"已创建示例配置文件: {config_path}")
        return urls
    
    try:
        with open(config_path, 'r', encoding='utf-8') as file:
            for line_num, line in enumerate(file, 1):
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                
                # 简单的URL验证
                if line.startswith(('http://', 'https://')):
                    urls.append(line)
                else:
                    logger.warning(f"第{line_num}行不是有效的URL: {line}")
    except Exception as e:
        logger.error(f"读取配置文件失败: {e}")
    
    logger.info(f"从配置读取 {len(urls)} 个EPG源")
    return urls

def cleanup_old_files(output_dir: str, keep_count: int = 5):
    """
    清理旧的EPG文件，保留最新的几个文件
    
    Args:
        output_dir: 输出目录
        keep_count: 保留的文件数量
    """
    try:
        if not os.path.exists(output_dir):
            return 0
            
        # 获取所有epg_开头的XML文件
        epg_files = []
        for file in os.listdir(output_dir):
            if file.startswith('epg_') and file.endswith('.xml'):
                file_path = os.path.join(output_dir, file)
                # 排除软链接
                if not os.path.islink(file_path):
                    try:
                        mtime = os.path.getmtime(file_path)
                        epg_files.append((file_path, mtime, file))
                    except OSError:
                        continue
        
        if len(epg_files) <= keep_count:
            return 0
        
        # 按修改时间排序（最新的在前面）
        epg_files.sort(key=lambda x: x[1], reverse=True)
        
        # 删除旧文件，保留指定数量的最新文件
        files_to_remove = epg_files[keep_count:]
        removed_count = 0
        
        for file_path, mtime, filename in files_to_remove:
            try:
                os.remove(file_path)
                logger.info(f"清理旧文件: {filename}")
                removed_count += 1
            except Exception as e:
                logger.warning(f"删除文件失败 {filename}: {e}")
        
        logger.info(f"文件清理完成: 保留 {keep_count} 个最新文件，删除了 {removed_count} 个旧文件")
        return removed_count
        
    except Exception as e:
        logger.error(f"清理文件失败: {e}")
        return 0

def get_file_md5(filename: str) -> Optional[str]:
    """计算文件的MD5哈希值"""
    try:
        hash_md5 = hashlib.md5()
        with open(filename, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hash_md5.update(chunk)
        return hash_md5.hexdigest()
    except Exception as e:
        logger.warning(f"计算MD5失败 {filename}: {e}")
        return None

async def main():
    """主函数"""
    start_time = datetime.now()
    logger.info("=" * 60)
    logger.info("开始EPG合并处理")
    logger.info("=" * 60)
    
    # 读取配置
    urls = get_urls()
    if not urls:
        logger.error("没有可用的EPG源，请编辑config.txt文件")
        return
    
    # 创建输出目录
    output_dir = 'output'
    os.makedirs(output_dir, exist_ok=True)
    
    # 清理旧文件（在开始前清理，避免占用空间）
    cleanup_old_files(output_dir, keep_count=3)
    
    # 创建HTTP会话
    connector = aiohttp.TCPConnector(limit=10, ssl=False)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    # 记录获取的EPG源数量
    successful_sources = 0
    failed_sources = 0
    
    async with aiohttp.ClientSession(
        connector=connector, 
        headers=headers,
        timeout=aiohttp.ClientTimeout(total=120)
    ) as session:
        # 并行获取所有EPG数据
        logger.info(f"开始从 {len(urls)} 个源获取EPG数据...")
        tasks = [fetch_epg(url, session) for url in urls]
        epg_contents = await tqdm_asyncio.gather(
            *tasks, 
            desc="获取EPG", 
            unit="源",
            leave=False
        )
    
    # 统计获取结果
    for url, content in zip(urls, epg_contents):
        if content:
            successful_sources += 1
        else:
            failed_sources += 1
    
    logger.info(f"EPG获取完成: {successful_sources} 成功, {failed_sources} 失败")
    
    # 解析和合并数据
    all_channels = {}
    all_programmes = defaultdict(list)
    
    logger.info("开始解析EPG数据...")
    with tqdm(total=len(epg_contents), desc="解析EPG", unit="文件", leave=False) as pbar:
        for url, content in zip(urls, epg_contents):
            if not content:
                pbar.update(1)
                continue
            
            try:
                channels, programmes = parse_epg(content, url)
                
                # 合并数据
                all_channels = merge_channels(all_channels, channels)
                all_programmes = merge_programmes(all_programmes, programmes)
                
            except Exception as e:
                logger.error(f"处理 {url} 时出错: {e}")
                failed_sources += 1
            finally:
                pbar.update(1)
    
    if not all_channels:
        logger.error("没有成功解析到任何频道数据")
        return
    
    # 生成输出文件
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    xml_filename = os.path.join(output_dir, f'epg_{timestamp}.xml')
    gz_filename = os.path.join(output_dir, 'epg.xml.gz')
    
    logger.info("写入XML文件...")
    write_to_xml(all_channels, all_programmes, xml_filename)
    
    logger.info("生成压缩文件...")
    compress_to_gz(xml_filename, gz_filename)
    
    # 生成最新文件的软链接
    latest_link = os.path.join(output_dir, 'epg_latest.xml')
    
    # 检查并移除已存在的链接或文件
    if os.path.exists(latest_link) or os.path.islink(latest_link):
        try:
            os.remove(latest_link)
            logger.info(f"已移除旧的软链接: {latest_link}")
        except Exception as e:
            logger.warning(f"移除软链接失败: {e}")
    
    # 创建新的软链接（使用相对路径）
    try:
        # 使用相对路径创建软链接
        os.symlink(os.path.basename(xml_filename), latest_link)
        logger.info(f"创建软链接: {os.path.basename(xml_filename)} -> {latest_link}")
    except OSError as e:
        # Windows系统可能需要管理员权限才能创建软链接
        logger.warning(f"创建软链接失败: {e}")
        logger.info("尝试创建文件副本...")
        try:
            shutil.copy2(xml_filename, latest_link)
            logger.info(f"创建文件副本: {os.path.basename(xml_filename)} -> {latest_link}")
        except Exception as copy_error:
            logger.warning(f"创建文件副本失败: {copy_error}")
    
    # 计算处理时间
    end_time = datetime.now()
    processing_time = (end_time - start_time).total_seconds()
    
    # 统计信息
    total_programs = sum(len(progs) for progs in all_programmes.values())
    
    # 计算文件大小
    xml_size = os.path.getsize(xml_filename) / (1024 * 1024)
    gz_size = os.path.getsize(gz_filename) / (1024 * 1024) if os.path.exists(gz_filename) else 0
    
    # 计算MD5（可选）
    xml_md5 = get_file_md5(xml_filename)
    
    logger.info("=" * 60)
    logger.info("✅ EPG合并处理完成!")
    logger.info("-" * 60)
    logger.info(f"📊 统计数据:")
    logger.info(f"   ⏱️  处理时间: {processing_time:.1f}秒")
    logger.info(f"   📡 数据源: {successful_sources}成功/{failed_sources}失败")
    logger.info(f"   📺 频道数: {len(all_channels)}")
    logger.info(f"   🎬 节目数: {total_programs}")
    logger.info(f"   💾 文件大小: XML={xml_size:.1f}MB, GZ={gz_size:.1f}MB")
    logger.info(f"   📁 原始文件: {xml_filename}")
    logger.info(f"   📦 压缩文件: {gz_filename}")
    logger.info(f"   🔗 最新链接: {latest_link}")
    if xml_md5:
        logger.info(f"   🔐 文件校验: {xml_md5}")
    logger.info("=" * 60)
    
    # 清理旧文件（在结束后清理，保留最新的3个文件）
    cleaned_count = cleanup_old_files(output_dir, keep_count=3)
    if cleaned_count > 0:
        logger.info(f"🗑️  清理完成: 删除了 {cleaned_count} 个旧文件")

def check_environment():
    """检查运行环境"""
    logger.info("检查运行环境...")
    
    # 检查Python版本
    import sys
    logger.info(f"Python版本: {sys.version}")
    
    # 检查依赖模块
    required_modules = [
        'xml.etree.ElementTree',
        'aiohttp',
        'asyncio',
        'tqdm',
        'opencc',
        'gzip',
        'shutil'
    ]
    
    for module in required_modules:
        try:
            __import__(module.split('.')[0])
            logger.debug(f"✓ {module} 可用")
        except ImportError as e:
            logger.error(f"✗ {module} 不可用: {e}")
    
    # 检查输出目录权限
    output_dir = 'output'
    try:
        os.makedirs(output_dir, exist_ok=True)
        test_file = os.path.join(output_dir, 'test.tmp')
        with open(test_file, 'w') as f:
            f.write('test')
        os.remove(test_file)
        logger.debug("✓ 输出目录可写")
    except Exception as e:
        logger.error(f"✗ 输出目录不可写: {e}")

if __name__ == '__main__':
    try:
        # 检查环境
        check_environment()
        
        # 运行主程序
        asyncio.run(main())
        
    except KeyboardInterrupt:
        logger.info("👋 用户中断程序")
    except Exception as e:
        logger.error(f"❌ 程序异常: {e}")
        logger.exception("详细异常信息:")
        raise
