import argparse
import json
import os
import re
import sys
import time
from urllib.parse import urljoin, urlparse
import requests
from bs4 import BeautifulSoup

_hash_cache = None


def get(url, **kw):
    headers = kw.pop("headers", {})
    headers.setdefault(
        "User-Agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/113.0.0.0 Safari/537.36",
    )
    r = requests.get(url, headers=headers, timeout=20, **kw)
    r.raise_for_status()
    return r


def post(url, **kw):
    headers = kw.pop("headers", {})
    headers.setdefault(
        "User-Agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/113.0.0.0 Safari/537.36",
    )
    r = requests.post(url, headers=headers, timeout=20, **kw)
    r.raise_for_status()
    return r


def dle_hash():
    global _hash_cache
    if _hash_cache:
        return _hash_cache
    html = get("https://anitube.in.ua").text
    m = re.search(r"dle_login_hash\s*=\s*'([^']+)'", html)
    _hash_cache = m.group(1) if m else ""
    return _hash_cache


def quick_search(query):
    r = post("https://anitube.in.ua/engine/lazydev/dle_search/ajax.php",
             data={"story": query, "dle_hash": dle_hash()},
             headers={"X-Requested-With": "XMLHttpRequest", "Referer": "https://anitube.in.ua"})
    try:
        data = json.loads(r.content.decode("utf-8-sig"))
    except Exception:
        return []
    html = data.get("content") or ""
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    results = []
    for a in soup.select("a"):
        heading = a.select_one("span.searchheading")
        if heading:
            results.append({"name": heading.get_text(strip=True), "url": a["href"]})
    return results


def full_search(query):
    r = post("https://anitube.in.ua?do=search", data={
        "do": "search",
        "subaction": "search",
        "search_start": 0,
        "full_search": 0,
        "result_from": 1,
        "story": query,
    }, headers={"Referer": "https://anitube.in.ua"})
    soup = BeautifulSoup(r.text, "html.parser")
    results = []
    for article in soup.select("article.story"):
        h2 = article.select_one("h2 a")
        if h2:
            results.append({"name": h2.get_text(strip=True), "url": h2["href"]})
    return results


def anime_id_from_page(soup):
    article = soup.select_one("article.story")
    if article and article.get("id"):
        m = re.match(r"news-(\d+)", article["id"])
        if m:
            return int(m.group(1))
    ld = soup.select_one("script[type='application/ld+json']")
    if ld:
        m = re.search(r'"@id"\s*:\s*"[^"]*?/(\d+)-', ld.string or "")
        if m:
            return int(m.group(1))
    return None


def user_hash_from_page(soup):
    m = re.search(r"dle_login_hash\s*=\s*'([^']+)'", str(soup))
    return m.group(1) if m else ""


def parse_inline_player(soup):
    init_re = re.compile(r"RalodePlayer\.init\((.*?),(\[\[.*?\]\])", re.DOTALL)
    src_re = re.compile(r'src="([^"]+)"')
    script_text = ""
    for tag in soup.select("#dle-content > article script"):
        if "RalodePlayer.init(" in tag.get_text():
            script_text = tag.get_text()
            break
    if not script_text:
        return []
    m = init_re.search(script_text)
    if not m:
        return []
    try:
        audios = json.loads(m.group(1).strip())
        videos = json.loads(m.group(2).strip())
    except Exception:
        return []
    out = []
    for i, dubber in enumerate(audios):
        raw_eps = videos[i] if i < len(videos) else []
        eps = []
        for ep in raw_eps:
            code = ep.get("code", "")
            src_m = src_re.search(code)
            eps.append({
                "name": ep.get("name", f"Ep {i + 1}"),
                "url": src_m.group(1) if src_m else "",
            })
        out.append({"dubber": dubber, "episodes": eps})
    return out


def parse_ajax_playlist(news_id, user_hash, referer):
    r = get("https://anitube.in.ua/engine/ajax/playlists.php",
            params={"news_id": news_id, "xfield": "playlist", "user_hash": user_hash},
            headers={"X-Requested-With": "XMLHttpRequest", "Referer": referer})
    try:
        data = json.loads(r.content.decode("utf-8-sig"))
    except Exception:
        return []
    if not data.get("success"):
        return []
    html = data.get("response") or ""
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    labels = {}
    for li in soup.select(".playlists-lists .playlists-items li"):
        did = li.get("data-id", "").strip()
        if did:
            labels[did] = li.get_text(strip=True)
    episodes = []
    for li in soup.select(".playlists-videos .playlists-items li"):
        episodes.append({
            "player_id": li.get("data-id", "").strip(),
            "name": li.get_text(strip=True),
            "url": li.get("data-file", "").strip(),
        })
    if not labels:
        return [{"dubber": "Default", "episodes": episodes}] if episodes else []
    children = {did: [] for did in labels}
    roots = []
    for did in labels:
        cut = did.rfind("_")
        parent = did[:cut] if cut != -1 else None
        if parent and parent in children:
            children[parent].append(did)
        else:
            roots.append(did)
    results = []
    def walk(did, path):
        name = labels[did]
        eps_here = [e for e in episodes if e["player_id"] == did]
        full_path = path + [name]
        if eps_here:
            results.append({"dubber": " › ".join(full_path), "episodes": eps_here})
        for kid in children[did]:
            walk(kid, full_path)
    for r_id in roots:
        walk(r_id, [])
    return results


def parse_m3u8_qualities(m3u8_text, base_url):
    qualities = {}
    lines = m3u8_text.splitlines()
    res_re = re.compile(r"/(\d{3,4})/")
    for i, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF"):
            continue
        for j in range(i + 1, len(lines)):
            uri = lines[j].strip()
            if not uri or uri.startswith("#"):
                continue
            m = res_re.search(uri)
            label = f"{m.group(1)}p" if m else "AUTO"
            if not uri.startswith("http"):
                uri = urljoin(base_url, uri)
            qualities[label] = uri
            break
    return qualities


def extract_ashdi(iframe_url):
    page = get(iframe_url, headers={
        "Referer": "https://anitube.in.ua",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/113.0.0.0 Safari/537.36",
    }).text
    m = (re.search(r"file\s*:\s*'(https://[^']+\.m3u8[^']*)'", page)
         or re.search(r'file\s*:\s*"(https://[^"]+\.m3u8[^"]*)"', page)
         or re.search(r"(https://[^\s'\"]+\.m3u8)", page))
    if not m:
        print("  [ashdi] couldn't find an m3u8 url on that player page")
        return {"url": iframe_url, "qualities": {}}
    file_url = m.group(1).strip()
    print(f"  [ashdi] m3u8: {file_url}")
    dq = re.search(r"default_quality\s*:\s*['\"]([^'\"]+)['\"]", page)
    default_q = dq.group(1) if dq else ""
    try:
        text = get(file_url, headers={
            "Referer": iframe_url,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/113.0.0.0 Safari/537.36",
        }).text
        qualities = parse_m3u8_qualities(text, file_url)
    except Exception as e:
        print(f"  [ashdi] failed to fetch master m3u8: {e}")
        qualities = {}
    return {"url": file_url, "qualities": qualities, "default_quality": default_q}


def resolve_iframe(iframe_url):
    host = urlparse(iframe_url).netloc
    if "ashdi.vip" in host:
        return extract_ashdi(iframe_url)
    return {"url": iframe_url, "qualities": {}}


def download_file(url, dest, headers=None):
    h = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/113.0.0.0 Safari/537.36",
        "Referer": "https://anitube.in.ua",
    }
    if headers:
        h.update(headers)
    with requests.get(url, headers=h, stream=True, timeout=30) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        done = 0
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                f.write(chunk)
                done += len(chunk)
                if total:
                    pct = done * 100 // total
                    sys.stdout.write(f"\r  [{'#' * (pct // 2):<50}] {pct}%")
                    sys.stdout.flush()
    print()


def download_hls(m3u8_url, dest, referer="https://anitube.in.ua"):
    text = get(m3u8_url, headers={"Referer": referer}).text
    segments = [l.strip() for l in text.splitlines() if l.strip() and not l.startswith("#")]
    if not segments:
        print("  no segments found in that playlist, giving up")
        return
    base = m3u8_url.rsplit("/", 1)[0] + "/"
    total = len(segments)
    print(f"  downloading {total} segments...")
    with open(dest, "wb") as out:
        for i, seg in enumerate(segments, 1):
            seg_url = seg if seg.startswith("http") else base + seg
            chunk = get(seg_url, headers={"Referer": referer}).content
            out.write(chunk)
            pct = i * 100 // total
            sys.stdout.write(f"\r  [{'#' * (pct // 2):<50}] {pct}% ({i}/{total})")
            sys.stdout.flush()
    print()


def fetch_players(anime_url):
    print(f"\nFetching anime page: {anime_url}")
    r = get(anime_url)
    soup = BeautifulSoup(r.text, "html.parser")
    players = parse_inline_player(soup)
    if players:
        print(f"  found inline player, {len(players)} dub(s)")
        return players
    news_id = anime_id_from_page(soup)
    if news_id:
        uh = user_hash_from_page(soup)
        print(f"  news_id={news_id}, checking ajax playlist...")
        players = parse_ajax_playlist(news_id, uh, anime_url)
        if players:
            print(f"  found {len(players)} track(s)")
            return players
    print("  couldn't find any episodes on this page")
    return []


def choose(options, label=str, prompt="Select: "):
    if not options:
        return None
    for i, o in enumerate(options, 1):
        print(f"  {i:4}. {label(o)}")
    while True:
        raw = input(prompt).strip()
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1]
        print("  invalid choice, try again")


def parse_episode_range(raw, n):
    raw = raw.strip().lower()
    if raw == "all":
        return list(range(n))
    if "," in raw:
        return [int(x.strip()) - 1 for x in raw.split(",")]
    if "-" in raw:
        a, b = raw.split("-", 1)
        return list(range(int(a) - 1, int(b)))
    return [int(raw) - 1]


def run(anime_url=None, query=None):
    print("=" * 60)
    print("  Anitube Downloader")
    print("=" * 60)
    if not anime_url:
        if not query:
            query = input("\nSearch anime title: ").strip()
        print(f"\nSearching for: {query}")
        results = quick_search(query)
        if not results:
            print("  quick search came back empty, trying full search...")
            results = full_search(query)
        if not results:
            print("  no results found")
            return
        print(f"\nFound {len(results)} result(s):")
        chosen = choose(results, label=lambda r: r["name"], prompt="Select anime: ")
        if not chosen:
            return
        anime_url = chosen["url"]
        if not anime_url.startswith("http"):
            anime_url = "https://anitube.in.ua" + anime_url
    players = fetch_players(anime_url)
    if not players:
        print("Nothing to download.")
        return
    playable = [p for p in players if p.get("episodes")]
    skipped = len(players) - len(playable)
    if skipped:
        print(f"\n  (skipping {skipped} empty track(s))")
    if not playable:
        print("  no playable tracks found")
        return
    print("\nAvailable tracks:")
    player = choose(playable,
                     label=lambda p: f"{p['dubber']}  [{len(p['episodes'])} ep]",
                     prompt="Select track: ")
    if not player:
        return
    episodes = player["episodes"]
    print(f"\nEpisodes ({len(episodes)} total):")
    for i, ep in enumerate(episodes, 1):
        print(f"  {i:4}. {ep['name']}")
    raw = input("\nEpisode(s) to download (e.g. 1  or  1-5  or  1,3,5  or  all): ")
    to_dl = parse_episode_range(raw, len(episodes))
    out_dir = input("\nOutput folder [./downloads]: ").strip() or "./downloads"
    os.makedirs(out_dir, exist_ok=True)
    chosen_quality = None
    for idx in to_dl:
        if 0 <= idx < len(episodes) and episodes[idx]["url"]:
            print("\nProbing first episode to check available qualities...")
            probe = resolve_iframe(episodes[idx]["url"])
            qualities = probe.get("qualities", {})
            if qualities:
                print("  qualities:", ", ".join(sorted(qualities)))
                default_q = probe.get("default_quality") or sorted(qualities)[-1]
                chosen_quality = input(f"  quality for all episodes [{default_q}]: ").strip() or default_q
            break
    for idx in to_dl:
        if idx < 0 or idx >= len(episodes):
            print(f"  skipping out-of-range index {idx + 1}")
            continue
        ep = episodes[idx]
        iframe_url = ep["url"]
        clean_name = re.sub(r'[\\/:*?"<>|]', "_", ep["name"])
        ep_name = re.sub(r"^(\d+)", lambda m: m.group(1).zfill(2), clean_name)
        print(f"\n── {ep['name']} {'─' * 40}")
        print(f"  iframe: {iframe_url}")
        info = resolve_iframe(iframe_url)
        stream_url = info["url"]
        qualities = info.get("qualities", {})
        if qualities:
            if chosen_quality and chosen_quality in qualities:
                stream_url = qualities[chosen_quality]
            else:
                print("  qualities:", ", ".join(sorted(qualities)))
                default_q = info.get("default_quality") or sorted(qualities)[-1]
                q = input(f"  quality [{default_q}]: ").strip() or default_q
                stream_url = qualities.get(q, stream_url)
                if chosen_quality is None:
                    chosen_quality = q
        if not stream_url or not stream_url.startswith("http"):
            print("  no valid stream url, skipping")
            continue
        dest = os.path.join(out_dir, ep_name + ".mp4")
        print(f"  -> {dest}")
        if ".m3u8" in stream_url:
            download_hls(stream_url, dest, referer=iframe_url)
        else:
            download_file(stream_url, dest)
        print("  done")
        if idx != to_dl[-1]:
            time.sleep(1)
    print("\nAll done!")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Download anime from anitube.in.ua")
    parser.add_argument("query", nargs="?", help="Anime title to search for")
    parser.add_argument("--url", help="Direct anime page URL (skip search)")
    args = parser.parse_args()
    try:
        run(anime_url=args.url, query=args.query)
    except KeyboardInterrupt:
        print("\nCancelled.")
