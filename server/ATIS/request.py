import requests
import json
from tabulate import tabulate

atisdata = None

# 数据源前面挡着 Cloudflare，非浏览器形态的 User-Agent 一律 403——requests 的
# 默认 UA（python-requests/x.y）就在被拒之列，不带这个头取不到任何数据。
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; CanATIS/1.0)"}


def get_can_data():
    url = "https://data.ceruleanavi.net/v1/data.json"
    try:
        response = requests.get(url, headers=HEADERS, timeout=15)
        response.raise_for_status()
        data = response.json()
        return data
    except requests.exceptions.RequestException as e:
        print(f"Error fetching data: {e}")
    except json.JSONDecodeError as e:
        print(f"Error parsing JSON: {e}")
    return None

def display_atis_table(data):
    if not data or 'atis' not in data:
        print("No ATIS data available")
        return None
    
    # 准备表格数据
    table_data = []
    for atis in data['atis']:
        callsign = atis.get('callsign', 'N/A')
        frequency = atis.get('frequency', 'N/A')
        text = '\n'.join(atis.get('text_atis', ['N/A']))
        table_data.append([callsign, frequency, text])
    
    # 创建并显示表格
    print(table_data)
    return table_data

if __name__ == "__main__":
    data = get_can_data()
    if data:
        display_atis_table(data)