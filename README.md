# Map in a Box

Map in a Box is an accessible world map and local exploration program designed for blind and low-vision users. It runs on Windows and macOS and works with screen readers including NVDA, JAWS, and VoiceOver.

The program combines global geography with street-level navigation. You can explore countries, cities, oceans, and landmarks from the world map, then drop into any street network to walk intersections, look up businesses, plan routes, and check public transport and food options without a visual display.

## Features

- Explore the world map by country, city, coordinates, and geographic features.
- Explore streets, intersections, numbers, and nearby points of interest.
- Move along a real street network with controlled turn choices at intersections.
- Hear optional POI announcements while walking.
- Plan walking and driving routes, compare toll and toll-free routes, and check public transport.
- Look up weather, marine conditions, air quality, Wikipedia summaries, flights, airports, hotels, food, and nearby services.
- Use optional API keys for richer third-party data while keeping the core map experience usable without paid keys.

## License

Map in a Box versions 2026.7.0 and later are free software released under
the GNU General Public License version 3 or later. See [LICENSE](LICENSE).
Anyone distributing the application or a modified version must comply with
the GPL, including its corresponding-source requirements. Versions through
1.0.0.34 remain available under the MIT License included with those releases.
Bundled third-party software and data retain their own licences; see
[THIRD_PARTY_NOTICES.txt](THIRD_PARTY_NOTICES.txt).

The Map in a Box name and branding identify Sam Taylor's official builds.
The GPL does not grant permission to imply that a modified build is an
official or endorsed Map in a Box release. See [TRADEMARKS.md](TRADEMARKS.md).

## Versioning

Map in a Box uses calendar versions in the form `YEAR.MONTH.PATCH`. For
example, `2026.7.0` is the initial July 2026 release and `2026.7.1` is a
corrective release from the same month. Historical `1.0.0.x` tags predate
this scheme and remain unchanged.

## Downloads, install, and uninstall

Release builds are published from GitHub Actions at:

https://github.com/sjtaylor82/MapInABox/releases

On Windows, download and run `MapInABox-<version>-setup.exe`. The installer creates Start Menu entries for the app and for uninstalling it. You can also uninstall Map in a Box from Windows Settings or Control Panel.

Windows portable zip builds are also published for users who cannot use an installer. Extract the zip to a folder you can write to, then run `MapInABox.exe` from inside the extracted `MapInABox` folder. Portable settings, logs, favourites, downloads, and caches remain inside its `Data` folder; the portable build does not write Map in a Box data to AppData.

On macOS, download `MapInABox-macOS.zip`, extract it, and follow the included `README.txt` and `install-macos.sh` instructions. To uninstall the macOS build, remove `MapInABox.app` from Applications or from the folder where you placed it.

Release checksum files are published as `SHA256SUMS.txt` so downloaded artifacts can be checked for corruption or tampering.

## Privacy and network access

Map in a Box stores settings, API keys, caches, favourites, renamed POIs, suppressed POIs, and personal POIs locally on the user's computer. User-supplied API keys are stored in the local settings file and are only sent to the service they belong to.

The program will not transfer information to networked systems unless the user uses a feature that requires network data, enables a setting that uses network data, enters an API key for a third-party service, opens an external web page, checks for updates, or submits an OpenStreetMap note. Requests generally include the location, place, route, airport, hotel, or query needed for the feature being used.

Core features that do not require the user to provide an API key may contact:

- OpenStreetMap services, including Nominatim, Overpass API, and OpenStreetMap Notes.
- OSRM and Photon for routing and geocoding fallbacks.
- Wikipedia for place summaries.
- Open-Meteo for weather, marine forecasts, sunrise/sunset, and air quality.
- OpenSky Network for flight tracking and route lookup.
- Mapillary as an open street-level imagery fallback.
- GitHub for update checks and release downloads.
- MobilityData and OurAirports public datasets for transit and airport data.
- `samtaylor9.nfshost.com` for GNAF address lookup in Australia when GNAF address data is enabled.
- Google Search, Bing, DuckDuckGo, Google Maps, Uber (`m.uber.com`), venue websites, and provider websites when the user explicitly opens a search result, menu, review page, map page, donation page, ride booking link, or other external browser link.
- `miab-menu-search.miab.workers.dev` for best-effort menu and website discovery features.

Optional features are only contacted when the user supplies the relevant key or chooses the relevant action:

- Google Maps, Routes, Places, Static Maps, and Street View.
- HERE search, routing, geocoding, transit, and POI services.
- Mistral AI for image descriptions, POI summaries, transit questions, and narrative walking briefings.
- AviationStack for airport arrival and departure boards.
- OpenRouteService for distance calculations.
- RapidAPI-hosted services including Priceline, TripAdvisor, and timetable lookup.

## Code signing policy

Windows releases are not currently code-signed. The project is evaluating the least-friction signing path for accessible public distribution.

The Map in a Box code signing policy is:

- Signed artifacts must be built from the project release workflow.
- The release workflow must run on GitHub-hosted runners.
- Signed Windows artifacts are expected to include the Windows installer, `MapInABox-<version>-setup.exe`, and any executable files inside that installer that are produced by the project build.
- Release signing requests must be manually approved by a project approver before publication.
- Signing must only be used for Map in a Box artifacts built from this repository. Upstream third-party binaries must not be signed as if they were Map in a Box binaries.
- The product name in signed artifact metadata must be `Map in a Box`, and product version metadata must match the release version.
- Maintainers must use multi-factor authentication on accounts used for release or signing access.

Project roles:

- Committers and reviewers: Map in a Box repository maintainers.
- Approvers: Sam Taylor and any future repository maintainer explicitly granted release/signing approval responsibility.

## Development and releases

The release workflow builds Windows and macOS artifacts from GitHub Actions. Windows packaging uses PyInstaller and Inno Setup; macOS packaging creates a zipped app bundle with the install helper script.
