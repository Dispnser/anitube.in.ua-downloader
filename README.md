# anitube.in.ua downloader
 
anitube.in.ua downloader written in Python
 
## Requirements
 
```bash
pip install requests beautifulsoup4
```
 
> **Note:** [ffmpeg](https://ffmpeg.org/download.html) is strongly recommended for downloading HLS streams. Without it the downloader will fall back to a raw segment-stitching method which produces lower compatibility output.
 
## Usage
 
```bash
# Interactive search
python anitube_downloader.py
 
# Search by title directly
python anitube_downloader.py "Неймовірні пригоди ДжоДжо: Незламний Діамант"
 
# Skip search, provide a direct anime page URL
python anitube_downloader.py --url https://anitube.in.ua/3686-neymovrn-prigodi-dzhodzho-nezlamniy-damant.html
```
 
## Features
 
- Search anime by title
- Select dubbing track / player
- Download single episodes, ranges (`1-12`), specific episodes (`1,3,5`), or (`all`) at once
- Quality selection (1080p / 720p / 480p etc.) — asked once and applied to the whole batch
- Support for different players(see [below](https://github.com/Dispnser/anitube.in.ua-downloader#supported-players))

## Supported players
Currently only **ASHDI** is supported

## Roadmap

- Add MOON Player support
- Add TRG Player support
- Fix edgecases
- Add folder structuring


## Credits
 
[AnitubeApp](https://github.com/MrIkso/AnitubeApp) — for inspiring the logic for downloading episodes
 
