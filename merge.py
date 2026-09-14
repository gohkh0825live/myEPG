import os
import gzip
import shutil
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

CONFIG_FILE = "config.txt"
OUTPUT_DIR = "output"
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "epg.xml")
OUTPUT_GZ_FILE = os.path.join(OUTPUT_DIR, "epg.xml.gz")

# 限制解压前文件最大大小：98MB (确保完全低于 100MB 限制)
MAX_FILE_SIZE = 98 * 1024 * 1024 

# 定义 UTC+8 时区
TZ_UTC8 = timezone(timedelta(hours=8))

def download_or_read_xml(source_path):
    """根据路径或 URL 获取 XML 根元素"""
    source_path = source_path.strip()
    if not source_path:
        return None
    
    try:
        if source_path.startswith("http://") or source_path.startswith("https://"):
            req = urllib.request.Request(source_path, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=15) as response:
                return ET.fromstring(response.read())
        elif os.path.exists(source_path):
            return ET.parse(source_path).getroot()
        else:
            print(f"警告: 源路径不存在 - {source_path}")
            return None
    except Exception as e:
        print(f"解析 XML 失败 ({source_path}): {e}")
        return None

def is_within_two_days_utc8(start_str):
    """判断节目开始时间是否在 UTC+8 的【今天】或【明天】"""
    if not start_str or len(start_str) < 8:
        return True  # 格式异常时默认保留
    
    try:
        # 获取当前 UTC+8 的日期与次日日期
        now_utc8 = datetime.now(timezone.utc).astimezone(TZ_UTC8)
        today_utc8 = now_utc8.date()
        tomorrow_utc8 = today_utc8 + timedelta(days=1)
        
        # 提取 XML 中节目的年月日 (YYYYMMDD)
        prog_date = datetime.strptime(start_str[:8], "%Y%m%d").date()
        
        # 仅保留 UTC+8 时区下今明两天的节目
        return today_utc8 <= prog_date <= tomorrow_utc8
    except ValueError:
        return True

def merge_epg():
    if not os.path.exists(CONFIG_FILE):
        print(f"错误: 找不到配置文件 {CONFIG_FILE}")
        return

    # 按 config.txt 顺序读取源
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        sources = [line.strip() for line in f if line.strip() and not line.startswith("#")]

    if not sources:
        print("警告: config.txt 中未找到有效的 EPG 源")
        return

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    seen_channel_ids = set()   # 记录已添加的频道 ID
    seen_programmes = set()    # 记录已添加的节目 (channel_id, start, stop)

    parsed_sources = []
    for source in sources:
        root = download_or_read_xml(source)
        if root is not None:
            parsed_sources.append(root)

    # 1. 流式写入未压缩的 XML 文件
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f_out:
        header = '<?xml version="1.0" encoding="utf-8"?>\n<tv generator-info-name="myEPG-Merger">\n'
        f_out.write(header)
        current_bytes = len(header.encode("utf-8"))

        # 优先按 config.txt 顺序写入频道列表
        for root in parsed_sources:
            for channel in root.findall("channel"):
                channel_id = channel.attrib.get("id")
                if channel_id and channel_id not in seen_channel_ids:
                    seen_channel_ids.add(channel_id)
                    
                    xml_str = ET.tostring(channel, encoding="utf-8").decode("utf-8") + "\n"
                    encoded_bytes = xml_str.encode("utf-8")
                    
                    if current_bytes + len(encoded_bytes) >= MAX_FILE_SIZE:
                        print("警告: 写入频道列表时已达到文件大小上限！")
                        f_out.write("</tv>\n")
                        break
                    
                    f_out.write(xml_str)
                    current_bytes += len(encoded_bytes)

        # 优先按 config.txt 顺序写入节目列表 (仅限 UTC+8 今明两天)
        is_truncated = False
        for root in parsed_sources:
            if is_truncated:
                break
            for programme in root.findall("programme"):
                channel_id = programme.attrib.get("channel")
                start = programme.attrib.get("start")
                stop = programme.attrib.get("stop")

                if not channel_id or not start:
                    continue

                # UTC+8 今明两天过滤
                if not is_within_two_days_utc8(start):
                    continue

                prog_key = (channel_id, start, stop)
                if prog_key not in seen_programmes:
                    seen_programmes.add(prog_key)
                    
                    xml_str = ET.tostring(programme, encoding="utf-8").decode("utf-8") + "\n"
                    encoded_bytes = xml_str.encode("utf-8")
                    
                    # 精确控制解压前文件不超过 100MB
                    if current_bytes + len(encoded_bytes) >= MAX_FILE_SIZE:
                        print("提示: 文件大小接近 100MB 限制，已截断后续节目。")
                        is_truncated = True
                        break
                    
                    f_out.write(xml_str)
                    current_bytes += len(encoded_bytes)

        f_out.write("</tv>\n")

    xml_size_mb = os.path.getsize(OUTPUT_FILE) / (1024 * 1024)
    print(f"XML 文件已生成: {OUTPUT_FILE} ({xml_size_mb:.2f} MB)")

    # 2. 生成 epg.xml.gz 压缩文件
    with open(OUTPUT_FILE, 'rb') as f_in:
        with gzip.open(OUTPUT_GZ_FILE, 'wb', compresslevel=9) as f_out:
            shutil.copyfileobj(f_in, f_out)
            
    gz_size_mb = os.path.getsize(OUTPUT_GZ_FILE) / (1024 * 1024)
    print(f"GZ 压缩文件已生成: {OUTPUT_GZ_FILE} ({gz_size_mb:.2f} MB)")

if __name__ == "__main__":
    merge_epg()
