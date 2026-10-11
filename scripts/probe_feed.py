"""Read-only public quote probe; never accesses the trading account."""
import concurrent.futures
import urllib.request

URLS = [
    "https://hq.sinajs.cn/list=nf_IM2610,CFF_RE_IM2610",
    "https://push2.eastmoney.com/api/qt/stock/get?secid=220.IM2610&fields=f57,f58,f59,f43,f86,f19,f20,f17,f18,f39,f40,f31,f32,f60,f51,f52",
]


def probe(url):
    try:
        request = urllib.request.Request(url, headers={
            "Referer": "https://finance.sina.com.cn/", "User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=15) as response:
            return url, response.read().decode("gb18030", errors="replace")
    except Exception as exc:
        return url, type(exc).__name__, str(exc)


if __name__ == "__main__":
    with concurrent.futures.ThreadPoolExecutor() as pool:
        for result in pool.map(probe, URLS):
            print(result)
