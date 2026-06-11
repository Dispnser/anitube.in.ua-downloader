import argparse
import json
import os
import re
import sys
import time
from typing import Optional
from urllib.parse import urljoin, urlparse
import requests
from bs4 import BeautifulSoup

BASE_URL = "https://anitube.in.ua"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/113.0.0.0 Safari/537.36"
)
PLAYLIST_AJAX = "/engine/ajax/playlists.php"
QUICK_SEARCH_URL = "/engine/lazydev/dle_search/ajax.php"

session = requests.Session()
session.headers.update({"User-Agent": UA})

_dle_hash: Optional[str] = None


def get(url: str, **kwargs) -> requests.Response:
    r = session.get(url, timeout=20, **kwargs)
    r.raise_for_status()
    return r


def post(url: str, **kwargs) -> requests.Response:
    r = session.post(url, timeout=20, **kwargs)
    r.raise_for_status()
    return r


def get_dle_hash() -> str:
    global _dle_hash
    if _dle_hash:
        return _dle_hash
    html = get(BASE_URL).text
    m = re.search(r"dle_login_hash\s*=\s*'([^']+)'", html)
    _dle_hash = m.group(1) if m else ""
    return _dle_hash


def quick_search(query: str) -> list:
    r = post(
        BASE_URL + QUICK_SEARCH_URL,
        data={"story": query, "dle_hash": get_dle_hash()},
        headers={"X-Requested-With": "XMLHttpRequest", "Referer": BASE_URL},
    )
    try:
        data = json.loads(r.content.decode("utf-8-sig"))
    except Exception:
        return []

    html = data.get("content", "")
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    out = []
    for a in soup.select("a"):
        heading = a.select_one("span.searchheading")
        if heading:
            out.append({"name": heading.get_text(strip=True), "url": a["href"]})
    return out


def full_search(query: str) -> list:
    r = post(
        BASE_URL + "?do=search",
        data={
            "do": "search",
            "subaction": "search",
            "search_start": 0,
            "full_search": 0,
            "result_from": 1,
            "story": query,
        },
        headers={"Referer": BASE_URL},
    )
    soup = BeautifulSoup(r.text, "html.parser")
    out = []
    for article in soup.select("article.story"):
        h2 = article.select_one("h2 a")
        if h2:
            out.append({"name": h2.get_text(strip=True), "url": h2["href"]})
    return out


def get_anime_id(soup: BeautifulSoup) -> Optional[int]:
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


def get_user_hash(soup: BeautifulSoup) -> str:
    m = re.search(r"dle_login_hash\s*=\s*'([^']+)'", str(soup))
    return m.group(1) if m else ""


def parse_inline_player(soup: BeautifulSoup) -> list:
    pat = re.compile(r"RalodePlayer\.init\((.*?),(\[\[.*?\]\])", re.DOTALL)
    src_pat = re.compile(r'src="([^"]+)"')

    script_text = ""
    for s in soup.select("#dle-content > article script"):
        if "RalodePlayer.init(" in s.get_text():
            script_text = s.get_text()
            break

    if not script_text:
        return []

    m = pat.search(script_text)
    if not m:
        return []

    try:
        audios = json.loads(m.group(1).strip())
        videos = json.loads(m.group(2).strip())
    except Exception:
        return []

    players = []
    for i, dubber_name in enumerate(audios):
        eps_raw = videos[i] if i < len(videos) else []
        episodes = []
        for ep in eps_raw:
            code = ep.get("code", "")
            src_m = src_pat.search(code)
            url = src_m.group(1) if src_m else ""
            episodes.append({"name": ep.get("name", f"Ep {i+1}"), "url": url})
        players.append({"dubber": dubber_name, "episodes": episodes})
    return players


def parse_ajax_playlist(news_id: int, user_hash: str, referer: str) -> list:
    r = get(
        BASE_URL + PLAYLIST_AJAX,
        params={"news_id": news_id, "xfield": "playlist", "user_hash": user_hash},
        headers={"X-Requested-With": "XMLHttpRequest", "Referer": referer},
    )
    try:
        data = json.loads(r.content.decode("utf-8-sig"))
    except Exception:
        return []

    if not data.get("success"):
        return []

    html = data.get("response", "")
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")

    label_map = {}
    for li in soup.select(".playlists-lists .playlists-items li"):
        did = li.get("data-id", "").strip()
        name = li.get_text(strip=True)
        if did:
            label_map[did] = name

    all_episodes = []
    for li in soup.select(".playlists-videos .playlists-items li"):
        player_id = li.get("data-id", "").strip()
        name = li.get_text(strip=True)
        ep_url = li.get("data-file", "").strip()
        all_episodes.append({"player_id": player_id, "name": name, "url": ep_url})

    if not label_map:
        return [{"dubber": "Default", "episodes": all_episodes}] if all_episodes else []

    children = {did: [] for did in label_map}
    roots = []
    for did in label_map:
        last = did.rfind("_")
        if last == -1:
            roots.append(did)
        else:
            parent = did[:last]
            if parent in children:
                children[parent].append(did)
            else:
                roots.append(did)

    def node_eps(did):
        return [e for e in all_episodes if e["player_id"] == did]

    results = []

    def walk(did, path):
        name = label_map[did]
        kids = children[did]
        eps = node_eps(did)
        full_path = path + [name]
        if eps:
            results.append({"dubber": " › ".join(full_path), "episodes": eps})
        for kid in kids:
            walk(kid, full_path)

    for root_id in roots:
        walk(root_id, [])

    return results


def parse_m3u8_qualities(m3u8_text: str, base_url: str) -> dict:
    qualities = {}
    lines = m3u8_text.splitlines()
    res_pat = re.compile(r"/(\d{3,4})/")
    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF"):
            for j in range(i + 1, len(lines)):
                uri = lines[j].strip()
                if uri and not uri.startswith("#"):
                    m = res_pat.search(uri)
                    label = (m.group(1) + "p") if m else "AUTO"
                    if not uri.startswith("http"):
                        uri = urljoin(base_url, uri)
                    qualities[label] = uri
                    break
    return qualities


def extract_ashdi(iframe_url: str) -> dict:
    r = get(iframe_url, headers={"Referer": BASE_URL, "User-Agent": UA})
    page = r.text

    m = re.search(r"file\s*:\s*'(https://[^']+\.m3u8[^']*)'", page)
    if not m:
        m = re.search(r'file\s*:\s*"(https://[^"]+\.m3u8[^"]*)"', page)
    if not m:
        m = re.search(r"(https://[^\s'\"]+\.m3u8)", page)

    if not m:
        print("  [ashdi] could not find m3u8 URL in player page")
        return {"url": iframe_url, "qualities": {}}

    file_url = m.group(1).strip()
    print(f"  [ashdi] m3u8: {file_url}")

    dq = re.search(r"default_quality\s*:\s*['\"]([^'\"]+)['\"]", page)
    default_q = dq.group(1) if dq else ""

    try:
        m3u8_text = get(file_url, headers={"Referer": iframe_url, "User-Agent": UA}).text
        qualities = parse_m3u8_qualities(m3u8_text, file_url)
    except Exception as e:
        print(f"  [ashdi] m3u8 fetch failed: {e}")
        qualities = {}

    return {
        "url": file_url,
        "qualities": qualities,
        "subtitle": "",
        "default_quality": default_q,
    }


def resolve_iframe_url(iframe_url: str) -> dict:
    domain = urlparse(iframe_url).netloc
    if "ashdi.vip" in domain:
        return extract_ashdi(iframe_url)
    return {"url": iframe_url, "qualities": {}}


def download_file(url: str, dest: str, headers: Optional[dict] = None):
    h = {"User-Agent": UA, "Referer": BASE_URL}
    if headers:
        h.update(headers)

    with session.get(url, headers=h, stream=True, timeout=30) as r:
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


def _manual_hls(m3u8_url: str, dest: str, referer: str):
    text = get(m3u8_url, headers={"Referer": referer}).text
    segs = [l.strip() for l in text.splitlines() if l.strip() and not l.startswith("#")]
    if not segs:
        print("  No segments found.")
        return

    base = m3u8_url.rsplit("/", 1)[0] + "/"
    total = len(segs)
    print(f"  Downloading {total} segments...")

    with open(dest, "wb") as out:
        for i, seg in enumerate(segs, 1):
            seg_url = seg if seg.startswith("http") else base + seg
            out.write(get(seg_url, headers={"Referer": referer}).content)
            pct = i * 100 // total
            sys.stdout.write(f"\r  [{'#' * (pct // 2):<50}] {pct}% ({i}/{total})")
            sys.stdout.flush()
    print()


def download_m3u8(m3u8_url: str, dest: str, referer: str = BASE_URL):
    ffmpeg_cmd = (
        f'ffmpeg -y '
        f'-headers "Referer: {referer}\r\nUser-Agent: {UA}\r\n" '
        f'-i "{m3u8_url}" -c copy -bsf:a aac_adtstoasc "{dest}" -loglevel warning'
    )
    print("  Running ffmpeg...")
    ret = os.system(ffmpeg_cmd)
    if ret != 0:
        print("  ffmpeg failed, falling back to manual segment download...")
        _manual_hls(m3u8_url, dest, referer)


def fetch_players(anime_url: str) -> list:
    print(f"\nFetching anime page: {anime_url}")
    r = get(anime_url)
    soup = BeautifulSoup(r.text, "html.parser")

    players = parse_inline_player(soup)
    if players:
        print(f"  Found inline player with {len(players)} dubbing(s).")
        return players

    news_id = get_anime_id(soup)
    if news_id:
        user_hash = get_user_hash(soup)
        print(f"  news_id={news_id}, fetching AJAX playlist...")
        players = parse_ajax_playlist(news_id, user_hash, anime_url)
        if players:
            print(f"  Found {len(players)} track(s).")
            return players

    print("  Could not find any episodes on this page.")
    return []


def choose(options: list, label_fn=str, prompt="Select: "):
    if not options:
        return None
    for i, o in enumerate(options, 1):
        print(f"  {i:4}. {label_fn(o)}")
    while True:
        raw = input(prompt).strip()
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1]
        print("  Invalid choice, try again.")


def run(anime_url: Optional[str] = None, query: Optional[str] = None):
    print("=" * 60)
    print("  Anitube Downloader")
    print("=" * 60)

    if not anime_url:
        if not query:
            query = input("\nSearch anime title: ").strip()
        print(f"\nSearching for: {query}")
        results = quick_search(query)
        if not results:
            print("  Quick search empty, trying full search...")
            results = full_search(query)
        if not results:
            print("  No results found.")
            return
        print(f"\nFound {len(results)} result(s):")
        chosen = choose(results, label_fn=lambda r: r["name"], prompt="Select anime: ")
        if not chosen:
            return
        anime_url = chosen["url"]
        if not anime_url.startswith("http"):
            anime_url = BASE_URL + anime_url

    players = fetch_players(anime_url)
    if not players:
        print("Nothing to download.")
        return

    playable = [p for p in players if p.get("episodes")]
    skipped = len(players) - len(playable)
    if skipped:
        print(f"\n  (Skipping {skipped} empty track(s))")
    if not playable:
        print("  No playable tracks found.")
        return

    print("\nAvailable tracks:")

    def track_label(p):
        return f"{p['dubber']}  [{len(p['episodes'])} ep]"

    player = choose(playable, label_fn=track_label, prompt="Select track: ")
    if not player:
        return

    episodes = player["episodes"]

    print(f"\nEpisodes ({len(episodes)} total):")
    for i, ep in enumerate(episodes, 1):
        print(f"  {i:4}. {ep['name']}")

    raw = input(
        "\nEpisode(s) to download (e.g. 1  or  1-5  or  1,3,5  or  all): "
    ).strip().lower()

    if raw == "all":
        to_dl = list(range(len(episodes)))
    elif "-" in raw and "," not in raw:
        a, b = raw.split("-", 1)
        to_dl = list(range(int(a) - 1, int(b)))
    elif "," in raw:
        to_dl = [int(x.strip()) - 1 for x in raw.split(",")]
    else:
        to_dl = [int(raw) - 1]

    out_dir = input("\nOutput folder [./downloads]: ").strip() or "./downloads"
    os.makedirs(out_dir, exist_ok=True)

    chosen_quality = None
    for idx in to_dl:
        if 0 <= idx < len(episodes) and episodes[idx]["url"]:
            print("\nProbing first episode to determine available qualities...")
            probe = resolve_iframe_url(episodes[idx]["url"])
            qualities = probe.get("qualities", {})
            if qualities:
                print("  Qualities:", ", ".join(sorted(qualities)))
                default_q = probe.get("default_quality", "") or sorted(qualities)[-1]
                chosen_quality = input(f"  Quality for all episodes [{default_q}]: ").strip() or default_q
            break

    for idx in to_dl:
        if idx < 0 or idx >= len(episodes):
            print(f"  Skipping out-of-range index {idx + 1}")
            continue

        ep = episodes[idx]
        iframe_url = ep["url"]
        raw_name = re.sub(r'[\\/:*?"<>|]', "_", ep["name"])
        ep_name = re.sub(r'^(\d+)', lambda m: m.group(1).zfill(2), raw_name)

        print(f"\n── {ep['name']} {'─' * 40}")
        print(f"  iframe: {iframe_url}")

        info = resolve_iframe_url(iframe_url)
        stream_url = info["url"]
        qualities = info.get("qualities", {})

        if qualities:
            if chosen_quality and chosen_quality in qualities:
                stream_url = qualities[chosen_quality]
            else:
                print("  Qualities:", ", ".join(sorted(qualities)))
                default_q = info.get("default_quality", "") or sorted(qualities)[-1]
                q = input(f"  Quality [{default_q}]: ").strip() or default_q
                stream_url = qualities.get(q, stream_url)
                if chosen_quality is None:
                    chosen_quality = q

        if not stream_url or not stream_url.startswith("http"):
            print("  No valid stream URL, skipping.")
            continue

        dest = os.path.join(out_dir, ep_name + ".mp4")
        print(f"  -> {dest}")

        if ".m3u8" in stream_url:
            download_m3u8(stream_url, dest, referer=iframe_url)
        else:
            download_file(stream_url, dest)

        print("  ✓ Done")
        if idx != to_dl[-1]:
            time.sleep(1)

    print("\n✓ All done!")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Download anime from anitube.in.ua")
    parser.add_argument("query", nargs="?", help="Anime title to search for")
    parser.add_argument("--url", help="Direct anime page URL (skip search)")
    args = parser.parse_args()

    try:
        run(anime_url=args.url, query=args.query)
    except KeyboardInterrupt:
        print("\nCancelled.")
