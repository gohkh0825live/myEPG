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
import sys
from functools import lru_cache

# ================= 配置常量 =================
TZ_UTC_PLUS_8 = timezone(timedelta(hours=8))
OUTPUT_DIR = 'output'
CONFIG_FILE = 'config.txt'

# ============ EPG 源预处理规则 ============
def _adjust_timezone(programme: ET.Element, from_offset: str, to_offset: str):
    """将 programme 节点的 start/stop 时区偏移进行替换"""
    for attr in ('start', 'stop'):
        val = programme.get(attr, '')
        if from_offset in val:
            programme.set(attr, val.replace(from_offset, to_offset))

def _make_tz_rule(channel_keyword: str, from_offset: str, to_offset: str):
    def rule(channel_name: str, programme: ET.Element):
        if channel_keyword in channel_name:
            _adjust_timezone(programme, from_offset, to_offset)
    return rule

PREPROCESS_RULES = [
    # 天映经典频道: 时区 +0800 → +0700 
    ("kuke31/xmlgz", _make_tz_rule("天映经典", "+0800", "+0700")),
]

# ================= 核心处理类 =================
class EPGProcessor:
    def __init__(self):
        self.cc = OpenCC("t2s")
        # LRU 缓存，避免重复节目名导致的性能损耗
        self.transform_zh = lru_cache(maxsize=20000)(self._transform_text)
        self.today = datetime.now(TZ_UTC_PLUS_8).date()
        
        # 存储最终数据
        self.channels_map = {} # display_name -> map_id
        self.channel_names = defaultdict(list)
        
        # 【关键修改】: 使用 dict 替代 set()，利用 Python 3.7+ 字典保持插入顺序的特性
        # 从而严格保证输出顺序与 config.txt 抓取顺序完全一致
        self.channel_ids = {} 
        self.programmes = defaultdict(list)

    def _transform_text(self, text: str) -> str:
        return self.cc.convert(text) if text else ""

    def process_display_name(self, display_name: str) -> str:
        return display_name[:-2] if display_name.endswith('高清') else display_name

    async def fetch_epg(self, url: str) -> tuple:
        connector = aiohttp.TCPConnector(limit=16, ssl=False)
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/113.0.0.0 Safari/537.36"
        }
        try:
            async with aiohttp.ClientSession(connector=connector, headers=headers) as session:
                async with session.get(url, timeout=30) as response:
                    if response.status == 200:
                        data = await response.read()
                        if url.endswith('.gz') or data.startswith(b'\x1f\x8b'):
                            return url, gzip.decompress(data).decode('utf-8', errors='ignore')
                        return url, data.decode('utf-8', errors='ignore')
        except Exception as e:
            print(f"❌ 下载失败 {url}: {e}")
        return url, None

    def parse_and_merge(self, url: str, epg_content: str):
        if not epg_content: return
        
        # 提取当前源适用的预处理规则
        matched_rules = [rule for keyword, rule in PREPROCESS_RULES if keyword in url]
        
        try:
            parser = ET.XMLParser(encoding='UTF-8')
            root = ET.fromstring(epg_content, parser=parser)
        except ET.ParseError as e:
            print(f"⚠️ XML解析错误: {e}")
            return

        local_channels = {}
        local_programmes = defaultdict(list)
        # 此处也用 dict 维持单个源内部频道的先后顺序
        valid_local_channels = {} 
        channel_names_str = {}

        # 1. 提取频道信息
        for channel in root.findall('channel'):
            raw_id = channel.get('id', '')
            channel_id = self.transform_zh(raw_id)
            names_data = []
            
            raw_names = []
            for name in channel.findall('display-name'):
                if not name.text: continue
                t_name = self.process_display_name(self.transform_zh(name.text))
                lang = name.get('lang', 'zh')
                names_data.append((t_name, lang))
                raw_names.append(t_name)
                
            if not channel_id.isdigit() and not any(channel_id == n[0] for n in names_data):
                names_data.append((channel_id, 'zh'))
                
            local_channels[channel_id] = names_data
            channel_names_str[raw_id] = ' '.join(raw_names) + ' ' + raw_id

        # 2. 提取并预处理节目信息
        for prog in root.findall('programme'):
            raw_cid = prog.get('channel', '')
            channel_id = self.transform_zh(raw_cid)
            
            # 触发预处理规则 (时区修改等)
            if matched_rules:
                c_name_str = channel_names_str.get(raw_cid, raw_cid)
                for rule in matched_rules:
                    rule(c_name_str, prog)

            # 解析时间
            try:
                start_dt = datetime.strptime(re.sub(r'\s+', '', prog.get('start', '')), "%Y%m%d%H%M%S%z").astimezone(TZ_UTC_PLUS_8)
                stop_dt = datetime.strptime(re.sub(r'\s+', '', prog.get('stop', '')), "%Y%m%d%H%M%S%z").astimezone(TZ_UTC_PLUS_8)
            except ValueError:
                continue # 时间格式错误直接丢弃

            if stop_dt.date() == self.today:
                valid_local_channels[channel_id] = True # 记录为当天有效频道

            new_prog = ET.Element('programme', attrib={
                "start": start_dt.strftime("%Y%m%d%H%M%S %z"), 
                "stop": stop_dt.strftime("%Y%m%d%H%M%S %z")
            })

            # 处理标题
            title = prog.find('title')
            title_text = "精彩节目" if title is None or not title.text else title.text.strip()
            lang = title.get('lang') if title is not None else None
            
            if lang in ('zh', None): title_text = self.transform_zh(title_text)
            ET.SubElement(new_prog, 'title', attrib={'lang': lang} if lang else {}).text = title_text

            # 处理描述
            for desc in prog.findall('desc'):
                if not desc.text: continue
                d_lang = desc.get('lang')
                desc_text = self.transform_zh(desc.text.strip()) if d_lang in ('zh', None) else desc.text.strip()
                ET.SubElement(new_prog, 'desc', attrib={'lang': d_lang} if d_lang else {}).text = desc_text

            local_programmes[channel_id].append(new_prog)

        # 3. 合并到全局数据结构，保证按 config 顺序插入
        for channel_id in valid_local_channels.keys():
            display_names = local_channels.get(channel_id, [])
            if not local_programmes[channel_id]: continue

            is_in_map = False
            map_id = ""
            for d_name, _ in display_names:
                if is_in_map: break
                is_in_map = d_name in self.channels_map
                map_id = d_name
                
            map_id = self.channels_map.get(map_id, channel_id)

            if not is_in_map:
                # 【关键修改】第一次遇到该频道，按照当前顺序注册到有序字典中
                self.channel_ids[map_id] = True 
                self.channel_names[map_id] = display_names
                self.programmes[map_id] = local_programmes[channel_id]
                for d_name, _ in display_names:
                    self.channels_map[d_name] = map_id
            else:
                # 已经是已知频道，补充数据但不再更改其原有的先后排位
                if len(self.programmes[map_id]) < len(local_programmes[channel_id]):
                    self.programmes[map_id] = local_programmes[channel_id]
                for d_name, lang in display_names:
                    if d_name not in self.channels_map:
                        self.channel_names[map_id].append((d_name, lang))
                        self.channels_map[d_name] = map_id

    def save(self):
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        xml_file = os.path.join(OUTPUT_DIR, 'epg.xml')
        gz_file = os.path.join(OUTPUT_DIR, 'epg.xml.gz')
        
        current_time = datetime.now(TZ_UTC_PLUS_8).strftime("%Y%m%d%H%M%S %z")
        root = ET.Element('tv', attrib={'date': current_time})

        # 【关键修改】标准 XMLTV 结构：先集中写入所有 <channel> 频道声明
        for map_id in self.channel_ids.keys():
            c_elem = ET.SubElement(root, 'channel', attrib={"id": map_id})
            for d_name, lang in self.channel_names[map_id]:
                ET.SubElement(c_elem, 'display-name', attrib={"lang": lang}).text = d_name
                
        # 然后再集中写入所有的 <programme> 节目数据
        for map_id in self.channel_ids.keys():
            for prog in self.programmes[map_id]:
                prog.set('channel', map_id)
                root.append(prog)

        # 内存极低的内置缩进 (避免 minidom 崩溃)
        if hasattr(ET, 'indent'): ET.indent(root, space="\t")
        
        tree = ET.ElementTree(root)
        tree.write(xml_file, encoding='utf-8', xml_declaration=True)

        with open(xml_file, 'rb') as f_in, gzip.open(gz_file, 'wb') as f_out:
            shutil.copyfileobj(f_in, f_out)
        print(f"✅ 生成完毕! XML大小: {os.path.getsize(xml_file)/1024/1024:.2f}MB")

async def main():
    if not os.path.exists(CONFIG_FILE):
        print(f"❌ 找不到配置文件: {CONFIG_FILE}")
        return
        
    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        urls = [line.strip() for line in f if line.strip() and not line.startswith('#')]

    processor = EPGProcessor()
    
    print("📡 正在并发获取 EPG 数据...")
    # asyncio.gather 能够保证返回的 results 顺序与 urls 的初始顺序完全对应
    tasks = [processor.fetch_epg(url) for url in urls]
    results = await tqdm_asyncio.gather(*tasks, desc="下载进度")

    print("\n⚙️ 正在解析与合并数据...")
    # 严格按照 config.txt 中的 url 顺序依次处理
    for url, content in tqdm(results, desc="处理进度"):
        processor.parse_and_merge(url, content)

    print("\n💾 正在按顺序写入文件并压缩...")
    processor.save()

if __name__ == '__main__':
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
