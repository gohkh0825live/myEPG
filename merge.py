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
        self.transform_zh = lru_cache(maxsize=20000)(self._transform_text)
        self.today = datetime.now(TZ_UTC_PLUS_8).date()
        
        # 核心映射字典
        self.channels_order = []            # 保持 config.txt 的频道出现顺序
        self.canonical_channels = {}        # 最终输出的唯一纯净名称: cid -> (name, lang)
        self.canonical_icons = {}           # 最终输出的台标: cid -> icon_url
        self.name_to_canonical_id = {}      # 别名映射器: "TV1 HD" / "101.astro" -> "101"
        self.best_programmes = defaultdict(list) # 存储每个频道最长（最全）的节目单
        self.valid_canonical_ids = set()    # 记录今天有节目的频道

    def _transform_text(self, text: str) -> str:
        return self.cc.convert(text) if text else ""

    def process_display_name(self, display_name: str) -> str:
        """强化版名称清理：自动去除后缀的 高清 和 HD"""
        name = display_name.strip()
        if name.endswith('高清'): name = name[:-2]
        if name.upper().endswith(' HD'): name = name[:-3]
        return name.strip()

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
        matched_rules = [rule for keyword, rule in PREPROCESS_RULES if keyword in url]
        
        try:
            parser = ET.XMLParser(encoding='UTF-8')
            root = ET.fromstring(epg_content, parser=parser)
        except ET.ParseError as e:
            print(f"⚠️ XML解析错误: {e}")
            return

        # 本地字典，仅用于当前源的 ID 关联
        local_id_to_canonical = {}
        local_programmes = defaultdict(list)
        channel_names_str = {}

        # ================= 1. 频道解析与纯净映射 =================
        for channel in root.findall('channel'):
            raw_id = channel.get('id', '')
            
            # 提取所有显示名称并清理
            raw_names = []
            for name_node in channel.findall('display-name'):
                if name_node.text:
                    clean_name = self.process_display_name(self.transform_zh(name_node.text))
                    raw_names.append((clean_name, name_node.get('lang', 'zh')))
            
            # 兜底：如果完全没有名字，就把 ID 当名字
            if not raw_names:
                raw_names.append((self.transform_zh(raw_id), 'zh'))

            # 提取台标
            icon_src = None
            icon_node = channel.find('icon')
            if icon_node is not None:
                icon_src = icon_node.get('src')

            # --- 核心：寻找或创建 Canonical ID (基准 ID) ---
            canonical_id = None
            # 用提取到的每一个名字，以及原始 ID 去映射表里找
            search_keys = [n[0] for n in raw_names] + [raw_id]
            for key in search_keys:
                if key in self.name_to_canonical_id:
                    canonical_id = self.name_to_canonical_id[key]
                    break
            
            # 如果是新频道
            if not canonical_id:
                canonical_id = raw_id
                self.channels_order.append(canonical_id)
                # 【关键】只保存第一个被发现的名字，保证绝对纯净！
                self.canonical_channels[canonical_id] = raw_names[0]
                if icon_src:
                    self.canonical_icons[canonical_id] = icon_src

            # 无论是不是新频道，都要把当前源所有杂乱的名字指向基准 ID，防止断链
            for name_str, _ in raw_names:
                self.name_to_canonical_id[name_str] = canonical_id
            self.name_to_canonical_id[raw_id] = canonical_id
            
            local_id_to_canonical[raw_id] = canonical_id
            channel_names_str[raw_id] = ' '.join([n[0] for n in raw_names]) + ' ' + raw_id

        # ================= 2. 节目单解析与预处理 =================
        for prog in root.findall('programme'):
            raw_cid = prog.get('channel', '')
            # 获取对应的基准 ID
            canonical_id = local_id_to_canonical.get(raw_cid)
            if not canonical_id: continue

            if matched_rules:
                c_name_str = channel_names_str.get(raw_cid, raw_cid)
                for rule in matched_rules:
                    rule(c_name_str, prog)

            try:
                start_dt = datetime.strptime(re.sub(r'\s+', '', prog.get('start', '')), "%Y%m%d%H%M%S%z").astimezone(TZ_UTC_PLUS_8)
                stop_dt = datetime.strptime(re.sub(r'\s+', '', prog.get('stop', '')), "%Y%m%d%H%M%S%z").astimezone(TZ_UTC_PLUS_8)
            except ValueError:
                continue

            if stop_dt.date() == self.today:
                self.valid_canonical_ids.add(canonical_id)

            new_prog = ET.Element('programme', attrib={
                "start": start_dt.strftime("%Y%m%d%H%M%S %z"), 
                "stop": stop_dt.strftime("%Y%m%d%H%M%S %z")
            })

            title = prog.find('title')
            title_text = "精彩节目" if title is None or not title.text else title.text.strip()
            lang = title.get('lang') if title is not None else None
            if lang in ('zh', None): title_text = self.transform_zh(title_text)
            ET.SubElement(new_prog, 'title', attrib={'lang': lang} if lang else {}).text = title_text

            for desc in prog.findall('desc'):
                if not desc.text: continue
                d_lang = desc.get('lang')
                desc_text = self.transform_zh(desc.text.strip()) if d_lang in ('zh', None) else desc.text.strip()
                ET.SubElement(new_prog, 'desc', attrib={'lang': d_lang} if d_lang else {}).text = desc_text

            local_programmes[canonical_id].append(new_prog)

        # ================= 3. 优胜劣汰保存节目单 =================
        # 如果当前源提取到的节目单比已存在的长（数据更全），就完全替换它
        for cid, plist in local_programmes.items():
            if len(plist) > len(self.best_programmes[cid]):
                self.best_programmes[cid] = plist

    def save(self):
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        xml_file = os.path.join(OUTPUT_DIR, 'epg.xml')
        gz_file = os.path.join(OUTPUT_DIR, 'epg.xml.gz')
        
        current_time = datetime.now(TZ_UTC_PLUS_8).strftime("%Y%m%d%H%M%S %z")
        root = ET.Element('tv', attrib={'date': current_time})

        # 严格按照 config 的顺序写入纯净的频道声明
        for canonical_id in self.channels_order:
            if canonical_id not in self.valid_canonical_ids or not self.best_programmes[canonical_id]:
                continue
                
            c_elem = ET.SubElement(root, 'channel', attrib={"id": canonical_id})
            
            # 【完美解决】只写入唯一的基准名称
            d_name, lang = self.canonical_channels[canonical_id]
            ET.SubElement(c_elem, 'display-name', attrib={"lang": lang}).text = d_name
            
            # 顺便写入台标
            if canonical_id in self.canonical_icons:
                ET.SubElement(c_elem, 'icon', attrib={"src": self.canonical_icons[canonical_id]})
                
        # 写入节目单
        for canonical_id in self.channels_order:
            if canonical_id not in self.valid_canonical_ids:
                continue
            for prog in self.best_programmes[canonical_id]:
                prog.set('channel', canonical_id)
                root.append(prog)

        if hasattr(ET, 'indent'): ET.indent(root, space="\t")
        tree = ET.ElementTree(root)
        tree.write(xml_file, encoding='utf-8', xml_declaration=True)

        with open(xml_file, 'rb') as f_in, gzip.open(gz_file, 'wb') as f_out:
            shutil.copyfileobj(f_in, f_out)
        print(f"✅ 生成完毕! 纯净版 XML 大小: {os.path.getsize(xml_file)/1024/1024:.2f}MB")

async def main():
    if not os.path.exists(CONFIG_FILE):
        print(f"❌ 找不到配置文件: {CONFIG_FILE}")
        return
        
    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        urls = [line.strip() for line in f if line.strip() and not line.startswith('#')]

    processor = EPGProcessor()
    
    print("📡 正在并发获取 EPG 数据...")
    tasks = [processor.fetch_epg(url) for url in urls]
    results = await tqdm_asyncio.gather(*tasks, desc="下载进度")

    print("\n⚙️ 正在解析与合并数据...")
    for url, content in tqdm(results, desc="处理进度"):
        processor.parse_and_merge(url, content)

    print("\n💾 正在按顺序写入纯净版文件...")
    processor.save()

if __name__ == '__main__':
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
