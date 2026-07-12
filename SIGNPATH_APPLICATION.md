# SignPath Foundation application draft

This is draft text for the SignPath Foundation application. Replace bracketed notes if needed before submitting.

## Project

Project name: Map in a Box

Repository: https://github.com/sjtaylor82/MapInABox

License: MIT License

Maintainer/contact: Sam Taylor

Platforms: Windows and macOS. The immediate code-signing request is for Windows release artifacts.

Release artifacts to sign: `MapInABox-<version>-setup.exe` and project-built executable files included in the Windows installer.

## Short description

Map in a Box is an accessible world map and local exploration application for blind and low-vision users. It works with screen readers such as NVDA, JAWS, and VoiceOver. Users can explore countries, cities, oceans, landmarks, streets, intersections, house numbers, nearby points of interest, walking routes, public transport, flights, hotels, food options, weather, and place summaries without needing a visual display.

## Open source status

Map in a Box is published as open source under the MIT License. The repository contains the application source, build scripts, installer script, bundled data files, manual, and GitHub Actions release workflow.

The project is actively maintained and has public GitHub release artifacts. Windows releases are built in GitHub Actions using GitHub-hosted runners. The existing release workflow produces a Windows installer artifact named `MapInABox-<version>-setup.exe` and a macOS zip artifact.

## Why code signing is needed

Map in a Box is intended for end users who may rely on screen readers and may be less able to work around operating-system security warnings. Unsigned Windows installers produce SmartScreen and trust warnings that make installation harder and less reassuring. Code signing would give users a clearer publisher/trust path while preserving a verifiable connection between the public repository, the automated build, and the release binary.

## Build and signing plan

After approval, the project will add SignPath signing to the GitHub Actions release workflow. The workflow will upload the unsigned Windows installer artifact, submit it to SignPath using the GitHub trusted build integration, wait for approval/completion, and publish the signed artifact to the GitHub release.

The SignPath GitHub Action values `SIGNPATH_API_TOKEN`, `organization-id`, `project-slug`, and `signing-policy-slug` are not present yet because they are created after SignPath approval and organization/project setup.

The signing policy will require:

- builds from the public GitHub repository,
- GitHub-hosted runners for release builds,
- manual approval for signing requests,
- release version metadata matching the artifact version,
- product metadata using `Map in a Box`,
- signing only Map in a Box project artifacts.

## Code signing policy page

The repository README contains a `Code signing policy` section with:

- the required wording: "Free code signing provided by SignPath.io, certificate by SignPath Foundation",
- project signing rules,
- artifact expectations,
- release approval expectations,
- project signing roles,
- privacy/network behavior.

## Privacy and third-party services

The application stores settings, optional API keys, caches, favourites, renamed POIs, suppressed POIs, and personal POIs locally on the user's computer. User-supplied API keys are only sent to the service they belong to.

The app contacts networked services only when the user uses a feature that requires network data, enables a network-backed setting, supplies an API key, opens an external browser link, checks for updates, or submits an OpenStreetMap note. Network requests may include the location, place, route, airport, hotel, or query needed to perform that feature.

Core no-user-key services include OpenStreetMap/Nominatim/Overpass/Notes, OSRM, Photon, Wikipedia, Open-Meteo, OpenSky Network, Mapillary, GitHub, MobilityData, OurAirports, `samtaylor9.nfshost.com` for Australian GNAF address lookup, and explicit browser-opened search/map/provider links.

Optional user-key services include Google Maps/Routes/Places/Static Maps/Street View, HERE, Mistral AI, AviationStack, OpenRouteService, and RapidAPI-hosted services such as Priceline, TripAdvisor, and timetable lookup.

## Maintainer and approval roles

Committers and reviewers: Map in a Box repository maintainers.

Approvers: Sam Taylor and any future repository maintainer explicitly granted release/signing approval responsibility.

Maintainers with release or signing access will use multi-factor authentication on GitHub and SignPath.

## Notes for application form

Suggested one-paragraph summary:

Map in a Box is an MIT-licensed accessible map and local exploration application for blind and low-vision users. It provides screen-reader-friendly world-map exploration, street/intersection walking, points of interest, routes, transit, weather, flights, hotels, and place summaries. Windows releases are built from the public GitHub repository using GitHub Actions and published as `MapInABox-<version>-setup.exe`. We are requesting SignPath Foundation code signing so users can install the app without unsigned-publisher warnings and can trust that the binary was produced from the public repository release workflow.
