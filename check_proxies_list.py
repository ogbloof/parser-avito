#!/usr/bin/env python3
"""Разовый скрипт: проверка списка прокси (IP + Авито)."""
from selenium_fetcher import check_proxy

PROXIES = """
mobpool.proxy.market:10000@igZ1DHFfU1CF:k5qZQHYB
mobpool.proxy.market:10000@NfJGAymN0vg8:rVti5wJb
mobpool.proxy.market:10000@f7N8FBKmrEbM:YgUWKQxh
mobpool.proxy.market:10000@o3NTb0pgqqW4:pSkQymjD
mobpool.proxy.market:10000@0Jpf9PpA0dJK:u3GiBaMK
mobpool.proxy.market:10000@5Ve1WcaTlLMd:etEYp2wf
mobpool.proxy.market:10000@j9BKLZOlebvg:HQUs2V0b
mobpool.proxy.market:10000@7zdhD08jkLx1:yloHW46n
mobpool.proxy.market:10000@fa9TewmlRZTu:cKgrXkys
""".strip().splitlines()

if __name__ == "__main__":
    for i, proxy in enumerate(PROXIES, 1):
        proxy = proxy.strip()
        if not proxy:
            continue
        print(f"\n[{i}/{len(PROXIES)}] {proxy[:40]}***")
        try:
            ip_result, avito_result = check_proxy(proxy)
            print(f"  IP: {ip_result}")
            print(f"  Авито: {avito_result}")
        except Exception as e:
            print(f"  Ошибка: {e}")
    print("\nГотово.")
