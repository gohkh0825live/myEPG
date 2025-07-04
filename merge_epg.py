import requests
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
import os
import gzip
import shutil

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

def fetch_xml(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        r.raise_for_status()
        if "<html" in r.text.lower():
            print(f"[BLOCKED] {url}")
            return None
        return r.content
    except Exception as e:
        print(f"[ERROR] {url}: {e}")
        return None

def merge_epg(urls):
    merged_tv = ET.Element("tv")
    for content in ThreadPoolExecutor(max_workers=5).map(fetch_xml, urls):
        if content:
            try:
                root = ET.fromstring(content)
                for child in root:
                    merged_tv.append(child)
            except ET.ParseError:
                print("[PARSE ERROR] One source was invalid XML")
    return merged_tv

def compress_to_gz(input_filename, output_filename):
    with open(input_filename, 'rb') as f_in:
        with gzip.open(output_filename, 'wb') as f_out:
            shutil.copyfileobj(f_in, f_out)

def main():
    with open("config.txt", "r", encoding="utf-8") as f:
        urls = [
            line.strip()
            for line in f
            if line.strip() and not line.strip().startswith("#")
        ]

    merged = merge_epg(urls)
    os.makedirs("output", exist_ok=True)
    xml_path = "output/epg.xml"
    gz_path = "output/epg.xml.gz"
    tree = ET.ElementTree(merged)
    tree.write(xml_path, encoding="utf-8", xml_declaration=True)
    compress_to_gz(xml_path, gz_path)
    print(f"[DONE] Merged XML saved to {xml_path}")
    print(f"[DONE] Compressed XML saved to {gz_path}")

if __name__ == "__main__":
    main()
